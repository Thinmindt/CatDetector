"""WebStreamer keeps the newest frame, serves it as MJPEG, and reports status."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import LORES_SIZE, MAIN_SIZE, StubRecorder, make_frame


@pytest.fixture
def make_streamer(camera_manager: Any) -> Any:
    from src.web_streamer import WebStreamer

    def build(recorder: Any = None) -> Any:
        return WebStreamer(camera_manager=camera_manager, motion_recorder=recorder)

    return build


def test_status_without_a_recorder_says_stream_only(make_streamer: Any) -> None:
    assert make_streamer(None).status() == {"detector": True, "recorder": False}


def test_status_reports_the_recorder_state(make_streamer: Any, tmp_path: Path) -> None:
    clip = tmp_path / "cat_video_20260912_080000.h264"
    streamer = make_streamer(StubRecorder(recording=True, filename=clip))

    status = streamer.status()

    assert status["recording"] is True
    assert status["clip"] == "cat_video_20260912_080000.h264"
    assert status["motion_threshold"] == 300


def test_generate_frames_yields_jpeg_parts(make_streamer: Any) -> None:
    streamer = make_streamer(None)
    streamer.latest_frame = make_frame(MAIN_SIZE, 128)

    chunks = list(itertools.islice(streamer.generate_frames(), 2))

    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
        assert b"\xff\xd8\xff" in chunk  # JPEG start-of-image


def test_consuming_a_frame_stores_a_copy(make_streamer: Any) -> None:
    streamer = make_streamer(None)
    main = make_frame(MAIN_SIZE, 7)

    streamer._consume_frames(main, make_frame(LORES_SIZE))

    assert streamer.latest_frame is not None
    assert streamer.latest_frame.shape == (*MAIN_SIZE, 3)
    # A copy, so later mutation of the source cannot corrupt the served frame.
    assert streamer.latest_frame is not main


def test_annotates_with_a_path_filename(make_streamer: Any, tmp_path: Path) -> None:
    """Regression: current_filename is a Path, so the overlay uses .name.

    A str would have no .name, and this consumer runs on the capture thread.
    Asserts on pixels: without that, deleting the whole overlay block passes.
    """
    clip = tmp_path / "cat_video_20260824_120000.h264"
    streamer = make_streamer(StubRecorder(recording=True, filename=clip))
    plain = make_frame(MAIN_SIZE, 0)

    streamer._consume_frames(plain.copy(), make_frame(LORES_SIZE))

    assert streamer.latest_frame is not None
    # The overlay must have actually drawn something.
    assert not np.array_equal(streamer.latest_frame, plain)
    # ... in the top-left band, where RECORDING and the filename go.
    assert streamer.latest_frame[:80].any()


def test_generate_frames_does_not_hold_the_lock_while_suspended(
    make_streamer: Any,
) -> None:
    """Regression: the generator yielded from inside `with self.frame_lock`.

    A suspended generator keeps its context manager, so one slow MJPEG viewer
    held the lock forever and blocked the capture thread -- which also runs
    motion detection.
    """
    streamer = make_streamer(None)
    streamer.latest_frame = make_frame(MAIN_SIZE, 128)

    frames = streamer.generate_frames()
    next(frames)  # park the generator mid-stream, exactly as a viewer does

    acquired = streamer.frame_lock.acquire(blocking=False)
    if acquired:
        streamer.frame_lock.release()
    frames.close()

    assert acquired


def test_registers_itself_as_a_frame_consumer(
    make_streamer: Any, camera_manager: Any
) -> None:
    streamer = make_streamer(None)
    assert streamer._consume_frames in camera_manager._consumers
