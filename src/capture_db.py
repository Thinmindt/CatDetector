"""SQLite store for capture events and their review labels."""

import datetime
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CLIP_TIMESTAMP = re.compile(r"_(\d{8})_(\d{6})(?:_\d+)?\.h264$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY,
    clip_path  TEXT NOT NULL UNIQUE,
    started_at TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS label (
    event_id   INTEGER PRIMARY KEY REFERENCES event(id),
    value      TEXT NOT NULL,
    labeled_at TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'human'
);
"""


@dataclass(frozen=True)
class Event:
    id: int
    clip_path: str
    started_at: str | None
    size_bytes: int
    label: str | None


def _clip_started_at(name: str) -> str | None:
    """ISO timestamp parsed from a clip filename, or None if it has none."""
    match = CLIP_TIMESTAMP.search(name)
    if match is None:
        return None
    try:
        stamp = datetime.datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.isoformat()


class CaptureDB:
    """Local-disk SQLite database of clips and labels.

    Media stays on the share; this holds paths and labels only. Not safe for
    concurrent writers.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def ingest(self, clip_directory: str | Path) -> int:
        """Register clips not yet in the database. Returns how many were new."""
        directory = Path(clip_directory)
        if not directory.is_dir():
            log.warning("Clip directory %s does not exist; nothing ingested", directory)
            return 0

        now = datetime.datetime.now().isoformat()
        added = 0
        for clip in sorted(directory.glob("*.h264")):
            added += self._insert_new(clip, now)
        self._conn.commit()
        if added:
            log.info("Ingested %d new clip(s) from %s", added, directory)
        return added

    def _insert_new(self, clip: Path, now: str) -> int:
        """Register one clip. Returns 0 if it is already known or unreadable."""
        try:
            size = clip.stat().st_size
        except OSError as error:
            log.warning("Skipping %s: %s", clip, error)
            return 0

        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO event"
            " (clip_path, started_at, size_bytes, created_at) VALUES (?, ?, ?, ?)",
            (str(clip), _clip_started_at(clip.name), size, now),
        )
        return cursor.rowcount

    def next_unlabeled(self) -> Event | None:
        """Oldest event with no label yet."""
        row = self._conn.execute(
            "SELECT e.*, l.value AS label FROM event e"
            " LEFT JOIN label l ON l.event_id = e.id"
            " WHERE l.event_id IS NULL"
            " ORDER BY e.started_at IS NULL, e.started_at, e.id LIMIT 1"
        ).fetchone()
        return self._to_event(row)

    def get(self, event_id: int) -> Event | None:
        row = self._conn.execute(
            "SELECT e.*, l.value AS label FROM event e"
            " LEFT JOIN label l ON l.event_id = e.id WHERE e.id = ?",
            (event_id,),
        ).fetchone()
        return self._to_event(row)

    def set_label(self, event_id: int, value: str, source: str = "human") -> None:
        """Write or overwrite the label for one event."""
        self._conn.execute(
            "INSERT INTO label (event_id, value, labeled_at, source)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET"
            " value = excluded.value, labeled_at = excluded.labeled_at,"
            " source = excluded.source",
            (event_id, value, datetime.datetime.now().isoformat(), source),
        )
        self._conn.commit()

    def counts(self) -> dict[str, int]:
        """Totals for the progress line: overall, labeled, and per label value."""
        result = {"total": 0, "labeled": 0}
        row = self._conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()
        result["total"] = int(row["n"])
        for value_row in self._conn.execute(
            "SELECT value, COUNT(*) AS n FROM label GROUP BY value"
        ):
            result[str(value_row["value"])] = int(value_row["n"])
            result["labeled"] += int(value_row["n"])
        return result

    @staticmethod
    def _to_event(row: sqlite3.Row | None) -> Event | None:
        if row is None:
            return None
        return Event(
            id=int(row["id"]),
            clip_path=str(row["clip_path"]),
            started_at=row["started_at"],
            size_bytes=int(row["size_bytes"] or 0),
            label=row["label"],
        )
