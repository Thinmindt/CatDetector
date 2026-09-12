"""The web app without the camera: review only, on the same port as main.py.

Use it when the detector is not running. `--regroup` rebuilds every event
from the current thresholds and exits.
"""

import logging
import pathlib
import sys

from flask import Flask

from config import Config
from src.capture_db import CaptureDB
from src.clip_frames import ClipFrames
from src.logging_setup import configure_logging
from src.web_app import WEB_PORT, create_app, run

log = logging.getLogger(__name__)


def build_app() -> Flask:
    db = CaptureDB(Config.DB_PATH)
    added = db.ingest(pathlib.Path(Config.NETWORK_SHARE_DIR) / "captures")
    log.info("Ingest found %d new clip(s)", added)
    return create_app(None, db, ClipFrames(Config.REVIEW_CACHE_DIR))


def regroup_events() -> dict[str, int]:
    """Rebuild every event from the current thresholds. Event ids change."""
    db = CaptureDB(Config.DB_PATH)
    try:
        db.regroup()
        return db.counts()
    finally:
        db.close()


if __name__ == "__main__":
    configure_logging(__name__)
    if "--regroup" in sys.argv[1:]:
        log.info("Regrouped: %s", regroup_events())
        sys.exit(0)
    log.info("Review UI at http://<pi-ip>:%d/review (no camera)", WEB_PORT)
    run(build_app())
