import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Load configs from environment variables."""

    NETWORK_SHARE_DIR = os.getenv("NETWORK_SHARE_DIR", "/mnt/nas")

    # Clips are written here first, then moved to the share once finished.
    # Local disk: recording must not depend on the network.
    LOCAL_CLIP_DIR = os.getenv("LOCAL_CLIP_DIR", ".clip_cache")

    # Set to a local path to log per-frame detection metrics. Off when unset.
    METRICS_CSV = os.getenv("METRICS_CSV")

    # Local disk, never the share: SQLite locking is unreliable over CIFS.
    DB_PATH = os.getenv("DB_PATH", "captures.db")
    REVIEW_CACHE_DIR = os.getenv("REVIEW_CACHE_DIR", ".review_cache")
