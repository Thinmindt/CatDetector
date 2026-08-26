"""Capture database, frame extraction, and the review API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from src.capture_db import CaptureDB
from src.clip_frames import THUMB_COUNT, ClipFrames
from src.review import create_review_blueprint


def make_clip_file(path: Path, content: bytes = b"fake h264") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = CaptureDB(tmp_path / "captures.db")
    yield database
    database.close()


@pytest.fixture
def clip_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "captures"
    make_clip_file(directory / "cat_video_20260825_072452.h264")
    make_clip_file(directory / "cat_video_20260826_090000.h264")
    return directory


# --- CaptureDB --------------------------------------------------------------


def test_ingest_registers_new_clips_once(db: Any, clip_dir: Path) -> None:
    assert db.ingest(clip_dir) == 2
    assert db.ingest(clip_dir) == 0
    assert db.counts() == {"total": 2, "labeled": 0}


def test_ingest_parses_started_at_from_the_filename(db: Any, clip_dir: Path) -> None:
    db.ingest(clip_dir)
    event = db.next_unlabeled()
    assert event.started_at == "2026-08-25T07:24:52"


def test_ingest_accepts_unparseable_names(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_file(directory / "oddly-named.h264")
    assert db.ingest(directory) == 1
    assert db.next_unlabeled().started_at is None


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
    assert db.counts() == {"total": 2, "labeled": 1, "not_cat": 1}


# --- ClipFrames -------------------------------------------------------------


def write_test_video(path: Path, frames: int = 70) -> Path:
    """A tiny mp4; ffmpeg reads it the same way it reads the raw clips."""
    fourcc = cv2.VideoWriter.fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, 30, (160, 120))
    for n in range(frames):
        img = np.full((120, 160, 3), n % 256, dtype=np.uint8)
        writer.write(img)
    writer.release()
    return path


def test_strip_is_built_and_cached(tmp_path: Path) -> None:
    clip = write_test_video(tmp_path / "clip.mp4")
    frames = ClipFrames(tmp_path / "cache")

    strip = frames.strip(1, clip)

    assert strip is not None and strip.exists()
    image = cv2.imread(str(strip))
    assert image is not None
    assert image.shape[1] > image.shape[0]  # wider than tall: a montage

    first_mtime = strip.stat().st_mtime_ns
    again = frames.strip(1, clip)
    assert again is not None
    assert again.stat().st_mtime_ns == first_mtime  # cache hit


def test_full_frame_extraction(tmp_path: Path) -> None:
    clip = write_test_video(tmp_path / "clip.mp4")
    frames = ClipFrames(tmp_path / "cache")

    frame = frames.frame(1, clip, slot=1)

    assert frame is not None and cv2.imread(str(frame)) is not None
    assert frames.frame(1, clip, slot=THUMB_COUNT) is None  # out of range


def test_unreadable_clip_yields_none(tmp_path: Path) -> None:
    bad = make_clip_file(tmp_path / "garbage.h264", b"not video at all")
    frames = ClipFrames(tmp_path / "cache")
    assert frames.strip(1, bad) is None


# --- review API -------------------------------------------------------------


@pytest.fixture
def client(db: Any, clip_dir: Path, tmp_path: Path) -> Any:
    from flask import Flask

    db.ingest(clip_dir)
    app = Flask(__name__)
    app.register_blueprint(create_review_blueprint(db, ClipFrames(tmp_path / "cache")))
    return app.test_client()


def test_page_serves(client: Any) -> None:
    response = client.get("/review")
    assert response.status_code == 200
    assert b"Capture review" in response.data


def test_next_returns_the_oldest_event(client: Any) -> None:
    data = client.get("/api/review/next").get_json()
    assert data["event"]["started_at"] == "2026-08-25T07:24:52"
    assert data["counts"]["total"] == 2


def test_labeling_advances_and_counts(client: Any) -> None:
    first = client.get("/api/review/next").get_json()["event"]

    response = client.post(f"/api/review/{first['id']}/label", json={"value": "cat"})
    assert response.status_code == 200

    data = client.get("/api/review/next").get_json()
    assert data["event"]["id"] != first["id"]
    assert data["counts"] == {"total": 2, "labeled": 1, "cat": 1}


def test_all_labeled_returns_null_event(client: Any) -> None:
    for _ in range(2):
        event = client.get("/api/review/next").get_json()["event"]
        client.post(f"/api/review/{event['id']}/label", json={"value": "not_cat"})

    assert client.get("/api/review/next").get_json()["event"] is None


def test_invalid_label_is_rejected(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    response = client.post(f"/api/review/{event['id']}/label", json={"value": "dog"})
    assert response.status_code == 400
    response = client.post(f"/api/review/{event['id']}/label", json={})
    assert response.status_code == 400


def test_unknown_event_404s(client: Any) -> None:
    assert (
        client.post("/api/review/999/label", json={"value": "cat"}).status_code == 404
    )
    assert client.get("/api/review/event/999").status_code == 404
    assert client.get("/review/999/strip.jpg").status_code == 404


def test_undo_fetch_shows_the_existing_label(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    client.post(f"/api/review/{event['id']}/label", json={"value": "unsure"})

    data = client.get(f"/api/review/event/{event['id']}").get_json()
    assert data["event"]["label"] == "unsure"


def test_strip_route_for_an_unreadable_clip_404s(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    assert client.get(f"/review/{event['id']}/strip.jpg").status_code == 404
