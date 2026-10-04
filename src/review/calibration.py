"""A data run's progress, and the trigger settings it supports (roadmap A.2).

Reads the review database's events and the detector's metrics CSV, which is the
record of what the camera saw; nothing here touches the camera.
"""

import bisect
import datetime
import itertools
import math
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from src.clips.format import ANALYSIS_SIZE, CAMERA_FPS
from src.clips.sidecar import iso
from src.review.timeline import BOXES, TimelineRow

# The run is done when every condition holds (ROADMAP A.2).
VISITS_NEEDED = 50
VISITS_PREFERRED = 100
NIGHT_VISITS_NEEDED = 10
VISITS_PER_CAT_NEEDED = 15
STABLE_VISITS = 20
NIGHT_FROM_HOUR = 20
NIGHT_UNTIL_HOUR = 6

# A lower edge sits at this share of the weakest visit's peak; the band's upper
# edge this far above the strongest, and never above half the frame.
LOWER_MARGIN = 0.5
UPPER_MARGIN = 1.25
FRAME_AREA = ANALYSIS_SIZE[0] * ANALYSIS_SIZE[1]
# A.3's planned persistence: frames in the band before a clip starts.
PERSIST_FRAMES = 2

WEAKEST_TO_ASK = 3
# A not_cat event is asked about when it held a blob the size of a typical
# visit (from the weakest visit's peak to this share of the visits' peaks) for
# as long as all but this share of the visits did.
TYPICAL_PEAK_SHARE = 0.95
SHORTEST_VISITS_SHARE = 0.10

# The metrics CSV's columns this reads, by the header motion_metrics writes.
COLUMNS = ("timestamp", "foreground_px", "clean_area", "brightness")

# A frame of the CSV: its time as ISO text, foreground pixels, cleaned largest
# blob, brightness.
Frame = tuple[str, int, int, float]


def read_frames(lines: Iterable[str]) -> Iterator[Frame]:
    """The metrics CSV's rows, the columns found by its header."""
    rows = iter(lines)
    header = next(rows, "").strip().split(",")
    missing = [name for name in COLUMNS if name not in header]
    if missing:
        raise ValueError(f"not a metrics CSV: no {', '.join(missing)} column")
    at_time, at_fg, at_clean, at_bright = (header.index(name) for name in COLUMNS)
    for line in rows:
        cells = line.rstrip("\n").split(",")
        yield (
            cells[at_time],
            int(cells[at_fg] or 0),
            int(cells[at_clean] or 0),
            float(cells[at_bright] or 0),
        )


@dataclass(frozen=True)
class Window:
    """One event's span, as ISO text, which sorts in time order like the CSV's."""

    event_id: int
    start: str
    end: str


class WindowIndex:
    """The events whose span holds a given time. Spans may overlap, and the
    CSV's clock may step backwards, so every lookup is independent."""

    def __init__(self, windows: Iterable[Window]) -> None:
        self.windows = sorted(windows, key=lambda w: w.start)
        self.starts = [w.start for w in self.windows]
        self.reach: list[str] = []  # the latest end among windows up to each
        for window in self.windows:
            latest = self.reach[-1] if self.reach else window.end
            self.reach.append(max(latest, window.end))

    def holding(self, time: str) -> list[int]:
        found: list[int] = []
        index = bisect.bisect_right(self.starts, time) - 1
        while index >= 0 and self.reach[index] >= time:
            if self.windows[index].end >= time:
                found.append(self.windows[index].event_id)
            index -= 1
        return found


@dataclass
class Peaks:
    """What the CSV saw during one event, over its lit frames."""

    rows: int = 0
    foreground: int = 0
    clean: int = 0
    cleans: list[int] = field(default_factory=list)

    def add(self, foreground: int, clean: int) -> None:
        self.rows += 1
        self.foreground = max(self.foreground, foreground)
        self.clean = max(self.clean, clean)
        self.cleans.append(clean)


def scan_peaks(
    frames: Iterable[Frame], windows: WindowIndex, dark_brightness: float
) -> dict[int, Peaks]:
    """Each event's largest foreground count and cleaned blob, over lit frames."""
    peaks: dict[int, Peaks] = {}
    for time, foreground, clean, brightness in frames:
        if brightness < dark_brightness:
            continue
        for event_id in windows.holding(time):
            peaks.setdefault(event_id, Peaks()).add(foreground, clean)
    return peaks


# --- the visits and the conditions ------------------------------------------


