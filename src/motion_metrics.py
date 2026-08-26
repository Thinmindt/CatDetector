"""Per-frame detection metrics, written to CSV off the capture thread."""

import csv
import datetime
import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import cv2
import numpy as np
from numpy.typing import NDArray

log = logging.getLogger(__name__)

FIELDS = [
    "timestamp",
    "foreground_px",
    "blob_area",
    "x",
    "y",
    "w",
    "h",
    "cx",
    "cy",
    "recording",
]

QUEUE_LIMIT = 2000
FLUSH_EVERY_ROWS = 150
WRITER_JOIN_SECONDS = 10


@dataclass(frozen=True)
class Blob:
    """The largest foreground region in a frame."""

    area: int
    x: int
    y: int
    w: int
    h: int

    @property
    def centroid(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


class MetricsLog:
    """Append-only CSV of per-frame detection metrics.

    record() does not block: rows go onto a bounded queue and a daemon thread
    writes them. Rows are dropped once the queue is full, and the count is
    reported on close.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dropped = 0
        self.written = 0
        self._queue: queue.Queue[list[object] | None] = queue.Queue(maxsize=QUEUE_LIMIT)
        self._thread = threading.Thread(target=self._write_rows, daemon=True)
        self._thread.start()
        log.info("Recording detection metrics to %s", self.path)

    def record(self, foreground_px: int, blob: Blob | None, recording: bool) -> None:
        stamp = datetime.datetime.now().isoformat(timespec="milliseconds")
        if blob is None:
            row: list[object] = [stamp, foreground_px, 0, "", "", "", "", "", ""]
        else:
            cx, cy = blob.centroid
            row = [
                stamp,
                foreground_px,
                blob.area,
                blob.x,
                blob.y,
                blob.w,
                blob.h,
                cx,
                cy,
            ]
        row.append(int(recording))
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=WRITER_JOIN_SECONDS)
        if self.dropped:
            log.warning("Dropped %d metrics rows; the writer fell behind", self.dropped)
        log.info("Wrote %d metrics rows to %s", self.written, self.path)

    def _write_rows(self) -> None:
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        try:
            with self.path.open("a", newline="") as handle:
                writer = csv.writer(handle)
                if needs_header:
                    writer.writerow(FIELDS)
                    handle.flush()
                self._drain(writer, handle)
        except OSError:
            log.exception("Metrics writer stopped")

    def _drain(self, writer: Any, handle: TextIO) -> None:
        """Write queued rows until the sentinel arrives."""
        since_flush = 0
        while True:
            row = self._queue.get()
            if row is None:
                handle.flush()
                return
            writer.writerow(row)
            self.written += 1
            since_flush += 1
            if since_flush >= FLUSH_EVERY_ROWS:
                handle.flush()
                since_flush = 0


def largest_blob(mask: NDArray[np.uint8]) -> Blob | None:
    """Largest connected foreground region, or None if the mask is empty."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    biggest = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(biggest)
    return Blob(
        area=int(cv2.contourArea(biggest)), x=int(x), y=int(y), w=int(w), h=int(h)
    )
