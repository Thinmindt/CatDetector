import datetime
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import cv2
from picamera2.encoders import H264Encoder
from picamera2.outputs import CircularOutput

from src.camera_manager import CameraManager, Frame


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
        if getattr(self, "dead", False):
            return  # this clip is already lost; stop hammering the share
        try:
            super()._write(frame, timestamp)
        except OSError as error:
            self.dead = True
            print(f"Clip write failed, abandoning clip: {error}")


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
        buffer_seconds: int = 5,  # pre-motion footage; see _start_saving
        warmup_frames: int = 30,  # ~1s at 30fps; see detect_motion
        max_clip_seconds: float = 300,
        min_free_bytes: int = 1_000_000_000,
    ) -> None:
        """
        Initializes the MotionRecorder with a shared camera manager.
        """
        if warmup_frames < 0:
            raise ValueError("warmup_frames must not be negative")

        # Use shared camera
        self.camera_manager = camera_manager
        self.picam2 = camera_manager.get_camera()
        self.recording = False

        # Initialize motion detection parameters
        self.motion_threshold = motion_threshold
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2()
        self.last_motion_pixels = 0
        self.warmup_frames = warmup_frames
        self._frames_seen = 0
        if warmup_frames == 0:
            print("Motion detection armed (warmup disabled)")

        # File management
        self.file_prefix = file_prefix
        self.video_directory = Path(video_directory)
        self.video_directory.mkdir(parents=True, exist_ok=True)

        # Motion tracking. Durations use the monotonic clock: this Pi has no RTC
        # backup cell, and an NTP step would otherwise hold a clip open (or cut
        # it short) by the size of the jump.
        self.last_motion_time = time.monotonic()
        self.motion_timeout = motion_timeout
        self.max_clip_seconds = max_clip_seconds
        self.min_free_bytes = min_free_bytes
        self._clip_started = 0.0

        # Circular buffer setup
        self.buffer_seconds = buffer_seconds
        self.circular_output: Any = None
        self.encoder: Any = None
        self.current_filename: Path | None = None
        self._drain_thread: threading.Thread | None = None
        self._closed = False

        # Initialize continuous recording
        self._setup_circular_recording()

        # Register as consumer of camera frames
        self.camera_manager.add_consumer(self._process_frames)

    def _setup_circular_recording(self) -> None:
        """Run the encoder continuously into an in-memory ring buffer.

        Nothing reaches disk until _start_saving() attaches a file to the output.
        Failures are deliberately not caught: a recorder that cannot buffer can
        never record, so it should fail at construction. main.py degrades to a
        bare stream rather than dying.
        """
        self.encoder = H264Encoder(bitrate=10000000)
        # buffersize is counted in frames, and the camera runs at ~30fps.
        self.circular_output = _ResilientCircularOutput(
            buffersize=self.buffer_seconds * 30
        )

        # Encodes the "main" stream. This is a separate path from the frame
        # fan-out in CameraManager, which only feeds detection and the web view.
        self.picam2.start_recording(self.encoder, self.circular_output)
        print("Circular recording started")

    def _process_frames(self, main_frame: Frame, lores_frame: Frame) -> None:
        """Process frames from camera manager"""
        # The consumer stays registered after cleanup(), so without this the
        # recorder would keep trying to open clips against a torn-down output.
        if self._closed:
            return

        now = time.monotonic()

        # Use lores frame for motion detection (more efficient)
        if self.detect_motion(lores_frame):
            self.last_motion_time = now

            # Start saving if not already saving
            if not self.recording:
                self._start_saving()

        # If saving but no motion for timeout period, stop saving
        elif self.recording and now - self.last_motion_time > self.motion_timeout:
            self._stop_saving()

        # A scene that keeps triggering -- a sunbeam or a shadow tracking across
        # the floor, both of which MOG2 reports as foreground -- would otherwise
        # record without bound at 10 Mbit/s (4.5 GB/hour).
        if self.recording and now - self._clip_started > self.max_clip_seconds:
            print(f"Clip reached the {self.max_clip_seconds:.0f}s limit")
            self._stop_saving()

    def detect_motion(self, frame: Frame) -> bool:
        # RGB -> BGR followed by BGR -> GRAY is the same as one RGB -> GRAY pass.
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        fg_mask = self.background_subtractor.apply(gray)

        # Kept so the threshold can be tuned from the clip-start log line below.
        # Deliberately not printed per frame: this runs ~30 times a second.
        self.last_motion_pixels = int(cv2.countNonZero(fg_mask))

        # MOG2 has no background model on its first frame, so it calls the whole
        # frame foreground -- which used to trigger a clip on every startup. Keep
        # feeding it frames so the model trains, but do not report motion until it
        # has settled. Measured on this camera: frame 0 is 100% foreground, frame 1
        # is ~3.8% (still over the default threshold), frame 2 onward is under 25
        # pixels. 30 frames is roughly a second of margin on top of that.
        if self._frames_seen < self.warmup_frames:
            self._frames_seen += 1
            if self._frames_seen == self.warmup_frames:
                print(f"Motion detection armed after {self.warmup_frames} frames")
            return False

        return self.last_motion_pixels > self.motion_threshold

    def _next_path(self) -> Path:
        """A timestamped clip path that does not collide with an existing file.

        The timestamp is local time, which is ambiguous across the DST fall-back
        where the same wall-clock second happens twice. picamera2 opens the file
        "wb", so without this guard the earlier clip would be silently truncated.
        """
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.video_directory / f"{self.file_prefix}_{stamp}.h264"
        attempt = 1
        while path.exists():
            path = self.video_directory / f"{self.file_prefix}_{stamp}_{attempt}.h264"
            attempt += 1
        return path

    def _has_room(self) -> bool:
        """Refuse to start a clip that would fill the share."""
        try:
            free = shutil.disk_usage(self.video_directory).free
        except OSError as error:
            print(f"Cannot check free space on {self.video_directory}: {error}")
            return False

        if free < self.min_free_bytes:
            print(f"Refusing to record: only {free / 1e9:.1f} GB free")
            return False
        return True

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

        # The previous clip is still flushing and still owns the output.
        if self._drain_thread is not None and self._drain_thread.is_alive():
            return None

        if not self._has_room():
            return None

        path = self._next_path()
        try:
            self.circular_output.dead = False
            self.circular_output.fileoutput = path
            self.circular_output.start()
        except Exception as e:
            print(f"Failed to start saving to {path}: {e}")
            return None

        self.current_filename = path
        self.recording = True
        self._clip_started = time.monotonic()
        print(f"Started saving to {path} (motion pixels: {self.last_motion_pixels})")
        return path

    def _stop_saving(self) -> Path | None:
        """Stop writing to the file and fall back to buffering only.

        The actual flush happens on its own thread: CircularOutput.stop() drains
        the whole ring buffer to disk, and doing that inline would stall the
        capture thread -- and picamera2's event loop with it -- for the length of
        a multi-megabyte network write.
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
        except Exception as error:
            print(f"Error finishing {path}: {error}")
            return

        if getattr(output, "dead", False):
            print(f"Clip {path} is incomplete: writes failed partway through")
        else:
            print(f"Stopped saving {path}")

    def cleanup(self) -> None:
        """Clean up resources"""
        self._closed = True
        try:
            if self.recording:
                self._stop_saving()

            # Let the last clip finish flushing before the encoder goes away.
            if self._drain_thread is not None:
                self._drain_thread.join(timeout=30)
                if self._drain_thread.is_alive():
                    print("Clip still flushing after 30s; abandoning it")

            if self.encoder:
                # Deliberately not stop_recording(): that also stops the shared
                # camera, cutting off every other consumer. The camera belongs
                # to CameraManager, so only the encoder is ours to stop.
                self.picam2.stop_encoder(self.encoder)
                self.encoder = None
                self.circular_output = None
        except Exception as e:
            print(f"Cleanup error: {e}")
