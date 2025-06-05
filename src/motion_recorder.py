import threading
import time
import cv2
import datetime
import os
from flask import Flask, Response
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput


class MotionRecorder:
    """
    A class to monitor a cats using a camera, detect motion, and record video.
    This class uses the Picamera2 library for camera interaction and OpenCV for motion detection.
    """

    def __init__(
        self,
        video_directory="videos",
        file_prefix="cat_video_",
        motion_threshold=5000,
        enable_streaming=False,
        stream_port=5000,
    ):
        """
        Initializes the MotionRecorder with a specified video directory.
        :param video_directory: Directory where recorded videos will be saved.
        """

        # Initialize the camera
        self.picam2 = Picamera2()
        self.picam2.configure(self.picam2.create_video_configuration())
        self.picam2.start()
        self.recording = False

        # Initialize motion detection parameters
        self.motion_threshold = motion_threshold
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2()

        # File management
        self.file_prefix = file_prefix
        self.video_directory = video_directory
        if not os.path.exists(video_directory):
            os.makedirs(video_directory)

        # Streaming setup
        self.enable_streaming = enable_streaming
        self.latest_frame = None
        self.frame_lock = threading.Lock()

        if enable_streaming:
            self.setup_streaming(stream_port)

    def setup_streaming(self, port):
        """Setup Flask web streaming"""

        self.app = Flask(__name__)

        @self.app.route("/")
        def index():
            return """
            <!DOCTYPE html>
            <html>
            <head><title>Cat Motion Detector</title></head>
            <body>
                <h1>Cat Motion Detector - Live Feed</h1>
                <img src="/video_feed" style="width:100%; max-width:800px;">
                <p>Motion Threshold: {}</p>
            </body>
            </html>
            """.format(
                self.motion_threshold
            )

        @self.app.route("/video_feed")
        def video_feed():
            return Response(
                self.generate_stream_frames(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
            )

        # Start streaming in background thread
        stream_thread = threading.Thread(
            target=lambda: self.app.run(
                host="0.0.0.0", port=port, debug=False, use_reloader=False
            )
        )
        stream_thread.daemon = True
        stream_thread.start()

    def generate_stream_frames(self):
        """Generate frames for web streaming"""
        while True:
            with self.frame_lock:
                if self.latest_frame is not None:
                    # Convert frame to JPEG
                    ret, buffer = cv2.imencode(
                        ".jpg", self.latest_frame, [cv2.IMWRITE_JPEG_QUALITY, 70]
                    )
                    if ret:
                        frame_bytes = buffer.tobytes()
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                        )
            time.sleep(0.033)  # ~30 FPS

    def detect_motion(self, frame):
        # Store latest frame for streaming
        if self.enable_streaming:
            with self.frame_lock:
                self.latest_frame = frame.copy()

        # ...existing motion detection code...
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        fg_mask = self.background_subtractor.apply(gray)
        motion_pixels = cv2.countNonZero(fg_mask)

        # Add debug visualization to stream
        if self.enable_streaming and motion_pixels > self.motion_threshold:
            with self.frame_lock:
                # Add motion detection overlay
                cv2.putText(
                    self.latest_frame,
                    f"MOTION DETECTED! ({motion_pixels} pixels)",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 0, 255),
                    2,
                )

        return motion_pixels > self.motion_threshold

    def start_recording(self):
        if self.recording:
            return self.current_filename

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.file_prefix}_{timestamp}.mp4"
        self.current_filename = os.path.join(
            str(self.video_directory), filename
        )  # Fix: set before using

        self.encoder = H264Encoder(bitrate=10000000)
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

        self.last_motion_time = time.time()
        self.motion_timeout = 120

        try:
            print("Starting motion monitoring...")
            while True:
                # Capture a frame
                frame = self.picam2.capture_array()
                if frame is None:
                    print("Failed to capture frame, retrying...")
                    time.sleep(1)
                    continue

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
