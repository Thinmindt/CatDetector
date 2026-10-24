"""What a clip is on disk: its names, and the stream inside them."""

from pathlib import Path

CAMERA_FPS = 30
H264_BITRATE = 10_000_000

# The analysis frame, (width, height) in pixels: a sidecar's blob coordinates
# are measured in it.
ANALYSIS_SIZE = (640, 480)

CLIP_SUFFIX = ".h264"  # raw elementary stream, not a container
PARTIAL_SUFFIX = ".part"


def partial_name(clip: Path) -> Path:
    """The name a clip is written under. Only the final name means "complete"."""
    return clip.with_name(clip.name + PARTIAL_SUFFIX)


def final_name(writing: Path) -> Path:
    """The clip a partial name is written for."""
    return writing.with_name(writing.name.removesuffix(PARTIAL_SUFFIX))
