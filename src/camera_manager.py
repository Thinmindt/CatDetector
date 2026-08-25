import threading
import time
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray
from picamera2 import Picamera2

# Frames arrive from picamera2 as HxWx3 uint8 arrays.
Frame = NDArray[np.uint8]

# A consumer receives (main_frame, lores_frame) and returns nothing.
FrameConsumer = Callable[[Frame, Frame], None]


class CameraManager:
    """Manages a single camera instance shared between multiple consumers"""

    def __init__(self) -> None:
        self.picam2 = Picamera2()
        self.picam2.configure(
            self.picam2.create_video_configuration(
                main={"size": (1280, 720), "format": "RGB888"},
                lores={"size": (640, 480), "format": "RGB888"},
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
        print("Frame distribution started")

    def stop_frame_distribution(self) -> None:
        """Stop distributing frames"""
        self._running = False
        if self._frame_thread:
            # Bounded join: a wedged consumer must not make shutdown hang forever.
            self._frame_thread.join(timeout=5.0)
            if self._frame_thread.is_alive():
                print("Frame distribution thread did not stop within 5s")

    def _distribute_frames(self) -> None:
        """Internal method to capture and distribute frames"""
        print("Starting frame distribution loop")
        frame_count = 0

        while self._running:
            try:
                with self._frame_lock:
                    # One request serves both streams. Two capture_array() calls
                    # would consume two *different* requests: the frames would be
                    # separate exposures ~33ms apart, and the loop would run at
                    # half the frame rate (measured: 15fps vs 30fps).
                    arrays, _ = self.picam2.capture_arrays(["main", "lores"])
                main_frame: Frame = arrays[0]
                lores_frame: Frame = arrays[1]

                # Send to all consumers. Still synchronous and in order, so a
                # slow consumer throttles the loop; it just no longer does so
                # while holding the capture lock.
                for consumer in self._consumers:
                    try:
                        consumer(main_frame, lores_frame)
                    except Exception as e:
                        print(f"Error in consumer: {e}")

                frame_count += 1
                if frame_count % 100 == 0:  # Log every 100 frames
                    print(f"Processed {frame_count} frames")

                # No sleep: capture_arrays blocks until the next frame is ready,
                # which paces this loop at the camera's ~30fps on its own.

            except Exception as e:
                print(f"Frame capture error: {e}")
                time.sleep(2)

    def get_camera(self) -> Picamera2:
        """Get the camera instance for recording"""
        return self.picam2

    def close(self) -> None:
        """Close the camera"""
        try:
            self.stop_frame_distribution()
            time.sleep(1)  # Give time for cleanup
            self.picam2.close()
            print("Camera closed successfully")
        except Exception as e:
            print(f"Error closing camera: {e}")
