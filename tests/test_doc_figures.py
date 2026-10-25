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
MB_A_MINUTE = H264_BITRATE * 60 / 8 / 1e6
UNIT = (ROOT / "deploy" / "catdetector.service.in").read_text()
RESTART_SECONDS = re.findall(r"^RestartSec=(\d+)$", UNIT, re.MULTILINE)[0]
NUMBERS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight"]


def spelled(n: float) -> str:
    assert n == int(n), f"{n} is not a whole number to spell"
    return NUMBERS[int(n)]


def s(seconds: float) -> str:
    return f"{seconds:g} s"


FIGURES = [
    (
        "README.md",
        f"about {MB_A_MINUTE:g} MB a minute ({H264_BITRATE / 1e6:g} Mbit/s)",
    ),
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
    (
        "docs/ROADMAP.md",
        f"**{VISITS_NEEDED}+ labeled visits, {VISITS_PREFERRED} preferred**",
    ),
    ("docs/ROADMAP.md", f"**{NIGHT_VISITS_NEEDED}+ at night under the light**"),
    ("docs/ROADMAP.md", f"**{VISITS_PER_CAT_NEEDED}+ per cat**"),
    (
        "docs/design/calibration.md",
        f"A box is used in a visit with a stay of {s(BOX_DWELL_SECONDS)}",
    ),
    (
        "docs/design/calibration.md",
        f"records {COVERAGE_KEPT:.0%} of the median visit".replace("%", " %"),
    ),
    (
        "docs/design/calibration.md",
        (
            f"no more than {POORLY_RECORDED_EXTRA:.0%} of the visits more under "
            f"{COVERAGE_POOR:.0%}"
        ).replace("%", " %"),
    ),
    ("docs/design/review.md", f"carries the first {TIMELINE_ROWS_SENT} rows"),
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
