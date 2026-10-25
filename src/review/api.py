"""Review API: events to label, their clips' media, and grouping corrections."""

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file
from flask.typing import ResponseReturnValue

from src.clips.sidecar import iso
from src.review.capture_db import CaptureDB, Clip, Event, JoinRefusedError
from src.review.clip_frames import ClipFrames
from src.review.strips_ahead import StripsAhead
from src.review.timeline import (
    BOXES,
    HOURS_IN_DAY,
    UNLABELED,
    Timeline,
    TimelineFilter,
    build_timeline,
)

log = logging.getLogger(__name__)

FIXED_LABELS = ("cat", "not_cat", "unsure", "clean")

# Two cats in one event: kept as a visit, excluded from single-label training.
MULTIPLE = "multiple"

# The walks besides the review queue and one label's events.
MULTI_WALK = "multi"
LABELED_WALK = "labeled"

# A count on the progress line, so no cat may be called it.
STAGED = "staged"

# Who may put an event back in the queue.
STAGING_SOURCES = ("human", "agent", "model")

# The timeline rows one response carries; the rest are counted, and their ids
# sent for staging.
TIMELINE_ROWS_SENT = 500


@dataclass(frozen=True)
class Review:
    """What the review routes are built on: the database, the frame extractor,
    the directory on the share that a rescan reads, the cats' names, and the
    builder that readies strips before the page asks for them."""

    db: CaptureDB
    frames: ClipFrames
    captures_dir: Path
    cat_names: tuple[str, ...] = ()
    ahead: StripsAhead | None = None

    def __post_init__(self) -> None:
        reserved = {
            *FIXED_LABELS,
            MULTIPLE,
            MULTI_WALK,
            LABELED_WALK,
            STAGED,
            UNLABELED,
        }
        for name in self.cat_names:
            if not name or name in reserved:
                raise ValueError(f"CAT_NAMES: {name!r} cannot be a cat's name")
        if len(set(self.cat_names)) != len(self.cat_names):
            raise ValueError(f"CAT_NAMES: names repeat in {self.cat_names}")

    @property
    def labels(self) -> tuple[str, ...]:
        """Every value a label may take. Without cats, the fixed set alone."""
        if not self.cat_names:
            return FIXED_LABELS
        return (*FIXED_LABELS, *self.cat_names, MULTIPLE)

    @property
    def cat_labels(self) -> tuple[str, ...]:
        """The labels that say a cat was there: only these may be a peek."""
        return cat_labels(self.cat_names)

    def queue_changed(self) -> None:
        """Ready the strips of whatever the queue now holds next."""
        if self.ahead is not None:
            self.ahead.wanted()


def cat_labels(cat_names: tuple[str, ...]) -> tuple[str, ...]:
    """The labels that say a cat was there."""
    return ("cat", *cat_names, MULTIPLE) if cat_names else ("cat",)


def _iso(when: datetime.datetime | None) -> str | None:
    return None if when is None else iso(when)


def error(message: str, status: int) -> ResponseReturnValue:
    return jsonify({"error": message}), status


def no_such_event() -> ResponseReturnValue:
    return error("no such event", 404)


def request_body() -> dict[str, Any]:
    """The request's JSON object, or an empty one if it sent anything else."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def staging_request(body: dict[str, Any]) -> tuple[str, str]:
    """The reason and source a staging request names. Raises ValueError."""
    reason, source = body.get("reason"), body.get("source", "human")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    if source not in STAGING_SOURCES:
        raise ValueError(f"source must be one of {STAGING_SOURCES}")
    return reason.strip(), source


def timeline_filter(labels: tuple[str, ...]) -> TimelineFilter:
    """The filter in the request's query string. Raises ValueError."""
    chips = request.args.getlist("label")
    unknown = set(chips) - {*labels, UNLABELED}
    if unknown:
        raise ValueError(f"label must be one of {(*labels, UNLABELED)}")
    return TimelineFilter(
        chips=frozenset(chips) if chips else None,
        first_day=_parsed(request.args.get("from"), datetime.date.fromisoformat),
        last_day=_parsed(request.args.get("to"), datetime.date.fromisoformat),
        hours=_hour_range(request.args.get("hours")),
        labeled_before=_parsed(
            request.args.get("labeled_before"), datetime.datetime.fromisoformat
        ),
    )


def _parsed[T](text: str | None, parse: Callable[[str], T]) -> T | None:
    return None if not text else parse(text)


