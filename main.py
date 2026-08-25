import os
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
        # Stop the producer first. Tearing the recorder down while the capture
        # thread is still calling into it races _start_saving, which can leave
        # a zero-byte clip behind and the recorder stuck reporting "recording".
        camera_manager.stop_frame_distribution()
        if motion_recorder:
            motion_recorder.cleanup()


def build_recorder(
    camera_manager: CameraManager, share: pathlib.Path
) -> MotionRecorder | None:
    """Build the recorder, or return None and keep the stream running.

    Two ways this legitimately fails on a Pi that boots faster than its NAS:
    the share is not mounted yet, in which case mkdir would cheerfully create
    captures/ on the SD card and clips would fill the boot media until the share
    mounts over them; or the encoder cannot start. Neither should cost the web
    stream, which is often how you find out something is wrong.
    """
    if not os.path.ismount(share):
        print(f"{share} is not mounted - starting without recording")
        return None

    try:
        return MotionRecorder(
            camera_manager=camera_manager,
            video_directory=share / "captures",
            file_prefix="cat_video",
        )
    except Exception as error:
        print(f"Recorder unavailable ({error}) - continuing with the stream only")
        return None


if __name__ == "__main__":
    share = pathlib.Path(Config.NETWORK_SHARE_DIR)

    camera_manager = CameraManager()
    recorder = build_recorder(camera_manager, share)

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
