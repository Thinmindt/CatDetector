"""One app, one port: the live feed and the review UI as tabs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
import pytest
from conftest import StubRecorder

from src.capture_db import CaptureDB
from src.clip_frames import ClipFrames
from src.web_app import create_app


@pytest.fixture
def review_parts(tmp_path: Path) -> Any:
    db = CaptureDB(tmp_path / "captures.db")
    yield db, ClipFrames(tmp_path / "cache")
    db.close()


@pytest.fixture
def streamer(camera_manager: Any) -> Any:
    from src.web_streamer import WebStreamer

    return WebStreamer(
        camera_manager=camera_manager, motion_recorder=cast(Any, StubRecorder())
    )


def test_both_routes_serve_the_tabbed_page(review_parts: Any) -> None:
    client = create_app(None, review_parts).test_client()

    for path in ("/", "/review"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert b'data-tab="live"' in response.data
        assert b'data-tab="review"' in response.data
    assert b"THUMB_WIDTH = 320" in response.data
    assert b'LABELS = ["cat", "not_cat", "unsure", "clean"]' in response.data


def test_without_a_streamer_the_live_tab_learns_there_is_no_detector(
    review_parts: Any,
) -> None:
    client = create_app(None, review_parts).test_client()

    assert client.get("/api/status").get_json() == {"detector": False}
    assert client.get("/video_feed").status_code == 404


def test_with_a_streamer_the_live_routes_are_served(
    streamer: Any, review_parts: Any
) -> None:
    app = create_app(streamer, review_parts)
    client = app.test_client()

    status = client.get("/api/status").get_json()
    assert status["detector"] is True
    assert status["recorder"] is True
    assert status["motion_threshold"] == 300

    # Built by calling the view, not through the client: the client buffers
    # the response, and generate_frames() never ends, so client.get() hangs.
    assert "live.video_feed" in app.view_functions
    with app.test_request_context("/video_feed"):
        response = streamer.video_feed()
    assert "multipart/x-mixed-replace" in response.content_type


def test_the_review_api_is_served_by_the_same_app(review_parts: Any) -> None:
    client = create_app(None, review_parts).test_client()

    data = client.get("/api/review/next").get_json()

    assert data["event"] is None
    assert data["counts"]["total"] == 0


def test_without_a_database_the_page_says_review_is_unavailable(
    streamer: Any,
) -> None:
    client = create_app(streamer, None).test_client()

    response = client.get("/review")

    assert response.status_code == 200
    assert b"Review is unavailable" in response.data
    assert client.get("/api/review/next").status_code == 404


def test_media_is_served_from_a_relative_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative cache path is the process's, not the Flask package's."""
    monkeypatch.chdir(tmp_path)
    clip = tmp_path / "captures" / "cat_video_20260917_120000.h264"
    clip.parent.mkdir()
    clip.write_bytes(b"h264")
    frames = ClipFrames(".review_cache")
    strip = np.zeros((8, 8, 3), np.uint8)
    cv2.imwrite(str(frames.cache_dir / "clip1_strip1s.jpg"), strip)
    db = CaptureDB(tmp_path / "captures.db")
    try:
        db.ingest(tmp_path / "captures")
        client = create_app(None, (db, frames)).test_client()

        response = client.get("/review/1/clip/0/strip.jpg")

        assert response.status_code == 200
        assert response.data[:2] == b"\xff\xd8"  # a JPEG
    finally:
        db.close()
