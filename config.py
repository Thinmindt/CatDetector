import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Load configs from environment variables."""

    NETWORK_SHARE_DIR = os.getenv("NETWORK_SHARE_DIR", "/mnt/nas")

    # Finished clips on the share: ClipTransfer fills it and the review page reads it.
    CAPTURES_DIR = str(Path(NETWORK_SHARE_DIR) / "captures")

    # Clips are written here first, then moved to the share once finished.
    # Local disk: recording must not depend on the network.
    LOCAL_CLIP_DIR = os.getenv("LOCAL_CLIP_DIR", ".clip_cache")

    # Set to a local path to log per-frame detection metrics. Off when unset.
    METRICS_CSV = os.getenv("METRICS_CSV")

    # Foreground pixels in the 640x480 analysis frame needed to start recording.
    MOTION_THRESHOLD = int(os.getenv("MOTION_THRESHOLD", "300"))

    # Mean grey level (0-255) of the analysis frame below which the scene counts
    # as dark and motion is ignored.
    DARK_BRIGHTNESS = float(os.getenv("DARK_BRIGHTNESS", "10"))

    # Seconds without motion before a clip closes.
    MOTION_TIMEOUT = float(os.getenv("MOTION_TIMEOUT", "10"))

    # MOG2 frames of history. A still subject fades into the background over
    # roughly this many frames.
    MOG2_HISTORY = int(os.getenv("MOG2_HISTORY", "500"))

    # A clip continues the previous visit if it starts within this many seconds
    # of that visit's last clip ending, in the same box.
    EVENT_GAP_SECONDS = float(os.getenv("EVENT_GAP_SECONDS", "180"))

    # "Same box": centroids this close, in the 640x480 analysis frame.
    EVENT_BOX_DISTANCE_PX = float(os.getenv("EVENT_BOX_DISTANCE_PX", "120"))

    # The cats, comma-separated. Each becomes a review label with a digit key.
    CAT_NAMES = tuple(
        name.strip() for name in os.getenv("CAT_NAMES", "").split(",") if name.strip()
    )

    # Local disk, never the share: SQLite locking is unreliable over CIFS.
    DB_PATH = os.getenv("DB_PATH", "captures.db")
    REVIEW_CACHE_DIR = os.getenv("REVIEW_CACHE_DIR", ".review_cache")
