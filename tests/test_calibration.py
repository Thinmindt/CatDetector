"""Calibration over a synthetic data run whose answer is known (roadmap A.2)."""

from __future__ import annotations

import datetime
import inspect
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from conftest import make_clip_with_facts

import main
from src.capture.motion_detector import MotionDetector
from src.capture.motion_metrics import FIELDS
from src.capture.motion_recorder import MotionRecorder
from src.clips.sidecar import iso
from src.review.api import cat_labels
from src.review.calibration import (
    COLUMNS,
    MAX_CLIP,
    PRE_ROLL,
    WARMUP_FRAMES,
    Outcome,
    Peaks,
    Replay,
    Rule,
    Settings,
    Tally,
    Visit,
    Window,
    WindowIndex,
    calibrate,
    proposal,
    read_frames,
    replay,
    report,
    split_doubts,
    stayed_in,
    sweep,
    threshold_rule,
    window_of,
)
from src.review.capture_db import CaptureDB
from src.review.timeline import BOXES, TimelineRow

CATS = ("Ada", "Bea", "Cy")
SETTINGS = Settings(
    cats=CATS,
    cat_labels=cat_labels(CATS),
    boxes=BOXES,
    dark_brightness=10,
    motion_timeout=10,
    gap_seconds=60,
    motion_threshold=150,
)
START = datetime.datetime(2026, 9, 16)
VISITS = 60
BOX_X = (100, 320, 540)
LIT, DARK = 100.0, 5.0
FPS = 30


@dataclass(frozen=True)
class Spec:
    """One event of the synthetic run: when, where, its label, what the CSV saw."""

    event_id: int
    started_at: datetime.datetime
    label: str
    box: int
    foreground: int
    clean: int
    peek: bool = False
    squat: int = 700  # the count while the cat sits still
    other_box: int | None = None  # a second box it stays in afterwards

    @property
    def ended_at(self) -> datetime.datetime:
        return self.started_at + datetime.timedelta(seconds=VISIT_SECONDS)

    def row(self) -> TimelineRow:
        return TimelineRow(
            id=self.event_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            clips=1,
            label=self.label,
            labeled_at=self.started_at,
            trigger_cx=BOX_X[self.box],
            staged=False,
            peek=self.peek,
        )


VISIT_SECONDS = 32


def visit_specs() -> list[Spec]:
    """One visit an hour, so 25 of 60 fall at night; the cats and boxes in turn;
    the weakest visit first, so later ones never undercut it."""
    return [
        Spec(
            event_id=i + 1,
            started_at=START + datetime.timedelta(hours=i, minutes=10),
            label=CATS[i % 3],
            box=i % 3,
            foreground=1000 + 10 * i,
            clean=500 + 10 * i,
        )
        for i in range(VISITS)
    ]


TICK = datetime.timedelta(seconds=1 / FPS)


def frame(
    when: datetime.datetime, foreground: int, clean: int, bright: float, box: int = 0
) -> str:
    x = f"{BOX_X[box] - 10},20" if clean else ","
    return f"0,{iso(when)},{foreground},{clean},{bright},{x}"


def burst(
    when: datetime.datetime,
    frames: int,
    foreground: int,
    clean: int,
    bright: float = LIT,
    *,
    box: int = 0,
    warm: bool = True,
) -> list[str]:
    """Frames from `when` at the camera's rate. Each burst follows a gap, which
    the replay takes for a restart, so a warm one is led in by quiet frames the
    detector's warmup uses up."""
    lead = WARMUP_FRAMES if warm else 0
    quiet = [frame(when - TICK * (lead - n), 0, 0, bright) for n in range(lead)]
    return quiet + [
        frame(when + TICK * n, foreground, clean, bright, box) for n in range(frames)
    ]


# Outside every visit: a person (big), a mid-sized change only the band takes,
# a one-frame flicker nothing takes, a dark burst the gate ignores, and the
# whole frame changing as the detector starts, which its warmup ignores.
PERSON = START + datetime.timedelta(hours=5, minutes=40)
MID = START + datetime.timedelta(hours=6, minutes=40)
FLICKER = START + datetime.timedelta(hours=7, minutes=40)
IN_THE_DARK = START + datetime.timedelta(hours=8, minutes=40)
RESTART = START + datetime.timedelta(hours=11, minutes=40)
# A not_cat event holding a visit-sized blob for 13 s, longer than nine visits in
# ten, and one holding it for a frame.
HIDDEN_CAT = START + datetime.timedelta(hours=9, minutes=40)
PASSING = START + datetime.timedelta(hours=10, minutes=40)


