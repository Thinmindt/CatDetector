import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Load configs from environment variables."""

    NETWORK_SHARE_DIR = os.getenv("NETWORK_SHARE_DIR", "/mnt/nas")

    # Set to a local path to log per-frame detection metrics. Off when unset.
    METRICS_CSV = os.getenv("METRICS_CSV")
