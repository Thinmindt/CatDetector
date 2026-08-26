import logging
import os
import pathlib
import threading
import time

from config import Config
from src.camera_manager import CameraManager
from src.motion_recorder import MotionRecorder
from src.web_streamer import WebStreamer

log = logging.getLogger(__name__)


def monitor(
    camera_manager: CameraManager, motion_recorder: MotionRecorder | None = None
) -> None:
    """Start monitoring by starting frame distribution"""
    log.info("Starting motion monitoring with circular buffer...")
    try:
        camera_manager.start_frame_distribution()

        # Keep main thread alive
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        log.info("Monitoring stopped by user")
    finally:
        camera_manager.stop_frame_distribution()
        if motion_recorder:
            motion_recorder.cleanup()


def build_recorder(
    camera_manager: CameraManager, share: pathlib.Path
) -> MotionRecorder | None:
    """Build the recorder, or return None if the share is missing or it fails."""
    if not os.path.ismount(share):
        log.info("%s is not mounted - starting without recording", share)
        return None

    try:
        return MotionRecorder(
            camera_manager=camera_manager,
            video_directory=share / "captures",
            file_prefix="cat_video",
        )
    except Exception as error:
        log.warning(
            "Recorder unavailable (%s) - continuing with the stream only", error
        )
        return None


def configure_logging() -> None:
    """Timestamped records to stderr: this application at INFO, the rest at WARNING."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("src").setLevel(logging.INFO)
    logging.getLogger(__name__).setLevel(logging.INFO)


if __name__ == "__main__":
    configure_logging()
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

    log.info("Starting cat detector with shared camera")
    log.info("Web stream available at http://<pi-ip>:5000")

    monitor(camera_manager, recorder)