def _hour_range(text: str | None) -> tuple[int, int] | None:
    """ "22-6" as (22, 6): from 22:00 up to 06:00."""
    if not text:
        return None
    first, _, last = text.partition("-")
    hours = (int(first), int(last))
    if not all(0 <= hour <= HOURS_IN_DAY for hour in hours) or hours[0] == hours[1]:
        raise ValueError("hours must be two different hours from 0 to 24, as 22-6")
    return hours


def timeline_payload(timeline: Timeline) -> dict[str, Any]:
    return {
        "total": len(timeline.rows),
        "stageable": [row.id for row in timeline.rows if row.stageable],
        "rows": [
            {
                "id": row.id,
                "started_at": _iso(row.started_at),
                "seconds": row.seconds,
                "clips": row.clips,
                "label": row.label,
                "box": row.box,
                "staged": row.staged,
                "peek": row.peek,
            }
            for row in timeline.rows[:TIMELINE_ROWS_SENT]
        ],
        "by_day": {day.isoformat(): dict(n) for day, n in timeline.by_day.items()},
        "by_hour": [dict(n) for n in timeline.by_hour],
        "first_day": _iso_day(timeline.first_day),
        "last_day": _iso_day(timeline.last_day),
    }


def _iso_day(day: datetime.date | None) -> str | None:
    return None if day is None else day.isoformat()


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

    def __init__(self, review: Review) -> None:
        self.review = review
        self.db = review.db
        self.frames = review.frames
        self.captures_dir = review.captures_dir
        self.labels = review.labels

    def next_event(self) -> ResponseReturnValue:
        """The next event in a walk: `filter` names the walk (none is the
        review queue), `after` the event to continue from."""
        walk = request.args.get("filter") or None
        after = request.args.get("after", type=int)
        if walk is None:
            return self._event_payload(self.db.next_to_review(after))
        if walk == MULTI_WALK:
            return self._event_payload(self.db.next_multi(after))
        if walk == LABELED_WALK:
            return self._event_payload(self.db.next_labeled(after, None))
        if walk in self.labels:
            return self._event_payload(self.db.next_labeled(after, walk))
        return error(f"filter must be {MULTI_WALK}, {LABELED_WALK} or a label", 400)

    def one_event(self, event_id: int) -> ResponseReturnValue:
        event = self.db.get(event_id)
        if event is None:
            return no_such_event()
        return self._event_payload(event)

    def set_label(self, event_id: int) -> ResponseReturnValue:
        value = request_body().get("value")
        if value not in self.labels:
            return error(f"value must be one of {self.labels}", 400)
        event = self.db.get(event_id)
        if event is None:
            return no_such_event()
        self.db.set_label(event_id, value)
        if event.peek and value not in self.review.cat_labels:
            self.db.set_peek(event_id, False)
        self.review.queue_changed()
        return jsonify({"ok": True, "counts": self.db.counts()})

    def stage(self, event_id: int) -> ResponseReturnValue:
        """Put an event back in the review queue with a reason, keeping its label."""
        try:
            reason, source = staging_request(request_body())
        except ValueError as refused:
            return error(str(refused), 400)
        event = self.db.get(event_id)
        if event is None:
            return no_such_event()
        if event.label is None:
            return error("an unlabelled event is in the review queue already", 409)
        self.db.stage(event_id, reason, source)
        self.review.queue_changed()
        return self._event_payload(self.db.get(event_id))

    def stage_many(self) -> ResponseReturnValue:
        """Stage the events named by id, what the timeline showed, skipping those
        with no label or already staged."""
        body = request_body()
        event_ids = body.get("event_ids")
        if not isinstance(event_ids, list) or not all(
            isinstance(event_id, int) for event_id in event_ids
        ):
            return error("event_ids must be a list of event ids", 400)
        try:
            reason, source = staging_request(body)
        except ValueError as refused:
            return error(str(refused), 400)
        staged = self.db.stage_events(event_ids, reason, source)
        self.review.queue_changed()
        return jsonify({"staged": staged})

    def timeline(self) -> ResponseReturnValue:
        """Every event the filter in the query string admits, newest first,
        with the counts behind the page's date and hour strips."""
        try:
            chosen = timeline_filter(self.labels)
        except ValueError as refused:
            return error(str(refused), 400)
        return jsonify(
            timeline_payload(build_timeline(self.db.timeline_rows(), chosen))
        )

    def set_counts(self, event_id: int) -> ResponseReturnValue:
        """Poops found per box at a cleaning, signalled by hand in the clip."""
        if self.db.get(event_id) is None:
            return no_such_event()
        counts = validated_counts(request_body().get("counts"))
        if counts is None:
            return error(f"counts must be {{box: n}} for {BOXES}", 400)
        self.db.set_poop_counts(event_id, counts)
        return self._event_payload(self.db.get(event_id))

    def set_peek(self, event_id: int) -> ResponseReturnValue:
        """Mark the event as a peek, or clear the mark."""
        peek = request_body().get("peek")
        if not isinstance(peek, bool):
            return error("peek must be true or false", 400)
        if not self.db.set_peek(event_id, peek):
            return no_such_event()
        return self._event_payload(self.db.get(event_id))

    def split(self, event_id: int) -> ResponseReturnValue:
        """A new visit starts at the index-th clip; the rest stay in this event."""
        event = self.db.get(event_id)
        if event is None:
            return no_such_event()
        index = request_body().get("index")
        if not isinstance(index, int) or not 0 < index < len(event.clips):
            return error("index must name a clip after the first", 400)
        self.db.split(event.clips[index].id)
        self.review.queue_changed()
        return self._event_payload(self.db.get(event_id))

    def join(self, event_id: int) -> ResponseReturnValue:
        if self.db.get(event_id) is None:
            return no_such_event()
        try:
            joined = self.db.join(event_id)
        except JoinRefusedError as refused:
            return error(str(refused), 409)
        self.review.queue_changed()
        return self._event_payload(joined)

    def rescan(self) -> ResponseReturnValue:
        added = self.db.ingest(self.captures_dir)
        self.review.queue_changed()
        return jsonify({"added": added, "counts": self.db.counts()})

    def strip(self, clip_id: int) -> Response:
        return self._media(
            clip_id, "image/jpeg", lambda c: self.frames.strip(c.id, c.path)
        )

    def frame(self, clip_id: int, slot: int) -> Response:
        return self._media(
            clip_id, "image/jpeg", lambda c: self.frames.frame(c.id, c.path, slot)
        )

    def video(self, clip_id: int) -> Response:
        return self._media(
            clip_id, "video/mp4", lambda c: self.frames.video(c.id, c.path)
        )

    def _media(
        self,
        clip_id: int,
        mimetype: str,
        produce: Callable[[Clip], Path | None],
    ) -> Response:
        """A file for one clip, or 404.

        send_file honours Range requests, which is what lets the player seek.
        """
        clip = self.db.clip(clip_id)
        path = produce(clip) if clip is not None else None
        if path is None:
            return Response("unavailable", status=404)
        return send_file(path, mimetype=mimetype)

    def _event_payload(self, event: Event | None) -> ResponseReturnValue:
        body = None
        if event is not None:
            body = {
                "id": event.id,
                "started_at": _iso(event.started_at),
                "ended_at": _iso(event.ended_at),
                "label": event.label,
                "poops": event.poops,
                "staged": event.staged._asdict() if event.staged else None,
                "peek": event.peek,
                "clips": [
                    {
                        "id": clip.id,
                        "index": index,
                        "started_at": _iso(clip.started_at),
                        "ended_at": _iso(clip.ended_at),
                        "close_reason": clip.close_reason.value,
                        "boundary": clip.boundary.value if clip.boundary else None,
                    }
                    for index, clip in enumerate(event.clips)
                ],
            }
        return jsonify({"event": body, "counts": self.db.counts()})


