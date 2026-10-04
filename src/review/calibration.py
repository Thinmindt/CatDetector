"""A data run's progress, and the trigger settings it supports (roadmap A.2).

Reads the review database's events and the detector's metrics CSV, which is the
record of what the camera saw; nothing here touches the camera.
"""

import bisect
import datetime
import itertools
import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final

from src.clips.format import ANALYSIS_SIZE, CAMERA_FPS
from src.clips.sidecar import iso
from src.review.timeline import TimelineRow, box_at

# The run is done when every condition holds (ROADMAP A.2).
VISITS_NEEDED = 50
VISITS_PREFERRED = 100
NIGHT_VISITS_NEEDED = 10
VISITS_PER_CAT_NEEDED = 15
STABLE_VISITS = 20
NIGHT_FROM_HOUR = 20
NIGHT_UNTIL_HOUR = 6

# A box is used in a visit when the cat's blob, at least LOWER_MARGIN of the
# weakest visit's peak, stays this long in the box's third of the frame.
BOX_DWELL_SECONDS = 10

# A lower edge sits at this share of the weakest visit's peak; the band's upper
# edge this far above the strongest, and never above half the frame.
LOWER_MARGIN = 0.5
UPPER_MARGIN = 1.25
FRAME_AREA = ANALYSIS_SIZE[0] * ANALYSIS_SIZE[1]
# A.3's planned persistence: frames in the band before a clip starts.
PERSIST_FRAMES = 2
# The proposed threshold is the highest of the sweep that misses no visit and
# records at least this share of the median visit.
COVERAGE_KEPT = 0.98
# Visits recorded less than this are counted in the report.
COVERAGE_POOR = 0.8

WEAKEST_TO_ASK = 3
# A not_cat event is asked about when it held a blob the size of a typical
# visit (from the weakest visit's peak to this share of the visits' peaks) for
# as long as all but this share of the visits did.
TYPICAL_PEAK_SHARE = 0.95
SHORTEST_VISITS_SHARE = 0.10

# The detector logs the frame that starts a clip a moment before the clip's
# recorded start, so an event's span reaches back this far.
WINDOW_LEAD = datetime.timedelta(seconds=1)
# After a restart the detector ignores its first frames while MOG2 learns the
# scene (MotionDetector's warmup_frames); a gap this long in the CSV is a restart.
WARMUP_FRAMES = 30
RESTART_GAP_SECONDS = 2.0
# A clip starts with the ring buffer's footage (MotionRecorder's buffer_seconds),
# no further back than the end of the clip before it.
PRE_ROLL = datetime.timedelta(seconds=5)

# The metrics CSV's columns this reads, by the header motion_metrics writes.
COLUMNS = (
    "timestamp",
    "foreground_px",
    "clean_area",
    "brightness",
    "clean_x",
    "clean_w",
)

# Where an ISO timestamp has its date's first dash and the T before the clock,
# which a damaged row's first cell does not; and where its minute ends.
DATE_DASH = len("9999")
CLOCK_T = len("9999-99-99")
MINUTE = len("9999-99-99T99:99")

# A frame of the CSV: its time as ISO text, foreground pixels, cleaned largest
# blob, brightness, and that blob's centre across the frame (-1 for none).
Frame = tuple[str, int, int, float, int]
FOREGROUND: Final = 1
CLEAN: Final = 2


@dataclass
class Tally:
    """Rows of the CSV that did not parse: a tail still being written, or a
    row a power cut left half-written."""

    skipped: int = 0


def read_frames(lines: Iterable[str], tally: Tally | None = None) -> Iterator[Frame]:
    """The metrics CSV's rows, the columns found by its header."""
    rows = iter(lines)
    header = next(rows, "").strip().split(",")
    missing = [name for name in COLUMNS if name not in header]
    if missing:
        raise ValueError(f"not a metrics CSV: no {', '.join(missing)} column")
    at = [header.index(name) for name in COLUMNS]
    damaged = tally if tally is not None else Tally()
    for line in rows:
        frame = _frame(line.strip("\x00\n").split(","), at)
        if frame is None:
            damaged.skipped += 1
        else:
            yield frame


