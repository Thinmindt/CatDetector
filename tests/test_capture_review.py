"""Capture database, frame extraction, and the review API."""

from __future__ import annotations

import datetime
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from src.capture_db import CaptureDB
from src.clip_frames import STAGING_SUFFIX, THUMB_COUNT, ClipFrames
from src.clip_sidecar import ClipFacts, CloseReason, read_sidecar, write_sidecar
from src.motion_metrics import Blob
from src.review import create_review_blueprint

GAP = 60
DISTANCE = 100
BOX_1 = (100, 240)
BOX_3 = (540, 240)


def make_clip_file(path: Path, content: bytes = b"fake h264") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_clip_with_facts(
    directory: Path,
    started: datetime.datetime,
    seconds: int,
    place: tuple[int, int] = BOX_1,
    reason: CloseReason = CloseReason.TIMEOUT,
) -> Path:
    """A clip file named for its start, plus the sidecar the recorder writes."""
    path = make_clip_file(directory / f"cat_video_{started:%Y%m%d_%H%M%S}.h264")
    blob = Blob(area=400, x=place[0] - 10, y=place[1] - 10, w=20, h=20)
    write_sidecar(
        path,
        ClipFacts(
            started_at=started,
            ended_at=started + datetime.timedelta(seconds=seconds),
            close_reason=reason,
            trigger_blob=blob,
            last_blob=blob,
        ),
    )
    return path


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = CaptureDB(
        tmp_path / "captures.db", gap_seconds=GAP, box_distance_px=DISTANCE
    )
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
    assert db.counts() == {"total": 2, "labeled": 0, "multi_clip": 0}


def test_ingest_parses_started_at_from_the_filename(db: Any, clip_dir: Path) -> None:
    db.ingest(clip_dir)
    event = db.next_unlabeled()
    assert event.started_at == "2026-08-25T07:24:52"


