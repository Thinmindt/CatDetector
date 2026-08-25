import datetime
import time
from pathlib import Path
from typing import Any

import cv2
from picamera2.encoders import H264Encoder
from picamera2.outputs import CircularOutput

from src.camera_manager import CameraManager, Frame


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
        buffer_seconds: int = 30,  # Keep 30 seconds of pre-motion footage
    ) -> None:
        """
        Initializes the MotionRecorder with a shared camera manager.
        """

        # Use shared camera
        self.camera_manager = camera_manager
        self.picam2 = camera_manager.get_camera()
        self.recording = False

        # Initialize motion detection parameters
        self.motion_threshold = motion_threshold
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2()
        self.last_motion_pixels = 0

        # File management
        self.file_prefix = file_prefix
        self.video_directory = Path(video_directory)
        self.video_directory.mkdir(parents=True, exist_ok=True)

        # Motion tracking
        self.last_motion_time = time.time()
        self.motion_timeout = motion_timeout

        # Circular buffer setup
        self.buffer_seconds = buffer_seconds
        self.circular_output: Any = None
        self.encoder: Any = None
        self.current_filename: Path | None = None

        # Initialize continuous recording
        self._setup_circular_recording()

        # Register as consumer of camera frames
        self.camera_manager.add_consumer(self._process_frames)

    def _setup_circular_recording(self) -> None:
        """Run the encoder continuously into an in-memory ring buffer.

        Nothing reaches disk until _start_saving() attaches a file to the output.
        Failures are deliberately not caught: a recorder that cannot buffer can
        never record, so it should fail at construction rather than silently.
        """
        self.encoder = H264Encoder(bitrate=10000000)
        # buffersize is counted in frames, and the camera runs at ~30fps.
        self.circular_output = CircularOutput(buffersize=self.buffer_seconds * 30)

        # Encodes the "main" stream. This is a separate path from the frame
        # fan-out in CameraManager, which only feeds detection and the web view.
        self.picam2.start_recording(self.encoder, self.circular_output)
        print("Circular recording started")

    def _process_frames(self, main_frame: Frame, lores_frame: Frame) -> None:
        """Process frames from camera manager"""
        # Use lores frame for motion detection (more efficient)
        if self.detect_motion(lores_frame):
            self.last_motion_time = time.time()

            # Start saving if not already saving
            if not self.recording:
                self._start_saving()

        # If saving but no motion for timeout period, stop saving
        elif (
            self.recording and time.time() - self.last_motion_time > self.motion_timeout
        ):
            self._stop_saving()

    def detect_motion(self, frame: Frame) -> bool:
        # RGB -> BGR followed by BGR -> GRAY is the same as one RGB -> GRAY pass.
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        fg_mask = self.background_subtractor.apply(gray)

        # Kept so the threshold can be tuned from the clip-start log line below.
        # Deliberately not printed per frame: this runs ~30 times a second.
        self.last_motion_pixels = int(cv2.countNonZero(fg_mask))
        return self.last_motion_pixels > self.motion_threshold

    def _start_saving(self) -> Path | None:
        """Divert the ring buffer to a file.

        CircularOutput.start() flushes the buffered frames from the most recent
        keyframe onward, so the clip opens with the pre-motion footage that was
        already in the buffer when motion fired.
        """
        if self.recording:
            return None

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        # A raw H.264 elementary stream, not a container -- hence .h264, not .mp4.
        path = self.video_directory / f"{self.file_prefix}_{timestamp}.h264"

        try:
            self.circular_output.fileoutput = path
            self.circular_output.start()
        except Exception as e:
            print(f"Failed to start saving to {path}: {e}")
            return None

        self.current_filename = path
        self.recording = True
        print(f"Started saving to {path} (motion pixels: {self.last_motion_pixels})")
        return path

    def _stop_saving(self) -> Path | None:
        """Stop writing to the file and fall back to buffering only."""
        if not self.recording:
            return None

        saved = self.current_filename
        try:
            # Drains whatever is still buffered, then closes the file handle.
            self.circular_output.stop()
        except Exception as e:
            print(f"Error stopping save: {e}")
        finally:
            self.recording = False
            self.current_filename = None

        print(f"Stopped saving {saved}")
        return saved

    def cleanup(self) -> None:
        """Clean up resources"""
        try:
            if self.recording:
                self._stop_saving()

            if self.encoder:
                # Deliberately not stop_recording(): that also stops the shared
                # camera, cutting off every other consumer. The camera belongs
                # to CameraManager, so only the encoder is ours to stop.
                self.picam2.stop_encoder(self.encoder)
                self.encoder = None
                self.circular_output = None
        except Exception as e:
            print(f"Cleanup error: {e}")
