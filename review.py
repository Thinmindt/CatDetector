"""Standalone label-review server. Runs without the camera, alongside main.py.

`--regroup` rebuilds every event from the current thresholds and exits.
"""

import logging
import pathlib
import sys

from flask import Flask

from config import Config
from src.capture_db import CaptureDB
from src.clip_frames import ClipFrames
from src.logging_setup import configure_logging
from src.review import create_review_blueprint

log = logging.getLogger(__name__)

REVIEW_PORT = 5001


def build_app() -> Flask:
    db = CaptureDB(Config.DB_PATH)
    added = db.ingest(pathlib.Path(Config.NETWORK_SHARE_DIR) / "captures")
    log.info("Ingest found %d new clip(s)", added)

    app = Flask(__name__)
    app.register_blueprint(
        create_review_blueprint(db, ClipFrames(Config.REVIEW_CACHE_DIR))
    )
    return app


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
    app = build_app()
    log.info("Review UI at http://<pi-ip>:%d/review", REVIEW_PORT)
    app.run(host="0.0.0.0", port=REVIEW_PORT, debug=False)  # noqa: S104 -- LAN-only by design, see the README
