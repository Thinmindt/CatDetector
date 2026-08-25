import datetime
import os
import time
from pathlib import Path
from typing import Any

import cv2
from picamera2.encoders import H264Encoder
from picamera2.outputs import CircularOutput, FfmpegOutput

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
        file_prefix: str = "cat_video_",
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

        # File management
        self.file_prefix = file_prefix
        self.video_directory = video_directory
        if not os.path.exists(video_directory):
            os.makedirs(video_directory)

        # Motion tracking
        self.last_motion_time = time.time()
        self.motion_timeout = motion_timeout

        # Circular buffer setup
        self.buffer_seconds = buffer_seconds
        self.circular_output: Any = None
        self.encoder: Any = None
        self.file_output: Any = None
        self.current_filename: str | None = None

        # Initialize continuous recording
        self._setup_circular_recording()

        # Register as consumer of camera frames
        self.camera_manager.add_consumer(self._process_frames)

    def _setup_circular_recording(self) -> None:
        """Setup circular buffer recording that runs continuously"""
        try:
            self.encoder = H264Encoder(bitrate=10000000)
            # Create circular buffer (keeps last N seconds in memory)
            self.circular_output = CircularOutput(
                buffersize=self.buffer_seconds * 30
            )  # ~30fps

            # Start continuous recording to circular buffer
            self.picam2.start_recording(self.encoder, self.circular_output)
            print("Circular recording started")
        except Exception as e:
            print(f"Failed to setup circular recording: {e}")

    def _process_frames(self, main_frame: Frame, lores_frame: Frame) -> None:
        """Process frames from camera manager"""
        # Use lores frame for motion detection (more efficient)
        motion_detected = self.detect_motion(lores_frame)

        # Update last motion time if motion is detected
        if motion_detected:
            self.last_motion_time = time.time()

            # Start saving if not already saving
            if not self.recording:
                self._start_saving()

        # If saving but no motion for timeout period, stop saving
        if self.recording and time.time() - self.last_motion_time > self.motion_timeout:
            self._stop_saving()

    def detect_motion(self, frame: Frame) -> bool:
        # Convert RGB to BGR for OpenCV processing
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        # Convert to grayscale and apply background subtraction
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        fg_mask = self.background_subtractor.apply(gray)
        motion_pixels = cv2.countNonZero(fg_mask)

        description = (
            f"Motion pixels: {motion_pixels}, threshold: {self.motion_threshold}"
        )
        if motion_pixels > self.motion_threshold:
            print(f"{description} - Motion detected!")
            return True

        print(description)
        return False

    def _start_saving(self) -> str | None:
        """Start saving using split recording method"""
        if self.recording:
            return None

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.file_prefix}_{timestamp}.mp4"
        self.current_filename = os.path.join(str(self.video_directory), filename)

        try:
            # Create output for the split
            self.file_output = FfmpegOutput(self.current_filename)

            # Split recording to save to file while keeping circular buffer
            self.picam2.split_recording(self.file_output)

            self.recording = True
            print(f"Started saving to {self.current_filename}")
            return self.current_filename
        except Exception as e:
            print(f"Failed to start saving: {e}")
            return None

    def _stop_saving(self) -> str | None:
        """Stop split recording"""
        if not self.recording:
            return None

        try:
            # Split back to just circular buffer (stops file recording)
            self.picam2.split_recording(self.circular_output)

            # Clean up file output
            self.file_output = None

            print(f"Stopped saving {self.current_filename}")
        except Exception as e:
            print(f"Error stopping save: {e}")
        finally:
            self.recording = False

        return self.current_filename

    def cleanup(self) -> None:
        """Clean up resources"""
        try:
            if self.recording:
                self._stop_saving()

            if self.circular_output:
                self.picam2.stop_recording()

            if self.encoder:
                self.encoder = None
        except Exception as e:
            print(f"Cleanup error: {e}")