def visit_frames(spec: Spec) -> list[str]:
    """The frame that starts the clip comes a moment before its recorded start,
    as the detector logs it; then 11 s at the visit's peak in its box, and 21 s
    sitting still at a lower count and half the blob, in its box or a second one."""
    second = seconds(1)
    still = spec.box if spec.other_box is None else spec.other_box
    return [
        *burst(spec.started_at - second / 10, 1, spec.foreground, spec.clean),
        *burst(spec.started_at, FPS, 0, 0, warm=False),
        *burst(
            spec.started_at + second,
            11 * FPS,
            spec.foreground,
            spec.clean,
            box=spec.box,
            warm=False,
        ),
        *burst(
            spec.started_at + 12 * second,
            21 * FPS,  # a second past the visit's end, as the camera runs on
            spec.squat,
            min(spec.squat, spec.clean // 2),
            box=still,
            warm=False,
        ),
    ]


def seconds(n: float) -> datetime.timedelta:
    return datetime.timedelta(seconds=n)


def metrics_lines(specs: list[Spec]) -> list[str]:
    rows: list[str] = []
    for spec in specs:
        rows += visit_frames(spec)
    rows += burst(PERSON, 5, 5000, 5000)
    rows += burst(MID, 5, 300, 800)
    rows += burst(FLICKER, 1, 100, 800)
    rows += burst(IN_THE_DARK, 30, 9000, 9000, DARK)
    rows += burst(RESTART, WARMUP_FRAMES, 300_000, 300_000, warm=False)
    rows += burst(HIDDEN_CAT, 400, 300, 700)
    rows += burst(PASSING, 1, 300, 700)
    rows.sort()
    return [",".join(("x", *COLUMNS)), *rows]  # an extra column, to read by name


def not_cat_specs() -> list[Spec]:
    return [
        Spec(1001, HIDDEN_CAT, "not_cat", 0, 0, 0),
        Spec(1002, PASSING, "not_cat", 0, 0, 0),
        Spec(1003, PERSON, "not_cat", 0, 0, 0),
    ]


def run_rows(specs: list[Spec] | None = None) -> list[TimelineRow]:
    """The visits, the not_cat events, one peek, one visit from before the run."""
    rows = [spec.row() for spec in (specs or visit_specs()) + not_cat_specs()]
    peek = Spec(2001, START + datetime.timedelta(hours=11), "Ada", 0, 0, 0, peek=True)
    early = Spec(2002, START - datetime.timedelta(days=1), "Bea", 0, 0, 0)
    return [*rows, peek.row(), early.row()]


def lines_opener(lines: list[str]) -> Any:
    return lambda: iter(lines)


def run(specs: list[Spec], settings: Settings = SETTINGS) -> Any:
    lines = metrics_lines(specs)
    return calibrate(run_rows(specs), START, lines_opener(lines), settings)


def chosen(result: Any) -> Outcome:
    outcome = proposal(result.outcomes, len(result.visits))
    assert outcome is not None
    return outcome


def visit_at(event_id: int, start: float, end: float) -> Visit:
    """A visit from `start` to `end` seconds after START."""
    when = START + seconds(start)
    spec = Spec(event_id, when, "Ada", 0, 0, 0)
    row = replace(spec.row(), ended_at=START + seconds(end))
    return Visit(row, Peaks())


def rows_at(start: float, end: float, foreground: int) -> list[str]:
    """Frames at the camera's rate from `start` to `end` seconds after START."""
    count = round((end - start) * FPS)
    return [
        frame(START + seconds(start) + TICK * n, foreground, 0, LIT)
        for n in range(count)
    ]


def outcomes_of(lines: list[str], visits: list[Visit], *rules: Rule) -> list[Outcome]:
    header = ",".join(("x", *COLUMNS))
    return replay(read_frames([header, *lines]), rules, visits, 10, seconds(10))


# --- reading ----------------------------------------------------------------


def test_the_csv_is_read_by_its_header_and_refused_without_its_columns() -> None:
    header = "clean_w,timestamp,brightness,clean_area,foreground_px,clean_x"
    lines = [header, "20,2026-09-16T10:00:00.000,9.5,7,3,90", "x"]
    frames = list(read_frames(lines))
    assert frames == [("2026-09-16T10:00:00.000", 3, 7, 9.5, 100)]
    no_blob = [header, ",2026-09-16T10:00:00.000,9.5,0,3,"]
    assert next(read_frames(no_blob))[4] == -1
    with pytest.raises(ValueError, match="foreground_px"):
        list(read_frames(["timestamp,clean_area,brightness,clean_x,clean_w"]))


def test_damaged_rows_are_skipped_and_counted() -> None:
    tally = Tally()
    lines = [
        "timestamp,foreground_px,clean_area,brightness,clean_x,clean_w",
        "\x00\x00\x002026-09-16T10:00:00.000,1,2,3.0,,",  # after a power cut
        "2026-09-16T10:00:00.033,1,2",  # a tail still being written
        "\x00\x00\x00\x00,,,,,",
        "2026-09-16T10:00:00.066,1,2,3.0,,",
    ]
    frames = list(read_frames(lines, tally))
    times = [f[0] for f in frames]
    assert times == ["2026-09-16T10:00:00.000", "2026-09-16T10:00:00.066"]
    assert tally.skipped == 2


def test_windows_overlap_and_lookups_need_no_order() -> None:
    index = WindowIndex(
        [
            Window(1, "2026-09-16T10:00", "2026-09-16T10:05"),
            Window(2, "2026-09-16T10:02", "2026-09-16T10:03"),
            Window(3, "2026-09-16T11:00", "2026-09-16T11:01"),
        ]
    )
    assert sorted(index.holding("2026-09-16T10:02:30")) == [1, 2]
    assert index.holding("2026-09-16T11:00:30") == [3]
    after_a_later_one = "2026-09-16T10:04"  # as a clock step back reads
    assert index.holding(after_a_later_one) == [1]
    assert index.holding("2026-09-16T10:30") == []


def test_the_columns_warmup_and_pre_roll_are_the_capture_side_s() -> None:
    assert set(COLUMNS) <= set(FIELDS)
    warmup = inspect.signature(MotionDetector).parameters["warmup_frames"]
    assert warmup.default == WARMUP_FRAMES
    recorder = inspect.signature(MotionRecorder).parameters
    assert datetime.timedelta(seconds=recorder["buffer_seconds"].default) == PRE_ROLL
    assert datetime.timedelta(seconds=recorder["max_clip_seconds"].default) == MAX_CLIP


# --- the whole run ----------------------------------------------------------


def test_a_finished_run_proposes_settings_that_miss_no_visit() -> None:
    result = run(visit_specs())

    left_out = (result.left_out, result.peeks, result.uncovered, result.blank)
    assert (len(result.visits), *left_out) == (60, 1, 1, 0, 0)
    assert [c.met for c in result.conditions] == [True, True, True, True, None, True]
    assert result.done
    assert result.outcomes is not None
    by_rule = {str(o.rule): o for o in result.outcomes}
    assert list(by_rule) == [
        "MOTION_THRESHOLD=150",
        "MOTION_THRESHOLD=300",
        "MOTION_THRESHOLD=500",
        "MOTION_THRESHOLD=500, held above 150",
        "band 250-1363 px, 2 frames",
    ]
    assert [o.missed for o in result.outcomes] == [0, 0, 0, 0, 0]
    assert all(o.median_coverage == 1.0 for o in result.outcomes)
    # Outside the visits, 150 takes the person, the mid-sized change, the hidden
    # cat and the passing frame; 300 and 500, held or not, only the person; the
    # band the mid-sized change and the hidden cat. Nothing takes the flicker,
    # the dark burst, the restart, or a visit's own first frame.
    hours = 1 / result.outcomes[0].false_per_hour * 4
    false = [round(o.false_per_hour * hours) for o in result.outcomes]
    assert false == [4, 1, 1, 1, 2]
    assert chosen(result).rule == threshold_rule(500)
    text = "\n".join(report(result))
    assert "Environment=MOTION_THRESHOLD=500" in text


def test_a_threshold_that_closes_clips_on_a_still_cat_is_not_proposed() -> None:
    squatting = [replace(spec, squat=400) for spec in visit_specs()]
    result = run(squatting)

    assert result.outcomes is not None
    coverage = {str(o.rule): o.median_coverage for o in result.outcomes}
    assert coverage["MOTION_THRESHOLD=300"] == 1.0
    assert coverage["MOTION_THRESHOLD=500"] < 0.8
    assert coverage["MOTION_THRESHOLD=500, held above 150"] == 1.0
    assert chosen(result).rule == threshold_rule(300)


def test_boxes_are_where_the_cat_stayed_not_where_it_came_in() -> None:
    specs = visit_specs()
    specs[0] = replace(specs[0], other_box=2)  # box 1, then 20 s in box 3
    specs[1] = replace(specs[1], squat=200)  # too small a blob to count anywhere
    result = run(specs)

    by_id = {visit.row.id: visit for visit in result.visits}
    assert set(by_id[1].boxes) == {1, 3}
    assert set(by_id[2].boxes) == {2}
    assert by_id[1].boxes[3] == 250  # half its peak, as it sat
    box_line = result.conditions[3].now
    assert box_line.split("; ") == [
        "box 1: 20, weakest 500 px",
        "box 2: 20, weakest 510 px",
        "box 3: 21, weakest 250 px",
    ]


def test_a_pass_through_a_box_does_not_count_as_using_it() -> None:
    specs = [spec for spec in visit_specs() if spec.box != 2]
    result = run([replace(spec, squat=0) for spec in specs])
    assert result.conditions[3].met is False
    assert "box 3: 0" in result.conditions[3].now
    left_and_middle = replace(SETTINGS, boxes=(1, 2))
    assert run([replace(s, squat=0) for s in specs], left_and_middle).conditions[3].met


def test_doubts_ask_about_the_weakest_visits_and_a_hidden_cat() -> None:
    result = run(visit_specs())

    asked = {(d.question, d.event_id) for d in result.doubts}
    expected = {("weakest", 1), ("weakest", 2), ("weakest", 3)}
    assert asked == expected | {("not_cat_blob", 1001)}


def test_open_questions_hold_back_the_settings() -> None:
    result = run(visit_specs())
    result.open_questions = 3

    text = "\n".join(report(result))
    assert "Provisional: 3 question(s)" in text
    assert "Environment=" not in text


def test_a_visit_with_no_blob_is_left_out_and_asked_about() -> None:
    specs = visit_specs()
    specs[30] = replace(specs[30], foreground=0, clean=0, squat=0)
    result = run(specs)

    assert (len(result.visits), result.blank) == (59, 1)
    assert ("blank", 31) in {(d.question, d.event_id) for d in result.doubts}
    assert result.outcomes is not None
    assert str(result.outcomes[-1].rule) == "band 250-1363 px, 2 frames"


def test_an_unfinished_run_says_what_is_missing_and_proposes_nothing() -> None:
    few = visit_specs()[:30]
    rows = [spec.row() for spec in few]
    result = calibrate(rows, START, lines_opener(metrics_lines(few)), SETTINGS)

    assert not result.done
    assert result.outcomes is None
    assert result.conditions[0].met is False
    assert "Not done" in "\n".join(report(result))


def test_a_new_low_among_the_last_visits_is_not_settled() -> None:
    specs = visit_specs()
    last = specs[-1].ended_at + datetime.timedelta(hours=1)
    specs.append(Spec(999, last, "Ada", 0, 900, 400))
    result = run(specs)

    assert result.conditions[-1].met is False
    assert not result.done


def test_a_visit_covered_only_by_its_neighbour_s_clip_is_missed() -> None:
    first, second = visit_at(1, 0, 15), visit_at(2, 16, 40)
    lines = rows_at(-1, 0, 0) + rows_at(0, 10, 1000) + rows_at(10, 40, 200)
    high, low = outcomes_of(
        lines, [first, second], threshold_rule(500), threshold_rule(150)
    )

    assert (high.missed, low.missed) == (1, 0)
    assert 0 < high.median_coverage < 1  # its clip ran on into the second visit


def test_a_restart_ends_the_clip_at_the_last_frame_before_it() -> None:
    visit = visit_at(1, 0, 60)
    steady = rows_at(-1, 0, 0) + rows_at(0, 60, 1000)
    gapped = rows_at(-1, 0, 0) + rows_at(0, 20, 1000) + rows_at(25, 60, 1000)
    rule = threshold_rule(500)

    (whole,) = outcomes_of(steady, [visit], rule)
    (broken,) = outcomes_of(gapped, [visit], rule)
    assert whole.median_coverage == 1.0
    assert 0.9 < broken.median_coverage < 1.0  # the 5 s with no frames are lost


def spans_of(
    lines: list[str], visit: Visit, rule: Rule
) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """The clips one rule records over `lines`."""
    run = Replay(rule, seconds(10))
    header = ",".join(("x", *COLUMNS))
    index = WindowIndex([window_of(visit.row)])  # type: ignore[list-item]
    for number, each in enumerate(read_frames([header, *lines])):
        if number > WARMUP_FRAMES:
            run.feed(each, datetime.datetime.fromisoformat(each[0]), number, index)
    run.finish()
    return run.spans


def test_a_clip_is_cut_at_the_recorder_s_limit_and_reopened() -> None:
    lines = rows_at(-1, 0, 0) + rows_at(0, 400, 1000)
    spans = spans_of(lines, visit_at(1, 0, 400), threshold_rule(500))
    assert len(spans) == 2
    assert spans[0][1] - spans[0][0] <= MAX_CLIP + PRE_ROLL


def test_a_clip_closed_on_timeout_is_not_stretched_to_the_limit() -> None:
    burst = rows_at(-1, 0, 0) + rows_at(0, 20, 1000)
    lines = burst + rows_at(20, 420, 0) + rows_at(420, 440, 1000)
    spans = spans_of(lines, visit_at(1, 0, 20), threshold_rule(500))
    header = ",".join(("x", *COLUMNS))
    *_, last_motion = read_frames([header, *burst])
    assert len(spans) == 2
    assert spans[0][1] == datetime.datetime.fromisoformat(last_motion[0]) + seconds(10)


def test_the_sweep_ends_from_a_run_threshold_of_zero() -> None:
    visits = [Visit(visit_at(1, 0, 10).row, Peaks(foreground=1000))]
    assert sweep(0, visits) == [0, 2, 4, 8, 16, 32, 64, 128, 256, 500]


def test_the_proposal_needs_coverage_for_most_visits_and_says_when_none_does() -> None:
    def outcome(threshold: int, median: float, poorly: int, missed: int = 0) -> Outcome:
        return Outcome(threshold_rule(threshold), missed, 1.0, median, poorly)

    run_own = outcome(150, 1.0, 4)
    # 100 visits allow 5 more poorly recorded than the run's 4.
    assert proposal([run_own, outcome(300, 0.99, 9)], 100) == outcome(300, 0.99, 9)
    assert proposal([run_own, outcome(300, 0.99, 10)], 100) == run_own
    assert proposal([run_own, outcome(300, 0.97, 4)], 100) == run_own
    assert proposal([run_own, outcome(300, 1.0, 4, missed=1)], 100) == run_own
    assert proposal([outcome(150, 0.9, 4)], 100) is None


def test_several_short_passes_through_a_box_are_not_a_stay() -> None:
    passes = Peaks()
    for _ in range(3):  # 4 s in box 3, then 4 s in box 1, three times
        for center in (BOX_X[2],) * 4 * FPS + (BOX_X[0],) * 4 * FPS:
            passes.add(1000, 600, center)
    assert stayed_in(passes, 300) == {}

    sitting = Peaks()
    for n in range(40 * FPS):  # in box 3, gone for 25 s as it fades while still
        seen = n < 5 * FPS or n >= 30 * FPS
        sitting.add(1000, 600 if seen else 0, BOX_X[2] if seen else -1)
    assert stayed_in(sitting, 300) == {3: 600}


def test_split_doubts_pair_one_cat_across_any_label_within_the_gap() -> None:
    def at(seconds: int, label: str, event_id: int) -> TimelineRow:
        when = START + datetime.timedelta(hours=1, seconds=seconds)
        return Spec(event_id, when, label, 0, 0, 0).row()

    rows = [
        at(0, "Ada", 1),
        at(52, "Ada", 2),  # 20 s after the first ends
        at(104, "not_cat", 3),
        at(156, "Ada", 4),
        at(3600, "Ada", 5),
    ]
    doubts = split_doubts(rows, SETTINGS.cat_labels, SETTINGS.gap_seconds)
    assert [(d.event_id, d.reason) for d in doubts] == [
        (1, "the same cat 20 s before #2: one visit? join with next")
    ]


# --- through main.py --------------------------------------------------------


def configure(monkeypatch: pytest.MonkeyPatch, db: CaptureDB, csv: Path) -> None:
    monkeypatch.setattr("main.service_environment", dict)
    for name, value in (
        ("DB_PATH", str(db.path)),
        ("CAT_NAMES", CATS),
        ("METRICS_CSV", str(csv)),
        ("DARK_BRIGHTNESS", 10.0),
        ("MOTION_THRESHOLD", 150),
        ("EVENT_GAP_SECONDS", 60.0),
    ):
        monkeypatch.setattr(f"main.Config.{name}", value)


def test_calibrate_reports_and_stages_each_doubt_once(
    db: CaptureDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "captures"
    specs = visit_specs()
    for spec in specs:
        place = (BOX_X[spec.box], 240)
        make_clip_with_facts(directory, spec.started_at, VISIT_SECONDS, place=place)
    db.ingest(directory)
    rows = sorted(db.timeline_rows(), key=lambda row: row.started_at or START)
    for spec, stored in zip(specs, rows, strict=True):
        db.set_label(stored.id, spec.label)
    csv = tmp_path / "metrics.csv"
    csv.write_text("\n".join(metrics_lines(specs)) + "\n")
    configure(monkeypatch, db, csv)
    args = main.parse_args(["--calibrate", "--since", START.isoformat()])

    assert main.run_command(args) == 0
    first = capsys.readouterr().out
    for row in db.timeline_rows():
        if row.staged:
            db.set_label(row.id, row.label or "")  # the reviewer confirms each
    assert main.run_command(args) == 0
    second = capsys.readouterr().out

    assert "Staged 3 event(s)" in first  # the three weakest
    assert "Provisional: 3 question(s)" in first
    assert "Staged 0 event(s)" in second
    assert "Environment=MOTION_THRESHOLD=500" in second


def test_ask_skips_unlabeled_and_staged_events_and_asks_once(
    db: CaptureDB, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    for hour in (1, 2, 3):
        make_clip_with_facts(directory, START + datetime.timedelta(hours=hour), 30)
    db.ingest(directory)
    first, second, third = sorted(row.id for row in db.timeline_rows())
    db.set_label(first, "Ada")
    db.set_label(second, "Ada")
    db.stage(second, "a reviewer's own", "human")

    assert db.ask([(first, "weakest", "why")], "calibration") == 1
    assert db.staged_by("calibration") == 1
    db.set_label(first, "Ada")  # confirmed
    assert db.ask([(first, "weakest", "why")], "calibration") == 0
    assert db.ask([(first, "split", "another")], "calibration") == 1
    assert db.ask([(second, "weakest", "why"), (third, "weakest", "why")], "x") == 0
    kept = db.get(second)
    assert kept is not None
    assert kept.staged is not None
    assert kept.staged.reason == "a reviewer's own"


def test_the_service_s_drop_in_settings_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = 'METRICS_CSV=/home/you/m.csv "MOTION_THRESHOLD=150" CAPTURE_HEARTBEAT_URL=x'

    def systemctl(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=shown + "\n")

    monkeypatch.setattr("main.subprocess.run", systemctl)
    service = main.service_environment()
    assert service == {"METRICS_CSV": "/home/you/m.csv", "MOTION_THRESHOLD": "150"}
    args = main.parse_args(["--calibrate", "--since", "2026-09-16"])
    settings, metrics = main.calibration_settings(args, service)
    assert (settings.motion_threshold, metrics) == (150, "/home/you/m.csv")

    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr("main.subprocess.run", missing)
    assert main.service_environment() == {}


@pytest.mark.parametrize(
    "argv",
    [
        ["--calibrate"],
        ["--since", "2026-09-16"],
        ["--metrics", "x.csv"],
        ["--boxes", "1,2"],
        ["--calibrate", "--since", "2026-09-16", "--boxes", "4"],
    ],
)
def test_calibrate_needs_since_and_its_options_need_calibrate(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        main.parse_args(argv)


def test_calibrate_fails_plainly_without_a_usable_metrics_csv(
    db: CaptureDB, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, db, tmp_path / "none.csv")
    args = main.parse_args(["--calibrate", "--since", "2026-09-16"])
    assert main.run_command(args) == 1
    csv = tmp_path / "metrics.csv"
    csv.write_text("timestamp\n")
    monkeypatch.setattr("main.Config.DB_PATH", str(tmp_path / "elsewhere.db"))
    args = main.parse_args(
        ["--calibrate", "--since", "2026-09-16", "--metrics", str(csv)]
    )
    assert main.run_command(args) == 1
    assert not (tmp_path / "elsewhere.db").exists()  # not created empty
    monkeypatch.setattr("main.Config.DB_PATH", str(db.path))
    other_layout = tmp_path / "old.csv"
    other_layout.write_text("timestamp,foreground_px\n")
    args = main.parse_args(
        ["--calibrate", "--since", "2026-09-16", "--metrics", str(other_layout)]
    )
    assert main.run_command(args) == 1