def create_review_blueprint(review: Review) -> Blueprint:
    """The review API and media routes. The page itself is served by web_app."""
    pages = ReviewPages(review)
    bp = Blueprint("review", __name__)
    bp.add_url_rule("/api/review/next", view_func=pages.next_event)
    bp.add_url_rule("/api/review/event/<int:event_id>", view_func=pages.one_event)
    for action, view in (
        ("label", pages.set_label),
        ("stage", pages.stage),
        ("counts", pages.set_counts),
        ("peek", pages.set_peek),
        ("split", pages.split),
        ("join", pages.join),
    ):
        bp.add_url_rule(
            f"/api/review/<int:event_id>/{action}", view_func=view, methods=["POST"]
        )
    bp.add_url_rule("/api/review/rescan", view_func=pages.rescan, methods=["POST"])
    bp.add_url_rule("/api/review/stage", view_func=pages.stage_many, methods=["POST"])
    bp.add_url_rule("/api/timeline", view_func=pages.timeline)
    media = "/review/clip/<int:clip_id>"
    bp.add_url_rule(f"{media}/strip.jpg", view_func=pages.strip)
    bp.add_url_rule(f"{media}/frame/<int:slot>.jpg", view_func=pages.frame)
    bp.add_url_rule(f"{media}/video.mp4", view_func=pages.video)
    return bp
