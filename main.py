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
    """Send timestamped records to stderr.

    Replaces bare print(), which is block-buffered when stdout is not a TTY --
    under systemd that loses every buffered line if the unit is killed rather
    than exiting cleanly.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Only this application logs at INFO. A blanket INFO root turns on every
    # third-party logger too -- picamera2 in particular narrates every state
    # change, which buries our own lines.
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
