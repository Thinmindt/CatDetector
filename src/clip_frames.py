"""Sample frames out of raw .h264 clips with ffmpeg, cached on local disk."""

import logging
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import cast
from uuid import uuid4

import cv2
import numpy as np

from src.camera_manager import Frame
from src.motion_recorder import CAMERA_FPS

log = logging.getLogger(__name__)

MAX_TILES = 48
THUMB_WIDTH = 320
FFMPEG_TIMEOUT_SECONDS = 60
STAGING_SUFFIX = ".part"
JPEG_QUALITY = 3  # ffmpeg qscale, 2 (best) to 31

CAPTION_FONT = cv2.FONT_HERSHEY_SIMPLEX
CAPTION_SCALE = 0.45
CAPTION_MARGIN = 5


def tile_seconds(keyframes: int) -> int:
    """Seconds between tiles: one, or more once MAX_TILES would not span the clip."""
    return max(1, math.ceil(keyframes / MAX_TILES))


def _offset_label(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _captioned(thumb: Frame, label: str) -> Frame:
    """The thumb with its offset drawn bottom-left on a dark box."""
    (width, height), baseline = cv2.getTextSize(label, CAPTION_FONT, CAPTION_SCALE, 1)
    x, y = CAPTION_MARGIN, thumb.shape[0] - CAPTION_MARGIN
    cv2.rectangle(
        thumb, (x - 3, y - height - 3), (x + width + 3, y + baseline), (0, 0, 0), -1
    )
    cv2.putText(thumb, label, (x, y), CAPTION_FONT, CAPTION_SCALE, (240, 240, 240), 1)
    return thumb


def _stride_of(strip: Path) -> int:
    """The seconds between tiles, as recorded in the strip's name."""
    return int(strip.stem.rsplit("_strip", 1)[1].removesuffix("s"))


class ClipFrames:
    """Extracts a thumbnail strip, full-size frames and a playable copy of a clip.

    OpenCV's VideoCapture cannot seek raw elementary streams, so extraction
    shells out to ffmpeg. Results are cached under cache_dir keyed by clip id;
    clips are immutable once closed, so the cache never needs invalidating.

    Only keyframes are decoded. The recorder writes one per second, so decoded
    frame n is n seconds into the file. One pass over a clip produces both the
    strip and the full frames behind its tiles; the strip is published last, so
    its presence means the set is complete.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = shutil.which("ffmpeg")
        if self._ffmpeg is None:
            log.warning("ffmpeg not found; frame extraction is disabled")

    def strip(self, clip_id: int, clip_path: str | Path) -> Path | None:
        """A montage spanning the clip, each tile captioned with its offset."""
        cached = next(self.cache_dir.glob(f"clip{clip_id}_strip*s.jpg"), None)
        if cached is not None:
            return cached
        return self._build_strip(clip_id, Path(clip_path))

    def frame(self, clip_id: int, clip_path: str | Path, slot: int) -> Path | None:
        """The full-resolution frame behind one strip slot."""
        strip = self.strip(clip_id, clip_path)
        if strip is None or slot < 0:
            return None
        cached = self._frame_path(clip_id, slot * _stride_of(strip))
        return cached if cached.exists() else None

    def _frame_path(self, clip_id: int, second: int) -> Path:
        return self.cache_dir / f"clip{clip_id}_t{second}s.jpg"

    def video(self, clip_id: int, clip_path: str | Path) -> Path | None:
        """The clip remuxed, not re-encoded, into an MP4 a browser can play and seek.

        The frame rate is pinned: a raw stream has no container to carry it.
        """
        cached = self.cache_dir / f"clip{clip_id}.mp4"
        if cached.exists():
            return cached
        clip = Path(clip_path)
        if self._ffmpeg is None or not clip.exists():
            return None
        staging = self._staged(cached)
        command = [
            self._ffmpeg,
            "-v",
            "error",
            "-r",
            str(CAMERA_FPS),
            "-i",
            str(clip),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(staging),
        ]
        if self._run(command, clip, staging) is None:
            return None
        return self._publish(staging, cached)

    @staticmethod
    def _staged(cached: Path) -> Path:
        """A private path beside the cache entry, unique per writer.

        The real suffix stays last: ffmpeg and cv2 pick the format from it.
        """
        return cached.with_name(
            f"{cached.stem}.{uuid4().hex}{STAGING_SUFFIX}{cached.suffix}"
        )

    @staticmethod
    def _publish(staging: Path, cached: Path) -> Path:
        """Rename within the cache, so no reader is served a half-written entry."""
        staging.replace(cached)
        return cached

    def _build_strip(self, clip_id: int, clip: Path) -> Path | None:
        with tempfile.TemporaryDirectory(dir=self.cache_dir) as scratch:
            pattern = Path(scratch) / "k%04d.jpg"
            if self._dump_keyframes(clip, pattern) is None:
                return None
            keyframes = sorted(Path(scratch).glob("k*.jpg"))
            if not keyframes:
                return None
            stride = tile_seconds(len(keyframes))
            thumbs = self._keep_tiled_frames(clip_id, keyframes[::stride], stride)

        if not thumbs:
            return None
        cached = self.cache_dir / f"clip{clip_id}_strip{stride}s.jpg"
        staging = self._staged(cached)
        if not cv2.imwrite(str(staging), np.hstack(thumbs)):
            return None
        return self._publish(staging, cached)

    def _keep_tiled_frames(
        self, clip_id: int, frames: list[Path], stride: int
    ) -> list[Frame]:
        """Publish the full frames behind the tiles; return their captioned thumbs."""
        thumbs: list[Frame] = []
        for slot, frame in enumerate(frames):
            image = cv2.imread(str(frame))
            if image is None:
                continue
            second = slot * stride
            scale = THUMB_WIDTH / image.shape[1]
            thumb = cast(Frame, cv2.resize(image, None, fx=scale, fy=scale))
            thumbs.append(_captioned(thumb, _offset_label(second)))
            self._publish(frame, self._frame_path(clip_id, second))
        return thumbs

    def _dump_keyframes(self, clip: Path, pattern: Path) -> Path | None:
        """Write every keyframe of the clip as a full-size JPEG, in order."""
        if self._ffmpeg is None or not clip.exists():
            return None
        command = [
            self._ffmpeg,
            *("-v", "error", "-skip_frame", "nokey", "-i", str(clip)),
            *("-fps_mode", "passthrough", "-q:v", str(JPEG_QUALITY)),
            *("-y", str(pattern)),
        ]
        return self._run(command, clip, pattern)

    @staticmethod
    def _run(command: list[str], clip: Path, out: Path) -> Path | None:
        """Run ffmpeg. A failure leaves nothing behind for a cache hit to serve."""
        try:
            done = subprocess.run(  # noqa: S603 -- fixed argv, no shell
                command,
                capture_output=True,
                timeout=FFMPEG_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.error("ffmpeg timed out on %s", clip)  # noqa: TRY400 -- no traceback worth printing
            out.unlink(missing_ok=True)
            return None
        if done.returncode != 0:
            log.error(
                "ffmpeg failed on %s: %s", clip, done.stderr.decode(errors="replace")
            )
            out.unlink(missing_ok=True)
            return None
        return out
