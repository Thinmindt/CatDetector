"""The capture database: ingest, events, labels and the reviewer's overrides."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    BOX_1,
    BOX_3,
    DISTANCE,
    GAP,
    at,
    make_clip_file,
    make_clip_with_facts,
    visit,
)

import review
from src.capture_db import CaptureDB
from src.clip_sidecar import CloseReason, read_sidecar

# --- ingest and labels ------------------------------------------------------


def test_ingest_registers_new_clips_once(db: Any, clip_dir: Path) -> None:
    assert db.ingest(clip_dir) == 2
    assert db.ingest(clip_dir) == 0
    assert db.counts() == {"total": 2, "labeled": 0, "multi_clip": 0}


def test_ingest_parses_started_at_from_the_filename(db: Any, clip_dir: Path) -> None:
    db.ingest(clip_dir)
    event = db.next_unlabeled()
    assert event.started_at == "2026-08-25T07:24:52"


def test_ingest_accepts_unparseable_names(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_file(directory / "oddly-named.h264")
    assert db.ingest(directory) == 1
    assert db.next_unlabeled().started_at is None


def test_ingest_survives_an_impossible_timestamp(db: Any, tmp_path: Path) -> None:
    """Digits matching the pattern but not a real date must not abort the scan."""
    directory = tmp_path / "captures"
    make_clip_file(directory / "cat_video_99999999_999999.h264")
    make_clip_file(directory / "cat_video_20260826_090000.h264")

    assert db.ingest(directory) == 2
    assert db.counts()["total"] == 2


def test_ingest_skips_a_clip_it_cannot_stat(db: Any, clip_dir: Path) -> None:
    """A clip removed from the share between the glob and the stat is skipped."""
    (clip_dir / "cat_video_20260827_120000.h264").symlink_to(clip_dir / "gone.h264")

    assert db.ingest(clip_dir) == 2
    assert db.counts()["total"] == 2


def test_ingest_of_a_missing_directory_is_zero(db: Any, tmp_path: Path) -> None:
    assert db.ingest(tmp_path / "nope") == 0


def test_next_unlabeled_is_oldest_first_and_skips_labeled(
    db: Any, clip_dir: Path
) -> None:
    db.ingest(clip_dir)
    first = db.next_unlabeled()
    db.set_label(first.id, "cat")

    second = db.next_unlabeled()
    assert second.id != first.id
    assert second.started_at > first.started_at


def test_set_label_overwrites(db: Any, clip_dir: Path) -> None:
    db.ingest(clip_dir)
    event = db.next_unlabeled()
    db.set_label(event.id, "cat")
    db.set_label(event.id, "not_cat")

    assert db.get(event.id).label == "not_cat"
    assert db.counts() == {"total": 2, "labeled": 1, "multi_clip": 0, "not_cat": 1}


# --- sidecars and grouping --------------------------------------------------


def test_ingest_takes_the_facts_from_the_sidecar(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=25, reason=CloseReason.MAX_LENGTH)

    db.ingest(directory)
    event = db.next_unlabeled()

    (clip,) = event.clips
    assert clip.started_at == "2026-09-12T08:00:00.000"
    assert clip.ended_at == "2026-09-12T08:00:25.000"
    assert clip.close_reason == "max_length"


def test_a_rescan_does_not_re_read_the_sidecars_it_already_has(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sidecars live on the CIFS share and ingest runs at every startup, so a
    growing archive must not add a round trip per clip to it."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    db.ingest(directory)

    read: list[str] = []

    def spy(clip: Path) -> Any:
        read.append(clip.name)
        return read_sidecar(clip)

    monkeypatch.setattr("src.clip_scan.read_sidecar", spy)
    make_clip_with_facts(directory, at(200), seconds=15)

    assert db.ingest(directory) == 1
    assert read == ["cat_video_20260912_080320.h264"]


def test_a_clip_without_a_sidecar_is_recovered_with_an_estimated_end(
    db: Any, tmp_path: Path
) -> None:
    """12.5 MB at 10 Mbit/s is ten seconds of footage."""
    directory = tmp_path / "captures"
    make_clip_file(directory / "cat_video_20260912_080000.h264", b"\0" * 12_500_000)

    db.ingest(directory)
    (clip,) = db.next_unlabeled().clips

    assert clip.close_reason == "recovered"
    assert clip.ended_at == "2026-09-12T08:00:10.000"


def test_clips_of_one_visit_become_one_event(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(40), seconds=15)
    make_clip_with_facts(directory, at(500), seconds=10)

    db.ingest(directory)

    assert db.counts() == {"total": 2, "labeled": 0, "multi_clip": 1}
    first = db.next_unlabeled()
    assert [c.started_at[11:19] for c in first.clips] == ["08:00:00", "08:00:40"]
    assert first.started_at == "2026-09-12T08:00:00.000"
    assert first.ended_at == "2026-09-12T08:00:55.000"


def test_a_later_ingest_keeps_event_ids_stable(db: Any, tmp_path: Path) -> None:
    """Deep links and the undo history hold event ids; a rescan must not move them."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    db.ingest(directory)
    before = db.next_unlabeled()
    db.set_label(before.id, "cat")

    make_clip_with_facts(directory, at(40), seconds=15)
    db.ingest(directory)

    after = db.get(before.id)
    assert len(after.clips) == 2
    assert after.label == "cat"
    assert db.next_unlabeled() is None


