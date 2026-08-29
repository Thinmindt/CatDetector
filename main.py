import logging
import pathlib
import threading
import time

from config import Config
from src.camera_manager import CameraManager
from src.clip_transfer import ClipTransfer
from src.logging_setup import configure_logging
from src.motion_metrics import MetricsLog
from src.motion_recorder import MotionRecorder
from src.web_streamer import WebStreamer

log = logging.getLogger(__name__)


def monitor(
    camera_manager: CameraManager,
    motion_recorder: MotionRecorder | None = None,
    clip_transfer: ClipTransfer | None = None,
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
        if clip_transfer:
            clip_transfer.stop()


def build_metrics() -> MetricsLog | None:
    """A metrics log if METRICS_CSV is configured, otherwise None."""
    if not Config.METRICS_CSV:
        return None
    return MetricsLog(Config.METRICS_CSV)


def build_recorder(
    camera_manager: CameraManager, local_clips: pathlib.Path
) -> MotionRecorder | None:
    """Build the recorder against local disk, or return None if it fails.

    Recording does not wait on the share: ClipTransfer moves finished clips
    across whenever it is reachable.
    """
    try:
        return MotionRecorder(
            camera_manager=camera_manager,
            video_directory=local_clips,
            file_prefix="cat_video",
            metrics=build_metrics(),
        )
    except Exception as error:
        log.warning(
            "Recorder unavailable (%s) - continuing with the stream only", error
        )
        return None


def build_transfer(local_clips: pathlib.Path, share: pathlib.Path) -> ClipTransfer:
    transfer = ClipTransfer(
        local_directory=local_clips,
        destination=share / "captures",
        share_root=share,
    )
    transfer.start()
    return transfer


if __name__ == "__main__":
    configure_logging(__name__)
    share = pathlib.Path(Config.NETWORK_SHARE_DIR)
    local_clips = pathlib.Path(Config.LOCAL_CLIP_DIR)

    camera_manager = CameraManager()
    recorder = build_recorder(camera_manager, local_clips)
    transfer = build_transfer(local_clips, share) if recorder else None

    web_streamer = WebStreamer(
        camera_manager=camera_manager,
        motion_recorder=recorder,
        port=5000,
    )

    stream_thread = threading.Thread(target=web_streamer.start, daemon=True)
    stream_thread.start()

    log.info("Starting cat detector with shared camera")
    log.info("Web stream available at http://<pi-ip>:5000")

    monitor(camera_manager, recorder, transfer)