def _frame(cells: list[str], at: list[int]) -> Frame | None:
    try:
        time, fg, clean, bright, x, w = (cells[i] for i in at)
        centre = int(x) + int(w) // 2 if x else -1
        frame = (time, int(fg or 0), int(clean or 0), float(bright), centre)
    except (IndexError, ValueError):
        return None
    shaped = (
        time[DATE_DASH : DATE_DASH + 1] == "-" and time[CLOCK_T : CLOCK_T + 1] == "T"
    )
    return frame if shaped else None


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
    """What the CSV saw during one event, over its lit frames: the largest
    foreground count and cleaned blob, and each frame's blob and its centre."""

    foreground: int = 0
    clean: int = 0
    cleans: list[int] = field(default_factory=list)
    centres: list[int] = field(default_factory=list)

    def add(self, foreground: int, clean: int, centre: int) -> None:
        self.foreground = max(self.foreground, foreground)
        self.clean = max(self.clean, clean)
        self.cleans.append(clean)
        self.centres.append(centre)

    def by_box(self, floor: int) -> dict[int, list[int]]:
        """The blobs of at least `floor` px, by the box their centre is in."""
        boxes: dict[int, list[int]] = {}
        for clean, centre in zip(self.cleans, self.centres, strict=True):
            if clean >= floor and centre >= 0:
                boxes.setdefault(box_at(centre), []).append(clean)
        return boxes


def scan_peaks(
    frames: Iterable[Frame], windows: WindowIndex, dark_brightness: float
) -> dict[int, Peaks]:
    """What the CSV saw during each event, over lit frames."""
    peaks: dict[int, Peaks] = {}
    for time, foreground, clean, brightness, centre in frames:
        if brightness < dark_brightness:
            continue
        for event_id in windows.holding(time):
            peaks.setdefault(event_id, Peaks()).add(foreground, clean, centre)
    return peaks


# --- the visits and the conditions ------------------------------------------


@dataclass(frozen=True)
class Visit:
    row: TimelineRow
    peaks: Peaks
    # The boxes the cat stayed in, each with the largest blob it made there.
    boxes: dict[int, int] = field(default_factory=dict)

    @property
    def night(self) -> bool:
        hour = self.row.started_at.hour if self.row.started_at else 12
        return hour >= NIGHT_FROM_HOUR or hour < NIGHT_UNTIL_HOUR

    @property
    def start(self) -> datetime.datetime:
        return _start(self.row) - WINDOW_LEAD

    @property
    def end(self) -> datetime.datetime:
        return _end(self.row)


def stayed_in(peaks: Peaks, floor: int) -> dict[int, int]:
    """The boxes where a blob of at least `floor` px stayed long enough to be
    used, each with the largest blob there."""
    return {
        box: max(blobs)
        for box, blobs in peaks.by_box(floor).items()
        if len(blobs) >= BOX_DWELL_SECONDS * CAMERA_FPS
    }


@dataclass(frozen=True)
class Condition:
    name: str
    now: str
    met: bool | None  # None: the reviewer's call


def window_of(row: TimelineRow) -> Window | None:
    if row.started_at is None or row.ended_at is None:
        return None
    return Window(row.id, iso(row.started_at - WINDOW_LEAD), iso(row.ended_at))


def conditions(
    visits: list[Visit], cats: tuple[str, ...], boxes: tuple[int, ...]
) -> list[Condition]:
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
        every_box(visits, boxes),
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


def every_box(visits: list[Visit], boxes: tuple[int, ...]) -> Condition:
    """Each box was stayed in, counted with the weakest blob made there."""
    strengths: dict[int, list[int]] = {box: [] for box in boxes}
    for visit in visits:
        for box, blob in visit.boxes.items():
            strengths.setdefault(box, []).append(blob)
    now = "; ".join(
        f"box {box}: {len(strengths[box])}"
        + (f", weakest {min(strengths[box])} px" if strengths[box] else "")
        for box in boxes
    )
    name = f"every box used ({BOX_DWELL_SECONDS} s or more there)"
    return Condition(name, now, all(strengths[box] for box in boxes))


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
    """A trigger to replay: a frame counts when its value (foreground or cleaned
    blob) is in [low, high], and a clip starts after `persist` such frames in a
    row; while it records, a value above `hold` keeps it going."""

    column: int
    low: int
    high: int
    persist: int = 1
    hold: int | None = None

    def __str__(self) -> str:
        if self.column == CLEAN:
            return f"band {self.low}-{self.high} px, {self.persist} frames"
        held = f", held above {self.hold}" if self.hold is not None else ""
        return f"MOTION_THRESHOLD={self.low - 1}{held}"

    @property
    def floor(self) -> int:
        """The smallest value this rule acts on."""
        return min(self.low, self.hold + 1 if self.hold is not None else self.low)


