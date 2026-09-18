import datetime
import logging
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import cv2
from picamera2.encoders import H264Encoder
from picamera2.outputs import CircularOutput

from src.camera_manager import CameraManager, Frame
from src.clip_sidecar import ClipFacts, CloseReason, write_sidecar
from src.motion_metrics import Blob, MetricsLog, clean_mask, largest_blob, measure

log = logging.getLogger(__name__)

CAMERA_FPS = 30
H264_BITRATE = 10_000_000

CLIP_SUFFIX = ".h264"  # raw elementary stream, not a container
PARTIAL_SUFFIX = ".part"

# MOG2 marks shadow pixels with this value; only 255 is real foreground.
SHADOW_PIXEL_VALUE = 127

CLIP_FLUSH_TIMEOUT_SECONDS = 30
DISK_CHECK_INTERVAL_SECONDS = 5


def partial_name(clip: Path) -> Path:
    """The name a clip is written under. Only the final name means "complete"."""
    return clip.with_name(clip.name + PARTIAL_SUFFIX)


def _has_content(writing: Path | None) -> bool:
    try:
        return writing is not None and writing.stat().st_size > 0
    except OSError:
        return False


class _ResilientCircularOutput(CircularOutput):  # type: ignore[misc]
    """CircularOutput that drops frames instead of raising into picamera2.

    Both methods run on the encoder's poll thread. That thread is the only one
    that returns camera buffers, so an exception escaping it starves the camera.
    """

    def _write(self, frame: Any, timestamp: Any = None) -> None:
        if self.is_abandoned():
            return
        try:
            super()._write(frame, timestamp)
        except OSError as error:
            self.dead = True
            log.error("Clip write failed, abandoning clip: %s", error)  # noqa: TRY400 -- the message is the whole story

    def outputframe(
        self,
        frame: Any,
        keyframe: bool = True,
        timestamp: Any = None,
        packet: Any = None,
        audio: bool = False,
    ) -> None:
        # stop() can drain the ring between this frame's append and its popleft.
        try:
            super().outputframe(frame, keyframe, timestamp, packet, audio)
        except IndexError:
            log.debug("Dropped a frame racing the end of a clip")

    def is_abandoned(self) -> bool:
        """Whether a write failed and the current clip has been given up on."""
        return bool(getattr(self, "dead", False))


