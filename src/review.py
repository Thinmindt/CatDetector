"""Review API: events to label, their clips' media, and grouping corrections."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from config import Config
from src.capture_db import CaptureDB, Clip, Event
from src.clip_frames import ClipFrames

log = logging.getLogger(__name__)

VALID_LABELS = ("cat", "not_cat", "unsure", "clean")

# Litter boxes by position in frame, left to right; the camera knows no more.
BOXES = (1, 2, 3)


def validated_counts(raw: Any) -> dict[int, int] | None:
    """{box: poops} for boxes 1-3 with counts of 0 or more, or None if it is not."""
    if not isinstance(raw, dict) or not raw:
        return None
    by_box = {str(box): count for box, count in raw.items()}
    if not all(box.isdigit() and int(box) in BOXES for box in by_box):
        return None
    if not all(isinstance(count, int) and count >= 0 for count in by_box.values()):
        return None
    return {int(box): count for box, count in by_box.items()}


class ReviewPages:
    """Route handlers bound to one database and one frame extractor."""

    def __init__(self, db: CaptureDB, frames: ClipFrames) -> None:
        self.db = db
        self.frames = frames

    def next_event(self) -> Any:
        return self._event_payload(self.db.next_unlabeled())

    def next_multi(self) -> Any:
        after = request.args.get("after", type=int)
        return self._event_payload(self.db.next_multi(after))

    def next_labeled(self) -> Any:
        after = request.args.get("after", type=int)
        value = request.args.get("value")
        if value is not None and value not in VALID_LABELS:
            return jsonify({"error": f"value must be one of {VALID_LABELS}"}), 400
        return self._event_payload(self.db.next_labeled(after, value))

    def one_event(self, event_id: int) -> Any:
        event = self.db.get(event_id)
        if event is None:
            return jsonify({"error": "no such event"}), 404
        return self._event_payload(event)

    def set_label(self, event_id: int) -> Any:
        value = (request.get_json(silent=True) or {}).get("value")
        if value not in VALID_LABELS:
            return jsonify({"error": f"value must be one of {VALID_LABELS}"}), 400
        if self.db.get(event_id) is None:
            return jsonify({"error": "no such event"}), 404
        self.db.set_label(event_id, value)
        return jsonify({"ok": True, "counts": self.db.counts()})

    def set_counts(self, event_id: int) -> Any:
        """Poops found per box at a cleaning, signalled by hand in the clip."""
        if self.db.get(event_id) is None:
            return jsonify({"error": "no such event"}), 404
        counts = validated_counts((request.get_json(silent=True) or {}).get("counts"))
        if counts is None:
            return jsonify({"error": f"counts must be {{box: n}} for {BOXES}"}), 400
        self.db.set_poop_counts(event_id, counts)
        return self._event_payload(self.db.get(event_id))

    def split(self, event_id: int) -> Any:
        """A new visit starts at the index-th clip; the rest stay in this event."""
        event = self.db.get(event_id)
        if event is None:
            return jsonify({"error": "no such event"}), 404
        index = (request.get_json(silent=True) or {}).get("index")
        if not isinstance(index, int) or not 0 < index < len(event.clips):
            return jsonify({"error": "index must name a clip after the first"}), 400
        self.db.split(event.clips[index].id)
        return self._event_payload(self.db.get(event_id))

    def join(self, event_id: int) -> Any:
        if self.db.get(event_id) is None:
            return jsonify({"error": "no such event"}), 404
        return self._event_payload(self.db.join(event_id))

    def rescan(self) -> Any:
        added = self.db.ingest(Path(Config.NETWORK_SHARE_DIR) / "captures")
        return jsonify({"added": added, "counts": self.db.counts()})

    def strip(self, event_id: int, index: int) -> Response:
        return self._media(
            event_id, index, "image/jpeg", lambda c: self.frames.strip(c.id, c.path)
        )

    def frame(self, event_id: int, index: int, slot: int) -> Response:
        return self._media(
            event_id,
            index,
            "image/jpeg",
            lambda c: self.frames.frame(c.id, c.path, slot),
        )

    def video(self, event_id: int, index: int) -> Response:
        return self._media(
            event_id, index, "video/mp4", lambda c: self.frames.video(c.id, c.path)
        )

    def _media(
        self,
        event_id: int,
        index: int,
        mimetype: str,
        produce: Callable[[Clip], Path | None],
    ) -> Response:
        """A file for the index-th clip of an event, or 404.

        send_file honours Range requests, which is what lets the player seek.
        """
        event = self.db.get(event_id)
        clip = event.clips[index] if event and 0 <= index < len(event.clips) else None
        path = produce(clip) if clip is not None else None
        if path is None:
            return Response("unavailable", status=404)
        return send_file(path, mimetype=mimetype)

    def _event_payload(self, event: Event | None) -> Any:
        body = None
        if event is not None:
            body = {
                "id": event.id,
                "started_at": event.started_at,
                "ended_at": event.ended_at,
                "label": event.label,
                "poops": event.poops,
                "clips": [
                    {
                        "index": index,
                        "started_at": clip.started_at,
                        "ended_at": clip.ended_at,
                        "close_reason": clip.close_reason,
                        "boundary": clip.boundary,
                    }
                    for index, clip in enumerate(event.clips)
                ],
            }
        return jsonify({"event": body, "counts": self.db.counts()})


def create_review_blueprint(db: CaptureDB, frames: ClipFrames) -> Blueprint:
    """The review API and media routes. The page itself is served by web_app."""
    pages = ReviewPages(db, frames)
    bp = Blueprint("review", __name__)
    bp.add_url_rule("/api/review/next", view_func=pages.next_event)
    bp.add_url_rule("/api/review/multi/next", view_func=pages.next_multi)
    bp.add_url_rule("/api/review/labeled/next", view_func=pages.next_labeled)
    bp.add_url_rule("/api/review/event/<int:event_id>", view_func=pages.one_event)
    for action, view in (
        ("label", pages.set_label),
        ("counts", pages.set_counts),
        ("split", pages.split),
        ("join", pages.join),
    ):
        bp.add_url_rule(
            f"/api/review/<int:event_id>/{action}", view_func=view, methods=["POST"]
        )
    bp.add_url_rule("/api/review/rescan", view_func=pages.rescan, methods=["POST"])
    media = "/review/<int:event_id>/clip/<int:index>"
    bp.add_url_rule(f"{media}/strip.jpg", view_func=pages.strip)
    bp.add_url_rule(f"{media}/frame/<int:slot>.jpg", view_func=pages.frame)
    bp.add_url_rule(f"{media}/video.mp4", view_func=pages.video)
    return bp