@dataclass(frozen=True)
class Visit:
    row: TimelineRow
    peaks: Peaks

    @property
    def night(self) -> bool:
        hour = self.row.started_at.hour if self.row.started_at else 12
        return hour >= NIGHT_FROM_HOUR or hour < NIGHT_UNTIL_HOUR


@dataclass(frozen=True)
class Condition:
    name: str
    now: str
    met: bool | None  # None: the reviewer's call


def window_of(row: TimelineRow) -> Window | None:
    if row.started_at is None or row.ended_at is None:
        return None
    return Window(row.id, iso(row.started_at), iso(row.ended_at))


def conditions(visits: list[Visit], cats: tuple[str, ...]) -> list[Condition]:
    """The table of ROADMAP A.2, counted from these visits, oldest first."""
    return [
        Condition(
            f"{VISITS_NEEDED}+ visits, {VISITS_PREFERRED} preferred",
            str(len(visits)),
            len(visits) >= VISITS_NEEDED,
        ),
        Condition(
            f"{NIGHT_VISITS_NEEDED}+ at night"
            f" ({NIGHT_FROM_HOUR}:00 to {NIGHT_UNTIL_HOUR}:00)",
            str(sum(visit.night for visit in visits)),
            sum(visit.night for visit in visits) >= NIGHT_VISITS_NEEDED,
        ),
        per_cat(visits, cats),
        every_box(visits),
        Condition("several days of varied weather, one overcast", "your call", None),
        weakest_settled(visits),
    ]


def per_cat(visits: list[Visit], cats: tuple[str, ...]) -> Condition:
    name = f"{VISITS_PER_CAT_NEEDED}+ per cat"
    if not cats:
        return Condition(name, "no CAT_NAMES, so not counted", None)
    counts = Counter(visit.row.label for visit in visits)
    now = ", ".join(f"{cat} {counts[cat]}" for cat in cats)
    return Condition(
        name, now, all(counts[cat] >= VISITS_PER_CAT_NEEDED for cat in cats)
    )


def every_box(visits: list[Visit]) -> Condition:
    counts = Counter(visit.row.box for visit in visits)
    now = ", ".join(f"box {box}: {counts[box]}" for box in BOXES)
    return Condition("every box used", now, all(counts[box] for box in BOXES))


def weakest_settled(visits: list[Visit]) -> Condition:
    """No visit among the last few is weaker than the weakest before them."""
    name = f"weakest visit no weaker over the last {STABLE_VISITS}"
    strengths = [visit.peaks.clean for visit in visits]
    if len(strengths) <= STABLE_VISITS:
        return Condition(name, f"needs more than {STABLE_VISITS} visits", False)
    before = min(strengths[:-STABLE_VISITS])
    recent = min(strengths[-STABLE_VISITS:])
    now = f"weakest {before} px; the last {STABLE_VISITS}: {recent} px"
    return Condition(name, now, recent >= before)


# --- the settings and their replay ------------------------------------------


@dataclass(frozen=True)
class Rule:
    """A trigger to replay: a frame counts when its value is in [low, high], and
    a clip starts after `persist` such frames in a row."""

    name: str
    value: Callable[[Frame], int]
    low: int
    high: int
    persist: int

    def counts(self, frame: Frame) -> bool:
        return self.low <= self.value(frame) <= self.high


def threshold_rule(threshold: int) -> Rule:
    """The detector's test today: foreground_px > MOTION_THRESHOLD."""
    return Rule(
        "MOTION_THRESHOLD", lambda frame: frame[1], threshold + 1, FRAME_AREA, 1
    )


