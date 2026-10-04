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
from src.clips.sidecar import iso
from src.review.api import cat_labels
from src.review.calibration import (
    COLUMNS,
    WARMUP_FRAMES,
    Settings,
    Tally,
    Window,
    WindowIndex,
    calibrate,
    read_frames,
    report,
    split_doubts,
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

    @property
    def ended_at(self) -> datetime.datetime:
        return self.started_at + datetime.timedelta(seconds=30)

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


def frame(when: datetime.datetime, foreground: int, clean: int, bright: float) -> str:
    return f"0,{iso(when)},{foreground},{clean},{bright}"


def burst(
    when: datetime.datetime,
    frames: int,
    foreground: int,
    clean: int,
    bright: float = LIT,
    *,
    warm: bool = True,
) -> list[str]:
    """Frames from `when` at the camera's rate. Each burst follows a gap, which
    the replay takes for a restart, so a warm one is led in by quiet frames the
    detector's warmup uses up."""
    tick = datetime.timedelta(seconds=1 / FPS)
    lead = WARMUP_FRAMES if warm else 0
    quiet = [frame(when - tick * (lead - n), 0, 0, bright) for n in range(lead)]
    return quiet + [
        frame(when + tick * n, foreground, clean, bright) for n in range(frames)
    ]


# Outside every visit: a person (big), a mid-sized change only the band takes,
# a one-frame flicker nothing takes, a dark burst the gate ignores, and the
# whole frame changing as the detector starts, which its warmup ignores.
PERSON = START + datetime.timedelta(hours=5, minutes=40)
MID = START + datetime.timedelta(hours=6, minutes=40)
FLICKER = START + datetime.timedelta(hours=7, minutes=40)
IN_THE_DARK = START + datetime.timedelta(hours=8, minutes=40)
RESTART = START + datetime.timedelta(hours=11, minutes=40)
# A not_cat event holding a visit-sized blob for 10 s, and one holding it for a frame.
HIDDEN_CAT = START + datetime.timedelta(hours=9, minutes=40)
PASSING = START + datetime.timedelta(hours=10, minutes=40)


def visit_frames(spec: Spec) -> list[str]:
    """The frame that starts the clip comes a moment before its recorded start,
    as the detector logs it; the visit's peak a second later."""
    before = spec.started_at - datetime.timedelta(seconds=0.1)
    return (
        burst(before, 1, spec.foreground, spec.clean)
        + [
            frame(spec.started_at + datetime.timedelta(seconds=n / FPS), 0, 0, LIT)
            for n in range(FPS)
        ]
        + burst(
            spec.started_at + datetime.timedelta(seconds=1),
            5,
            spec.foreground,
            spec.clean,
            warm=False,
        )
    )


def metrics_lines(specs: list[Spec]) -> list[str]:
    rows: list[str] = []
    for spec in specs:
        rows += visit_frames(spec)
    rows += burst(PERSON, 5, 5000, 5000)
    rows += burst(MID, 5, 300, 800)
    rows += burst(FLICKER, 1, 100, 800)
    rows += burst(IN_THE_DARK, 30, 9000, 9000, DARK)
    rows += burst(RESTART, WARMUP_FRAMES, 300_000, 300_000, warm=False)
    rows += burst(HIDDEN_CAT, 300, 300, 700)
    rows += burst(PASSING, 1, 300, 700)
    rows.sort()
    return [",".join(("x", *COLUMNS)), *rows]  # an extra column, to read by name


def not_cat_specs() -> list[Spec]:
    return [
        Spec(1001, HIDDEN_CAT, "not_cat", 0, 0, 0),
        Spec(1002, PASSING, "not_cat", 0, 0, 0),
        Spec(1003, PERSON, "not_cat", 0, 0, 0),
    ]


def run_rows() -> list[TimelineRow]:
    """The visits, the not_cat events, one peek, one visit from before the run."""
    rows = [spec.row() for spec in visit_specs() + not_cat_specs()]
    peek = Spec(2001, START + datetime.timedelta(hours=11), "Ada", 0, 0, 0, peek=True)
    early = Spec(2002, START - datetime.timedelta(days=1), "Bea", 0, 0, 0)
    return [*rows, peek.row(), early.row()]


def lines_opener(lines: list[str]) -> Any:
    return lambda: iter(lines)


# --- reading ----------------------------------------------------------------


def test_the_csv_is_read_by_its_header_and_refused_without_its_columns() -> None:
    header = "timestamp,brightness,clean_area,foreground_px"
    frames = list(read_frames([header, "2026-09-16T10:00:00.000,9.5,7,3"]))
    assert frames == [("2026-09-16T10:00:00.000", 3, 7, 9.5)]
    with pytest.raises(ValueError, match="foreground_px"):
        list(read_frames(["timestamp,clean_area,brightness"]))


def test_damaged_rows_are_skipped_and_counted() -> None:
    tally = Tally()
    lines = [
        "timestamp,foreground_px,clean_area,brightness",
        "\x00\x00\x002026-09-16T10:00:00.000,1,2,3.0",  # after a power cut
        "2026-09-16T10:00:00.033,1,2",  # a tail still being written
        "\x00\x00\x00\x00,,,",
        "2026-09-16T10:00:00.066,1,2,3.0",
    ]
    frames = list(read_frames(lines, tally))
    assert [f[0] for f in frames] == [
        "2026-09-16T10:00:00.000",
        "2026-09-16T10:00:00.066",
    ]
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


def test_the_columns_and_warmup_are_the_recorder_and_detector_s() -> None:
    assert set(COLUMNS) <= set(FIELDS)
    warmup = inspect.signature(MotionDetector).parameters["warmup_frames"]
    assert warmup.default == WARMUP_FRAMES


# --- the whole run ----------------------------------------------------------


def test_a_finished_run_proposes_settings_that_miss_no_visit() -> None:
    lines = metrics_lines(visit_specs())
    result = calibrate(run_rows(), START, lines_opener(lines), SETTINGS)

    left_out = (result.left_out, result.peeks, result.uncovered, result.blank)
    assert (len(result.visits), *left_out) == (60, 1, 1, 0, 0)
    assert [c.met for c in result.conditions] == [True, True, True, True, None, True]
    assert result.done
    assert result.result is not None
    now, threshold, band = result.result.replays
    assert now.rule.low - 1 == SETTINGS.motion_threshold
    assert (threshold.rule.low - 1, band.rule.low, band.rule.high) == (500, 250, 1363)
    assert [60 - len(each.caught) for each in result.result.replays] == [0, 0, 0]
    # Outside the visits, now (150) takes the person, the mid-sized change, the
    # hidden cat and the passing frame; the proposed threshold only the person;
    # the band the mid-sized change and the hidden cat. Nothing takes the
    # flicker, the dark burst, the restart, or a visit's own first frame.
    assert [each.false_triggers for each in result.result.replays] == [4, 1, 2]
    text = "\n".join(report(result))
    assert "Environment=MOTION_THRESHOLD=500" in text
    assert "misses 0 of 60 visits" in text


def test_doubts_ask_about_the_weakest_visits_and_a_hidden_cat() -> None:
    lines = metrics_lines(visit_specs())
    result = calibrate(run_rows(), START, lines_opener(lines), SETTINGS)

    asked = {(d.question, d.event_id) for d in result.doubts}
    expected = {("weakest", 1), ("weakest", 2), ("weakest", 3)}
    assert asked == expected | {("not_cat_blob", 1001)}


def test_open_questions_hold_back_the_settings() -> None:
    lines = metrics_lines(visit_specs())
    result = calibrate(run_rows(), START, lines_opener(lines), SETTINGS)
    result.open_questions = 3

    text = "\n".join(report(result))
    assert "Provisional: 3 question(s)" in text
    assert "Environment=" not in text


def test_a_visit_with_no_blob_is_left_out_and_asked_about() -> None:
    specs = visit_specs()
    specs[30] = replace(specs[30], foreground=0, clean=0)
    lines = metrics_lines(specs)
    result = calibrate(run_rows(), START, lines_opener(lines), SETTINGS)

    assert (len(result.visits), result.blank) == (59, 1)
    assert ("blank", 31) in {(d.question, d.event_id) for d in result.doubts}
    assert result.result is not None
    _, threshold, band = result.result.replays
    assert (threshold.rule.low - 1, band.rule.low) == (500, 250)


def test_an_unfinished_run_says_what_is_missing_and_proposes_nothing() -> None:
    few = visit_specs()[:30]
    rows = [spec.row() for spec in few]
    result = calibrate(rows, START, lines_opener(metrics_lines(few)), SETTINGS)

    assert not result.done
    assert result.result is None
    assert result.conditions[0].met is False
    assert "Not done" in "\n".join(report(result))


def test_only_the_boxes_named_need_a_visit() -> None:
    two = [spec for spec in visit_specs() if spec.box != 2]
    rows = [spec.row() for spec in two]
    lines = lines_opener(metrics_lines(two))

    assert calibrate(rows, START, lines, SETTINGS).conditions[3].met is False
    left_and_middle = replace(SETTINGS, boxes=(1, 2))
    assert calibrate(rows, START, lines, left_and_middle).conditions[3].met is True


def test_a_new_low_among_the_last_visits_is_not_settled() -> None:
    specs = visit_specs()
    last = specs[-1].ended_at + datetime.timedelta(hours=1)
    specs.append(Spec(999, last, "Ada", 0, 900, 400))
    rows = [spec.row() for spec in specs]
    result = calibrate(rows, START, lines_opener(metrics_lines(specs)), SETTINGS)

    assert result.conditions[-1].met is False
    assert not result.done


def test_split_doubts_pair_one_cat_across_any_label_within_the_gap() -> None:
    def at(seconds: int, label: str, event_id: int) -> TimelineRow:
        when = START + datetime.timedelta(hours=1, seconds=seconds)
        return Spec(event_id, when, label, 0, 0, 0).row()

    rows = [
        at(0, "Ada", 1),
        at(50, "Ada", 2),  # 20 s after the first ends
        at(100, "not_cat", 3),
        at(150, "Ada", 4),
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
        make_clip_with_facts(directory, spec.started_at, 30, place=place)
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


def test_ask_skips_unlabelled_and_staged_events_and_asks_once(
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
    other_layout = tmp_path / "old.csv"
    other_layout.write_text("timestamp,foreground_px\n")
    args = main.parse_args(
        ["--calibrate", "--since", "2026-09-16", "--metrics", str(other_layout)]
    )
    assert main.run_command(args) == 1
