"""The figures the documents quote are the code's: each phrase is built from the
constant it quotes, so a change to one without the other fails here."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from main import CAPTURE_STALL_SECONDS, STALL_EXIT_DEADLINE_SECONDS
from src.capture.capture_heartbeat import PING_TIMEOUT_SECONDS
from src.capture.clip_transfer import SCAN_INTERVAL_SECONDS
from src.capture.motion_recorder import MotionRecorder
from src.clips.format import CAMERA_FPS, H264_BITRATE
from src.review.api import TIMELINE_ROWS_SENT
from src.review.calibration import (
    BOX_DWELL_SECONDS,
    COVERAGE_KEPT,
    COVERAGE_POOR,
    NIGHT_FROM_HOUR,
    NIGHT_UNTIL_HOUR,
    NIGHT_VISITS_NEEDED,
    POORLY_RECORDED_EXTRA,
    VISITS_NEEDED,
    VISITS_PER_CAT_NEEDED,
    VISITS_PREFERRED,
)
from src.review.clip_frames import MAX_TILES

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = dict(
    re.findall(r'os\.getenv\("(\w+)", "([^"]*)"\)', (ROOT / "config.py").read_text())
)
RECORDER = inspect.signature(MotionRecorder).parameters
BUFFER_SECONDS = RECORDER["buffer_seconds"].default
LONGEST_CLIP_MINUTES = RECORDER["max_clip_seconds"].default / 60
MB_A_MINUTE = H264_BITRATE * 60 / 8 / 1e6
MOTION_TIMEOUT = float(DEFAULTS["MOTION_TIMEOUT"])
UNIT = (ROOT / "deploy" / "catdetector.service.in").read_text()
RESTART_SECONDS = re.findall(r"^RestartSec=(\d+)$", UNIT, re.MULTILINE)[0]
NUMBERS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight"]


def spelled(n: float) -> str:
    assert n == int(n), f"{n} is not a whole number to spell"
    return NUMBERS[int(n)]


def s(seconds: float) -> str:
    return f"{seconds:g} s"


FIGURES = [
    ("README.md", f"keeps the last {spelled(BUFFER_SECONDS)} seconds in memory"),
    ("README.md", f"begins up to {spelled(BUFFER_SECONDS)} seconds before the motion"),
    (
        "README.md",
        (
            f"A clip closes after {s(MOTION_TIMEOUT)} without motion, "
            f"or at {spelled(LONGEST_CLIP_MINUTES)} minutes"
        ),
    ),
    (
        "README.md",
        f"about {MB_A_MINUTE:g} MB a minute ({H264_BITRATE / 1e6:g} Mbit/s)",
    ),
    ("README.md", f"camera stops delivering frames for {s(CAPTURE_STALL_SECONDS)}"),
    (
        "README.md",
        (
            f"{VISITS_NEEDED} visits ({VISITS_PREFERRED} is better), "
            f"{NIGHT_VISITS_NEEDED} of them at night "
            f"({NIGHT_FROM_HOUR:02}:00\u2013{NIGHT_UNTIL_HOUR:02}:00), "
            f"and {VISITS_PER_CAT_NEEDED} per cat"
        ),
    ),
    ("README.md", f"a cat staying {s(BOX_DWELL_SECONDS)} in that box's third"),
    (
        "README.md",
        f"still records {COVERAGE_KEPT:.0%} of the median visit".replace("%", " %"),
    ),
    (
        "README.md",
        f"under {COVERAGE_POOR:.0%} of a few more visits than the run did, "
        f"at most {POORLY_RECORDED_EXTRA:.0%} of them".replace("%", " %"),
    ),
    ("README.md", f"it shows the newest {TIMELINE_ROWS_SENT} and counts them all"),
    ("docs/DESIGN.md", f"capture_arrays(main, lores), ~{CAMERA_FPS} fps"),
    ("docs/DESIGN.md", f"ClipTransfer, every {s(SCAN_INTERVAL_SECONDS)}"),
    ("docs/DESIGN.md", f"every {s(SCAN_INTERVAL_SECONDS)}, copy finished clips"),
    (
        "docs/DESIGN.md",
        (
            f"{s(PING_TIMEOUT_SECONDS)} timeout, while the last frame is under "
            f"{s(CAPTURE_STALL_SECONDS)} old"
        ),
    ),
    ("docs/DESIGN.md", f"no frame for {s(CAPTURE_STALL_SECONDS)}"),
    ("docs/DESIGN.md", f"No frame from the camera for {s(CAPTURE_STALL_SECONDS)}."),
    ("docs/DESIGN.md", f"{s(STALL_EXIT_DEADLINE_SECONDS)} timer armed"),
    ("docs/DESIGN.md", f"`os._exit(1)` {s(STALL_EXIT_DEADLINE_SECONDS)} after a stall"),
    ("docs/DESIGN.md", f"Restart=on-failure after RestartSec={RESTART_SECONDS}"),
    ("docs/DESIGN.md", f"stride = ⌈n / {MAX_TILES}⌉"),
    ("docs/DESIGN.md", f"(fewer past {MAX_TILES})"),
    ("docs/DESIGN.md", f"up to {s(BUFFER_SECONDS)} from the ring buffer"),
    (
        "docs/design/calibration.md",
        f"from {NIGHT_FROM_HOUR:02}:00 to {NIGHT_UNTIL_HOUR:02}:00",
    ),
    ("CLAUDE.md", f"**~{CAMERA_FPS} fps**"),
    ("CLAUDE.md", f"one that blocks for {s(CAPTURE_STALL_SECONDS)}"),
    ("CLAUDE.md", f"arms an `os._exit` after {s(STALL_EXIT_DEADLINE_SECONDS)}"),
    (
        "CLAUDE.md",
        f"~{s(round(int(DEFAULTS['MOG2_HISTORY']) / CAMERA_FPS))} at the default",
    ),
]


@pytest.mark.parametrize(
    ("document", "phrase"), [pytest.param(d, p, id=f"{d}: {p}") for d, p in FIGURES]
)
def test_the_document_quotes_the_code(document: str, phrase: str) -> None:
    text = " ".join((ROOT / document).read_text().split())
    assert phrase in text, f"{document} no longer says {phrase!r}: fix one or the other"
