"""StripsAhead: the queue's next strips are built before the page asks for them."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import visit

from src.review.clip_frames import ClipFrames
from src.review.strips_ahead import StripsAhead

WAIT_SECONDS = 5


class RecordingFrames(ClipFrames):
    """ClipFrames whose builds are recorded rather than run through ffmpeg.

    Each build also checks, from another thread, that the database lock is free.
    """

    def __init__(self, cache_dir: Path, db: Any, fail: bool = False) -> None:
        super().__init__(cache_dir)
        self.db = db
        self.fail = fail
        self.built: list[int] = []
        self.lock_was_free: list[bool] = []

    def _build_strip(self, clip_id: int, clip: Path) -> Path | None:
        self.built.append(clip_id)
        self.lock_was_free.append(self._lock_is_free())
        if self.fail:
            return None
        strip = self.cache_dir / f"clip{clip_id}_strip1s.jpg"
        strip.write_bytes(b"jpeg")
        return strip

    def _lock_is_free(self) -> bool:
        free: list[bool] = []

        def probe() -> None:
            got = self.db._lock.acquire(timeout=1)
            if got:
                self.db._lock.release()
            free.append(got)

        prober = threading.Thread(target=probe)
        prober.start()
        prober.join(timeout=WAIT_SECONDS)
        return free == [True]


@pytest.fixture
def queue(db: Any, tmp_path: Path) -> list[Any]:
    """Four visits, the first of two clips; the second labelled, the third
    labelled and staged. Returns the events in time order."""
    directory = tmp_path / "captures"
    for start, *offsets in ((0, 40), (500,), (1000,), (1500,)):
        visit(directory, start, *offsets)
    db.ingest(directory)
    events: list[Any] = []
    while (event := db.next_to_review(events[-1].id if events else None)) is not None:
        events.append(event)
    db.set_label(events[1].id, "cat")
    db.set_label(events[2].id, "not_cat")
    db.stage(events[2].id, "check", "human")
    return events


def clip_ids(*events: Any) -> list[int]:
    return [clip.id for event in events for clip in event.clips]


def test_builds_the_queue_in_order_skipping_labelled_and_built_clips(
    db: Any, tmp_path: Path, queue: list[Any]
) -> None:
    frames = RecordingFrames(tmp_path / "cache", db)
    already = queue[0].clips[1].id
    (frames.cache_dir / f"clip{already}_strip1s.jpg").write_bytes(b"jpeg")

    assert StripsAhead(db, frames).build_ahead() == 3

    assert frames.built == [queue[0].clips[0].id, *clip_ids(queue[2], queue[3])]
    assert all(frames.lock_was_free)
    assert StripsAhead(db, frames).build_ahead() == 0


def test_the_walk_is_bounded_to_the_next_few_events(
    db: Any, tmp_path: Path, queue: list[Any]
) -> None:
    frames = RecordingFrames(tmp_path / "cache", db)
    StripsAhead(db, frames, events=1).build_ahead()
    assert frames.built == clip_ids(queue[0])


def test_a_clip_that_cannot_be_built_is_not_retried(
    db: Any, tmp_path: Path, queue: list[Any]
) -> None:
    frames = RecordingFrames(tmp_path / "cache", db, fail=True)
    ahead = StripsAhead(db, frames)
    ahead.build_ahead()
    ahead.build_ahead()
    assert frames.built == clip_ids(queue[0], queue[2], queue[3])


def test_the_thread_builds_when_wanted_and_stops(
    db: Any, tmp_path: Path, queue: list[Any]
) -> None:
    frames = RecordingFrames(tmp_path / "cache", db)
    ahead = StripsAhead(db, frames)
    ahead.start()
    ahead.wanted()
    deadline = time.monotonic() + WAIT_SECONDS
    while len(frames.built) < len(clip_ids(queue[0], queue[2], queue[3])):
        assert time.monotonic() < deadline, frames.built
        time.sleep(0.01)
    assert ahead.stop(timeout=WAIT_SECONDS)


def test_a_failed_clip_is_tried_again_once_the_retry_time_has_passed(
    db: Any, tmp_path: Path, queue: list[Any]
) -> None:
    """Regression: a failure was remembered for the life of the process, so a
    brief outage of the share stopped building ahead for every clip it touched."""
    frames = RecordingFrames(tmp_path / "cache", db, fail=True)
    ahead = StripsAhead(db, frames, retry_seconds=0)
    ahead.build_ahead()
    frames.fail = False
    assert ahead.build_ahead() == 4
    assert frames.built == clip_ids(queue[0], queue[2], queue[3]) * 2


def test_a_request_for_a_strip_being_built_waits_for_that_build(
    db: Any, tmp_path: Path, queue: list[Any]
) -> None:
    """Two callers for one clip, the builder and the page, decode it once."""
    release = threading.Event()
    frames = RecordingFrames(tmp_path / "cache", db)
    build = frames._build_strip

    def slow_build(clip_id: int, clip: Path) -> Path | None:
        release.wait(WAIT_SECONDS)
        return build(clip_id, clip)

    frames._build_strip = slow_build  # type: ignore[method-assign]
    clip = queue[0].clips[0]
    results: list[Path | None] = []
    callers = [
        threading.Thread(
            target=lambda: results.append(frames.strip(clip.id, clip.path))
        )
        for _ in range(2)
    ]
    for caller in callers:
        caller.start()
    time.sleep(0.2)
    release.set()
    for caller in callers:
        caller.join(WAIT_SECONDS)
    assert frames.built == [clip.id]
    assert len(results) == 2
    assert results[0] == results[1] is not None


def test_built_strips_reads_the_cache_once_and_ignores_other_entries(
    db: Any, tmp_path: Path
) -> None:
    frames = RecordingFrames(tmp_path / "cache", db)
    for name in (
        "clip3_strip1s.jpg",
        "clip12_strip2s.jpg",
        "clip4_strip1s.0af1.part.jpg",
        "clip5_t3s.jpg",
        "clip6.mp4",
    ):
        (frames.cache_dir / name).write_bytes(b"x")
    assert frames.built_strips() == {3, 12}