def propose(visits: list[Visit]) -> tuple[Rule, Rule]:
    """A global-count threshold for today's detector, and A.3's band on the
    cleaned blob."""
    weakest_fg = min(visit.peaks.foreground for visit in visits)
    weakest = min(visit.peaks.clean for visit in visits)
    strongest = max(visit.peaks.clean for visit in visits)
    band = Rule(
        "A.3 band",
        lambda frame: frame[2],
        math.floor(weakest * LOWER_MARGIN),
        min(math.ceil(strongest * UPPER_MARGIN), FRAME_AREA // 2),
        PERSIST_FRAMES,
    )
    return threshold_rule(math.floor(weakest_fg * LOWER_MARGIN)), band


@dataclass
class Replay:
    """How a rule would have done over the CSV: the visits it caught, and the
    clips it would have started outside every visit."""

    rule: Rule
    timeout: datetime.timedelta
    caught: set[int] = field(default_factory=set)
    false_triggers: int = 0
    streak: int = 0
    last_trigger: datetime.datetime | None = None

    def feed(self, frame: Frame, visits_here: list[int]) -> None:
        self.streak = self.streak + 1 if self.rule.counts(frame) else 0
        if self.streak < self.rule.persist:
            return
        self.caught.update(visits_here)
        when = datetime.datetime.fromisoformat(frame[0])
        if not self._recording(when):
            self.false_triggers += not visits_here
        self.last_trigger = when

    def _recording(self, when: datetime.datetime) -> bool:
        last = self.last_trigger
        return last is not None and datetime.timedelta(0) <= when - last <= self.timeout


@dataclass
class ReplayResult:
    replays: list[Replay]
    lit_frames: int

    @property
    def lit_hours(self) -> float:
        return self.lit_frames / CAMERA_FPS / 3600


def replay(
    frames: Iterable[Frame],
    rules: Iterable[Rule],
    visits: WindowIndex,
    dark_brightness: float,
    timeout: datetime.timedelta,
) -> ReplayResult:
    """Run each rule over every lit frame, as the detector would have."""
    replays = [Replay(rule, timeout) for rule in rules]
    lit = 0
    for frame in frames:
        if frame[3] < dark_brightness:
            for each in replays:
                each.streak = 0
            continue
        lit += 1
        here = (
            visits.holding(frame[0])
            if any(r.rule.counts(frame) for r in replays)
            else []
        )
        for each in replays:
            each.feed(frame, here)
    return ReplayResult(replays, lit)


# --- what needs a person's eye ----------------------------------------------


@dataclass(frozen=True)
class Doubt:
    """An event to put back in the review queue, once, with this question."""

    event_id: int
    question: str  # a key, so the same doubt is asked once
    reason: str


def split_doubts(
    rows: list[TimelineRow], cat_labels: tuple[str, ...], gap_seconds: float
) -> list[Doubt]:
    """Consecutive events with one cat's label, closer than the grouping gap."""
    ordered = sorted((row for row in rows if window_of(row) is not None), key=_start)
    doubts: list[Doubt] = []
    for earlier, later in itertools.pairwise(ordered):
        gap = (_start(later) - _end(earlier)).total_seconds()
        same_cat = earlier.label == later.label and earlier.label in cat_labels
        if same_cat and gap < gap_seconds:
            when = f"{gap:.0f} s before" if gap >= 0 else "overlapping"
            doubts.append(
                Doubt(
                    earlier.id,
                    "split",
                    f"the same cat {when} #{later.id}: one visit? join with next",
                )
            )
    return doubts


def _start(row: TimelineRow) -> datetime.datetime:
    return row.started_at or datetime.datetime.min


def _end(row: TimelineRow) -> datetime.datetime:
    return row.ended_at or datetime.datetime.min


def weakest_doubts(visits: list[Visit]) -> list[Doubt]:
    weakest = sorted(visits, key=lambda visit: visit.peaks.clean)[:WEAKEST_TO_ASK]
    return [
        Doubt(
            visit.row.id,
            "weakest",
            f"one of the weakest visits ({visit.peaks.clean} px), which sets the"
            " trigger's floor: a visit, or a peek (p)?",
        )
        for visit in weakest
    ]


def not_cat_doubts(others: dict[int, Peaks], visits: list[Visit]) -> list[Doubt]:
    """Events labelled not_cat that held a visit-sized blob as long as a visit."""
    peaks = sorted(visit.peaks.clean for visit in visits)
    low, high = peaks[0], peaks[math.floor((len(peaks) - 1) * TYPICAL_PEAK_SHARE)]

    def seconds(peaks: Peaks) -> float:
        return sum(low <= clean <= high for clean in peaks.cleans) / CAMERA_FPS

    spans = sorted(seconds(visit.peaks) for visit in visits)
    enough = spans[math.floor((len(spans) - 1) * SHORTEST_VISITS_SHARE)]
    return [
        Doubt(
            event_id,
            "not_cat_blob",
            f"{seconds(held):.1f} s of a blob the size of a visit's: a cat after all?",
        )
        for event_id, held in sorted(others.items())
        if seconds(held) >= enough
    ]


# --- one calibration --------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """The installation's settings a calibration reads."""

    cats: tuple[str, ...]
    cat_labels: tuple[str, ...]
    dark_brightness: float
    motion_timeout: float
    gap_seconds: float
    motion_threshold: int  # today's, replayed beside the proposal


@dataclass
class Calibration:
    since: datetime.datetime
    visits: list[Visit]
    left_out: int  # events with a cat's label from before the run
    peeks: int
    uncovered: int  # visits the CSV has no lit frame of
    conditions: list[Condition]
    doubts: list[Doubt]
    result: ReplayResult | None = None

    @property
    def done(self) -> bool:
        """Every measured condition holds; the weather is the reviewer's call."""
        return all(condition.met is not False for condition in self.conditions)


def calibrate(
    rows: list[TimelineRow],
    since: datetime.datetime,
    open_csv: Callable[[], Iterable[str]],
    settings: Settings,
) -> Calibration:
    """Count the run from `since`, ask about what looks wrong, and once every
    condition holds, propose the settings and replay them. Reads the CSV once,
    or twice when done."""
    run = [row for row in rows if _start(row) >= since]
    cat_rows = [row for row in rows if row.label in settings.cat_labels]
    in_run = [row for row in run if row.label in settings.cat_labels]
    candidates = sorted((row for row in in_run if not row.peek), key=_start)
    not_cats = [row for row in run if row.label == "not_cat"]
    windows = [w for row in candidates + not_cats if (w := window_of(row))]
    peaks = scan_peaks(
        read_frames(open_csv()), WindowIndex(windows), settings.dark_brightness
    )
    visits = [Visit(row, peaks[row.id]) for row in candidates if row.id in peaks]
    calibration = Calibration(
        since,
        visits,
        left_out=len(cat_rows) - len(in_run),
        peeks=len(in_run) - len(candidates),
        uncovered=len(candidates) - len(visits),
        conditions=conditions(visits, settings.cats),
        doubts=_doubts(run, visits, peaks, not_cats, settings),
    )
    if calibration.done and visits:
        visit_windows = WindowIndex(w for v in visits if (w := window_of(v.row)))
        calibration.result = replay(
            read_frames(open_csv()),
            (threshold_rule(settings.motion_threshold), *propose(visits)),
            visit_windows,
            settings.dark_brightness,
            datetime.timedelta(seconds=settings.motion_timeout),
        )
    return calibration


def _doubts(
    run: list[TimelineRow],
    visits: list[Visit],
    peaks: dict[int, Peaks],
    not_cats: list[TimelineRow],
    settings: Settings,
) -> list[Doubt]:
    doubts = split_doubts(run, settings.cat_labels, settings.gap_seconds)
    if visits:
        others = {row.id: peaks[row.id] for row in not_cats if row.id in peaks}
        doubts += weakest_doubts(visits) + not_cat_doubts(others, visits)
    return doubts


# --- the report -------------------------------------------------------------


def report(calibration: Calibration) -> list[str]:
    """What `main.py --calibrate` prints."""
    c = calibration
    lines = [
        (
            f"Data run since {c.since:%Y-%m-%d %H:%M}: {len(c.visits)} visits"
            f" ({c.left_out} earlier left out, {c.peeks} peeks,"
            f" {c.uncovered} with no metrics rows)"
        ),
        "",
    ]
    for condition in c.conditions:
        state = {True: "met", False: "not yet", None: ""}[condition.met]
        lines.append(f"  {condition.name:<48} {condition.now:<40} {state}")
    lines.append("")
    if c.result is None:
        lines.append("Not done: the settings are proposed once every condition is met.")
    else:
        lines += _settings_lines(c.result, len(c.visits))
    return lines


def _settings_lines(result: ReplayResult, visits: int) -> list[str]:
    _, threshold, band = (each.rule for each in result.replays)
    lines = [f"Replayed over {result.lit_hours:.0f} lit hours of the CSV:"]
    for each, role in zip(result.replays, ("now", "proposed", "proposed"), strict=True):
        missed = visits - len(each.caught)
        rate = each.false_triggers / result.lit_hours if result.lit_hours else 0
        lines.append(
            f"  {role:<9}{_describe(each.rule):<34} misses {missed} of {visits} visits,"
            f" {rate:.2f} false triggers an hour"
        )
    lines += [
        "",
        f"The band ({band.low}-{band.high} px) waits for A.3; today's detector",
        "takes the threshold. In the service's drop-in",
        "(sudo systemctl edit catdetector):",
        "  [Service]",
        f"  Environment=MOTION_THRESHOLD={threshold.low - 1}",
        "and drop METRICS_CSV there once the data run is over.",
    ]
    return lines


def _describe(rule: Rule) -> str:
    if rule.persist == 1:
        return f"MOTION_THRESHOLD={rule.low - 1}"
    return f"band {rule.low}-{rule.high} px, {rule.persist} frames"
