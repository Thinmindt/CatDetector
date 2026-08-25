import pathlib
import threading
import time

from config import Config
from src.camera_manager import CameraManager
from src.motion_recorder import MotionRecorder
from src.web_streamer import WebStreamer


def monitor(
    camera_manager: CameraManager, motion_recorder: MotionRecorder | None = None
) -> None:
    """Start monitoring by starting frame distribution"""
    print("Starting motion monitoring with circular buffer...")
    try:
        camera_manager.start_frame_distribution()

        # Keep main thread alive
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("Monitoring stopped by user")
    finally:
        if motion_recorder:
            motion_recorder.cleanup()
        camera_manager.stop_frame_distribution()


if __name__ == "__main__":
    video_directory = pathlib.Path(f"{Config.NETWORK_SHARE_DIR}/captures/")

    camera_manager = CameraManager()

    recorder = MotionRecorder(
        camera_manager=camera_manager,
        video_directory=video_directory,
        file_prefix="cat_video",
    )

    web_streamer = WebStreamer(
        camera_manager=camera_manager,
        motion_recorder=recorder,
        port=5000,
    )

    stream_thread = threading.Thread(target=web_streamer.start, daemon=True)
    stream_thread.start()

    print("Starting cat detector with shared camera")
    print("Web stream available at http://<pi-ip>:5000")

    monitor(camera_manager, recorder)
