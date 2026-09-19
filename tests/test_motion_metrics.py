"""Shadow exclusion, mask cleanup and the per-frame metrics log."""

from __future__ import annotations

import csv
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from conftest import LORES_SIZE, make_frame

from src.motion_metrics import (
    Blob,
    FrameMetrics,
    MetricsLog,
    clean_mask,
    largest_blob,
    measure,
)

SHADOW_VALUE = 127

NO_BLOB = FrameMetrics(foreground_px=1, blob=None, clean_blob=None, brightness=0.0)


# Enough pixels for MOG2 to train on; a quarter the size of a lores frame.
SMALL_SIZE = (120, 160)


def frame_with(background: int, patches: list[tuple[slice, slice, int]]) -> Any:
    img = np.full((*SMALL_SIZE, 3), background, dtype=np.uint8)
    for rows, cols, value in patches:
        img[rows, cols] = value
    return img


def two_fragments() -> Any:
    """One object split by a 4 px gap, as MOG2 splits a cat that matches the floor."""
    mask = np.zeros(LORES_SIZE, dtype=np.uint8)
    mask[100:140, 100:160] = 255
    mask[144:200, 100:160] = 255
    return mask


# --- shadow handling --------------------------------------------------------


def test_mog2_marks_shadows_with_a_distinct_value() -> None:
    """Documents the behaviour the fix depends on, against real MOG2."""
    subtractor = cv2.createBackgroundSubtractorMOG2()
    background = np.full(SMALL_SIZE, 200, dtype=np.uint8)
    for _ in range(60):
        subtractor.apply(background)

    frame = background.copy()
    frame[20:60, 20:60] = 100  # shadow: same texture, darker
    frame[80:110, 80:140] = 20  # object: very different
    mask = subtractor.apply(frame)

    assert SHADOW_VALUE in np.unique(mask)
    assert 255 in np.unique(mask)


def test_disabling_shadow_detection_does_not_exclude_shadows() -> None:
    """The tempting fix is a no-op: it relabels shadows 255 rather than dropping
    them, so countNonZero is unchanged."""
    counts = []
    for detect_shadows in (True, False):
        subtractor = cv2.createBackgroundSubtractorMOG2(detectShadows=detect_shadows)
        background = np.full(SMALL_SIZE, 200, dtype=np.uint8)
        for _ in range(60):
            subtractor.apply(background)
        frame = background.copy()
        frame[20:60, 20:60] = 100
        frame[80:110, 80:140] = 20
        counts.append(cv2.countNonZero(subtractor.apply(frame)))

    assert counts[0] == counts[1]


def test_shadow_pixels_do_not_count_as_motion(
    make_recorder: Callable[..., Any],
) -> None:
    """Regression: countNonZero counted MOG2's 127s, so a moving shadow read as
    a cat."""
    recorder = make_recorder(stub_mog2=False, warmup_frames=0)
    background = frame_with(200, [])
    for _ in range(60):
        recorder.detect_motion(background)

    shadow_only = frame_with(200, [(slice(20, 60), slice(20, 100), 100)])
    recorder.detect_motion(shadow_only)
    shadow_pixels = recorder.last_motion_pixels

    real_object = frame_with(200, [(slice(20, 60), slice(20, 100), 20)])
    recorder.detect_motion(real_object)
    object_pixels = recorder.last_motion_pixels

    assert shadow_pixels == 0
    assert object_pixels > 1000


# --- largest_blob -----------------------------------------------------------


def test_largest_blob_finds_the_biggest_region() -> None:
    mask = np.zeros(LORES_SIZE, dtype=np.uint8)
    mask[10:20, 10:20] = 255  # small
    mask[100:200, 100:250] = 255  # large

    blob = largest_blob(mask)

    assert blob is not None
    assert blob.area > 10_000
    assert (blob.x, blob.y) == (100, 100)
    assert blob.centroid == (175, 150)


def test_largest_blob_on_an_empty_mask_is_none() -> None:
    assert largest_blob(np.zeros(LORES_SIZE, dtype=np.uint8)) is None


# --- mask cleanup -----------------------------------------------------------


def test_cleanup_removes_speckle() -> None:
    mask = np.zeros(LORES_SIZE, dtype=np.uint8)
    mask[::20, ::20] = 255

    assert largest_blob(mask) is not None
    assert largest_blob(clean_mask(mask)) is None


def test_cleanup_joins_nearby_fragments() -> None:
    raw = largest_blob(two_fragments())
    cleaned = largest_blob(clean_mask(two_fragments()))

    assert raw is not None
    assert cleaned is not None
    assert raw.h < 60
    assert cleaned.y <= 100
    assert cleaned.y + cleaned.h >= 200


