import logging
import threading
import time
from collections.abc import Callable

from picamera2 import Picamera2

from src.clips.frame import Frame

log = logging.getLogger(__name__)

JOIN_TIMEOUT_SECONDS = 5.0
CAPTURE_ERROR_BACKOFF_SECONDS = 2.0

FrameConsumer = Callable[[Frame, Frame], None]


class CameraManager:
    """Manages a single camera instance shared between multiple consumers"""

    def __init__(self) -> None:
        self.picam2 = Picamera2()
        self.picam2.configure(
            self.picam2.create_video_configuration(
                main={"size": (1280, 720), "format": "RGB888"},
                lores={"size": (640, 480), "format": "RGB888"},
                # The full sensor. Chosen by size alone, picamera2 picks a mode
                # that crops the edges off.
                sensor={"output_size": (2304, 1296), "bit_depth": 10},
            )
        )
        self.picam2.start()

        self._consumers: list[FrameConsumer] = []
        self._running = False
        self._frame_thread: threading.Thread | None = None
        self._frame_lock = threading.Lock()

    def add_consumer(self, consumer_func: FrameConsumer) -> None:
        """Add a function that will receive frames"""
        self._consumers.append(consumer_func)

    def start_frame_distribution(self) -> None:
        """Start distributing frames to consumers"""
        if self._running:
            return

        self._running = True
        self._frame_thread = threading.Thread(
            target=self._distribute_frames, daemon=True
        )
        self._frame_thread.start()
        log.info("Frame distribution started")

    def stop_frame_distribution(self) -> None:
        """Stop distributing frames"""
        self._running = False
        if self._frame_thread:
            self._frame_thread.join(timeout=JOIN_TIMEOUT_SECONDS)
            if self._frame_thread.is_alive():
                log.warning(
                    "Frame distribution thread did not stop within %ss",
                    JOIN_TIMEOUT_SECONDS,
                )

    def _distribute_frames(self) -> None:
        """Capture and dispatch until stopped. Paced by the blocking capture."""
        log.info("Starting frame distribution loop")
        while self._running:
            try:
                main_frame, lores_frame = self._capture_next_frames()
                self._dispatch(main_frame, lores_frame)
            except Exception:
                log.exception("Frame capture error")
                time.sleep(CAPTURE_ERROR_BACKOFF_SECONDS)

    def _capture_next_frames(self) -> tuple[Frame, Frame]:
        """Both streams from one libcamera request. Blocks until the next frame."""
        with self._frame_lock:
            arrays, _ = self.picam2.capture_arrays(["main", "lores"])
        return arrays[0], arrays[1]

    def _dispatch(self, main_frame: Frame, lores_frame: Frame) -> None:
        """Call each consumer in order, outside the capture lock.

        Runs on the capture thread, so a slow consumer throttles the loop.
        A raising consumer does not stop the others.
        """
        for consumer in self._consumers:
            try:
                consumer(main_frame, lores_frame)
            except Exception:
                log.exception("Error in consumer")

    def get_camera(self) -> Picamera2:
        """The camera itself, for the recorder's encoder pipeline."""
        return self.picam2
