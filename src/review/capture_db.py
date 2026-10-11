"""SQLite store for clips, the events that group them, and review labels."""

import datetime
import fcntl
import itertools
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, NamedTuple

from src.clips.sidecar import CloseReason, iso
from src.review.clip_scan import FoundClip, scan_clips
from src.review.event_grouping import Boundary, ClipRow, group_clips
from src.review.timeline import TimelineRow

log = logging.getLogger(__name__)

HOLD_WAIT_SECONDS = 60
HOLD_POLL_SECONDS = 0.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clip (
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER REFERENCES event(id),
    path         TEXT NOT NULL UNIQUE,
    started_at   TEXT,
    ended_at     TEXT,
    close_reason TEXT NOT NULL,
    trigger_cx   INTEGER,
    trigger_cy   INTEGER,
    last_cx      INTEGER,
    last_cy      INTEGER,
    size_bytes   INTEGER,
    boundary     TEXT,
    joins        TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS label (
    event_id   INTEGER PRIMARY KEY REFERENCES event(id),
    value      TEXT NOT NULL,
    labeled_at TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'human'
);
CREATE TABLE IF NOT EXISTS poop_count (
    clip_id  INTEGER NOT NULL REFERENCES clip(id),
    box      INTEGER NOT NULL,          -- position in frame, 1 = leftmost
    count    INTEGER NOT NULL,
    noted_at TEXT NOT NULL,
    PRIMARY KEY (clip_id, box)
);
CREATE INDEX IF NOT EXISTS clip_event ON clip(event_id);
CREATE TABLE IF NOT EXISTS staged (
    clip_id   INTEGER PRIMARY KEY REFERENCES clip(id),
    reason    TEXT NOT NULL,
    staged_at TEXT NOT NULL,
    source    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peek (
    clip_id  INTEGER PRIMARY KEY REFERENCES clip(id),
    noted_at TEXT NOT NULL
);
"""

# An event is a peek while every one of its clips carries the mark.
EVENT_IS_PEEK = (
    "NOT EXISTS (SELECT 1 FROM clip k LEFT JOIN peek p ON p.clip_id = k.id"
    " WHERE k.event_id = {event} AND p.clip_id IS NULL)"
)
EVENTS_PARTLY_PEEK = (
    "SELECT k.event_id FROM clip k LEFT JOIN peek p ON p.clip_id = k.id"
    " WHERE k.event_id IS NOT NULL GROUP BY k.event_id"
    " HAVING COUNT(p.clip_id) > 0 AND COUNT(p.clip_id) < COUNT(*)"
)
PEEK_GREW = "a clip was grouped into this peek: is it a visit now?"

# Events oldest first; those with no dated clip last.
EVENTS_IN_ORDER = (
    "SELECT e.id FROM event e JOIN clip c ON c.event_id = e.id GROUP BY e.id"
    " ORDER BY MIN(c.started_at) IS NULL, MIN(c.started_at), e.id"
)
STAGED_EVENTS = (
    "SELECT DISTINCT c.event_id FROM staged s JOIN clip c ON c.id = s.clip_id"
    " WHERE c.event_id IS NOT NULL"
)
TIMELINE_ROWS = """
SELECT e.id, MIN(c.started_at) AS started_at, MAX(c.ended_at) AS ended_at,
       COUNT(c.id) AS clips, l.value AS label, l.labeled_at,
       (SELECT f.trigger_cx FROM clip f WHERE f.event_id = e.id
        ORDER BY f.started_at IS NULL, f.started_at, f.id LIMIT 1) AS trigger_cx,
       EXISTS (SELECT 1 FROM staged s JOIN clip k ON k.id = s.clip_id
               WHERE k.event_id = e.id) AS staged,
       {peek} AS peek
FROM event e JOIN clip c ON c.event_id = e.id
LEFT JOIN label l ON l.event_id = e.id
GROUP BY e.id
""".replace("{peek}", EVENT_IS_PEEK.format(event="e.id"))
EVENTS_WITH_SEVERAL_CLIPS = (
    "SELECT event_id FROM clip WHERE event_id IS NOT NULL"
    " GROUP BY event_id HAVING COUNT(*) > 1"
)


def serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Runs the method under the database's lock: one connection, many threads."""

    @wraps(method)
    def locked(*args: P.args, **kwargs: P.kwargs) -> R:
        db: Any = args[0]
        with db._lock:
            return method(*args, **kwargs)

    return locked


class Label(NamedTuple):
    value: str
    labeled_at: str
    source: str


class Staging(NamedTuple):
    """Why a labelled event is back in the review queue, since when, and by whom."""

    reason: str
    staged_at: str
    source: str


class DatabaseInUseError(RuntimeError):
    """Another process holds the database, and the operation renumbers events."""


class JoinRefusedError(ValueError):
    """A join the grouping could not honour."""


@dataclass(frozen=True)
class Clip:
    id: int
    path: str
    started_at: datetime.datetime | None
    ended_at: datetime.datetime | None
    close_reason: CloseReason
    size_bytes: int
    boundary: Boundary | None


@dataclass(frozen=True)
class Event:
    """One visit: a label and the clips it spans, oldest first."""

    id: int
    label: str | None
    clips: tuple[Clip, ...]
    # Poops found per box at a cleaning, from the reviewer's hand signal.
    poops: dict[int, int] = field(default_factory=dict)
    staged: Staging | None = None
    # A cat looked in and used no box: not a visit, though it keeps its label.
    peek: bool = False

    @property
    def started_at(self) -> datetime.datetime | None:
        return self.clips[0].started_at if self.clips else None

    @property
    def ended_at(self) -> datetime.datetime | None:
        return self.clips[-1].ended_at if self.clips else None


# Timestamps are stored as ISO text, which sorts in time order.


def _iso(when: datetime.datetime | None) -> str | None:
    return None if when is None else iso(when)


def _when(text: str | None) -> datetime.datetime | None:
    return None if text is None else datetime.datetime.fromisoformat(text)


def _boundary(text: str | None) -> Boundary | None:
    return None if text is None else Boundary(text)


def _clip_row(row: sqlite3.Row) -> ClipRow:
    started = datetime.datetime.fromisoformat(row["started_at"])
    ended = _when(row["ended_at"]) or started
    return ClipRow(
        path=row["path"],
        started_at=started,
        ended_at=ended,
        trigger=_centroid(row["trigger_cx"], row["trigger_cy"]),
        last=_centroid(row["last_cx"], row["last_cy"]),
        boundary=_boundary(row["boundary"]),
        joins=row["joins"],
    )


def _centroid(cx: int | None, cy: int | None) -> tuple[int, int] | None:
    return None if cx is None or cy is None else (int(cx), int(cy))


class CaptureDB:
    """Local-disk SQLite database of clips, events and labels.

    Media stays on the share; this holds paths and labels only. Every instance
    holds a shared lock on a file beside the database for as long as it is open,
    so an operation that renumbers events can tell when another process has it.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        gap_seconds: float,
        box_distance_px: float,
        hold_wait_seconds: float = HOLD_WAIT_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.gap_seconds = gap_seconds
        self.box_distance_px = box_distance_px
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._holder = self._hold(
            self.path.with_name(self.path.name + ".lock"), hold_wait_seconds
        )
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._refuse_old_layout()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @serialized
    def close(self) -> None:
        self._conn.close()
        self._holder.close()

    @staticmethod
    def _hold(lock_path: Path, wait_seconds: float) -> Any:
        """The lock file, opened and held shared. Waits for a regroup that holds
        it exclusively, and raises DatabaseInUseError if it outlasts the wait."""
        holder = lock_path.open("a")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(holder, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    holder.close()
                    raise DatabaseInUseError(
                        f"{lock_path} is still held by a regroup"
                    ) from None
                time.sleep(HOLD_POLL_SECONDS)
            else:
                return holder

    def _refuse_old_layout(self) -> None:
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(event)")
        }
        if "clip_path" in columns:
            raise RuntimeError(
                f"{self.path} predates event grouping; move it aside and re-ingest"
            )

    # --- ingest -------------------------------------------------------------

    def ingest(self, clip_directory: str | Path) -> int:
        """Register clips not yet known and place them in events.

        Existing events keep their ids. Returns how many clips were new. The
        share is read without the lock held: it can stall for minutes.
        """
        directory = Path(clip_directory)
        found = scan_clips(directory, skip=self._known_paths())
        added = self._register(found)
        if added:
            log.info("Ingested %d new clip(s) from %s", added, directory)
        return added

    @serialized
    def _known_paths(self) -> set[str]:
        """Clips already registered, whose sidecars a rescan must not re-read.

        Sidecars live on the CIFS share and ingest runs at every startup.
        """
        return {str(row["path"]) for row in self._conn.execute("SELECT path FROM clip")}

    @serialized
    def _register(self, found: list[FoundClip]) -> int:
        """Insert the clips and place any new ones in events."""
        now = datetime.datetime.now().isoformat()
        added = sum(self._insert_new(clip, now) for clip in found)
        if added:
            self._place_clips(keep_events=True, by_reviewer=False)
        self._conn.commit()
        return added

    def _insert_new(self, clip: FoundClip, now: str) -> int:
        """Register one clip. Returns 0 if it is already known."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO clip (path, started_at, ended_at, close_reason,"
            " trigger_cx, trigger_cy, last_cx, last_cy, size_bytes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(clip.path),
                _iso(clip.started_at),
                _iso(clip.ended_at),
                clip.close_reason.value,
                *(clip.trigger or (None, None)),
                *(clip.last or (None, None)),
                clip.size,
                now,
            ),
        )
        return cursor.rowcount

    # --- grouping -----------------------------------------------------------

    @serialized
    def regroup(self) -> None:
        """Rebuild every event from the rule. Event ids change; labels follow clips.

        Raises DatabaseInUseError if another process has the database open.
        """
        try:
            fcntl.flock(self._holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(self._holder, fcntl.LOCK_SH)  # a refused upgrade drops it
            raise DatabaseInUseError(
                f"{self.path} is open in another process; stop it before regrouping"
            ) from None
        try:
            self._place_clips(keep_events=False, by_reviewer=False)
            self._conn.commit()
        finally:
            fcntl.flock(self._holder, fcntl.LOCK_SH)

    # --- reviewer overrides -------------------------------------------------

    @serialized
    def split(self, clip_id: int) -> None:
        """A new visit starts at this clip. The new event starts unlabeled."""
        row = self._conn.execute(
            "SELECT event_id FROM clip WHERE id = ?", (clip_id,)
        ).fetchone()
        if row is None:
            return
        self._conn.execute(
            "UPDATE clip SET boundary = ?, joins = NULL WHERE id = ?",
            (Boundary.SPLIT.value, clip_id),
        )
        self._place_clips(keep_events=True, by_reviewer=True)
        moved = self._conn.execute(
            "SELECT event_id FROM clip WHERE id = ?", (clip_id,)
        ).fetchone()
        if moved["event_id"] != row["event_id"]:
            self._conn.execute(
                "DELETE FROM label WHERE event_id = ?", (moved["event_id"],)
            )
        self._conn.commit()

    @serialized
    def join(self, event_id: int) -> Event | None:
        """Merge this event with the next one in time. Returns the merged event,
        or None if there is no later event.

        Raises JoinRefusedError if either side has a clip with no start time.
        """
        ordered = self._event_ids_in_order()
        if event_id not in ordered or ordered.index(event_id) + 1 >= len(ordered):
            return None
        next_id = ordered[ordered.index(event_id) + 1]
        this, following = self.get(event_id), self.get(next_id)
        if this is None or following is None:
            return None
        joined = self._merge(this, following)
        self._conn.commit()
        return joined

    def _merge(self, this: Event, following: Event) -> Event:
        """Make each clip continue the one before it in time, then place them.

        The events' clips can interleave, and an override naming a later clip is
        never honoured, so the first clip of the later event is not enough.
        Raises JoinRefusedError, with nothing changed, if they cannot be grouped.
        """
        for event in (this, following):
            if event.started_at is None:
                raise JoinRefusedError(
                    f"#{event.id} has a clip with no start time, so it cannot be"
                    " grouped with another"
                )
        merged = sorted(
            (*this.clips, *following.clips), key=lambda c: (c.started_at, c.path)
        )
        self._conn.executemany(
            "UPDATE clip SET boundary = ?, joins = ? WHERE id = ?",
            [
                (Boundary.JOIN.value, earlier.path, clip.id)
                for earlier, clip in itertools.pairwise(merged)
            ],
        )
        self._place_clips(keep_events=True, by_reviewer=True)
        joined = self.get(min(this.id, following.id))
        if joined is None or len(joined.clips) != len(merged):
            self._conn.rollback()
            raise JoinRefusedError(
                f"#{this.id} and #{following.id} could not be grouped"
            )
        return joined

    @serialized
    def next_multi(self, after_id: int | None) -> Event | None:
        """The next multi-clip event in time order, for auditing the grouping.

        after_id need not still hold several clips: a split can leave it single,
        and the walk carries on from its place rather than starting over.
        """
        return self._next_in_order(after_id, self._multi_clip_event_ids())

    @serialized
    def next_labeled(self, after_id: int | None, value: str | None) -> Event | None:
        """The next labeled event in time order, or the next with this label."""
        return self._next_in_order(after_id, self._labeled_event_ids(value))

    def _next_in_order(self, after_id: int | None, wanted: set[int]) -> Event | None:
        """The first wanted event after after_id in time order, whether or not
        after_id is itself still wanted."""
        order = self._event_ids_in_order()
        if after_id in order:
            order = order[order.index(after_id) + 1 :]
        return next(
            (self.get(event_id) for event_id in order if event_id in wanted), None
        )

    def _labeled_event_ids(self, value: str | None) -> set[int]:
        query = "SELECT event_id FROM label"
        params: tuple[str, ...] = ()
        if value is not None:
            query += " WHERE value = ?"
            params = (value,)
        return {int(row["event_id"]) for row in self._conn.execute(query, params)}

    def _multi_clip_event_ids(self) -> set[int]:
        rows = self._conn.execute(EVENTS_WITH_SEVERAL_CLIPS)
        return {int(row["event_id"]) for row in rows}

    def _staged_event_ids(self) -> set[int]:
        return {int(row["event_id"]) for row in self._conn.execute(STAGED_EVENTS)}

    def _unlabeled_event_ids(self) -> set[int]:
        rows = self._conn.execute(
            "SELECT e.id FROM event e LEFT JOIN label l ON l.event_id = e.id"
            " WHERE l.event_id IS NULL"
        )
        return {int(row["id"]) for row in rows}

    def _event_ids_in_order(self) -> list[int]:
        return [int(row["id"]) for row in self._conn.execute(EVENTS_IN_ORDER)]

    def _place_clips(self, keep_events: bool, *, by_reviewer: bool) -> None:
        remembered = self._labels_by_clip()
        if not keep_events:
            self._conn.execute("UPDATE clip SET event_id = NULL")
        claimed: set[int] = set()
        for paths in self._grouped_paths():
            event_id = self._event_for(paths, claimed)
            claimed.add(event_id)
            self._conn.executemany(
                "UPDATE clip SET event_id = ? WHERE path = ?",
                [(event_id, path) for path in paths],
            )
        self._drop_empty_events()
        self._relabel(remembered)
        self._settle_peeks(stage=not by_reviewer)

    def _settle_peeks(self, *, stage: bool) -> None:
        """Clear the mark from events that gained an unmarked clip, so a later
        split cannot revive it; staged unless the reviewer made the change."""
        partly = [
            int(row["event_id"]) for row in self._conn.execute(EVENTS_PARTLY_PEEK)
        ]
        for event_id in partly:
            clip_ids = self._clip_ids(event_id)
            self._conn.execute(
                f"DELETE FROM peek WHERE clip_id IN ({self._marks(clip_ids)})",  # noqa: S608 -- placeholders, not values
                clip_ids,
            )
            if stage and event_id in self._labeled_event_ids(None):
                self._stage(event_id, Staging(PEEK_GREW, self._now(), "grouping"))

    def _grouped_paths(self) -> list[list[str]]:
        """Clip paths grouped by the rule. Clips with no start time stand alone."""
        rows = self._conn.execute(
            "SELECT path, started_at, ended_at, trigger_cx, trigger_cy,"
            " last_cx, last_cy, boundary, joins FROM clip"
        ).fetchall()
        datable = [_clip_row(row) for row in rows if row["started_at"] is not None]
        groups = group_clips(datable, self.gap_seconds, self.box_distance_px)
        paths = [[clip.path for clip in group] for group in groups]
        paths += [[row["path"]] for row in rows if row["started_at"] is None]
        return paths

    def _event_for(self, paths: list[str], claimed: set[int]) -> int:
        """The lowest unclaimed event id already among these clips, or a new event.

        An id can go to one group per pass, so the older half of a split keeps it.
        """
        ids = {
            row["event_id"]
            for path in paths
            for row in self._conn.execute(
                "SELECT event_id FROM clip WHERE path = ?", (path,)
            )
        }
        ids.discard(None)
        ids -= claimed
        if ids:
            return int(min(ids))
        cursor = self._conn.execute(
            "INSERT INTO event (created_at) VALUES (?)",
            (datetime.datetime.now().isoformat(),),
        )
        return int(cursor.lastrowid or 0)

    def _drop_empty_events(self) -> None:
        live = "SELECT event_id FROM clip WHERE event_id IS NOT NULL"
        self._conn.execute(f"DELETE FROM label WHERE event_id NOT IN ({live})")  # noqa: S608 -- no user input
        self._conn.execute(f"DELETE FROM event WHERE id NOT IN ({live})")  # noqa: S608 -- no user input

    def _labels_by_clip(self) -> dict[str, Label]:
        rows = self._conn.execute(
            "SELECT c.path, l.value, l.labeled_at, l.source"
            " FROM clip c JOIN label l ON l.event_id = c.event_id"
        )
        return {
            row["path"]: Label(row["value"], row["labeled_at"], row["source"])
            for row in rows
        }

    def _relabel(self, remembered: dict[str, Label]) -> None:
        """Keep a label where an event's clips agree; drop it where they clash."""
        for event_id, paths in self._paths_by_event().items():
            labels = {remembered[path] for path in paths if path in remembered}
            values = {label.value for label in labels}
            if len(values) == 1:
                earliest = min(labels, key=lambda label: label.labeled_at)
                self._write_label(event_id, earliest)
            elif len(values) > 1:
                self._conn.execute("DELETE FROM label WHERE event_id = ?", (event_id,))
                log.warning(
                    "Event %d now holds clips labeled %s; back in the queue",
                    event_id,
                    sorted(values),
                )

    def _paths_by_event(self) -> dict[int, list[str]]:
        result: dict[int, list[str]] = {}
        for row in self._conn.execute(
            "SELECT event_id, path FROM clip WHERE event_id IS NOT NULL"
        ):
            result.setdefault(int(row["event_id"]), []).append(row["path"])
        return result

    def _write_label(self, event_id: int, label: Label) -> None:
        self._conn.execute(
            "INSERT INTO label (event_id, value, labeled_at, source)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET"
            " value = excluded.value, labeled_at = excluded.labeled_at,"
            " source = excluded.source",
            (event_id, *label),
        )

    # --- review -------------------------------------------------------------

    @serialized
    def next_to_review(self, after_id: int | None = None) -> Event | None:
        """The next event in the review queue, in time order; the oldest by default.

        The queue is every event with no label yet and every staged one.
        """
        return self._next_in_order(after_id, self._queued_event_ids())

    def _queued_event_ids(self) -> set[int]:
        return self._unlabeled_event_ids() | self._staged_event_ids()

    @serialized
    def clips_ahead(self, events: int) -> list[Clip]:
        """The clips of the first few events in the review queue, in order."""
        queued = self._queued_event_ids()
        order = [
            event_id for event_id in self._event_ids_in_order() if event_id in queued
        ]
        clips: list[Clip] = []
        for event_id in order[:events]:
            event = self.get(event_id)
            if event is not None:
                clips.extend(event.clips)
        return clips

    @serialized
    def clip(self, clip_id: int) -> Clip | None:
        row = self._conn.execute(
            "SELECT * FROM clip WHERE id = ?", (clip_id,)
        ).fetchone()
        return None if row is None else self._to_clip(row)

    @serialized
    def get(self, event_id: int) -> Event | None:
        row = self._conn.execute(
            "SELECT e.id, l.value AS label FROM event e"
            " LEFT JOIN label l ON l.event_id = e.id WHERE e.id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        rows = self._conn.execute(
            "SELECT * FROM clip WHERE event_id = ?"
            " ORDER BY started_at IS NULL, started_at, id",
            (event_id,),
        )
        clips = tuple(self._to_clip(row) for row in rows)
        clip_ids = [clip.id for clip in clips]
        return Event(
            id=int(row["id"]),
            label=row["label"],
            clips=clips,
            poops=self._poop_counts(clip_ids),
            staged=self._staging(clip_ids),
            peek=self._is_peek(event_id),
        )

    @serialized
    def set_label(self, event_id: int, value: str, source: str = "human") -> None:
        """Write or overwrite one event's label, which takes it out of the queue."""
        self._write_label(
            event_id, Label(value, datetime.datetime.now().isoformat(), source)
        )
        self._unstage(event_id)
        self._conn.commit()

    @serialized
    def stage(self, event_id: int, reason: str, source: str) -> bool:
        """Put an event back in the review queue, keeping its label.

        Stored against the event's first clip. Returns False if there is no
        such event.
        """
        staged = self._stage(event_id, Staging(reason, self._now(), source))
        self._conn.commit()
        return staged

    @serialized
    def stage_labeled(
        self,
        reason: str,
        source: str,
        *,
        value: str | None = None,
        labeled_before: datetime.datetime | None = None,
    ) -> int:
        """Stage every labelled event with this value, labelled before this time.
        Returns how many were staged."""
        query = "SELECT event_id FROM label WHERE 1 = 1"
        params: list[str] = []
        if value is not None:
            query += " AND value = ?"
            params.append(value)
        if labeled_before is not None:
            query += " AND labeled_at < ?"
            params.append(labeled_before.isoformat())
        event_ids = [int(row["event_id"]) for row in self._conn.execute(query, params)]
        return self.stage_events(event_ids, reason, source)

    @serialized
    def stage_events(self, event_ids: list[int], reason: str, source: str) -> int:
        """Stage those of these events that are labelled and not staged already,
        so no earlier staging is overwritten. Returns how many were staged."""
        wanted = set(event_ids) & self._labeled_event_ids(None)
        wanted -= self._staged_event_ids()
        staging = Staging(reason, self._now(), source)
        staged = sum(self._stage(event_id, staging) for event_id in sorted(wanted))
        self._conn.commit()
        return staged

    @serialized
    def timeline_rows(self) -> list[TimelineRow]:
        """Every event as one timeline row, in no particular order."""
        return [
            TimelineRow(
                id=int(row["id"]),
                started_at=_when(row["started_at"]),
                ended_at=_when(row["ended_at"]),
                clips=int(row["clips"]),
                label=row["label"],
                labeled_at=_when(row["labeled_at"]),
                trigger_cx=row["trigger_cx"],
                staged=bool(row["staged"]),
                peek=bool(row["peek"]),
            )
            for row in self._conn.execute(TIMELINE_ROWS)
        ]

    def _stage(self, event_id: int, staging: Staging) -> bool:
        clip_ids = self._clip_ids(event_id)
        if not clip_ids:
            return False
        self._unstage(event_id)
        self._conn.execute(
            "INSERT INTO staged (clip_id, reason, staged_at, source)"
            " VALUES (?, ?, ?, ?)",
            (clip_ids[0], *staging),
        )
        return True

    def _unstage(self, event_id: int) -> None:
        clip_ids = self._clip_ids(event_id)
        self._conn.execute(
            f"DELETE FROM staged WHERE clip_id IN ({self._marks(clip_ids)})",  # noqa: S608 -- placeholders, not values
            clip_ids,
        )

    def _clip_ids(self, event_id: int) -> list[int]:
        """The event's clips, oldest first."""
        rows = self._conn.execute(
            "SELECT id FROM clip WHERE event_id = ?"
            " ORDER BY started_at IS NULL, started_at, id",
            (event_id,),
        )
        return [int(row["id"]) for row in rows]

    def _staging(self, clip_ids: list[int]) -> Staging | None:
        """The event's staging, from whichever of its clips carries it; the
        earliest if a merge brought two."""
        marks = self._marks(clip_ids)
        row = self._conn.execute(
            f"SELECT reason, staged_at, source FROM staged WHERE clip_id IN ({marks})"  # noqa: S608 -- placeholders, not values
            " ORDER BY staged_at LIMIT 1",
            clip_ids,
        ).fetchone()
        return None if row is None else Staging(*row)

    @serialized
    def set_peek(self, event_id: int, peek: bool) -> bool:
        """Mark an event as a peek, or clear the mark. Returns False if there is
        no such event.

        Written on every clip, so the event is a peek only while all its clips are.
        """
        clip_ids = self._clip_ids(event_id)
        if not clip_ids:
            return False
        if peek:
            noted_at = self._now()
            self._conn.executemany(
                "INSERT OR IGNORE INTO peek (clip_id, noted_at) VALUES (?, ?)",
                [(clip_id, noted_at) for clip_id in clip_ids],
            )
        else:
            self._conn.execute(
                f"DELETE FROM peek WHERE clip_id IN ({self._marks(clip_ids)})",  # noqa: S608 -- placeholders, not values
                clip_ids,
            )
        self._conn.commit()
        return True

    def _is_peek(self, event_id: int) -> bool:
        if not self._clip_ids(event_id):
            return False
        query = f"SELECT {EVENT_IS_PEEK.format(event='?')} AS peek"
        row = self._conn.execute(query, (event_id,)).fetchone()
        return bool(row["peek"])

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now().isoformat()

    @serialized
    def set_poop_counts(self, event_id: int, counts: dict[int, int]) -> None:
        """Record poops found per box at a cleaning, replacing any earlier count.

        Stored against the event's first clip: clip ids never change, while a
        regroup renumbers events.
        """
        event = self.get(event_id)
        if event is None or not event.clips:
            return
        clip_ids = [clip.id for clip in event.clips]
        self._conn.execute(
            f"DELETE FROM poop_count WHERE clip_id IN ({self._marks(clip_ids)})",  # noqa: S608 -- placeholders, not values
            clip_ids,
        )
        noted_at = datetime.datetime.now().isoformat()
        self._conn.executemany(
            "INSERT INTO poop_count (clip_id, box, count, noted_at)"
            " VALUES (?, ?, ?, ?)",
            [(clip_ids[0], box, count, noted_at) for box, count in counts.items()],
        )
        self._conn.commit()

    def _poop_counts(self, clip_ids: list[int]) -> dict[int, int]:
        """Poops per box for an event, from whichever of its clips carries them."""
        if not clip_ids:
            return {}
        marks = self._marks(clip_ids)
        rows = self._conn.execute(
            f"SELECT box, count FROM poop_count WHERE clip_id IN ({marks})",  # noqa: S608 -- placeholders, not values
            clip_ids,
        )
        return {int(row["box"]): int(row["count"]) for row in rows}

    @staticmethod
    def _marks(values: list[int]) -> str:
        return ",".join("?" * len(values))

    @serialized
    def counts(self) -> dict[str, int]:
        """Totals for the progress line: events, labeled, multi-clip, staged
        (when any are), per label."""
        result = {"total": 0, "labeled": 0, "multi_clip": 0}
        row = self._conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()
        result["total"] = int(row["n"])
        result["multi_clip"] = len(self._multi_clip_event_ids())
        staged = len(self._staged_event_ids())
        if staged:
            result["staged"] = staged
        for value_row in self._conn.execute(
            "SELECT value, COUNT(*) AS n FROM label GROUP BY value"
        ):
            result[str(value_row["value"])] = int(value_row["n"])
            result["labeled"] += int(value_row["n"])
        return result

    @staticmethod
    def _to_clip(row: sqlite3.Row) -> Clip:
        return Clip(
            id=int(row["id"]),
            path=str(row["path"]),
            started_at=_when(row["started_at"]),
            ended_at=_when(row["ended_at"]),
            close_reason=CloseReason(row["close_reason"]),
            size_bytes=int(row["size_bytes"] or 0),
            boundary=_boundary(row["boundary"]),
        )
