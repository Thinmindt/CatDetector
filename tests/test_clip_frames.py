"""Frame extraction: strips, tiles, full frames and the MP4 remux, via ffmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import cv2
import pytest
from conftest import make_clip_file

from src.review.clip_frames import (
    MAX_TILES,
    STAGING_SUFFIX,
    THUMB_WIDTH,
    ClipFrames,
    tile_seconds,
)


def tile_greys(strip: Path) -> list[int]:
    """The mean grey of each tile's top half, left to right, clear of the caption."""
    image = cv2.imread(str(strip))
    assert image is not None
    assert image.shape[1] % THUMB_WIDTH == 0
    top = image[: image.shape[0] // 2]
    return [
        round(float(top[:, left : left + THUMB_WIDTH].mean()))
        for left in range(0, image.shape[1], THUMB_WIDTH)
    ]


def frame_grey(frame: Path | None) -> float:
    assert frame is not None
    image = cv2.imread(str(frame))
    assert image is not None
    return float(image.mean())


def test_strip_is_built_and_cached(tmp_path: Path, short_clip: Path) -> None:
    frames = ClipFrames(tmp_path / "cache")

    strip = frames.strip(1, short_clip)

    assert strip is not None and strip.exists()
    image = cv2.imread(str(strip))
    assert image is not None
    assert image.shape[1] > image.shape[0]  # wider than tall: a montage

    first_mtime = strip.stat().st_mtime_ns
    again = frames.strip(1, short_clip)
    assert again is not None
    assert again.stat().st_mtime_ns == first_mtime  # cache hit


def test_strip_spans_the_whole_clip_one_tile_a_second(
    tmp_path: Path, long_clip: Path
) -> None:
    frames = ClipFrames(tmp_path / "cache")

    strip = frames.strip(1, long_clip)

    assert strip is not None
    greys = tile_greys(strip)
    assert len(greys) == 12
    # Tile k is frame 30k, drawn at grey 15k; the codec shifts it a little.
    assert greys == pytest.approx([15 * k for k in range(12)], abs=3)


def test_long_clips_get_sparser_tiles(
    tmp_path: Path, long_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = ClipFrames(tmp_path / "cache")
    monkeypatch.setattr("src.review.clip_frames.MAX_TILES", 3)

    strip = frames.strip(1, long_clip)

    assert strip is not None
    assert tile_greys(strip) == pytest.approx([0, 60, 120], abs=3)  # 0, 4, 8 s


def test_tiles_are_captioned_with_their_offset(
    tmp_path: Path, short_clip: Path
) -> None:
    frames = ClipFrames(tmp_path / "cache")

    strip = frames.strip(1, short_clip)

    assert strip is not None
    image = cv2.imread(str(strip))
    assert image is not None
    # The second tile is a flat mid grey except for its "0:01" caption.
    second = image[:, THUMB_WIDTH : 2 * THUMB_WIDTH]
    assert second[: second.shape[0] // 2].std() < 1
    assert second[second.shape[0] // 2 :].std() > 5


def test_tile_seconds_keeps_the_tile_count_bounded() -> None:
    assert tile_seconds(0) == 1
    assert tile_seconds(MAX_TILES) == 1
    assert tile_seconds(MAX_TILES + 1) == 2
    assert tile_seconds(300) * MAX_TILES >= 300


def test_full_frames_behind_the_tiles_are_kept(tmp_path: Path, long_clip: Path) -> None:
    frames = ClipFrames(tmp_path / "cache")

    frame = frames.frame(1, long_clip, slot=2)

    assert frame_grey(frame) == pytest.approx(30, abs=3)  # frame 60, 2 s in
    assert frame is not None and frame.stat().st_size > 0
    assert frames.frame(1, long_clip, slot=12) is None  # past the end
    assert frames.frame(1, long_clip, slot=-1) is None


def test_full_frame_follows_the_strip_stride(
    tmp_path: Path, long_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = ClipFrames(tmp_path / "cache")
    monkeypatch.setattr("src.review.clip_frames.MAX_TILES", 3)

    frame = frames.frame(1, long_clip, slot=2)

    assert frame_grey(frame) == pytest.approx(120, abs=3)  # frame 240, 8 s in
    assert frames.frame(1, long_clip, slot=3) is None


def test_the_strip_is_published_after_its_frames(
    tmp_path: Path, short_clip: Path
) -> None:
    cache = tmp_path / "cache"
    frames = ClipFrames(cache)

    strip = frames.strip(1, short_clip)

    assert strip is not None
    kept = sorted(f.name for f in cache.iterdir())
    assert kept == [
        "clip1_strip1s.jpg",
        "clip1_t0s.jpg",
        "clip1_t1s.jpg",
        "clip1_t2s.jpg",
    ]
    assert strip.stat().st_mtime_ns >= max(
        (cache / f"clip1_t{n}s.jpg").stat().st_mtime_ns for n in range(3)
    )


def test_unreadable_clip_yields_none(tmp_path: Path) -> None:
    bad = make_clip_file(tmp_path / "garbage.h264", b"not video at all")
    frames = ClipFrames(tmp_path / "cache")
    assert frames.strip(1, bad) is None


def test_video_is_a_playable_cached_copy(tmp_path: Path, short_clip: Path) -> None:
    frames = ClipFrames(tmp_path / "cache")

    video = frames.video(1, short_clip)

    assert video is not None and video.suffix == ".mp4"
    capture = cv2.VideoCapture(str(video))
    ok, _ = capture.read()
    capture.release()
    assert ok
    first_mtime = video.stat().st_mtime_ns
    again = frames.video(1, short_clip)
    assert again is not None
    assert again.stat().st_mtime_ns == first_mtime  # cache hit


def test_a_failed_remux_leaves_nothing_for_the_cache_to_serve(
    tmp_path: Path, short_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg writes its output as it goes, so a failure can leave a partial
    file under the cache name; the next request must not be served that."""
    frames = ClipFrames(tmp_path / "cache")

    def half_written_then_error(command: list[str], **kwargs: Any) -> Any:
        Path(command[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, b"", b"boom")

    monkeypatch.setattr(subprocess, "run", half_written_then_error)

    assert frames.video(1, short_clip) is None
    assert not (tmp_path / "cache" / "clip1.mp4").exists()


def test_a_cache_entry_appears_only_once_it_is_complete(
    tmp_path: Path, short_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flask serves these threaded, and ffmpeg creates its output up front: a
    second request for the same clip must never be handed the file being written."""
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

    assert frames.video(1, short_clip) == cached
    assert visible_midway == [False]
    assert cached.read_bytes() == b"complete"
    assert list(cache.glob(f"*{STAGING_SUFFIX}*")) == []
