"""Per-frame detection metrics, written to CSV off the capture thread."""

import csv
import datetime
import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

import cv2

from src.clips.blob import Blob
from src.clips.frame import Frame

log = logging.getLogger(__name__)

RAW_BLOB_COLUMNS = ("blob_area", "x", "y", "w", "h")
CLEAN_BLOB_COLUMNS = ("clean_area", "clean_x", "clean_y", "clean_w", "clean_h")
FIELDS = [
    "timestamp",
    "foreground_px",
    *RAW_BLOB_COLUMNS,
    "cx",
    "cy",
    "recording",
    *CLEAN_BLOB_COLUMNS,
    "brightness",
]

QUEUE_LIMIT = 2000
FLUSH_EVERY_ROWS = 150
WRITER_JOIN_SECONDS = 10
SENTINEL_TIMEOUT_SECONDS = 2

SPECKLE_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
MERGE_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))


@dataclass(frozen=True)
class FrameMetrics:
    """What one analysis frame measured."""

    foreground_px: int
    blob: Blob | None
    clean_blob: Blob | None
    brightness: float


class MetricsLog:
    """Append-only CSV of per-frame detection metrics.

    record() does not block: rows go onto a bounded queue and a daemon thread
    writes them. Rows are dropped once the queue is full, and the count is
    reported on close. A file with a different column layout is left alone and
    a timestamped sibling is written instead.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = _path_for_current_layout(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dropped = 0
        self.written = 0
        self._queue: queue.Queue[dict[str, object] | None] = queue.Queue(
            maxsize=QUEUE_LIMIT
        )
        self._thread = threading.Thread(target=self._write_rows, daemon=True)
        self._thread.start()
        log.info("Recording detection metrics to %s", self.path)

    def record(self, frame: FrameMetrics, recording: bool) -> None:
        row: dict[str, object] = {
            "timestamp": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "foreground_px": frame.foreground_px,
            **_blob_columns(RAW_BLOB_COLUMNS, frame.blob),
            **_centroid_columns(frame.blob),
            "recording": int(recording),
            **_blob_columns(CLEAN_BLOB_COLUMNS, frame.clean_blob),
            "brightness": f"{frame.brightness:.1f}",
        }
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        """Stop the writer and report the totals. Never blocks indefinitely."""
        try:
            self._queue.put(None, timeout=SENTINEL_TIMEOUT_SECONDS)
        except queue.Full:
            log.warning("Metrics writer is not draining; closing without it")
        self._thread.join(timeout=WRITER_JOIN_SECONDS)
        if self.dropped:
            log.warning("Dropped %d metrics rows; the writer fell behind", self.dropped)
        log.info("Wrote %d metrics rows to %s", self.written, self.path)

    def _write_rows(self) -> None:
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        try:
            with self.path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                if needs_header:
                    writer.writeheader()
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


def _path_for_current_layout(path: Path) -> Path:
    """path, or a timestamped sibling if path already holds another layout."""
    try:
        with path.open(newline="") as handle:
            header = next(csv.reader(handle), None)
    except OSError:
        return path
    if header is None or header == FIELDS:
        return path

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    sibling = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    log.warning("%s has a different column layout; writing to %s", path, sibling)
    return sibling


def _blob_columns(names: tuple[str, ...], blob: Blob | None) -> dict[str, object]:
    values = (0, "", "", "", "") if blob is None else (blob.area, *blob.bounds)
    return dict(zip(names, values, strict=True))


def _centroid_columns(blob: Blob | None) -> dict[str, object]:
    cx, cy = ("", "") if blob is None else blob.centroid
    return {"cx": cx, "cy": cy}


def largest_blob(mask: Frame) -> Blob | None:
    """Largest connected foreground region, or None if the mask is empty."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    biggest = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(biggest)
    return Blob(
        area=int(cv2.contourArea(biggest)), x=int(x), y=int(y), w=int(w), h=int(h)
    )


def clean_mask(mask: Frame) -> Frame:
    """The mask with speckle removed and nearby fragments joined."""
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, SPECKLE_KERNEL)
    return cast(Frame, cv2.morphologyEx(opened, cv2.MORPH_CLOSE, MERGE_KERNEL))


def measure(gray: Frame, mask: Frame) -> FrameMetrics:
    """The metrics for one analysis frame, from its grey image and foreground mask."""
    return FrameMetrics(
        foreground_px=int(cv2.countNonZero(mask)),
        blob=largest_blob(mask),
        clean_blob=largest_blob(clean_mask(mask)),
        brightness=float(cv2.mean(gray)[0]),
    )
