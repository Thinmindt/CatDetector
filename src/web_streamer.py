import logging
import threading
import time
from collections.abc import Iterator

import cv2
from flask import Blueprint, Response, jsonify

from src.camera_manager import CameraManager
from src.frame import Frame
from src.motion_recorder import MotionRecorder

log = logging.getLogger(__name__)

JPEG_QUALITY = 70
STREAM_INTERVAL_SECONDS = 1 / 30

# picamera2's "RGB888" is BGR in memory, which is what OpenCV expects.
RED = (0, 0, 255)


def caption(frame: Frame, text: str, row: int, scale: float) -> None:
    cv2.putText(frame, text, (10, row), cv2.FONT_HERSHEY_SIMPLEX, scale, RED, 2)


class WebStreamer:
    """Keeps the newest annotated frame and serves it as MJPEG, with a status feed."""

    def __init__(
        self,
        camera_manager: CameraManager,
        motion_recorder: MotionRecorder | None = None,
    ) -> None:
        self.camera_manager = camera_manager
        self.motion_recorder = motion_recorder
        self.latest_frame: Frame | None = None
        self.frame_lock = threading.Lock()
        self.camera_manager.add_consumer(self._consume_frames)

    def _consume_frames(self, main_frame: Frame, lores_frame: Frame) -> None:  # noqa: ARG002 -- signature fixed by FrameConsumer
        """Consumer callback: keep a copy of the main frame, captioned if recording."""
        frame = main_frame.copy()
        recorder = self.motion_recorder
        clip = recorder.current_filename if recorder is not None else None
        if clip is not None:
            caption(frame, "RECORDING", row=30, scale=1.0)
            caption(frame, f"File: {clip.name}", row=70, scale=0.6)

        with self.frame_lock:
            self.latest_frame = frame

    def status(self) -> dict[str, object]:
        """What the Live tab shows beside the feed."""
        recorder = self.motion_recorder
        if recorder is None:
            return {"detector": True, "recorder": False}
        clip = recorder.current_filename
        return {
            "detector": True,
            "recorder": True,
            "recording": recorder.recording,
            "clip": clip.name if clip else None,
            "motion_threshold": recorder.motion_threshold,
            "motion_timeout": recorder.motion_timeout,
        }

    def blueprint(self) -> Blueprint:
        bp = Blueprint("live", __name__)
        bp.add_url_rule("/video_feed", view_func=self.video_feed)
        bp.add_url_rule("/api/status", view_func=self.api_status)
        return bp

    def video_feed(self) -> Response:
        return Response(
            self.generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame"
        )

    def api_status(self) -> Response:
        return jsonify(self.status())

    def generate_frames(self) -> Iterator[bytes]:
        while True:
            # Encode and yield outside the lock.
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
