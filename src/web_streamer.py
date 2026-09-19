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
        """Consumer callback: annotate a copy of the main frame and keep it."""
        frame_rgb = main_frame.copy()

        recorder = self.motion_recorder
        if recorder:
            if recorder.recording:
                cv2.putText(
                    frame_rgb,
                    "RECORDING",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.0,
                    (0, 0, 255),
                    2,
                )
                if recorder.current_filename:
                    cv2.putText(
                        frame_rgb,
                        f"File: {recorder.current_filename.name}",
                        (10, 70),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 0, 255),
                        2,
                    )
            cv2.putText(
                frame_rgb,
                f"Motion Threshold: {recorder.motion_threshold}",
                (10, frame_rgb.shape[0] - 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),
                1,
            )

        with self.frame_lock:
            self.latest_frame = frame_rgb

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
