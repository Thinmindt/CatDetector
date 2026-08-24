import cv2
import threading
from flask import Flask, Response
import time

from src.camera_manager import CameraManager


class WebStreamer:
    """
    Web streaming component that consumes frames from CameraManager
    """

    def __init__(self, camera_manager: CameraManager, motion_recorder=None, port=5000):
        self.camera_manager = camera_manager
        self.motion_recorder = motion_recorder
        self.port = port
        self.app = Flask(__name__)
        self.latest_frame = None
        self.frame_lock = threading.Lock()

        self.setup_routes()

        # Register as consumer
        self.camera_manager.add_consumer(self._consume_frames)

    def _consume_frames(self, main_frame, lores_frame):
        """Consume frames from camera manager"""
        # Use lores frame for streaming (more efficient)
        frame_rgb = main_frame.copy()

        if self.motion_recorder:
            # Check if currently recording
            if self.motion_recorder.recording:
                cv2.putText(
                    frame_rgb,
                    "RECORDING",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.0,
                    (0, 0, 255),  # Red
                    2,
                )
                # Add recording filename if available
                if (
                    hasattr(self.motion_recorder, "current_filename")
                    and self.motion_recorder.current_filename
                ):
                    filename = self.motion_recorder.current_filename.split("/")[-1]
                    cv2.putText(
                        frame_rgb,
                        f"File: {filename}",
                        (10, 70),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 0, 255),  # Red
                        2,
                    )

            # Add motion threshold info
            cv2.putText(
                frame_rgb,
                f"Motion Threshold: {self.motion_recorder.motion_threshold}",
                (10, frame_rgb.shape[0] - 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),  # White
                1,
            )

        with self.frame_lock:
            self.latest_frame = frame_rgb.copy()

    def setup_routes(self):
        @self.app.route("/")
        def index():
            status_info = ""
            if self.motion_recorder:
                status_info = f"""
                <p><strong>Motion Threshold:</strong> {self.motion_recorder.motion_threshold}</p>
                <p><strong>Recording:</strong> {'Yes' if self.motion_recorder.recording else 'No'}</p>
                <p><strong>Timeout:</strong> {self.motion_recorder.motion_timeout} seconds</p>
                """

            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Cat Detector Live Feed</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .status {{ background-color: #f0f0f0; padding: 10px; margin: 10px 0; border-radius: 5px; }}
                    img {{ max-width: 100%; height: auto; }}
                </style>
            </head>
            <body>
                <h1>Cat Detector Live Feed</h1>
                <div class="status">
                    {status_info}
                </div>
                <img src="/video_feed" style="width:100%; max-width:800px;">
            </body>
            </html>
            """

        @self.app.route("/video_feed")
        def video_feed():
            return Response(
                self.generate_frames(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
            )

    def generate_frames(self):
        while True:
            with self.frame_lock:
                if self.latest_frame is not None:
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

    def start(self):
        self.app.run(host="0.0.0.0", port=self.port, debug=False, threaded=True)
