"""MotionDetector: warmup, the dark gate, the threshold and the last blob."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from conftest import LORES_SIZE, make_detector, make_frame

from src.clips.frame import Frame

# Mean grey levels either side of a dark cutoff of 10, as measured at night and by day.
DARK_CUTOFF = 10
NIGHT_GREY = 2
DAY_GREY = 60


@pytest.fixture
def detector() -> Any:
    return make_detector()


def moved(detector: Any, frame: Frame) -> bool:
    return bool(detector.detect(frame).moving)


def arm(detector: Any) -> None:
    """Push the detector past its warmup with quiet frames."""
    detector.background_subtractor.motion_pixels = 0
    for _ in range(detector.warmup_frames):
        moved(detector, make_frame(LORES_SIZE))


def test_warmup_suppresses_motion_then_arms(detector: Any) -> None:
    """Regression: MOG2 calls the whole first frame foreground, which used to
    trigger a clip on every startup."""
    detector.background_subtractor.motion_pixels = 10**6  # far over threshold
    frame = make_frame(LORES_SIZE)

    during = [moved(detector, frame) for _ in range(detector.warmup_frames)]
    assert during == [False] * detector.warmup_frames

    # The very next frame is armed.
    assert moved(detector, frame) is True


def test_quiet_frames_do_not_report_motion(detector: Any) -> None:
    arm(detector)
    detector.background_subtractor.motion_pixels = 10
    assert moved(detector, make_frame(LORES_SIZE)) is False


def test_every_frame_is_measured_even_when_quiet(detector: Any) -> None:
    arm(detector)
    detector.background_subtractor.motion_pixels = 10
    detection = detector.detect(make_frame(LORES_SIZE))
    assert detection.metrics.foreground_px == 10
    assert detection.moving is False


def test_motion_in_a_dark_scene_does_not_trigger(detector: Any) -> None:
    """Regression: in the unlit room, sensor speckle alone crossed the threshold
    and recorded black video nonstop."""
    detector.dark_brightness = DARK_CUTOFF
    arm(detector)
    detector.background_subtractor.motion_pixels = 10**6

    assert moved(detector, make_frame(LORES_SIZE, NIGHT_GREY)) is False
    assert moved(detector, make_frame(LORES_SIZE, DAY_GREY)) is True


def test_logs_each_change_between_dark_and_lit(
    detector: Any, caplog: pytest.LogCaptureFixture
) -> None:
    detector.dark_brightness = DARK_CUTOFF
    arm(detector)

    with caplog.at_level(logging.INFO, logger="src.capture.motion_detector"):
        for grey in (NIGHT_GREY, NIGHT_GREY, DAY_GREY, DAY_GREY, NIGHT_GREY):
            moved(detector, make_frame(LORES_SIZE, grey))

    changes = [r.getMessage() for r in caplog.records if "Scene" in r.getMessage()]
    assert changes == [
        "Scene is dark; ignoring motion",
        "Scene is lit; detecting motion",
        "Scene is dark; ignoring motion",
    ]


def test_last_blob_follows_motion_frames_only(detector: Any) -> None:
    """A quiet frame must not wipe the position of the last thing that moved:
    it is what places the clip in a box once it closes."""
    arm(detector)
    detector.background_subtractor.motion_pixels = 5000
    moved(detector, make_frame(LORES_SIZE))
    seen = detector.last_blob

    detector.background_subtractor.motion_pixels = 0
    moved(detector, make_frame(LORES_SIZE))

    assert seen is not None
    assert detector.last_blob is seen


def test_negative_warmup_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_detector(warmup_frames=-1)


def test_zero_warmup_announces_that_it_is_armed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """warmup_frames=0 legitimately means "no warmup", but it must still say so:
    the armed line is otherwise only logged from inside the warmup branch."""
    with caplog.at_level(logging.INFO):
        make_detector(warmup_frames=0)

    assert "armed" in caplog.text
