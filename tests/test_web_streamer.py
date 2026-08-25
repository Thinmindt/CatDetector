"""WebStreamer serves the MJPEG feed and degrades without a recorder."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import LORES_SIZE, MAIN_SIZE, make_frame


class StubRecorder:
    """Only the attributes WebStreamer actually reads."""

    def __init__(self, recording: bool = False, filename: Path | None = None) -> None:
        self.recording = recording
        self.current_filename = filename
        self.motion_threshold = 5000
        self.motion_timeout = 10


@pytest.fixture
def make_streamer(camera_manager: Any) -> Any:
    from src.web_streamer import WebStreamer

    def build(recorder: Any = None) -> Any:
        return WebStreamer(
            camera_manager=camera_manager, motion_recorder=recorder, port=5000
        )

    return build


def test_status_page_renders_without_a_recorder(make_streamer: Any) -> None:
    streamer = make_streamer(None)
    client = streamer.app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"Cat Detector Live Feed" in response.data
    assert b"Recording:" not in response.data


def test_status_page_shows_recorder_state(make_streamer: Any) -> None:
    streamer = make_streamer(StubRecorder(recording=True))
    client = streamer.app.test_client()

    response = client.get("/")

    assert b"Recording:" in response.data
    assert b"Yes" in response.data
    assert b"5000" in response.data


def test_video_feed_is_multipart(make_streamer: Any) -> None:
    """Built via the view function, not the test client.

    The client buffers the response, and generate_frames() never ends, so a
    plain client.get("/video_feed") hangs forever.
    """
    streamer = make_streamer(None)

    with streamer.app.test_request_context("/video_feed"):
        response = streamer.app.view_functions["video_feed"]()

    assert response.status_code == 200
    assert "multipart/x-mixed-replace" in response.content_type


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
