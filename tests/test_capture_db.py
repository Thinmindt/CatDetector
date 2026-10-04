"""The capture database: ingest, events, labels and the reviewer's overrides."""

from __future__ import annotations

import datetime
import fcntl
import sqlite3
import threading
import time
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

import main
from src.clips.sidecar import CloseReason, read_sidecar
from src.review.capture_db import CaptureDB, DatabaseInUseError

# --- ingest and labels ------------------------------------------------------


def test_ingest_registers_new_clips_once(db: Any, clip_dir: Path) -> None:
    assert db.ingest(clip_dir) == 2
    assert db.ingest(clip_dir) == 0
    assert db.counts() == {"total": 2, "labeled": 0, "multi_clip": 0}


def test_ingest_parses_started_at_from_the_filename(db: Any, clip_dir: Path) -> None:
    db.ingest(clip_dir)
    event = db.next_to_review()
    assert event.started_at == datetime.datetime(2026, 8, 25, 7, 24, 52)


def test_ingest_accepts_unparseable_names(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_file(directory / "oddly-named.h264")
    assert db.ingest(directory) == 1
    assert db.next_to_review().started_at is None


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
    first = db.next_to_review()
    db.set_label(first.id, "cat")

    second = db.next_to_review()
    assert second.id != first.id
    assert second.started_at > first.started_at


def test_set_label_overwrites(db: Any, clip_dir: Path) -> None:
    db.ingest(clip_dir)
    event = db.next_to_review()
    db.set_label(event.id, "cat")
    db.set_label(event.id, "not_cat")

    assert db.get(event.id).label == "not_cat"
    assert db.counts() == {"total": 2, "labeled": 1, "multi_clip": 0, "not_cat": 1}


# --- sidecars and grouping --------------------------------------------------


def test_ingest_takes_the_facts_from_the_sidecar(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=25, reason=CloseReason.MAX_LENGTH)

    db.ingest(directory)
    event = db.next_to_review()

    (clip,) = event.clips
    assert clip.started_at == at(0)
    assert clip.ended_at == at(25)
    assert clip.close_reason is CloseReason.MAX_LENGTH


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

    monkeypatch.setattr("src.review.clip_scan.read_sidecar", spy)
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
    (clip,) = db.next_to_review().clips

    assert clip.close_reason is CloseReason.RECOVERED
    assert clip.ended_at == at(10)


def test_clips_of_one_visit_become_one_event(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(40), seconds=15)
    make_clip_with_facts(directory, at(500), seconds=10)

    db.ingest(directory)

    assert db.counts() == {"total": 2, "labeled": 0, "multi_clip": 1}
    first = db.next_to_review()
    assert [c.started_at for c in first.clips] == [at(0), at(40)]
    assert first.started_at == at(0)
    assert first.ended_at == at(55)


def test_a_later_ingest_keeps_event_ids_stable(db: Any, tmp_path: Path) -> None:
    """Deep links and the undo history hold event ids; a rescan must not move them."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    db.ingest(directory)
    before = db.next_to_review()
    db.set_label(before.id, "cat")

    make_clip_with_facts(directory, at(40), seconds=15)
    db.ingest(directory)

    after = db.get(before.id)
    assert len(after.clips) == 2
    assert after.label == "cat"
    assert db.next_to_review() is None


def test_regroup_with_a_wider_gap_merges_and_carries_an_agreeing_label(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(200), seconds=15)
    db.ingest(directory)
    for _ in range(2):
        db.set_label(db.next_to_review().id, "cat")
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
    db.set_label(db.next_to_review().id, "cat")
    db.set_label(db.next_to_review().id, "not_cat")

    db.gap_seconds = 600
    db.regroup()

    assert db.counts() == {"total": 1, "labeled": 0, "multi_clip": 1}
    assert db.next_to_review() is not None


def test_a_bridge_between_disagreeing_labels_drops_the_label(
    db: Any, tmp_path: Path
) -> None:
    """The merged event cannot be both, so it goes back in the queue."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(100), seconds=10)
    db.ingest(directory)
    db.set_label(db.next_to_review().id, "cat")
    db.set_label(db.next_to_review().id, "not_cat")

    make_clip_with_facts(directory, at(50), seconds=10)
    db.ingest(directory)

    assert db.counts() == {"total": 1, "labeled": 0, "multi_clip": 1}
    assert db.next_to_review() is not None


def test_a_late_clip_that_bridges_two_events_merges_them(
    db: Any, tmp_path: Path
) -> None:
    """A clip held back by an outage of the share can arrive after its neighbours."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(100), seconds=10)
    db.ingest(directory)
    first_id = db.next_to_review().id
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

    assert main.regroup_events()["total"] == 1


def test_split_starts_a_new_unlabeled_event_and_the_first_half_keeps_its_id(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40, 80)
    db.ingest(directory)
    event = db.next_to_review()
    db.set_label(event.id, "cat")

    db.split(event.clips[1].id)

    first = db.get(event.id)
    assert [c.started_at for c in first.clips] == [at(0)]
    assert first.label == "cat"
    second = db.next_to_review()
    assert second.id != first.id
    assert [c.started_at for c in second.clips] == [at(40), at(80)]
    assert db.counts() == {"total": 2, "labeled": 1, "multi_clip": 1, "cat": 1}


def test_a_split_survives_regroup(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    db.ingest(directory)
    db.split(db.next_to_review().clips[1].id)

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
    first = db.next_to_review()
    db.set_label(first.id, "cat")
    db.set_label(db.next_to_review().id, "cat")

    merged = db.join(first.id)

    assert merged.id == first.id
    assert len(merged.clips) == 2
    assert merged.label == "cat"
    db.gap_seconds = 1
    db.regroup()
    assert db.counts()["total"] == 1


def test_join_merges_events_whose_clips_interleave(db: Any, tmp_path: Path) -> None:
    """Two boxes at once: each event has a clip that starts inside the other's span."""
    directory = tmp_path / "captures"
    for offset, place in ((0, BOX_1), (30, BOX_3), (50, BOX_1), (80, BOX_3)):
        make_clip_with_facts(directory, at(offset), seconds=15, place=place)
    db.ingest(directory)
    first, second = walk_queue(db)
    assert [c.started_at for c in first.clips] == [at(0), at(50)]
    assert [c.started_at for c in second.clips] == [at(30), at(80)]

    merged = db.join(first.id)

    assert [c.started_at for c in merged.clips] == [at(o) for o in (0, 30, 50, 80)]
    assert db.counts()["total"] == 1
    db.regroup()
    assert db.counts()["total"] == 1


def test_join_with_no_later_event_is_none(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0)
    db.ingest(directory)
    assert db.join(db.next_to_review().id) is None


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

    assert first.started_at == at(0)
    assert second.started_at == at(1000)
    assert db.next_multi(second.id) is None


def test_next_labeled_walks_labeled_events_in_order_or_one_label(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    for start in (0, 500, 1000, 1500):
        visit(directory, start)
    db.ingest(directory)
    first = db.next_to_review()
    db.set_label(first.id, "cat")
    second = db.next_to_review()
    db.set_label(second.id, "not_cat")
    third = db.next_to_review()
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
    assert third.started_at == at(1000)


def test_poop_counts_survive_a_regroup(db: Any, tmp_path: Path) -> None:
    """They hang off a clip, not an event: regroup renumbers every event."""
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    db.ingest(directory)
    event = db.next_to_review()
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
    event = db.next_to_review()

    db.set_poop_counts(event.id, {1: 2, 2: 1})
    db.set_poop_counts(event.id, {1: 3})

    assert db.get(event.id).poops == {1: 3}


def test_a_database_from_before_grouping_is_refused(tmp_path: Path) -> None:
    old = tmp_path / "captures.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, clip_path TEXT)")

    with pytest.raises(RuntimeError, match="predates event grouping"):
        CaptureDB(old, gap_seconds=GAP, box_distance_px=DISTANCE)


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

    monkeypatch.setattr("src.review.clip_scan.read_sidecar", stalled_sidecar)
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
    event = db.next_to_review()
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


# --- the review queue: staging ----------------------------------------------


def test_a_staged_event_stays_staged_across_a_regroup(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    visit(directory, 500)
    db.ingest(directory)
    first, second = walk_queue(db)
    db.set_label(first.id, "cat")
    db.set_label(second.id, "not_cat")
    assert db.next_to_review() is None

    db.stage(second.id, "judged from eight seconds", "agent")
    db.gap_seconds = 1
    db.regroup()

    (staged,) = walk_queue(db)
    assert [clip.id for clip in staged.clips] == [clip.id for clip in second.clips]
    assert staged.label == "not_cat"
    assert staged.staged.reason == "judged from eight seconds"


def test_staging_by_label_and_time_takes_only_what_matches(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    for start in (0, 500, 1000):
        visit(directory, start)
    db.ingest(directory)
    first, second, third = (event.id for event in walk_queue(db))
    db.set_label(first, "not_cat")
    db.set_label(second, "not_cat")
    cutoff = datetime.datetime.now()
    db.set_label(third, "cat")
    db.set_label(second, "not_cat")  # relabelled after the cutoff

    assert db.stage_labeled("early", "human", value="cat", labeled_before=cutoff) == 0
    assert (
        db.stage_labeled("early", "human", value="not_cat", labeled_before=cutoff) == 1
    )
    assert [event.id for event in walk_queue(db)] == [first]
    assert db.counts()["staged"] == 1


# --- peeks -----------------------------------------------------------------


def test_a_peek_keeps_its_label_and_survives_a_regroup(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    visit(directory, 500)
    db.ingest(directory)
    peek, other = walk_queue(db)
    db.set_label(peek.id, "cat")
    db.set_label(other.id, "cat")
    assert db.set_peek(peek.id, True)

    db.gap_seconds = 1
    db.regroup()

    events = walk_labeled(db)
    marked = [event for event in events if event.peek]
    assert [clip.id for event in marked for clip in event.clips] == [
        clip.id for clip in peek.clips
    ]
    assert [event.label for event in marked] == ["cat", "cat"]
    assert len(events) == 3


def test_a_split_keeps_the_peek_on_both_halves(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    db.ingest(directory)
    event = db.next_to_review()
    db.set_peek(event.id, True)

    db.split(event.clips[1].id)

    first, second = walk_queue(db)
    assert first.peek and second.peek


def test_joining_a_peek_to_a_visit_makes_the_whole_a_visit(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0)
    visit(directory, 500)
    db.ingest(directory)
    peek, _ = walk_queue(db)
    db.set_peek(peek.id, True)

    merged = db.join(peek.id)

    assert len(merged.clips) == 2
    assert not merged.peek


def test_a_clip_grouped_into_a_peek_at_ingest_makes_it_a_visit(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0)
    db.ingest(directory)
    peek = db.next_to_review()
    db.set_label(peek.id, "cat")
    db.set_peek(peek.id, True)

    visit(directory, 40)
    db.ingest(directory)

    grown = db.get(peek.id)
    assert len(grown.clips) == 2
    assert not grown.peek
    assert (grown.label, grown.staged.source) == ("cat", "grouping")
    assert [event.id for event in walk_queue(db)] == [peek.id]


def test_a_peek_joined_to_a_visit_stays_a_visit_after_a_split(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0)
    visit(directory, 500)
    db.ingest(directory)
    peek, _ = walk_queue(db)
    db.set_label(peek.id, "cat")
    db.set_peek(peek.id, True)
    merged = db.join(peek.id)

    db.split(merged.clips[1].id)

    first, *_ = walk_labeled(db)
    assert first.id == peek.id
    assert not first.peek
    assert first.staged is None


def test_a_peek_can_be_cleared_and_shows_on_the_timeline(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    visit(directory, 500)
    db.ingest(directory)
    peek, other = walk_queue(db)

    db.set_peek(peek.id, True)
    assert {row.id: row.peek for row in db.timeline_rows()} == {
        peek.id: True,
        other.id: False,
    }
    db.set_peek(peek.id, False)
    assert not db.get(peek.id).peek
    assert not any(row.peek for row in db.timeline_rows())
    assert not db.set_peek(999, True)


def walk_labeled(db: Any) -> list[Any]:
    events: list[Any] = []
    while (
        event := db.next_labeled(events[-1].id if events else None, None)
    ) is not None:
        events.append(event)
    return events


def walk_queue(db: Any) -> list[Any]:
    events: list[Any] = []
    while (event := db.next_to_review(events[-1].id if events else None)) is not None:
        events.append(event)
    return events


# --- regroup refuses while another process has the database -----------------


def test_regroup_refuses_while_the_database_is_open_elsewhere(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a regroup beside the service could lose a label, or put one
    on a renumbered event the page still held by its old id."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(200), seconds=15)
    db.ingest(directory)
    monkeypatch.setattr("main.Config.DB_PATH", str(db.path))
    monkeypatch.setattr("main.Config.EVENT_GAP_SECONDS", 600.0)

    with pytest.raises(DatabaseInUseError, match="another process"):
        main.regroup_events()
    assert db.counts()["total"] == 2

    db.close()
    assert main.regroup_events()["total"] == 1


def test_the_database_opens_again_after_a_refused_regroup(
    db: Any, tmp_path: Path
) -> None:
    other = CaptureDB(db.path, gap_seconds=GAP, box_distance_px=DISTANCE)
    with pytest.raises(DatabaseInUseError):
        other.regroup()
    other.close()
    db.regroup()
    CaptureDB(db.path, gap_seconds=GAP, box_distance_px=DISTANCE).close()


def test_a_refused_regroup_keeps_its_shared_hold(db: Any) -> None:
    """flock drops the shared lock before trying for the exclusive one, so a
    refused upgrade left the database open with no hold at all."""
    other = CaptureDB(db.path, gap_seconds=GAP, box_distance_px=DISTANCE)
    with pytest.raises(DatabaseInUseError):
        db.regroup()
    other.close()

    third = CaptureDB(db.path, gap_seconds=GAP, box_distance_px=DISTANCE)
    try:
        with pytest.raises(DatabaseInUseError):
            third.regroup()
    finally:
        third.close()


def test_opening_waits_out_a_regroup_and_gives_up_after_the_wait(
    tmp_path: Path,
) -> None:
    """A service restarted during a regroup must not lose review for the run."""
    path = tmp_path / "captures.db"
    lock = (tmp_path / "captures.db.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    with pytest.raises(DatabaseInUseError):
        CaptureDB(path, gap_seconds=GAP, box_distance_px=DISTANCE, hold_wait_seconds=0)

    threading.Timer(0.3, lock.close).start()
    started = time.monotonic()
    CaptureDB(path, gap_seconds=GAP, box_distance_px=DISTANCE).close()
    assert time.monotonic() - started >= 0.2
