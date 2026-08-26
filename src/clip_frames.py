"""Sample frames out of raw .h264 clips with ffmpeg, cached on local disk."""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

THUMB_COUNT = 8
THUMB_WIDTH = 320
FRAME_STRIDE = 30  # sample once per second at the camera's 30fps
FFMPEG_TIMEOUT_SECONDS = 60


class ClipFrames:
    """Extracts a thumbnail strip and full-size frames from clips.

    OpenCV's VideoCapture cannot seek raw elementary streams, so extraction
    shells out to ffmpeg. Results are cached under cache_dir keyed by event id;
    clips are immutable once closed, so the cache never needs invalidating.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = shutil.which("ffmpeg")
        if self._ffmpeg is None:
            log.warning("ffmpeg not found; frame extraction is disabled")

    def strip(self, event_id: int, clip_path: str | Path) -> Path | None:
        """A horizontal montage of THUMB_COUNT frames, one per second."""
        cached = self.cache_dir / f"{event_id}_strip.jpg"
        if cached.exists():
            return cached
        return self._build_strip(Path(clip_path), cached)

    def frame(self, event_id: int, clip_path: str | Path, slot: int) -> Path | None:
        """The full-resolution frame behind one strip slot."""
        if not 0 <= slot < THUMB_COUNT:
            return None
        cached = self.cache_dir / f"{event_id}_f{slot}.jpg"
        if cached.exists():
            return cached

        frame_number = slot * FRAME_STRIDE
        args = [f"select='eq(n\\,{frame_number})'", "-frames:v", "1"]
        return self._extract(Path(clip_path), args, cached)

    def _build_strip(self, clip: Path, cached: Path) -> Path | None:
        with tempfile.TemporaryDirectory(dir=self.cache_dir) as scratch:
            pattern = Path(scratch) / "t%02d.jpg"
            select = f"select='not(mod(n\\,{FRAME_STRIDE}))',scale={THUMB_WIDTH}:-2"
            if (
                self._extract(clip, [select, "-frames:v", str(THUMB_COUNT)], pattern)
                is None
            ):
                return None
            loaded = (cv2.imread(str(f)) for f in sorted(Path(scratch).glob("t*.jpg")))
            thumbs = [t for t in loaded if t is not None]

        if not thumbs:
            return None
        cv2.imwrite(str(cached), np.hstack(thumbs))
        return cached

    def _extract(self, clip: Path, filter_args: list[str], out: Path) -> Path | None:
        """Run one ffmpeg extraction. Returns out on success, None otherwise."""
        if self._ffmpeg is None or not clip.exists():
            return None
        command = [
            self._ffmpeg,
            "-v",
            "error",
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
        try:
            done = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                timeout=FFMPEG_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.error("ffmpeg timed out on %s", clip)  # noqa: TRY400
            return None
        if done.returncode != 0:
            log.error(
                "ffmpeg failed on %s: %s", clip, done.stderr.decode(errors="replace")
            )
            return None
        return out
