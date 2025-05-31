import time
import cv2
import datetime
import os
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput


class MotionRecorder:
    """
    A class to monitor a litter box using a camera, detect motion, and record video.
    This class uses the Picamera2 library for camera interaction and OpenCV for motion detection.
    """

    def __init__(
        self, video_directory="videos", file_prefix="cat_video_", motion_threshold=1000
    ):
        """
        Initializes the MotionRecorder with a specified video directory.
        :param video_directory: Directory where recorded videos will be saved.
        """

        # Initialize the camera
        self.picam2 = Picamera2()
        self.picam2.configure(self.picam2.create_video_configuration())
        self.recording = False

        # Initialize motion detection parameters
        self.motion_threshold = motion_threshold
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2()

        # File management
        self.file_prefix = file_prefix
        self.video_directory = video_directory
        if not os.path.exists(video_directory):
            os.makedirs(video_directory)

    def detect_motion(self, frame):
        # Convert to grayscale and apply background subtraction
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        fg_mask = self.background_subtractor.apply(gray)

        # Count non-zero pixels (motion)
        motion_pixels = cv2.countNonZero(fg_mask)
        return motion_pixels > self.motion_threshold  # Threshold to tune

    def start_recording(self):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.file_prefix}_{timestamp}.mp4"

        self.encoder = H264Encoder(bitrate=10000000)  # 10Mbps
        self.output = FfmpegOutput(self.current_filename)

        self.picam2.start_recording(self.encoder, self.output)
        self.recording = True
        print(f"Started recording to {self.current_filename}")
        return self.current_filename

    def stop_recording(self):
        if not self.recording:
            return

        self.picam2.stop_recording()
        self.recording = False
        print(f"Stopped recording {self.current_filename}")
        return self.current_filename

    def monitor(self):
        """Main monitoring loop that handles motion detection and recording."""
        try:
            print("Starting motion monitoring...")
            while True:
                # Capture a frame
                frame = self.picam2.capture_array()

                # Check for motion
                motion_detected = self.detect_motion(frame)

                # Update last motion time if motion is detected
                if motion_detected:
                    self.last_motion_time = time.time()

                    # Start recording if not already recording
                    if not self.recording:
                        self.start_recording()

                # If recording but no motion for timeout period, stop recording
                if (
                    self.recording
                    and time.time() - self.last_motion_time > self.motion_timeout
                ):
                    self.stop_recording()

                # Sleep briefly to prevent high CPU usage
                time.sleep(0.1)

        except KeyboardInterrupt:
            print("Monitoring stopped by user")
            if self.recording:
                self.stop_recording()
            self.picam2.close()
        except Exception as e:
            print(f"Error in monitoring: {e}")
            if self.recording:
                self.stop_recording()
            self.picam2.close()
            raise