def test_regroup_with_a_wider_gap_merges_and_carries_an_agreeing_label(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(200), seconds=15)
    db.ingest(directory)
    for _ in range(2):
        db.set_label(db.next_unlabeled().id, "cat")
    assert db.counts()["total"] == 2

    db.gap_seconds = 600
    db.regroup()

    assert db.counts() == {"total": 1, "labeled": 1, "multi_clip": 1, "cat": 1}


def test_regroup_that_merges_disagreeing_labels_returns_the_event_to_the_queue(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(200), seconds=15)
    db.ingest(directory)
    db.set_label(db.next_unlabeled().id, "cat")
    db.set_label(db.next_unlabeled().id, "not_cat")

    db.gap_seconds = 600
    db.regroup()

    assert db.counts() == {"total": 1, "labeled": 0, "multi_clip": 1}
    assert db.next_unlabeled() is not None


def test_a_bridge_between_disagreeing_labels_drops_the_label(
    db: Any, tmp_path: Path
) -> None:
    """The merged event cannot be both, so it goes back in the queue."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(100), seconds=10)
    db.ingest(directory)
    db.set_label(db.next_unlabeled().id, "cat")
    db.set_label(db.next_unlabeled().id, "not_cat")

    make_clip_with_facts(directory, at(50), seconds=10)
    db.ingest(directory)

    assert db.counts() == {"total": 1, "labeled": 0, "multi_clip": 1}
    assert db.next_unlabeled() is not None


def test_a_late_clip_that_bridges_two_events_merges_them(
    db: Any, tmp_path: Path
) -> None:
    """A clip held back by a NAS outage can arrive after its neighbours."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(100), seconds=10)
    db.ingest(directory)
    first_id = db.next_unlabeled().id
    assert db.counts()["total"] == 2

    make_clip_with_facts(directory, at(50), seconds=10)
    db.ingest(directory)

    assert db.counts()["total"] == 1
    assert len(db.get(first_id).clips) == 3


def test_clips_in_different_boxes_stay_separate(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20, place=BOX_1)
    make_clip_with_facts(directory, at(30), seconds=10, place=BOX_3)

    db.ingest(directory)

    assert db.counts()["total"] == 2


def test_the_regroup_command_uses_the_current_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thresholds must be read when the database opens, not when the module
    loads, or a changed setting would not reach the rule."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(200), seconds=15)
    db_path = tmp_path / "captures.db"
    narrow = CaptureDB(db_path, gap_seconds=GAP, box_distance_px=DISTANCE)
    narrow.ingest(directory)
    assert narrow.counts()["total"] == 2
    narrow.close()

    monkeypatch.setattr("config.Config.DB_PATH", str(db_path))
    monkeypatch.setattr("config.Config.EVENT_GAP_SECONDS", 600.0)

    assert review.regroup_events()["total"] == 1


def test_split_starts_a_new_unlabeled_event_and_the_first_half_keeps_its_id(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40, 80)
    db.ingest(directory)
    event = db.next_unlabeled()
    db.set_label(event.id, "cat")

    db.split(event.clips[1].id)

    first = db.get(event.id)
    assert [c.started_at[11:19] for c in first.clips] == ["08:00:00"]
    assert first.label == "cat"
    second = db.next_unlabeled()
    assert second.id != first.id
    assert [c.started_at[11:19] for c in second.clips] == ["08:00:40", "08:01:20"]
    assert db.counts() == {"total": 2, "labeled": 1, "multi_clip": 1, "cat": 1}


