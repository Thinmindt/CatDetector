"""Builds the strips of the events next in the review queue before the page asks."""

import logging
import os
import sys
import threading

from src.review.capture_db import CaptureDB
from src.review.clip_frames import ClipFrames

log = logging.getLogger(__name__)

EVENTS_AHEAD = 30
NICENESS = 10


class StripsAhead:
    """One background thread that builds missing strips, one clip at a time.

    Call wanted() after anything that changes the queue; a walk in progress
    starts over from the front of the queue. The database lock is held only
    while the queue is read, never while ffmpeg reads the share.
    """

    def __init__(
        self, db: CaptureDB, frames: ClipFrames, events: int = EVENTS_AHEAD
    ) -> None:
        self.db = db
        self.frames = frames
        self.events = events
        self._wanted = threading.Event()
        self._stopping = threading.Event()
        self._failed: set[int] = set()
        self._thread = threading.Thread(
            target=self._run, name="strips-ahead", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def wanted(self) -> None:
        """The queue changed: walk it again."""
        self._wanted.set()

    def stop(self, timeout: float) -> bool:
        """Stop after the build in progress. Returns whether the thread ended."""
        self._stopping.set()
        self._wanted.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        _lower_this_threads_priority()
        while True:
            self._wanted.wait()
            if self._stopping.is_set():
                return
            self._wanted.clear()
            try:
                self.build_ahead()
            except Exception:
                log.exception("Building strips ahead failed")

    def build_ahead(self) -> int:
        """Build the missing strips of the next events in the queue. Returns how
        many were built; stops early when the queue changes again."""
        built = 0
        for clip in self.db.clips_ahead(self.events):
            if self._wanted.is_set():
                break
            if clip.id in self._failed or self.frames.cached_strip(clip.id):
                continue
            if self.frames.strip(clip.id, clip.path) is None:
                self._failed.add(clip.id)
            else:
                built += 1
        return built


def _lower_this_threads_priority() -> None:
    """Renice this thread, and so the ffmpeg it starts, below the camera's."""
    if sys.platform != "linux":
        return
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICENESS)
    except OSError as error:
        log.warning("Could not lower the strip builder's priority: %s", error)
