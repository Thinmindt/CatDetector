"""Calibration over a synthetic data run whose answer is known (roadmap A.2)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import make_clip_with_facts

import main
from src.capture.motion_metrics import FIELDS
from src.clips.sidecar import iso
from src.review.api import cat_labels
from src.review.calibration import (
    COLUMNS,
    Settings,
    Window,
    WindowIndex,
    calibrate,
    read_frames,
    report,
    split_doubts,
)
from src.review.capture_db import CaptureDB
from src.review.timeline import TimelineRow

CATS = ("Ada", "Bea", "Cy")
SETTINGS = Settings(
    cats=CATS,
    cat_labels=cat_labels(CATS),
    dark_brightness=10,
    motion_timeout=10,
    gap_seconds=60,
    motion_threshold=150,
)
START = datetime.datetime(2026, 9, 16)
VISITS = 60
BOX_X = (100, 320, 540)
LIT, DARK = 100.0, 5.0


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


def at(when: datetime.datetime, seconds: float) -> str:
    return iso(when + datetime.timedelta(seconds=seconds))


def frame(when: str, foreground: int = 0, clean: int = 0, bright: float = LIT) -> str:
    return f"{when},{foreground},{clean},{bright}"


def burst(
    when: datetime.datetime,
    frames: int,
    foreground: int,
    clean: int,
    bright: float = LIT,
) -> list[str]:
    return [frame(at(when, n / 30), foreground, clean, bright) for n in range(frames)]


# Outside every visit: a person (big), a mid-sized change only the band takes,
# a one-frame flicker nothing takes, and a dark burst the gate ignores.
PERSON = START + datetime.timedelta(hours=5, minutes=40)
MID = START + datetime.timedelta(hours=6, minutes=40)
FLICKER = START + datetime.timedelta(hours=7, minutes=40)
IN_THE_DARK = START + datetime.timedelta(hours=8, minutes=40)
# A not_cat event holding a visit-sized blob for 10 s, and one holding it for a frame.
HIDDEN_CAT = START + datetime.timedelta(hours=9, minutes=40)
PASSING = START + datetime.timedelta(hours=10, minutes=40)


def metrics_lines(specs: list[Spec]) -> list[str]:
    lines = [",".join(("x", *COLUMNS))]  # an extra column, to read by name
    rows: list[str] = []
    for spec in specs:
        rows += burst(spec.started_at, 10, 0, 0)
        rows += burst(
            spec.started_at + datetime.timedelta(seconds=1),
            5,
            spec.foreground,
            spec.clean,
        )
        rows += burst(spec.started_at + datetime.timedelta(seconds=2), 10, 0, 0)
    rows += burst(PERSON, 5, 5000, 5000)
    rows += burst(MID, 5, 300, 800)
    rows += burst(FLICKER, 1, 100, 800)
    rows += burst(IN_THE_DARK, 30, 9000, 9000, DARK)
    rows += burst(HIDDEN_CAT, 300, 300, 700)
    rows += burst(PASSING, 1, 300, 700)
    rows.sort()
    return lines + [f"0,{row}" for row in rows]


def not_cat_specs() -> list[Spec]:
    return [
        Spec(1001, HIDDEN_CAT, "not_cat", 0, 0, 0),
        Spec(1002, PASSING, "not_cat", 0, 0, 0),
        Spec(1003, PERSON, "not_cat", 0, 0, 0),
    ]


def run_rows() -> list[TimelineRow]:
    """The visits, the not_cat events, one peek, one visit from before the run."""
    rows = [spec.row() for spec in visit_specs() + not_cat_specs()]
    rows.append(
        Spec(
            2001, START + datetime.timedelta(hours=11), "Ada", 0, 0, 0, peek=True
        ).row()
    )
    rows.append(Spec(2002, START - datetime.timedelta(days=1), "Bea", 0, 0, 0).row())
    return rows


def lines_opener(lines: list[str]) -> Any:
    return lambda: iter(lines)


# --- reading ----------------------------------------------------------------


def test_the_csv_is_read_by_its_header_and_refused_without_its_columns() -> None:
    frames = list(
        read_frames(["timestamp,brightness,clean_area,foreground_px", "t,9.5,7,3"])
    )
    assert frames == [("t", 3, 7, 9.5)]
    with pytest.raises(ValueError, match="foreground_px"):
        list(read_frames(["timestamp,clean_area,brightness"]))


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
    assert index.holding("2026-09-16T10:04") == [
        1
    ]  # after a later one, as a clock step
    assert index.holding("2026-09-16T10:30") == []


def test_the_columns_read_are_the_ones_the_recorder_writes() -> None:
    assert set(COLUMNS) <= set(FIELDS)


# --- the whole run ----------------------------------------------------------


def test_a_finished_run_proposes_settings_that_miss_no_visit() -> None:
    lines = metrics_lines(visit_specs())
    result = calibrate(run_rows(), START, lines_opener(lines), SETTINGS)

    assert (len(result.visits), result.left_out, result.peeks, result.uncovered) == (
        60,
        1,
        1,
        0,
    )
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
    # flicker or the dark burst.
    assert [each.false_triggers for each in result.result.replays] == [4, 1, 2]
    text = "\n".join(report(result))
    assert "Environment=MOTION_THRESHOLD=500" in text
    assert "misses 0 of 60 visits" in text


def test_doubts_ask_about_the_weakest_visits_and_a_hidden_cat() -> None:
    result = calibrate(
        run_rows(), START, lines_opener(metrics_lines(visit_specs())), SETTINGS
    )

    by_question = {(d.question, d.event_id) for d in result.doubts}
    assert by_question == {
        ("weakest", 1),
        ("weakest", 2),
        ("weakest", 3),
        ("not_cat_blob", 1001),
    }


def test_an_unfinished_run_says_what_is_missing_and_proposes_nothing() -> None:
    few = visit_specs()[:30]
    rows = [spec.row() for spec in few]
    result = calibrate(rows, START, lines_opener(metrics_lines(few)), SETTINGS)

    assert not result.done
    assert result.result is None
    assert result.conditions[0].met is False
    assert "Not done" in "\n".join(report(result))


def test_a_new_low_among_the_last_visits_is_not_settled() -> None:
    specs = visit_specs()
    weaker = Spec(
        999, specs[-1].ended_at + datetime.timedelta(hours=1), "Ada", 0, 900, 400
    )
    specs.append(weaker)
    rows = [spec.row() for spec in specs]
    result = calibrate(rows, START, lines_opener(metrics_lines(specs)), SETTINGS)

    assert result.conditions[-1].met is False
    assert not result.done


def test_split_doubts_pair_one_cat_across_any_label_within_the_gap() -> None:
    base = START + datetime.timedelta(hours=1)
    rows = [
        Spec(1, base, "Ada", 0, 0, 0).row(),
        Spec(2, base + datetime.timedelta(seconds=50), "Ada", 0, 0, 0).row(),  # 20 s on
        Spec(3, base + datetime.timedelta(seconds=100), "not_cat", 0, 0, 0).row(),
        Spec(4, base + datetime.timedelta(seconds=150), "Ada", 0, 0, 0).row(),
        Spec(5, base + datetime.timedelta(hours=1), "Ada", 0, 0, 0).row(),
    ]
    doubts = split_doubts(rows, SETTINGS.cat_labels, SETTINGS.gap_seconds)
    assert [(d.event_id, d.reason) for d in doubts] == [
        (1, "the same cat 20 s before #2: one visit? join with next")
    ]


# --- asked once, through main.py --------------------------------------------


def test_calibrate_reports_and_stages_each_doubt_once(
    db: CaptureDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "captures"
    specs = visit_specs()
    for spec in specs:
        make_clip_with_facts(
            directory, spec.started_at, 30, place=(BOX_X[spec.box], 240)
        )
    db.ingest(directory)
    rows = sorted(db.timeline_rows(), key=lambda row: row.started_at or START)
    for spec, stored in zip(specs, rows, strict=True):
        db.set_label(stored.id, spec.label)
    csv = tmp_path / "metrics.csv"
    csv.write_text("\n".join(metrics_lines(specs)) + "\n")
    for name, value in (
        ("DB_PATH", str(db.path)),
        ("CAT_NAMES", CATS),
        ("METRICS_CSV", str(csv)),
        ("DARK_BRIGHTNESS", 10.0),
        ("MOTION_THRESHOLD", 150),
        ("EVENT_GAP_SECONDS", 60.0),
    ):
        monkeypatch.setattr(f"main.Config.{name}", value)
    args = main.parse_args(["--calibrate", "--since", START.isoformat()])

    assert main.run_command(args) == 0
    first = capsys.readouterr().out
    assert main.run_command(args) == 0
    second = capsys.readouterr().out

    assert "Environment=MOTION_THRESHOLD=500" in first
    assert "Staged 3 event(s)" in first  # the three weakest
    assert "Staged 0 event(s)" in second
    assert db.counts()["staged"] == 3


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

    assert db.ask(first, "weakest", "why", "calibration")
    db.set_label(first, "Ada")  # confirmed
    assert not db.ask(first, "weakest", "why", "calibration")
    assert db.ask(first, "split", "another question", "calibration")
    assert not db.ask(second, "weakest", "why", "calibration")
    kept = db.get(second)
    assert kept is not None
    assert kept.staged is not None
    assert kept.staged.reason == "a reviewer's own"
    assert not db.ask(third, "weakest", "why", "calibration")


@pytest.mark.parametrize(
    "argv",
    [["--calibrate"], ["--since", "2026-09-16"], ["--metrics", "x.csv"]],
)
def test_calibrate_needs_since_and_its_options_need_calibrate(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        main.parse_args(argv)


def test_calibrate_without_a_metrics_csv_fails_plainly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("main.Config.METRICS_CSV", None)
    args = main.parse_args(["--calibrate", "--since", "2026-09-16"])
    assert main.run_command(args) == 1
    args = main.parse_args(
        [
            "--calibrate",
            "--since",
            "2026-09-16",
            "--metrics",
            str(tmp_path / "none.csv"),
        ]
    )
    assert main.run_command(args) == 1
