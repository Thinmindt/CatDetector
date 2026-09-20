import logging
import pathlib
import signal
import sys
import threading
import time
from types import FrameType

from config import Config
from src.capture.camera_manager import CameraManager
from src.capture.clip_transfer import ClipTransfer
from src.capture.motion_detector import MotionDetector
from src.capture.motion_metrics import MetricsLog
from src.capture.motion_recorder import MotionRecorder
from src.capture.web_streamer import WebStreamer
from src.logging_setup import configure_logging
from src.review.api import Review
from src.review.capture_db import CaptureDB
from src.review.clip_frames import ClipFrames
from src.web_app import WEB_PORT, create_app, run

log = logging.getLogger(__name__)


def _exit_on_sigterm(signum: int, frame: FrameType | None) -> None:  # noqa: ARG001 -- signature fixed by signal.signal
    """Leave through monitor()'s cleanup, as Ctrl-C does. Later SIGTERMs are ignored."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    log.info("Received SIGTERM, shutting down")
    raise SystemExit(0)


def monitor(
    camera_manager: CameraManager,
    motion_recorder: MotionRecorder | None = None,
    clip_transfer: ClipTransfer | None = None,
) -> None:
    """Start monitoring by starting frame distribution"""
    log.info("Starting motion monitoring with circular buffer...")
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
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
        detector = MotionDetector(
            motion_threshold=Config.MOTION_THRESHOLD,
            dark_brightness=Config.DARK_BRIGHTNESS,
            mog2_history=Config.MOG2_HISTORY,
        )
        return MotionRecorder(
            camera_manager=camera_manager,
            video_directory=local_clips,
            detector=detector,
            motion_timeout=Config.MOTION_TIMEOUT,
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
        destination=Config.CAPTURES_DIR,
        share_root=share,
    )
    transfer.start()
    return transfer


def open_db() -> CaptureDB:
    return CaptureDB(
        Config.DB_PATH,
        gap_seconds=Config.EVENT_GAP_SECONDS,
        box_distance_px=Config.EVENT_BOX_DISTANCE_PX,
    )


def open_review() -> Review:
    """The capture database, brought up to date from the share, and its extractor."""
    captures = pathlib.Path(Config.CAPTURES_DIR)
    db = open_db()
    log.info("Ingest found %d new clip(s)", db.ingest(captures))
    return Review(db, ClipFrames(Config.REVIEW_CACHE_DIR), captures)


def build_review() -> Review | None:
    """Open the review half, or return None so the live feed still comes up."""
    try:
        return open_review()
    except Exception as error:
        log.warning("Review unavailable (%s) - continuing with the live feed", error)
        return None


def regroup_events() -> dict[str, int]:
    """Rebuild every event from the current thresholds. Event ids change."""
    db = open_db()
    try:
        db.regroup()
        return db.counts()
    finally:
        db.close()


if __name__ == "__main__":
    configure_logging(__name__)
    if "--regroup" in sys.argv[1:]:
        log.info("Regrouped: %s", regroup_events())
        sys.exit(0)
    share = pathlib.Path(Config.NETWORK_SHARE_DIR)
    local_clips = pathlib.Path(Config.LOCAL_CLIP_DIR)

    camera_manager = CameraManager()
    recorder = build_recorder(camera_manager, local_clips)
    transfer = build_transfer(local_clips, share) if recorder else None
    streamer = WebStreamer(camera_manager=camera_manager, motion_recorder=recorder)
    app = create_app(streamer, build_review())
    threading.Thread(target=run, args=(app,), daemon=True).start()

    log.info("Starting cat detector with shared camera")
    log.info("Live feed and review at http://<pi-ip>:%d", WEB_PORT)

    monitor(camera_manager, recorder, transfer)