class MotionRecorder:
    """
    A class to monitor cats using motion detection and record video.
    Uses circular buffer to avoid camera resource conflicts.
    """

    def __init__(
        self,
        camera_manager: CameraManager,
        video_directory: str | Path = "videos",
        file_prefix: str = "cat_video",
        motion_threshold: int = 300,
        dark_brightness: float = 10,
        motion_timeout: float = 10,
        mog2_history: int = 500,
        buffer_seconds: int = 5,
        warmup_frames: int = 30,
        max_clip_seconds: float = 300,
        min_free_bytes: int = 1_000_000_000,
        metrics: MetricsLog | None = None,
    ) -> None:
        """
        Initializes the MotionRecorder with a shared camera manager.
        """
        if warmup_frames < 0:
            raise ValueError("warmup_frames must not be negative")

        self.camera_manager = camera_manager
        self.picam2 = camera_manager.get_camera()
        self.recording = False

        self.motion_threshold = motion_threshold
        self.dark_brightness = dark_brightness
        self.dark = False
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=mog2_history
        )
        self.last_motion_pixels = 0
        self.last_blob: Blob | None = None
        self.metrics = metrics
        self.warmup_frames = warmup_frames
        self._frames_seen = 0
        if warmup_frames == 0:
            log.info("Motion detection armed (warmup disabled)")

        self.file_prefix = file_prefix
        self.video_directory = Path(video_directory)
        self.video_directory.mkdir(parents=True, exist_ok=True)

        self.last_motion_time = time.monotonic()
        self.motion_timeout = motion_timeout
        self.max_clip_seconds = max_clip_seconds
        self.min_free_bytes = min_free_bytes
        self._clip_started = 0.0
        self._clip_started_at: datetime.datetime | None = None
        self._trigger_blob: Blob | None = None
        self._disk_checked_at: float | None = None
        self._had_room = True

        self.buffer_seconds = buffer_seconds
        self.circular_output: Any = None
        self.encoder: Any = None
        self.current_filename: Path | None = None
        self._writing_path: Path | None = None
        self._drain_thread: threading.Thread | None = None
        self._closed = False

        self._recover_interrupted_clips()
        self._setup_circular_recording()
        self.camera_manager.add_consumer(self._process_frames)

    def _recover_interrupted_clips(self) -> None:
        """Give clips cut off by an unclean shutdown their final name, so they ship."""
        pattern = f"*{CLIP_SUFFIX}{PARTIAL_SUFFIX}"
        for writing in sorted(self.video_directory.glob(pattern)):
            clip = writing.with_name(writing.name.removesuffix(PARTIAL_SUFFIX))
            if clip.exists():
                log.warning("Left %s alone: %s already exists", writing.name, clip.name)
            elif self._promote(writing, clip):
                log.warning("Recovered %s, cut off by an unclean shutdown", clip.name)

    def _setup_circular_recording(self) -> None:
        """Run the encoder continuously into an in-memory ring buffer.

        Nothing reaches disk until _start_saving() attaches a file. Raises if the
        encoder cannot start.
        """
        # One keyframe per second: the review page samples keyframes.
        self.encoder = H264Encoder(bitrate=H264_BITRATE, iperiod=CAMERA_FPS)
        self.circular_output = _ResilientCircularOutput(
            buffersize=self.buffer_seconds * CAMERA_FPS
        )

        # Encodes the "main" stream, separately from the CameraManager fan-out.
        self.picam2.start_recording(self.encoder, self.circular_output)
        log.info("Circular recording started")

    # --- detection ----------------------------------------------------------

    def _process_frames(self, main_frame: Frame, lores_frame: Frame) -> None:  # noqa: ARG002 -- signature fixed by FrameConsumer
        """Consumer callback: detect on the lores frame and drive the recorder."""
        if self._closed:
            return

        now = time.monotonic()

        if self.detect_motion(lores_frame):
            self.last_motion_time = now
            self._start_saving()
        elif self._motion_has_stopped(now):
            self._stop_saving(CloseReason.TIMEOUT)

        if self._clip_has_run_too_long(now):
            log.info("Clip reached the %.0fs limit", self.max_clip_seconds)
            self._stop_saving(CloseReason.MAX_LENGTH)

    def detect_motion(self, frame: Frame) -> bool:
        # RGB -> BGR followed by BGR -> GRAY is the same as one RGB -> GRAY pass.
        gray = cast(Frame, cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY))
        mask = self._foreground_mask(gray)
        self.last_motion_pixels = int(cv2.countNonZero(mask))

        if self.metrics is not None:
            self.metrics.record(measure(gray, mask), self.recording)

        if self._is_warming_up() or self._is_dark(gray):
            return False
        moving = self.last_motion_pixels > self.motion_threshold
        if moving:
            self.last_blob = largest_blob(clean_mask(mask))
        return moving

    def _foreground_mask(self, gray: Frame) -> Frame:
        """Binary foreground mask, with MOG2's shadow pixels excluded.

        Shadow detection stays on: disabling it relabels shadows as foreground
        rather than removing them.
        """
        mask = self.background_subtractor.apply(gray)
        _, binary = cv2.threshold(mask, SHADOW_PIXEL_VALUE, 255, cv2.THRESH_BINARY)
        return cast(Frame, binary)

    def _is_warming_up(self) -> bool:
        """Whether MOG2 is still training. Detection is suppressed until it is not."""
        if self._frames_seen >= self.warmup_frames:
            return False

        self._frames_seen += 1
        if self._frames_seen == self.warmup_frames:
            log.info("Motion detection armed after %d frames", self.warmup_frames)
        return True

    def _is_dark(self, gray: Frame) -> bool:
        """Whether the scene is too dark to see. Logs each change."""
        dark = float(cv2.mean(gray)[0]) < self.dark_brightness
        if dark != self.dark:
            self.dark = dark
            log.info(
                "Scene is dark; ignoring motion"
                if dark
                else "Scene is lit; detecting motion"
            )
        return dark

    def _motion_has_stopped(self, now: float) -> bool:
        return self.recording and now - self.last_motion_time > self.motion_timeout

    def _clip_has_run_too_long(self, now: float) -> bool:
        return self.recording and now - self._clip_started > self.max_clip_seconds

    # --- clip lifecycle -----------------------------------------------------

    def _start_saving(self) -> Path | None:
        """Divert the ring buffer to a file.

        The clip opens with whatever pre-motion footage the buffer holds, which
        is bounded by the gap since the last clip ended.
        """
        if self.recording or self.circular_output is None:
            return None
        if self._previous_clip_is_still_flushing() or not self._has_room():
            return None

        started = datetime.datetime.now()
        path = self._next_clip_path(started)
        writing = partial_name(path)
        try:
            self.circular_output.dead = False
            self.circular_output.fileoutput = writing
            self.circular_output.start()
        except Exception:
            log.exception("Failed to start saving to %s", path)
            return None

        self.current_filename = path
        self._writing_path = writing
        self.recording = True
        self._clip_started = time.monotonic()
        self._clip_started_at = started
        self._trigger_blob = self.last_blob
        log.info(
            "Started saving to %s (motion pixels: %d)", path, self.last_motion_pixels
        )
        return path

    def _stop_saving(self, reason: CloseReason) -> Path | None:
        """Stop writing to the file and fall back to buffering only.

        The ring buffer is drained to disk on a separate thread.
        """
        if not self.recording:
            return None

        saved = self.current_filename
        writing = self._writing_path
        output = self.circular_output
        facts = self._clip_facts(reason)
        self.recording = False
        self.current_filename = None
        self._writing_path = None

        self._drain_thread = threading.Thread(
            target=self._finish_clip,
            args=(output, writing, saved, facts),
            daemon=True,
        )
        self._drain_thread.start()
        return saved

    def _clip_facts(self, reason: CloseReason) -> ClipFacts | None:
        if self._clip_started_at is None:
            return None
        return ClipFacts(
            started_at=self._clip_started_at,
            ended_at=datetime.datetime.now(),
            close_reason=reason,
            trigger_blob=self._trigger_blob,
            last_blob=self.last_blob,
        )

    def _finish_clip(
        self,
        output: Any,
        writing: Path | None,
        path: Path | None,
        facts: ClipFacts | None,
    ) -> None:
        """Drain the ring buffer into the clip and close it. Runs off-thread."""
        try:
            output.stop()
        except Exception:
            log.exception("Error finishing %s", path)
            return

        abandoned = output.is_abandoned()
        if abandoned and facts is not None:
            facts = replace(facts, close_reason=CloseReason.ABANDONED)
        self._record_facts(writing, path, facts)
        promoted = self._promote(writing, path)
        if abandoned:
            log.warning("Clip %s is incomplete: writes failed partway through", path)
        elif promoted:
            log.info("Stopped saving %s", path)

    @staticmethod
    def _record_facts(
        writing: Path | None, path: Path | None, facts: ClipFacts | None
    ) -> None:
        """Write the sidecar. Runs before the clip takes its final name."""
        if facts is None or path is None or not _has_content(writing):
            return
        try:
            write_sidecar(path, facts)
        except OSError as error:
            log.error("Could not write the sidecar for %s: %s", path.name, error)  # noqa: TRY400 -- the message is the whole story

    def _promote(self, writing: Path | None, path: Path | None) -> bool:
        """Give the clip its final name, which is what marks it ready to ship."""
        if writing is None or path is None:
            return False
        try:
            if writing.stat().st_size == 0:
                writing.unlink()
                log.warning("Discarded empty clip %s", path.name)
                return False
            writing.replace(path)
        except OSError:
            log.exception("Could not finish %s", path)
            return False
        return True

    def _previous_clip_is_still_flushing(self) -> bool:
        return self._drain_thread is not None and self._drain_thread.is_alive()

    def _next_clip_path(self, started: datetime.datetime) -> Path:
        """A local-time clip path, suffixed if that name is already taken."""
        stamp = started.strftime("%Y%m%d_%H%M%S")
        path = self.video_directory / f"{self.file_prefix}_{stamp}{CLIP_SUFFIX}"
        attempt = 1
        while path.exists() or partial_name(path).exists():
            name = f"{self.file_prefix}_{stamp}_{attempt}{CLIP_SUFFIX}"
            path = self.video_directory / name
            attempt += 1
        return path

    def _has_room(self) -> bool:
        """Whether the clip directory has space, rechecked every few seconds.

        Called on the capture thread for every motion frame, so the check is
        throttled rather than run per frame.
        """
        now = time.monotonic()
        if (
            self._disk_checked_at is not None
            and now - self._disk_checked_at < DISK_CHECK_INTERVAL_SECONDS
        ):
            return self._had_room

        self._disk_checked_at = now
        self._had_room = self._measure_room()
        return self._had_room

    def _measure_room(self) -> bool:
        try:
            free = shutil.disk_usage(self.video_directory).free
        except OSError as error:
            log.error(  # noqa: TRY400 -- the message is the whole story
                "Cannot check free space on %s: %s", self.video_directory, error
            )
            return False

        if free < self.min_free_bytes:
            log.warning("Refusing to record: only %.1f GB free", free / 1e9)
            return False
        return True

    def cleanup(self) -> None:
        """Clean up resources"""
        self._closed = True
        try:
            if self.recording:
                self._stop_saving(CloseReason.SHUTDOWN)
            self._await_final_flush()

            if self.metrics is not None:
                self.metrics.close()

            if self.encoder:
                # stop_encoder, not stop_recording: the camera is shared.
                self.picam2.stop_encoder(self.encoder)
                self.encoder = None
                self.circular_output = None
        except Exception:
            log.exception("Cleanup error")

    def _await_final_flush(self) -> None:
        if self._drain_thread is None:
            return
        self._drain_thread.join(timeout=CLIP_FLUSH_TIMEOUT_SECONDS)
        if self._drain_thread.is_alive():
            log.warning(
                "Clip still flushing after %ds; abandoning it",
                CLIP_FLUSH_TIMEOUT_SECONDS,
            )
