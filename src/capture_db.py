"""SQLite store for clips, the events that group them, and review labels."""

import datetime
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, NamedTuple

from config import Config
from src.clip_scan import FoundClip, scan_clips
from src.event_grouping import ClipRow, group_clips

log = logging.getLogger(__name__)

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
"""

# Events oldest first; those with no dated clip last.
EVENTS_IN_ORDER = (
    "SELECT e.id FROM event e JOIN clip c ON c.event_id = e.id GROUP BY e.id"
    " ORDER BY MIN(c.started_at) IS NULL, MIN(c.started_at), e.id"
)
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


@dataclass(frozen=True)
class Clip:
    id: int
    path: str
    started_at: str | None
    ended_at: str | None
    close_reason: str
    size_bytes: int
    boundary: str | None


@dataclass(frozen=True)
class Event:
    """One visit: a label and the clips it spans, oldest first."""

    id: int
    label: str | None
    clips: tuple[Clip, ...]
    # Poops found per box at a cleaning, from the reviewer's hand signal.
    poops: dict[int, int] = field(default_factory=dict)

    @property
    def started_at(self) -> str | None:
        return self.clips[0].started_at if self.clips else None

    @property
    def ended_at(self) -> str | None:
        return self.clips[-1].ended_at if self.clips else None


def _clip_row(row: sqlite3.Row) -> ClipRow:
    started = datetime.datetime.fromisoformat(row["started_at"])
    ended = (
        datetime.datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else started
    )
    return ClipRow(
        path=row["path"],
        started_at=started,
        ended_at=ended,
        trigger=_centroid(row["trigger_cx"], row["trigger_cy"]),
        last=_centroid(row["last_cx"], row["last_cy"]),
        boundary=row["boundary"],
        joins=row["joins"],
    )


def _centroid(cx: int | None, cy: int | None) -> tuple[int, int] | None:
    return None if cx is None or cy is None else (int(cx), int(cy))


class CaptureDB:
    """Local-disk SQLite database of clips, events and labels.

    Media stays on the share; this holds paths and labels only. Not safe for
    concurrent writers.
    """

    def __init__(
        self,
        path: str | Path,
        gap_seconds: float | None = None,
        box_distance_px: float | None = None,
    ) -> None:
        self.path = Path(path)
        self.gap_seconds = (
            Config.EVENT_GAP_SECONDS if gap_seconds is None else gap_seconds
        )
        self.box_distance_px = (
            Config.EVENT_BOX_DISTANCE_PX if box_distance_px is None else box_distance_px
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._refuse_old_layout()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @serialized
    def close(self) -> None:
        self._conn.close()

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
            self._place_clips(keep_events=True)
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
                clip.started_at,
                clip.ended_at,
                clip.close_reason,
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
        """Rebuild every event from the rule. Event ids change; labels follow clips."""
        self._place_clips(keep_events=False)
        self._conn.commit()

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
            "UPDATE clip SET boundary = 'split', joins = NULL WHERE id = ?", (clip_id,)
        )
        self._place_clips(keep_events=True)
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
        """Merge this event with the next one in time. Returns the merged event."""
        ordered = self._event_ids_in_order()
        if event_id not in ordered or ordered.index(event_id) + 1 >= len(ordered):
            return None
        next_id = ordered[ordered.index(event_id) + 1]
        this, following = self.get(event_id), self.get(next_id)
        if this is None or following is None:
            return None
        self._conn.execute(
            "UPDATE clip SET boundary = 'join', joins = ? WHERE id = ?",
            (this.clips[-1].path, following.clips[0].id),
        )
        self._place_clips(keep_events=True)
        self._conn.commit()
        return self.get(min(event_id, next_id))

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

    def _unlabeled_event_ids(self) -> set[int]:
        rows = self._conn.execute(
            "SELECT e.id FROM event e LEFT JOIN label l ON l.event_id = e.id"
            " WHERE l.event_id IS NULL"
        )
        return {int(row["id"]) for row in rows}

    def _event_ids_in_order(self) -> list[int]:
        return [int(row["id"]) for row in self._conn.execute(EVENTS_IN_ORDER)]

    def _place_clips(self, keep_events: bool) -> None:
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
    def next_unlabeled(self) -> Event | None:
        """Oldest event with no label yet."""
        return self._next_in_order(None, self._unlabeled_event_ids())

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
        return Event(
            id=int(row["id"]),
            label=row["label"],
            clips=clips,
            poops=self._poop_counts([clip.id for clip in clips]),
        )

    @serialized
    def set_label(self, event_id: int, value: str, source: str = "human") -> None:
        """Write or overwrite the label for one event."""
        self._write_label(
            event_id, Label(value, datetime.datetime.now().isoformat(), source)
        )
        self._conn.commit()

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
        """Totals for the progress line: events, labeled, multi-clip, per label."""
        result = {"total": 0, "labeled": 0, "multi_clip": 0}
        row = self._conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()
        result["total"] = int(row["n"])
        result["multi_clip"] = len(self._multi_clip_event_ids())
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
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            close_reason=str(row["close_reason"]),
            size_bytes=int(row["size_bytes"] or 0),
            boundary=row["boundary"],
        )