def threshold_rule(threshold: int, hold: int | None = None) -> Rule:
    """The detector's test today: foreground_px > MOTION_THRESHOLD."""
    return Rule(FOREGROUND, threshold + 1, FRAME_AREA, 1, hold)


def band_rule(visits: list[Visit]) -> Rule:
    """A.3's band on the cleaned blob."""
    weakest = min(visit.peaks.clean for visit in visits)
    strongest = max(visit.peaks.clean for visit in visits)
    return Rule(
        CLEAN,
        math.floor(weakest * LOWER_MARGIN),
        min(math.ceil(strongest * UPPER_MARGIN), FRAME_AREA // 2),
        PERSIST_FRAMES,
    )


def sweep(run_threshold: int, visits: list[Visit]) -> list[int]:
    """Thresholds from the run's upward, doubling, to half the weakest visit's
    largest foreground count."""
    ceiling = math.floor(min(visit.peaks.foreground for visit in visits) * LOWER_MARGIN)
    thresholds = [run_threshold]
    while thresholds[-1] * 2 < ceiling:
        thresholds.append(thresholds[-1] * 2)
    return [*thresholds, ceiling] if ceiling > run_threshold else thresholds


@dataclass
class Replay:
    """One rule run over the CSV: the clips it would have recorded, and how many
    of them started outside every visit."""

    rule: Rule
    timeout: datetime.timedelta
    false_triggers: int = 0
    spans: list[tuple[datetime.datetime, datetime.datetime]] = field(
        default_factory=list
    )
    streak: int = 0
    counted_at: int = -2  # the frame number the streak last grew on
    opened: datetime.datetime | None = None
    last: datetime.datetime | None = None

    def feed(self, frame: Frame, number: int, visits: WindowIndex) -> None:
        value = frame[CLEAN] if self.rule.column == CLEAN else frame[FOREGROUND]
        if self.rule.low <= value <= self.rule.high:
            self.streak = self.streak + 1 if self.counted_at == number - 1 else 1
            self.counted_at = number
            if self.streak >= self.rule.persist:
                self._trigger(frame, visits)
                return
        if self.rule.hold is not None and value > self.rule.hold:
            self._extend(frame)

    def _trigger(self, frame: Frame, visits: WindowIndex) -> None:
        when = datetime.datetime.fromisoformat(frame[0])
        if not self._recording(when):
            self.finish()
            self.opened = when
            self.false_triggers += not visits.holding(frame[0])
        self.last = when

    def _extend(self, frame: Frame) -> None:
        if self.last is None:
            return
        when = datetime.datetime.fromisoformat(frame[0])
        if self._recording(when):
            self.last = when

    def _recording(self, when: datetime.datetime) -> bool:
        last = self.last
        return last is not None and datetime.timedelta(0) <= when - last <= self.timeout

    def finish(self) -> None:
        """Close the clip being recorded, if any."""
        if self.opened is None or self.last is None:
            return
        start = self.opened - PRE_ROLL
        if self.spans:
            start = max(start, self.spans[-1][1])
        self.spans.append((start, self.last + self.timeout))
        self.opened = None

    def coverage(self, visits: list[Visit]) -> list[float]:
        """The share of each visit the rule's clips recorded."""
        spans = sorted(self.spans)
        starts = [start for start, _ in spans]
        shares = []
        for visit in visits:
            length = (visit.end - visit.start).total_seconds() or 1.0
            index = max(bisect.bisect_right(starts, visit.start) - 1, 0)
            recorded = 0.0
            while index < len(spans) and spans[index][0] < visit.end:
                start, end = spans[index]
                overlap = min(end, visit.end) - max(start, visit.start)
                recorded += max(overlap.total_seconds(), 0.0)
                index += 1
            shares.append(min(recorded / length, 1.0))
        return shares


@dataclass(frozen=True)
class Outcome:
    """What one rule would have done."""

    rule: Rule
    missed: int
    false_per_hour: float
    median_coverage: float
    poorly_recorded: int


def replay(
    frames: Iterable[Frame],
    rules: Iterable[Rule],
    visits: list[Visit],
    dark_brightness: float,
    timeout: datetime.timedelta,
) -> list[Outcome]:
    """Run each rule over every lit frame, as the detector would have: not in
    the dark, and not while it warms up after a restart."""
    replays = [Replay(rule, timeout) for rule in rules]
    index = WindowIndex(w for v in visits if (w := window_of(v.row)))
    floor = min(each.rule.floor for each in replays)
    lit = 0
    warming = _Warmup()
    for number, frame in enumerate(frames):
        if warming.skips(frame[0]) or frame[3] < dark_brightness:
            continue
        lit += 1
        if frame[FOREGROUND] < floor and frame[CLEAN] < floor:
            continue
        for each in replays:
            each.feed(frame, number, index)
    hours = lit / CAMERA_FPS / 3600
    return [_outcome(each, visits, hours) for each in replays]


def _outcome(each: Replay, visits: list[Visit], hours: float) -> Outcome:
    each.finish()
    shares = each.coverage(visits)
    return Outcome(
        each.rule,
        missed=sum(share == 0 for share in shares),
        false_per_hour=each.false_triggers / hours if hours else 0.0,
        median_coverage=statistics.median(shares) if shares else 0.0,
        poorly_recorded=sum(share < COVERAGE_POOR for share in shares),
    )


def proposal(outcomes: list[Outcome]) -> Outcome:
    """The highest plain threshold that misses no visit and keeps the median
    visit recorded; the run's own when none above it does."""
    plain = [o for o in outcomes if o.rule.column == FOREGROUND and o.rule.hold is None]
    kept = [o for o in plain if not o.missed and o.median_coverage >= COVERAGE_KEPT]
    return max(kept or plain[:1], key=lambda o: o.rule.low)


@dataclass
class _Warmup:
    """The frames after each restart the detector does not act on."""

    last: str | None = None
    left: int = 0

    def skips(self, time: str) -> bool:
        restarted = self.last is None or not _soon_after(self.last, time)
        self.last = time
        self.left = WARMUP_FRAMES if restarted else max(self.left - 1, 0)
        return self.left > 0


def _soon_after(earlier: str, later: str) -> bool:
    """Whether `later` follows `earlier` within the restart gap. Rows of one
    minute compare their seconds, which spares parsing millions of times."""
    if earlier[:MINUTE] == later[:MINUTE]:
        gap = float(later[MINUTE + 1 :]) - float(earlier[MINUTE + 1 :])
    else:
        then, now = (datetime.datetime.fromisoformat(t) for t in (earlier, later))
        gap = (now - then).total_seconds()
    return 0 <= gap <= RESTART_GAP_SECONDS


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
    boxes: tuple[int, ...]  # the thirds of the frame that hold a box
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
    blank: int  # visits whose frames show no blob at all
    conditions: list[Condition]
    doubts: list[Doubt]
    skipped_rows: int = 0
    outcomes: list[Outcome] | None = None
    open_questions: int = 0  # staged for review and not answered yet

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
    in_run = [row for row in run if row.label in settings.cat_labels]
    candidates = sorted((row for row in in_run if not row.peek), key=_start)
    not_cats = [row for row in run if row.label == "not_cat"]
    tally = Tally()
    peaks = scan_peaks(
        read_frames(open_csv(), tally),
        WindowIndex(w for row in candidates + not_cats if (w := window_of(row))),
        settings.dark_brightness,
    )
    seen = [Visit(row, peaks[row.id]) for row in candidates if row.id in peaks]
    visits = _with_boxes([v for v in seen if v.peaks.clean and v.peaks.foreground])
    calibration = Calibration(
        since,
        visits,
        left_out=sum(row.label in settings.cat_labels for row in rows) - len(in_run),
        peeks=len(in_run) - len(candidates),
        uncovered=len(candidates) - len(seen),
        blank=len(seen) - len(visits),
        conditions=conditions(visits, settings.cats, settings.boxes),
        doubts=_doubts(run, seen, peaks, not_cats, settings),
        skipped_rows=tally.skipped,
    )
    if calibration.done and visits:
        calibration.outcomes = replay(
            read_frames(open_csv()),
            _rules(settings.motion_threshold, visits),
            visits,
            settings.dark_brightness,
            datetime.timedelta(seconds=settings.motion_timeout),
        )
    return calibration


def _with_boxes(visits: list[Visit]) -> list[Visit]:
    if not visits:
        return visits
    floor = math.floor(min(visit.peaks.clean for visit in visits) * LOWER_MARGIN)
    return [Visit(v.row, v.peaks, stayed_in(v.peaks, floor)) for v in visits]


def _rules(run_threshold: int, visits: list[Visit]) -> list[Rule]:
    """The sweep, A.3's start-and-hold pair, and A.3's band."""
    thresholds = sweep(run_threshold, visits)
    return [
        *(threshold_rule(threshold) for threshold in thresholds),
        threshold_rule(thresholds[-1], hold=run_threshold),
        band_rule(visits),
    ]


def _doubts(
    run: list[TimelineRow],
    seen: list[Visit],
    peaks: dict[int, Peaks],
    not_cats: list[TimelineRow],
    settings: Settings,
) -> list[Doubt]:
    doubts = split_doubts(run, settings.cat_labels, settings.gap_seconds)
    doubts += [
        Doubt(
            visit.row.id,
            "blank",
            "the metrics show no blob during this visit: is the label right?",
        )
        for visit in seen
        if not (visit.peaks.clean and visit.peaks.foreground)
    ]
    visits = [visit for visit in seen if visit.peaks.clean and visit.peaks.foreground]
    if visits:
        others = {row.id: peaks[row.id] for row in not_cats if row.id in peaks}
        doubts += weakest_doubts(visits) + not_cat_doubts(others, visits)
    return doubts


# --- the report -------------------------------------------------------------


def report(calibration: Calibration) -> list[str]:
    """What `main.py --calibrate` prints."""
    c = calibration
    lines = [
        f"Data run since {c.since:%Y-%m-%d %H:%M}: {len(c.visits)} visits",
        (
            f"  left out: {c.left_out} from before the run, {c.peeks} peeks,"
            f" {c.uncovered} with no metrics rows, {c.blank} with no blob in them"
        ),
        "",
    ]
    for condition in c.conditions:
        state = {True: "met", False: "not yet", None: ""}[condition.met]
        lines += [f"  {condition.name:<48} {state}", f"      {condition.now}"]
    lines.append("")
    if c.skipped_rows:
        lines += [f"Skipped {c.skipped_rows} damaged rows of the CSV.", ""]
    if c.outcomes is None:
        lines.append("Not done: the settings are proposed once every condition is met.")
        return lines
    lines += _outcome_lines(c.outcomes, len(c.visits))
    if c.open_questions:
        return [
            *lines,
            "",
            f"Provisional: {c.open_questions} question(s) wait in the review queue,",
            "and the answers can move these numbers. Answer them, then run again.",
        ]
    return lines + _apply_lines(c.outcomes)


def _outcome_lines(outcomes: list[Outcome], visits: int) -> list[str]:
    chosen = proposal(outcomes)
    lines = [
        f"Replayed over the CSV ({visits} visits):",
        (
            f"  {'rule':<44} {'missed':>6} {'false/h':>8} {'recorded':>9}"
            f" {'<' + format(COVERAGE_POOR, '.0%'):>6}"
        ),
    ]
    for outcome in outcomes:
        mark = " <- proposed" if outcome is chosen else ""
        lines.append(
            f"  {outcome.rule!s:<44} {outcome.missed:>6} {outcome.false_per_hour:>8.2f}"
            f" {outcome.median_coverage:>9.0%} {outcome.poorly_recorded:>6}{mark}"
        )
    kept = f"{COVERAGE_KEPT:.0%}"
    lines += [
        "",
        "'recorded' is the median share of a visit its clips cover; the proposal",
        f"is the highest plain threshold that misses none and records {kept}.",
        "A held threshold (start high, keep recording above the run's) and the band",
        "wait for A.3.",
    ]
    return lines


def _apply_lines(outcomes: list[Outcome]) -> list[str]:
    return [
        "",
        "In the service's drop-in (sudo systemctl edit catdetector):",
        "  [Service]",
        f"  Environment=MOTION_THRESHOLD={proposal(outcomes).rule.low - 1}",
        "and drop METRICS_CSV there once the data run is over.",
    ]
