"""One web app for the live feed and the review UI, as tabs on one port."""

import logging

from flask import Flask, Response, jsonify, render_template

from src.capture.web_streamer import WebStreamer
from src.review.api import VALID_LABELS, Review, create_review_blueprint
from src.review.clip_frames import THUMB_WIDTH

log = logging.getLogger(__name__)

WEB_PORT = 5000


def create_app(streamer: WebStreamer | None, review: Review | None) -> Flask:
    """The app with whichever halves are available.

    Without a streamer the Live tab reports that the detector is not running;
    without a database the Review tab says so instead of failing.
    """
    app = Flask(__name__)

    def page() -> str:
        return render_template(
            "app.html",
            thumb_width=THUMB_WIDTH,
            labels=VALID_LABELS,
            review_available=review is not None,
        )

    def no_detector() -> Response:
        return jsonify({"detector": False})

    app.add_url_rule("/", "index", page)
    app.add_url_rule("/review", "review_page", page)
    if streamer is not None:
        app.register_blueprint(streamer.blueprint())
    else:
        app.add_url_rule("/api/status", "status", no_detector)
    if review is not None:
        app.register_blueprint(create_review_blueprint(review))
    return app


def run(app: Flask, port: int = WEB_PORT) -> None:
    """Serve on every interface. Blocks; main.py runs this on a daemon thread."""
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)  # noqa: S104 -- LAN-only by design, see the README
