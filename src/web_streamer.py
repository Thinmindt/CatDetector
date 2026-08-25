import logging
import threading
import time
from collections.abc import Iterator

import cv2
from flask import Flask, Response

from src.camera_manager import CameraManager, Frame
from src.motion_recorder import MotionRecorder

log = logging.getLogger(__name__)

JPEG_QUALITY = 70
STREAM_INTERVAL_SECONDS = 1 / 30

# Kept out of the page f-string below so the CSS braces need no escaping.
_PAGE_CSS = """
body { font-family: Arial, sans-serif; margin: 20px; }
.status {
    background-color: #f0f0f0;
    padding: 10px;
    margin: 10px 0;
    border-radius: 5px;
}
img { max-width: 100%; height: auto; }
"""


class WebStreamer:
    """
    Web streaming component that consumes frames from CameraManager
    """

    def __init__(
        self,
        camera_manager: CameraManager,
        motion_recorder: MotionRecorder | None = None,
        port: int = 5000,
    ) -> None:
        self.camera_manager = camera_manager
        self.motion_recorder = motion_recorder
        self.port = port
        self.app = Flask(__name__)
        self.latest_frame: Frame | None = None
        self.frame_lock = threading.Lock()

        self.setup_routes()

        # Register as consumer
        self.camera_manager.add_consumer(self._consume_frames)

    def _consume_frames(self, main_frame: Frame, lores_frame: Frame) -> None:
        """Consume frames from camera manager"""
        # Use lores frame for streaming (more efficient)
        frame_rgb = main_frame.copy()

        recorder = self.motion_recorder
        if recorder:
            # Check if currently recording
            if recorder.recording:
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
                if recorder.current_filename:
                    filename = recorder.current_filename.name
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
                f"Motion Threshold: {recorder.motion_threshold}",
                (10, frame_rgb.shape[0] - 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),  # White
                1,
            )

        # frame_rgb is already a private copy of main_frame and is never touched
        # again after publishing, so a second copy here would be wasted work.
        with self.frame_lock:
            self.latest_frame = frame_rgb

    def setup_routes(self) -> None:
        @self.app.route("/")
        def index() -> str:
            status_info = ""
            recorder = self.motion_recorder
            if recorder:
                recording = "Yes" if recorder.recording else "No"
                status_info = f"""
                <p><strong>Motion Threshold:</strong>
                    {recorder.motion_threshold}</p>
                <p><strong>Recording:</strong> {recording}</p>
                <p><strong>Timeout:</strong>
                    {recorder.motion_timeout} seconds</p>
                """

            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Cat Detector Live Feed</title>
                <style>{_PAGE_CSS}</style>
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
        def video_feed() -> Response:
            return Response(
                self.generate_frames(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
            )

    def generate_frames(self) -> Iterator[bytes]:
        while True:
            # Take a reference under the lock, then encode and yield outside it.
            # A suspended generator keeps hold of its context manager, so yielding
            # inside the lock would let one slow viewer (a backgrounded tab, a
            # phone on bad wifi) block the capture thread indefinitely -- which
            # now also runs motion detection. Publishing replaces latest_frame
            # wholesale and never mutates it, so the reference stays valid.
            with self.frame_lock:
                frame = self.latest_frame

            if frame is not None:
                ret, buffer = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                )
                if ret:
                    frame_bytes = buffer.tobytes()
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                    )
            time.sleep(STREAM_INTERVAL_SECONDS)

    def start(self) -> None:
        self.app.run(host="0.0.0.0", port=self.port, debug=False, threaded=True)
