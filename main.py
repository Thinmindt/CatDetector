import logging
import os
import pathlib
import signal
import sys
import threading
import time
from types import FrameType

from config import Config
from src.capture.camera_manager import CameraManager
from src.capture.capture_heartbeat import CaptureHeartbeat, http_ping
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

CAPTURE_STALL_SECONDS = 10.0
STALL_EXIT_DEADLINE_SECONDS = 60.0
EXIT_CAPTURE_STALLED = 1


def _exit_on_sigterm(signum: int, frame: FrameType | None) -> None:  # noqa: ARG001 -- signature fixed by signal.signal
    """Leave through monitor()'s cleanup, as Ctrl-C does. Later SIGTERMs are ignored."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    log.info("Received SIGTERM, shutting down")
    raise SystemExit(0)


def check_capture(camera_manager: CameraManager) -> None:
    """Exit through monitor()'s cleanup if the capture loop has stopped."""
    stalled = camera_manager.seconds_since_last_frame()
    if stalled > CAPTURE_STALL_SECONDS:
        log.error(
            "No frame for %.0f s from the capture loop; exiting to be restarted",
            stalled,
        )
        leave_after_stall()


def leave_after_stall() -> None:
    """Exit 1 through the usual cleanup, or without it once the deadline passes."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    deadline = threading.Timer(STALL_EXIT_DEADLINE_SECONDS, _force_exit)
    deadline.daemon = True
    deadline.start()
    raise SystemExit(EXIT_CAPTURE_STALLED)


def _force_exit() -> None:
    """End the process now, skipping cleanup that may wait on the camera."""
    log.error(
        "Shutdown after the stall took over %.0f s; exiting without it",
        STALL_EXIT_DEADLINE_SECONDS,
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    os._exit(EXIT_CAPTURE_STALLED)


def monitor(
    camera_manager: CameraManager,
    motion_recorder: MotionRecorder | None = None,
    clip_transfer: ClipTransfer | None = None,
    heartbeat: CaptureHeartbeat | None = None,
) -> None:
    """Start monitoring by starting frame distribution"""
    log.info("Starting motion monitoring with circular buffer...")
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        camera_manager.start_frame_distribution()
        if heartbeat:
            heartbeat.start()
        watch_capture(camera_manager)
    except KeyboardInterrupt:
        log.info("Monitoring stopped by user")
    finally:
        shut_down(camera_manager, motion_recorder, clip_transfer, heartbeat)


def watch_capture(camera_manager: CameraManager) -> None:
    """Block until a signal, or until check_capture ends the process."""
    while True:
        time.sleep(1)
        check_capture(camera_manager)


def shut_down(
    camera_manager: CameraManager,
    motion_recorder: MotionRecorder | None,
    clip_transfer: ClipTransfer | None,
    heartbeat: CaptureHeartbeat | None,
) -> None:
    """Stop the producer first, then everything it feeds."""
    camera_manager.stop_frame_distribution()
    if motion_recorder:
        motion_recorder.cleanup()
    if clip_transfer:
        clip_transfer.stop()
    if heartbeat:
        heartbeat.stop()


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


def build_heartbeat(
    camera_manager: CameraManager, recorder: MotionRecorder | None
) -> CaptureHeartbeat | None:
    """A heartbeat if CAPTURE_HEARTBEAT_URL is set and usable and clips are
    being recorded, otherwise None."""
    if not Config.CAPTURE_HEARTBEAT_URL:
        return None
    if recorder is None:
        log.warning("Capture heartbeat off: the recorder is not running")
        return None
    try:
        send = http_ping(Config.CAPTURE_HEARTBEAT_URL)
    except ValueError as error:
        log.warning("Capture heartbeat off: %s", error)
        return None
    return CaptureHeartbeat(
        camera_manager.seconds_since_last_frame, send, CAPTURE_STALL_SECONDS
    )


def open_db() -> CaptureDB:
    return CaptureDB(
        Config.DB_PATH,
        gap_seconds=Config.EVENT_GAP_SECONDS,
        box_distance_px=Config.EVENT_BOX_DISTANCE_PX,
    )


def ingest(review: Review) -> None:
    try:
        log.info("Ingest found %d new clip(s)", review.db.ingest(review.captures_dir))
    except Exception:
        log.exception("Startup ingest failed; rescan from the review page")


def open_review() -> Review:
    """The capture database and its extractor. The share is scanned on its own
    thread, so a stalled mount cannot hold up the camera."""
    captures = pathlib.Path(Config.CAPTURES_DIR)
    review = Review(
        open_db(), ClipFrames(Config.REVIEW_CACHE_DIR), captures, Config.CAT_NAMES
    )
    threading.Thread(
        target=ingest, args=(review,), name="startup-ingest", daemon=True
    ).start()
    return review


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

    heartbeat = build_heartbeat(camera_manager, recorder)
    monitor(camera_manager, recorder, transfer, heartbeat)