def test_measure_reports_both_blobs_and_the_brightness() -> None:
    gray = np.full(LORES_SIZE, 100, dtype=np.uint8)
    mask = two_fragments()

    result = measure(gray, mask)

    assert result.foreground_px == np.count_nonzero(mask)
    assert result.brightness == 100.0
    assert result.blob is not None
    assert result.clean_blob is not None
    assert result.clean_blob.area > result.blob.area


# --- MetricsLog -------------------------------------------------------------


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_records_rows_with_a_header(tmp_path: Path) -> None:
    log = MetricsLog(tmp_path / "metrics.csv")
    log.record(
        FrameMetrics(
            foreground_px=1234,
            blob=Blob(area=500, x=1, y=2, w=10, h=20),
            clean_blob=Blob(area=900, x=0, y=0, w=30, h=30),
            brightness=87.46,
        ),
        recording=True,
    )
    log.record(NO_BLOB, recording=False)
    log.close()

    rows = read_rows(tmp_path / "metrics.csv")
    assert len(rows) == 2
    assert rows[0]["foreground_px"] == "1234"
    assert rows[0]["blob_area"] == "500"
    assert rows[0]["cx"] == "6"
    assert rows[0]["recording"] == "1"
    assert rows[0]["clean_area"] == "900"
    assert rows[0]["clean_w"] == "30"
    assert rows[0]["brightness"] == "87.5"
    assert rows[1]["blob_area"] == "0"
    assert rows[1]["x"] == ""
    assert rows[1]["clean_area"] == "0"
    assert rows[1]["clean_x"] == ""


def test_appends_without_repeating_the_header(tmp_path: Path) -> None:
    path = tmp_path / "metrics.csv"
    first = MetricsLog(path)
    first.record(NO_BLOB, recording=False)
    first.close()

    second = MetricsLog(path)
    second.record(NO_BLOB, recording=False)
    second.close()

    assert len(read_rows(path)) == 2
    assert path.read_text().count("foreground_px") == 1


def test_a_file_with_another_column_layout_is_left_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Appending rows under an older header would misalign every column."""
    path = tmp_path / "metrics.csv"
    old = "timestamp,foreground_px,recording\n2026-09-10T12:00:00.000,5,0\n"
    path.write_text(old)

    with caplog.at_level(logging.WARNING):
        log = MetricsLog(path)
    log.record(NO_BLOB, recording=False)
    log.close()

    assert path.read_text() == old
    assert log.path != path
    assert read_rows(log.path)[0]["brightness"] == "0.0"
    assert "column layout" in caplog.text


def test_record_does_not_block_when_the_writer_falls_behind(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The capture thread must never wait on the metrics log."""
    log = MetricsLog(tmp_path / "metrics.csv")
    log._queue.maxsize = 1
    for _ in range(5000):
        log.record(NO_BLOB, recording=False)

    assert log.dropped > 0
    with caplog.at_level(logging.WARNING):
        log.close()
    assert "Dropped" in caplog.text


def test_creates_the_parent_directory(tmp_path: Path) -> None:
    log = MetricsLog(tmp_path / "nested" / "deeper" / "metrics.csv")
    log.close()
    assert (tmp_path / "nested" / "deeper").is_dir()


def test_recorder_feeds_the_metrics_log(
    make_recorder: Callable[..., Any], tmp_path: Path
) -> None:
    log = MetricsLog(tmp_path / "metrics.csv")
    rec = make_recorder(stub_mog2=False, metrics=log, warmup_frames=2)
    for _ in range(5):
        rec.detect_motion(make_frame(LORES_SIZE, 100))
    rec.cleanup()

    rows = read_rows(tmp_path / "metrics.csv")
    assert len(rows) == 5
    assert {row["brightness"] for row in rows} == {"100.0"}


def test_close_returns_when_the_writer_thread_has_died(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead writer plus a full queue must not wedge shutdown.

    close() runs on the main thread during cleanup(), so a blocking enqueue
    there hangs the process.
    """
    monkeypatch.setattr("src.motion_metrics.SENTINEL_TIMEOUT_SECONDS", 0.1)
    directory = tmp_path / "readonly"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        log = MetricsLog(directory / "metrics.csv")
        log._thread.join(timeout=5)
        assert not log._thread.is_alive()

        log._queue.maxsize = 2
        for _ in range(10):
            log.record(NO_BLOB, recording=False)
        assert log._queue.full()

        finished = threading.Event()

        def close_it() -> None:
            log.close()
            finished.set()

        threading.Thread(target=close_it, daemon=True).start()
        assert finished.wait(timeout=20)
    finally:
        directory.chmod(0o700)
