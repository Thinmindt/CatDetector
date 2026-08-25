import datetime
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import cv2
from picamera2.encoders import H264Encoder
from picamera2.outputs import CircularOutput

from src.camera_manager import CameraManager, Frame

log = logging.getLogger(__name__)

CAMERA_FPS = 30
H264_BITRATE = 10_000_000

# CircularOutput writes a raw H.264 elementary stream, not a container.
CLIP_SUFFIX = ".h264"

CLIP_FLUSH_TIMEOUT_SECONDS = 30


class _ResilientCircularOutput(CircularOutput):  # type: ignore[misc]
    """CircularOutput that never lets a storage error reach picamera2.

    Frames are written inline on picamera2's camera event-loop thread. The base
    class only catches connection errors, so an OSError from the network share
    (ENOSPC, EIO, ESTALE on a soft CIFS mount) would propagate into that thread
    and kill it -- taking the camera with it and leaving every capture_arrays()
    call blocked forever. Swallow it, mark the output dead, and let the recorder
    report the clip as incomplete.
    """

    def _write(self, frame: Any, timestamp: Any = None) -> None:
        if self.is_abandoned():
            return
        try:
            super()._write(frame, timestamp)
        except OSError as error:
            self.dead = True
            # Anticipated (share full or gone) and the errno text says it all,
            # so no traceback.
            log.error("Clip write failed, abandoning clip: %s", error)  # noqa: TRY400

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
        motion_threshold: int = 5000,
        motion_timeout: float = 10,
        buffer_seconds: int = 5,
        warmup_frames: int = 30,
        max_clip_seconds: float = 300,
        min_free_bytes: int = 1_000_000_000,
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
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2()
        self.last_motion_pixels = 0
        self.warmup_frames = warmup_frames
        self._frames_seen = 0
        if warmup_frames == 0:
            log.info("Motion detection armed (warmup disabled)")

        self.file_prefix = file_prefix
        self.video_directory = Path(video_directory)
        self.video_directory.mkdir(parents=True, exist_ok=True)

        # Durations use the monotonic clock: this Pi has no RTC backup cell, and
        # an NTP step would otherwise hold a clip open (or cut it short) by the
        # size of the jump.
        self.last_motion_time = time.monotonic()
        self.motion_timeout = motion_timeout
        self.max_clip_seconds = max_clip_seconds
        self.min_free_bytes = min_free_bytes
        self._clip_started = 0.0

        self.buffer_seconds = buffer_seconds
        self.circular_output: Any = None
        self.encoder: Any = None
        self.current_filename: Path | None = None
        self._drain_thread: threading.Thread | None = None
        self._closed = False

        self._setup_circular_recording()
        self.camera_manager.add_consumer(self._process_frames)

    def _setup_circular_recording(self) -> None:
        """Run the encoder continuously into an in-memory ring buffer.

        Nothing reaches disk until _start_saving() attaches a file to the output.
        Failures are deliberately not caught: a recorder that cannot buffer can
        never record, so it should fail at construction. main.py degrades to a
        bare stream rather than dying.
        """
        self.encoder = H264Encoder(bitrate=H264_BITRATE)
        self.circular_output = _ResilientCircularOutput(
            buffersize=self.buffer_seconds * CAMERA_FPS
        )

        # Encodes the "main" stream. This is a separate path from the frame
        # fan-out in CameraManager, which only feeds detection and the web view.
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
            self._stop_saving()

        if self._clip_has_run_too_long(now):
            log.info("Clip reached the %.0fs limit", self.max_clip_seconds)
            self._stop_saving()

    def detect_motion(self, frame: Frame) -> bool:
        self.last_motion_pixels = self._count_foreground_pixels(frame)
        if self._is_warming_up():
            return False
        return self.last_motion_pixels > self.motion_threshold

    def _count_foreground_pixels(self, frame: Frame) -> int:
        # RGB -> BGR followed by BGR -> GRAY is the same as one RGB -> GRAY pass.
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        return int(cv2.countNonZero(self.background_subtractor.apply(gray)))

    def _is_warming_up(self) -> bool:
        """Whether MOG2 is still building its background model.

        It has no model on its first frame and so calls the whole frame
        foreground, which used to trigger a clip on every startup. Measured on
        this camera: frame 0 is 100% foreground, frame 1 ~3.8% (still over the
        default threshold), frame 2 onward under 25 pixels.
        """
        if self._frames_seen >= self.warmup_frames:
            return False

        self._frames_seen += 1
        if self._frames_seen == self.warmup_frames:
            log.info("Motion detection armed after %d frames", self.warmup_frames)
        return True

    def _motion_has_stopped(self, now: float) -> bool:
        return self.recording and now - self.last_motion_time > self.motion_timeout

    def _clip_has_run_too_long(self, now: float) -> bool:
        """A sunbeam or a shadow tracking across the floor reads as motion
        indefinitely, and would otherwise record without bound."""
        return self.recording and now - self._clip_started > self.max_clip_seconds

    # --- clip lifecycle -----------------------------------------------------

    def _start_saving(self) -> Path | None:
        """Divert the ring buffer to a file.

        CircularOutput.start() flushes the buffered frames from the *oldest*
        keyframe onward, so the clip opens with the pre-motion footage that was
        in the buffer when motion fired. Note that stopping a clip drains the
        buffer, so pre-motion footage is only as long as the gap since the last
        clip ended, up to buffer_seconds.
        """
        if self.recording or self.circular_output is None:
            return None
        if self._previous_clip_is_still_flushing() or not self._has_room():
            return None

        path = self._next_clip_path()
        try:
            self.circular_output.dead = False
            self.circular_output.fileoutput = path
            self.circular_output.start()
        except Exception:
            log.exception("Failed to start saving to %s", path)
            return None

        self.current_filename = path
        self.recording = True
        self._clip_started = time.monotonic()
        log.info(
            "Started saving to %s (motion pixels: %d)", path, self.last_motion_pixels
        )
        return path

    def _stop_saving(self) -> Path | None:
        """Stop writing to the file and fall back to buffering only.

        The flush happens on its own thread: CircularOutput.stop() drains the
        whole ring buffer to disk, and doing that inline would stall the capture
        thread -- and picamera2's event loop with it -- for the length of a
        multi-megabyte network write.
        """
        if not self.recording:
            return None

        saved = self.current_filename
        output = self.circular_output
        self.recording = False
        self.current_filename = None

        self._drain_thread = threading.Thread(
            target=self._finish_clip, args=(output, saved), daemon=True
        )
        self._drain_thread.start()
        return saved

    def _finish_clip(self, output: Any, path: Path | None) -> None:
        """Drain the ring buffer into the clip and close it. Runs off-thread."""
        try:
            output.stop()
        except Exception:
            log.exception("Error finishing %s", path)
            return

        if output.is_abandoned():
            log.warning("Clip %s is incomplete: writes failed partway through", path)
        else:
            log.info("Stopped saving %s", path)

    def _previous_clip_is_still_flushing(self) -> bool:
        return self._drain_thread is not None and self._drain_thread.is_alive()

    def _next_clip_path(self) -> Path:
        """A timestamped clip path that does not collide with an existing file.

        The timestamp is local time, which is ambiguous across the DST fall-back
        where the same wall-clock second happens twice. picamera2 opens the file
        "wb", so without this guard the earlier clip would be silently truncated.
        """
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.video_directory / f"{self.file_prefix}_{stamp}{CLIP_SUFFIX}"
        attempt = 1
        while path.exists():
            name = f"{self.file_prefix}_{stamp}_{attempt}{CLIP_SUFFIX}"
            path = self.video_directory / name
            attempt += 1
        return path

    def _has_room(self) -> bool:
        try:
            free = shutil.disk_usage(self.video_directory).free
        except OSError as error:
            log.error(  # noqa: TRY400 -- anticipated; the errno text is enough
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
                self._stop_saving()
            self._await_final_flush()

            if self.encoder:
                # Deliberately not stop_recording(): that also stops the shared
                # camera, cutting off every other consumer. The camera belongs
                # to CameraManager, so only the encoder is ours to stop.
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
