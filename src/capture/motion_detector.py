"""Whether an analysis frame shows something moving, and what."""

import logging
from dataclasses import dataclass
from typing import cast

import cv2

from src.capture.motion_metrics import FrameMetrics, measure
from src.clips.blob import Blob
from src.clips.frame import Frame

log = logging.getLogger(__name__)

# MOG2 marks shadow pixels with this value; only 255 is real foreground.
SHADOW_PIXEL_VALUE = 127


@dataclass(frozen=True)
class Detection:
    """What one frame measured, and whether it counts as motion."""

    metrics: FrameMetrics
    moving: bool


class MotionDetector:
    """MOG2 background subtraction on the lores stream, gated by warmup and light.

    Holds the background model, so one instance serves one camera. Stateful:
    last_blob is the largest region of the most recent frame that moved.
    """

    def __init__(
        self,
        *,
        motion_threshold: int,
        dark_brightness: float,
        mog2_history: int,
        warmup_frames: int = 30,
    ) -> None:
        if warmup_frames < 0:
            raise ValueError("warmup_frames must not be negative")
        self.motion_threshold = motion_threshold
        self.dark_brightness = dark_brightness
        self.warmup_frames = warmup_frames
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=mog2_history
        )
        self.dark = False
        self.last_blob: Blob | None = None
        self._frames_seen = 0
        if warmup_frames == 0:
            log.info("Motion detection armed (warmup disabled)")

    def detect(self, frame: Frame) -> Detection:
        """Measure the frame and decide whether it moved."""
        # RGB -> BGR followed by BGR -> GRAY is the same as one RGB -> GRAY pass.
        gray = cast(Frame, cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY))
        measured = measure(gray, self._foreground_mask(gray))

        if self._still_warming_up() or self._is_dark(measured.brightness):
            return Detection(measured, moving=False)
        moving = measured.foreground_px > self.motion_threshold
        if moving:
            self.last_blob = measured.clean_blob
        return Detection(measured, moving)

    def _foreground_mask(self, gray: Frame) -> Frame:
        """Binary foreground mask, with MOG2's shadow pixels excluded.

        Shadow detection stays on: disabling it relabels shadows as foreground
        rather than removing them.
        """
        mask = self.background_subtractor.apply(gray)
        _, binary = cv2.threshold(mask, SHADOW_PIXEL_VALUE, 255, cv2.THRESH_BINARY)
        return cast(Frame, binary)

    def _still_warming_up(self) -> bool:
        """Count this frame towards MOG2's training; True until it has enough."""
        if self._frames_seen >= self.warmup_frames:
            return False

        self._frames_seen += 1
        if self._frames_seen == self.warmup_frames:
            log.info("Motion detection armed after %d frames", self.warmup_frames)
        return True

    def _is_dark(self, brightness: float) -> bool:
        """Whether the scene is too dark to see. Logs each change."""
        dark = brightness < self.dark_brightness
        if dark != self.dark:
            self.dark = dark
            log.info(
                "Scene is dark; ignoring motion"
                if dark
                else "Scene is lit; detecting motion"
            )
        return dark