def test_a_split_survives_regroup(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    db.ingest(directory)
    db.split(db.next_unlabeled().clips[1].id)

    db.gap_seconds = 10_000
    db.regroup()

    assert db.counts()["total"] == 2


def test_join_merges_with_the_next_event_and_survives_regroup(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0)
    visit(directory, 500)
    db.ingest(directory)
    first = db.next_unlabeled()
    db.set_label(first.id, "cat")
    db.set_label(db.next_unlabeled().id, "cat")

    merged = db.join(first.id)

    assert merged.id == first.id
    assert len(merged.clips) == 2
    assert merged.label == "cat"
    db.gap_seconds = 1
    db.regroup()
    assert db.counts()["total"] == 1


def test_join_with_no_later_event_is_none(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0)
    db.ingest(directory)
    assert db.join(db.next_unlabeled().id) is None


def test_next_multi_walks_the_multi_clip_events_in_order(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    visit(directory, 500)
    visit(directory, 1000, 40)
    db.ingest(directory)

    first = db.next_multi(None)
    second = db.next_multi(first.id)

    assert first.started_at[11:19] == "08:00:00"
    assert second.started_at[11:19] == "08:16:40"
    assert db.next_multi(second.id) is None


def test_next_labeled_walks_labeled_events_in_order_or_one_label(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    for start in (0, 500, 1000, 1500):
        visit(directory, start)
    db.ingest(directory)
    first = db.next_unlabeled()
    db.set_label(first.id, "cat")
    second = db.next_unlabeled()
    db.set_label(second.id, "not_cat")
    third = db.next_unlabeled()
    db.set_label(third.id, "cat")

    walked = []
    event = db.next_labeled(None, None)
    while event is not None:
        walked.append(event.id)
        event = db.next_labeled(event.id, None)
    cats = [db.next_labeled(None, "cat"), db.next_labeled(first.id, "cat")]

    assert walked == [first.id, second.id, third.id]
    assert [c.id for c in cats] == [first.id, third.id]
    assert db.next_labeled(third.id, "cat") is None


def test_next_multi_carries_on_after_a_split_leaves_one_clip(
    db: Any, tmp_path: Path
) -> None:
    """Regression: splitting the event under the walker sent it back to the
    oldest multi-clip event, so the audit looped over the same events."""
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    visit(directory, 500, 40)
    visit(directory, 1000, 40)
    db.ingest(directory)
    first = db.next_multi(None)
    second = db.next_multi(first.id)

    db.split(second.clips[1].id)

    third = db.next_multi(second.id)
    assert third is not None
    assert third.started_at[11:19] == "08:16:40"


def test_poop_counts_survive_a_regroup(db: Any, tmp_path: Path) -> None:
    """They hang off a clip, not an event: regroup renumbers every event."""
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    db.ingest(directory)
    event = db.next_unlabeled()
    db.set_label(event.id, "clean")
    db.set_poop_counts(event.id, {1: 2, 3: 0})

    db.gap_seconds = 10_000
    db.regroup()

    after = db.next_multi(None)
    assert after.id != event.id
    assert after.poops == {1: 2, 3: 0}


def test_setting_poop_counts_again_replaces_them(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0)
    db.ingest(directory)
    event = db.next_unlabeled()

    db.set_poop_counts(event.id, {1: 2, 2: 1})
    db.set_poop_counts(event.id, {1: 3})

    assert db.get(event.id).poops == {1: 3}


def test_a_database_from_before_grouping_is_refused(tmp_path: Path) -> None:
    old = tmp_path / "captures.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, clip_path TEXT)")

    with pytest.raises(RuntimeError, match="predates event grouping"):
        CaptureDB(old)


# --- one connection, many threads -------------------------------------------


def read_repeatedly(db: Any, event_id: int, failures: list[BaseException]) -> None:
    try:
        for _ in range(200):
            got = db.get(event_id)
            assert got is not None and len(got.clips) == 4
            assert got.poops == {1: 1, 2: 0, 3: 2}
            db.counts()
    except BaseException as exc:
        failures.append(exc)


def test_ingest_reads_the_share_without_holding_the_lock(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled share must stall the rescan, not every reader of the database."""
    directory = tmp_path / "captures"
    directory.mkdir()
    make_clip_file(directory / "cat_video_20260912_080000.h264")
    reading = threading.Event()
    release = threading.Event()

    def stalled_sidecar(clip: Path) -> None:
        reading.set()
        assert release.wait(timeout=10)

    monkeypatch.setattr("src.clip_scan.read_sidecar", stalled_sidecar)
    ingest = threading.Thread(target=db.ingest, args=(directory,), daemon=True)
    ingest.start()
    assert reading.wait(timeout=10)

    reader = threading.Thread(target=db.counts, daemon=True)
    reader.start()
    reader.join(timeout=5)
    reader_stuck = reader.is_alive()
    release.set()
    ingest.join(timeout=10)

    assert not ingest.is_alive()
    assert not reader_stuck
    assert db.counts()["total"] == 1


def test_concurrent_reads_share_the_connection_safely(db: Any, tmp_path: Path) -> None:
    """The web server is threaded and a browser asks for every clip's media at once."""
    directory = tmp_path / "captures"
    visit(directory, 0, 40, 80, 120)
    db.ingest(directory)
    event = db.next_unlabeled()
    db.set_poop_counts(event.id, {1: 1, 2: 0, 3: 2})
    failures: list[BaseException] = []
    threads = [
        threading.Thread(target=read_repeatedly, args=(db, event.id, failures))
        for _ in range(8)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
