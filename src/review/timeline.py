"""The timeline: every event as one row, filtered, with counts for the filter strips."""

import datetime
from collections import Counter
from dataclasses import dataclass, field

from src.clips.format import ANALYSIS_SIZE

# Litter boxes by position in frame, left to right; the camera knows no more.
BOXES = (1, 2, 3)

HOURS_IN_DAY = 24

# The chip for events with no label yet.
UNLABELED = "unlabeled"

DATE = "date"
HOURS = "hours"


@dataclass(frozen=True)
class TimelineRow:
    id: int
    started_at: datetime.datetime | None
    ended_at: datetime.datetime | None
    clips: int
    label: str | None
    labeled_at: datetime.datetime | None
    trigger_cx: int | None
    staged: bool

    @property
    def box(self) -> int | None:
        """The box by position in frame, 1 = leftmost: the third holding the trigger."""
        if self.trigger_cx is None:
            return None
        width = ANALYSIS_SIZE[0]
        return min(len(BOXES), 1 + self.trigger_cx * len(BOXES) // width)

    @property
    def seconds(self) -> int | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at).total_seconds())

    @property
    def chip(self) -> str:
        return self.label or UNLABELED

    @property
    def stageable(self) -> bool:
        """Labelled and not already staged: what "back for review" may take."""
        return self.label is not None and not self.staged


@dataclass(frozen=True)
class TimelineFilter:
    """What the timeline shows. Every field left as None lets everything through.

    Days are inclusive. Hours are [first, last) and wrap midnight when first is
    the larger, so (22, 6) is the night. A date or hour filter drops events with
    no start time.
    """

    chips: frozenset[str] | None = None
    first_day: datetime.date | None = None
    last_day: datetime.date | None = None
    hours: tuple[int, int] | None = None
    labeled_before: datetime.datetime | None = None

    def admits(self, row: TimelineRow, ignoring: str | None = None) -> bool:
        """Whether the row passes, leaving out one filter (DATE or HOURS) if named."""
        if self.chips is not None and row.chip not in self.chips:
            return False
        if self.labeled_before is not None and not (
            row.labeled_at is not None and row.labeled_at < self.labeled_before
        ):
            return False
        if ignoring != DATE and not self._admits_day(row.started_at):
            return False
        return ignoring == HOURS or self._admits_hour(row.started_at)

    def _admits_day(self, started: datetime.datetime | None) -> bool:
        if self.first_day is None and self.last_day is None:
            return True
        if started is None:
            return False
        day = started.date()
        after_first = self.first_day is None or day >= self.first_day
        return after_first and (self.last_day is None or day <= self.last_day)

    def _admits_hour(self, started: datetime.datetime | None) -> bool:
        if self.hours is None:
            return True
        if started is None:
            return False
        first, last = self.hours
        if first <= last:
            return first <= started.hour < last
        return started.hour >= first or started.hour < last


@dataclass(frozen=True)
class Timeline:
    """The rows the filter admits, newest first, and the counts behind each strip.

    Each strip counts under every filter but its own, so it shows what its
    brush can select.
    """

    rows: list[TimelineRow]
    by_day: dict[datetime.date, Counter[str]] = field(default_factory=dict)
    by_hour: list[Counter[str]] = field(default_factory=list)
    first_day: datetime.date | None = None
    last_day: datetime.date | None = None


def build_timeline(rows: list[TimelineRow], chosen: TimelineFilter) -> Timeline:
    shown = _newest_first([row for row in rows if chosen.admits(row)])
    by_day: dict[datetime.date, Counter[str]] = {}
    by_hour = [Counter[str]() for _ in range(HOURS_IN_DAY)]
    for row in rows:
        if row.started_at is None:
            continue
        if chosen.admits(row, ignoring=DATE):
            by_day.setdefault(row.started_at.date(), Counter())[row.chip] += 1
        if chosen.admits(row, ignoring=HOURS):
            by_hour[row.started_at.hour][row.chip] += 1
    days = [row.started_at.date() for row in rows if row.started_at is not None]
    return Timeline(
        rows=shown,
        by_day=by_day,
        by_hour=by_hour,
        first_day=min(days, default=None),
        last_day=max(days, default=None),
    )


def _newest_first(rows: list[TimelineRow]) -> list[TimelineRow]:
    """Newest first by start; rows with no start time last."""
    dated = [(row.started_at, row) for row in rows if row.started_at is not None]
    dated.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
    undated = sorted(
        (row for row in rows if row.started_at is None),
        key=lambda row: row.id,
        reverse=True,
    )
    return [row for _, row in dated] + undated
