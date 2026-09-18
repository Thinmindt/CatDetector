"""Sample frames out of raw .h264 clips with ffmpeg, cached on local disk."""

import logging
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from src.motion_recorder import CAMERA_FPS

log = logging.getLogger(__name__)

MAX_TILES = 48
THUMB_WIDTH = 320
FFMPEG_TIMEOUT_SECONDS = 60
STAGING_SUFFIX = ".part"


def tile_seconds(clip_seconds: float | None) -> int:
    """Seconds between tiles: one, or more once MAX_TILES would not span the clip."""
    if clip_seconds is None:
        return 1
    return max(1, math.ceil(clip_seconds / MAX_TILES))


class ClipFrames:
    """Extracts a thumbnail strip, full-size frames and a playable copy of a clip.

    OpenCV's VideoCapture cannot seek raw elementary streams, so extraction
    shells out to ffmpeg. Results are cached under cache_dir keyed by clip id;
    clips are immutable once closed, so the cache never needs invalidating.

    Only keyframes are decoded. The recorder writes one per second, so decoded
    frame n is n seconds into the clip.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = shutil.which("ffmpeg")
        if self._ffmpeg is None:
            log.warning("ffmpeg not found; frame extraction is disabled")

    def strip(
        self, clip_id: int, clip_path: str | Path, clip_seconds: float | None
    ) -> Path | None:
        """A horizontal montage spanning the clip, tile_seconds(clip_seconds) apart."""
        stride = tile_seconds(clip_seconds)
        cached = self.cache_dir / f"clip{clip_id}_strip{stride}s.jpg"
        if cached.exists():
            return cached
        return self._build_strip(Path(clip_path), stride, cached)

    def frame(
        self, clip_id: int, clip_path: str | Path, slot: int, clip_seconds: float | None
    ) -> Path | None:
        """The full-resolution frame behind one strip slot."""
        if not 0 <= slot < MAX_TILES:
            return None
        second = slot * tile_seconds(clip_seconds)
        cached = self.cache_dir / f"clip{clip_id}_t{second}s.jpg"
        if cached.exists():
            return cached

        args = [f"select='eq(n\\,{second})'", "-frames:v", "1"]
        staging = self._staged(cached)
        if self._extract(Path(clip_path), args, staging) is None:
            return None
        return self._publish(staging, cached)

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

    def _build_strip(self, clip: Path, stride: int, cached: Path) -> Path | None:
        with tempfile.TemporaryDirectory(dir=self.cache_dir) as scratch:
            pattern = Path(scratch) / "t%02d.jpg"
            select = f"select='not(mod(n\\,{stride}))',scale={THUMB_WIDTH}:-2"
            if (
                self._extract(clip, [select, "-frames:v", str(MAX_TILES)], pattern)
                is None
            ):
                return None
            loaded = (cv2.imread(str(f)) for f in sorted(Path(scratch).glob("t*.jpg")))
            thumbs = [t for t in loaded if t is not None]

        if not thumbs:
            return None
        staging = self._staged(cached)
        if not cv2.imwrite(str(staging), np.hstack(thumbs)):
            return None
        return self._publish(staging, cached)

    def _extract(self, clip: Path, filter_args: list[str], out: Path) -> Path | None:
        """Run one ffmpeg extraction. Returns out on success, None otherwise."""
        if self._ffmpeg is None or not clip.exists():
            return None
        command = [
            self._ffmpeg,
            "-v",
            "error",
            "-skip_frame",
            "nokey",
            "-i",
            str(clip),
            "-vf",
            filter_args[0],
            "-fps_mode",
            "passthrough",
            *filter_args[1:],
            "-y",
            str(out),
        ]
        return self._run(command, clip, out)

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