def test_ingest_accepts_unparseable_names(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_file(directory / "oddly-named.h264")
    assert db.ingest(directory) == 1
    assert db.next_unlabeled().started_at is None


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
    assert db.counts() == {"total": 2, "labeled": 1, "multi_clip": 0, "not_cat": 1}


# --- sidecars and grouping --------------------------------------------------

T0 = datetime.datetime(2026, 9, 12, 8, 0, 0)


def at(seconds: int) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


def test_ingest_takes_the_facts_from_the_sidecar(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=25, reason=CloseReason.MAX_LENGTH)

    db.ingest(directory)
    event = db.next_unlabeled()

    (clip,) = event.clips
    assert clip.started_at == "2026-09-12T08:00:00.000"
    assert clip.ended_at == "2026-09-12T08:00:25.000"
    assert clip.close_reason == "max_length"


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

    monkeypatch.setattr("src.capture_db.read_sidecar", spy)
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
    (clip,) = db.next_unlabeled().clips

    assert clip.close_reason == "recovered"
    assert clip.ended_at == "2026-09-12T08:00:10.000"


def test_clips_of_one_visit_become_one_event(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(40), seconds=15)
    make_clip_with_facts(directory, at(500), seconds=10)

    db.ingest(directory)

    assert db.counts() == {"total": 2, "labeled": 0, "multi_clip": 1}
    first = db.next_unlabeled()
    assert [c.started_at[11:19] for c in first.clips] == ["08:00:00", "08:00:40"]
    assert first.started_at == "2026-09-12T08:00:00.000"
    assert first.ended_at == "2026-09-12T08:00:55.000"


def test_a_later_ingest_keeps_event_ids_stable(db: Any, tmp_path: Path) -> None:
    """Deep links and the undo history hold event ids; a rescan must not move them."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    db.ingest(directory)
    before = db.next_unlabeled()
    db.set_label(before.id, "cat")

    make_clip_with_facts(directory, at(40), seconds=15)
    db.ingest(directory)

    after = db.get(before.id)
    assert len(after.clips) == 2
    assert after.label == "cat"
    assert db.next_unlabeled() is None


def test_regroup_with_a_wider_gap_merges_and_carries_an_agreeing_label(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(200), seconds=15)
    db.ingest(directory)
    for _ in range(2):
        db.set_label(db.next_unlabeled().id, "cat")
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
    db.set_label(db.next_unlabeled().id, "cat")
    db.set_label(db.next_unlabeled().id, "not_cat")

    db.gap_seconds = 600
    db.regroup()

    assert db.counts() == {"total": 1, "labeled": 0, "multi_clip": 1}
    assert db.next_unlabeled() is not None


def test_a_bridge_between_disagreeing_labels_drops_the_label(
    db: Any, tmp_path: Path
) -> None:
    """The merged event cannot be both, so it goes back in the queue."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(100), seconds=10)
    db.ingest(directory)
    db.set_label(db.next_unlabeled().id, "cat")
    db.set_label(db.next_unlabeled().id, "not_cat")

    make_clip_with_facts(directory, at(50), seconds=10)
    db.ingest(directory)

    assert db.counts() == {"total": 1, "labeled": 0, "multi_clip": 1}
    assert db.next_unlabeled() is not None


def test_a_late_clip_that_bridges_two_events_merges_them(
    db: Any, tmp_path: Path
) -> None:
    """A clip held back by a NAS outage can arrive after its neighbours."""
    directory = tmp_path / "captures"
    make_clip_with_facts(directory, at(0), seconds=20)
    make_clip_with_facts(directory, at(100), seconds=10)
    db.ingest(directory)
    first_id = db.next_unlabeled().id
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
    import review

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

    assert review.regroup_events()["total"] == 1


def visit(directory: Path, start: int, *offsets: int) -> None:
    """Clips at start and each offset, close enough to group as one visit."""
    for offset in (0, *offsets):
        make_clip_with_facts(directory, at(start + offset), seconds=15)


def test_split_starts_a_new_unlabeled_event_and_the_first_half_keeps_its_id(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40, 80)
    db.ingest(directory)
    event = db.next_unlabeled()
    db.set_label(event.id, "cat")

    db.split(event.clips[1].id)

    first = db.get(event.id)
    assert [c.started_at[11:19] for c in first.clips] == ["08:00:00"]
    assert first.label == "cat"
    second = db.next_unlabeled()
    assert second.id != first.id
    assert [c.started_at[11:19] for c in second.clips] == ["08:00:40", "08:01:20"]
    assert db.counts() == {"total": 2, "labeled": 1, "multi_clip": 1, "cat": 1}


def test_a_split_survives_regroup(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    db.ingest(directory)
    db.split(db.next_unlabeled().clips[1].id)

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
    first = db.next_unlabeled()
    db.set_label(first.id, "cat")
    db.set_label(db.next_unlabeled().id, "cat")

    merged = db.join(first.id)

    assert merged.id == first.id
    assert len(merged.clips) == 2
    assert merged.label == "cat"
    db.gap_seconds = 1
    db.regroup()
    assert db.counts()["total"] == 1


def test_join_with_no_later_event_is_none(db: Any, tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0)
    db.ingest(directory)
    assert db.join(db.next_unlabeled().id) is None


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

    assert first.started_at[11:19] == "08:00:00"
    assert second.started_at[11:19] == "08:16:40"
    assert db.next_multi(second.id) is None


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
    assert third.started_at[11:19] == "08:16:40"


def test_poop_counts_survive_a_regroup(db: Any, tmp_path: Path) -> None:
    """They hang off a clip, not an event: regroup renumbers every event."""
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    db.ingest(directory)
    event = db.next_unlabeled()
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
    event = db.next_unlabeled()

    db.set_poop_counts(event.id, {1: 2, 2: 1})
    db.set_poop_counts(event.id, {1: 3})

    assert db.get(event.id).poops == {1: 3}


def test_a_database_from_before_grouping_is_refused(tmp_path: Path) -> None:
    old = tmp_path / "captures.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, clip_path TEXT)")

    with pytest.raises(RuntimeError, match="predates event grouping"):
        CaptureDB(old)


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


def test_video_is_a_playable_cached_copy(tmp_path: Path) -> None:
    clip = write_test_video(tmp_path / "clip.mp4")
    frames = ClipFrames(tmp_path / "cache")

    video = frames.video(1, clip)

    assert video is not None and video.suffix == ".mp4"
    capture = cv2.VideoCapture(str(video))
    ok, _ = capture.read()
    capture.release()
    assert ok
    first_mtime = video.stat().st_mtime_ns
    again = frames.video(1, clip)
    assert again is not None
    assert again.stat().st_mtime_ns == first_mtime  # cache hit


def test_a_failed_remux_leaves_nothing_for_the_cache_to_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg writes its output as it goes, so a failure can leave a partial
    file under the cache name; the next request must not be served that."""
    clip = write_test_video(tmp_path / "clip.mp4")
    frames = ClipFrames(tmp_path / "cache")

    def half_written_then_error(command: list[str], **kwargs: Any) -> Any:
        Path(command[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, b"", b"boom")

    monkeypatch.setattr(subprocess, "run", half_written_then_error)

    assert frames.video(1, clip) is None
    assert not (tmp_path / "cache" / "clip1.mp4").exists()


def test_a_cache_entry_appears_only_once_it_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flask serves these threaded, and ffmpeg creates its output up front: a
    second request for the same clip must never be handed the file being written."""
    clip = write_test_video(tmp_path / "clip.mp4")
    cache = tmp_path / "cache"
    frames = ClipFrames(cache)
    cached = cache / "clip1.mp4"
    visible_midway: list[bool] = []

    def write_then_finish(command: list[str], **kwargs: Any) -> Any:
        out = Path(command[-1])
        out.write_bytes(b"half")
        visible_midway.append(cached.exists())
        out.write_bytes(b"complete")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", write_then_finish)

    assert frames.video(1, clip) == cached
    assert visible_midway == [False]
    assert cached.read_bytes() == b"complete"
    assert list(cache.glob(f"*{STAGING_SUFFIX}*")) == []


# --- review API -------------------------------------------------------------


@pytest.fixture
def client(db: Any, clip_dir: Path, tmp_path: Path) -> Any:
    from flask import Flask

    db.ingest(clip_dir)
    app = Flask(__name__)
    app.register_blueprint(create_review_blueprint(db, ClipFrames(tmp_path / "cache")))
    return app.test_client()


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
    assert data["counts"] == {"total": 2, "labeled": 1, "multi_clip": 0, "cat": 1}


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


def test_a_cleaning_is_labeled_and_carries_its_poop_counts(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]

    labeled = client.post(f"/api/review/{event['id']}/label", json={"value": "clean"})
    assert labeled.status_code == 200
    assert labeled.get_json()["counts"]["clean"] == 1

    counted = client.post(
        f"/api/review/{event['id']}/counts", json={"counts": {"1": 2, "3": 0}}
    )
    assert counted.status_code == 200
    assert counted.get_json()["event"]["poops"] == {"1": 2, "3": 0}


def test_poop_counts_reject_unknown_boxes_and_impossible_counts(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    bodies: tuple[dict[str, Any], ...] = (
        {"counts": {"4": 1}},
        {"counts": {"1": -1}},
        {"counts": {}},
        {},
    )
    for body in bodies:
        response = client.post(f"/api/review/{event['id']}/counts", json=body)
        assert response.status_code == 400, body
    assert (
        client.post("/api/review/999/counts", json={"counts": {"1": 1}}).status_code
        == 404
    )


def test_unknown_event_404s(client: Any) -> None:
    assert (
        client.post("/api/review/999/label", json={"value": "cat"}).status_code == 404
    )
    assert client.get("/api/review/event/999").status_code == 404
    assert client.get("/review/999/clip/0/strip.jpg").status_code == 404


def test_the_payload_lists_every_clip_of_the_event(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    assert event["ended_at"] is not None
    assert [c["index"] for c in event["clips"]] == [0]
    assert event["clips"][0]["close_reason"] == "recovered"


def test_a_clip_index_past_the_end_404s(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    assert client.get(f"/review/{event['id']}/clip/5/strip.jpg").status_code == 404


# --- review API: grouping ---------------------------------------------------


@pytest.fixture
def grouped_client(db: Any, tmp_path: Path) -> Any:
    """One two-clip visit and, later, a one-clip visit."""
    from flask import Flask

    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    visit(directory, 500)
    db.ingest(directory)
    app = Flask(__name__)
    app.register_blueprint(create_review_blueprint(db, ClipFrames(tmp_path / "cache")))
    return app.test_client()


def test_the_page_walks_multi_clip_events_on_request(grouped_client: Any) -> None:
    first = grouped_client.get("/api/review/multi/next").get_json()["event"]
    assert len(first["clips"]) == 2

    after = grouped_client.get(f"/api/review/multi/next?after={first['id']}")
    assert after.get_json()["event"] is None


def test_split_and_join_from_the_api(grouped_client: Any) -> None:
    event = grouped_client.get("/api/review/next").get_json()["event"]

    split = grouped_client.post(f"/api/review/{event['id']}/split", json={"index": 1})
    assert split.status_code == 200
    assert len(split.get_json()["event"]["clips"]) == 1
    assert split.get_json()["counts"]["total"] == 3

    joined = grouped_client.post(f"/api/review/{event['id']}/join")
    assert joined.status_code == 200
    assert len(joined.get_json()["event"]["clips"]) == 2
    assert joined.get_json()["event"]["clips"][1]["boundary"] == "join"


def test_split_rejects_the_first_clip_and_bad_input(grouped_client: Any) -> None:
    event = grouped_client.get("/api/review/next").get_json()["event"]
    for body in ({"index": 0}, {"index": 9}, {"index": "1"}, {}):
        response = grouped_client.post(f"/api/review/{event['id']}/split", json=body)
        assert response.status_code == 400, body
    assert (
        grouped_client.post("/api/review/999/split", json={"index": 1}).status_code
        == 404
    )
    assert grouped_client.post("/api/review/999/join").status_code == 404


def test_the_video_route_serves_a_playable_clip(db: Any, tmp_path: Path) -> None:
    from flask import Flask

    directory = tmp_path / "captures"
    directory.mkdir()
    # OpenCV picks the muxer from the extension, so write as mp4 and rename:
    # ffmpeg probes by content, as it does for the raw clips.
    write_test_video(directory / "clip.mp4").rename(
        directory / "cat_video_20260912_080000.h264"
    )
    db.ingest(directory)
    app = Flask(__name__)
    app.register_blueprint(create_review_blueprint(db, ClipFrames(tmp_path / "cache")))
    client = app.test_client()
    event = client.get("/api/review/next").get_json()["event"]

    response = client.get(f"/review/{event['id']}/clip/0/video.mp4")
    assert response.status_code == 200
    assert response.mimetype == "video/mp4"

    # Seeking in the player needs byte ranges honoured, not just advertised.
    partial = client.get(
        f"/review/{event['id']}/clip/0/video.mp4", headers={"Range": "bytes=0-9"}
    )
    assert partial.status_code == 206
    assert len(partial.data) == 10


def test_the_video_route_404s_for_an_unreadable_clip(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    assert client.get(f"/review/{event['id']}/clip/0/video.mp4").status_code == 404


def test_undo_fetch_shows_the_existing_label(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    client.post(f"/api/review/{event['id']}/label", json={"value": "unsure"})

    data = client.get(f"/api/review/event/{event['id']}").get_json()
    assert data["event"]["label"] == "unsure"


def test_strip_route_for_an_unreadable_clip_404s(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    assert client.get(f"/review/{event['id']}/clip/0/strip.jpg").status_code == 404
