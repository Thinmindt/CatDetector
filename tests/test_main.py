"""Startup wiring: a missing share or a broken recorder must not cost the stream."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import FakePicamera2, wait_for

import main
from config import Config
from src.review.capture_db import CaptureDB


def test_a_broken_recorder_still_leaves_the_stream(
    camera_manager: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: _setup_circular_recording no longer swallows failures, so an
    OSError propagated out of __init__ and killed startup before WebStreamer
    was ever constructed -- leaving no web feed at all."""

    def explode(**kwargs: object) -> None:
        raise OSError("share went away mid-startup")

    monkeypatch.setattr(main, "MotionRecorder", explode)

    with caplog.at_level(logging.INFO):
        assert main.build_recorder(camera_manager, tmp_path) is None
    assert "stream only" in caplog.text


def test_detection_settings_reach_the_recorder(
    camera_manager: Any,
    fake_encoders: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("main.Config.MOTION_THRESHOLD", 200)
    monkeypatch.setattr("main.Config.DARK_BRIGHTNESS", 12.5)
    monkeypatch.setattr("main.Config.MOTION_TIMEOUT", 30.0)
    monkeypatch.setattr("main.Config.MOG2_HISTORY", 1500)

    recorder = main.build_recorder(camera_manager, tmp_path / "clip_cache")

    assert recorder is not None
    assert recorder.detector.motion_threshold == 200
    assert recorder.detector.dark_brightness == 12.5
    assert recorder.motion_timeout == 30.0
    assert recorder.detector.background_subtractor.getHistory() == 1500


STALL_LIMIT_SECONDS = 0.2
EXIT_DEADLINE_SECONDS = 0.2
RECORDING: Any = object()


@pytest.fixture(autouse=True)
def contained_exits(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A stall arms a real os._exit and changes the SIGTERM handler; keep both
    inside the test."""
    forced: list[str] = []
    monkeypatch.setattr(main, "_force_exit", lambda: forced.append("forced"))
    monkeypatch.setattr(main, "STALL_EXIT_DEADLINE_SECONDS", EXIT_DEADLINE_SECONDS)
    previous = signal.getsignal(signal.SIGTERM)
    yield forced
    signal.signal(signal.SIGTERM, previous)


class MonitorSpy:
    """Plays camera manager, recorder and transfer, noting each call in order."""

    def __init__(self, stalled_for: float = 0.0) -> None:
        self.calls: list[str] = []
        self.stalled_for = stalled_for

    def seconds_since_last_frame(self) -> float:
        return self.stalled_for

    def start_frame_distribution(self) -> None:
        self.calls.append("start")

    def stop_frame_distribution(self) -> None:
        self.calls.append("stop distribution")

    def cleanup(self) -> None:
        self.calls.append("recorder cleanup")

    def stop(self) -> None:
        self.calls.append("transfer stop")


def fail_if_not_replaced(*args: object) -> None:
    raise AssertionError("monitor() did not install its SIGTERM handler")


def test_sigterm_runs_the_same_cleanup_as_ctrl_c() -> None:
    """systemd and kill stop a process with SIGTERM. Without a handler the
    process dies on the spot and the clip being written is never finished."""
    spy: Any = MonitorSpy()
    previous = signal.signal(signal.SIGTERM, fail_if_not_replaced)
    timer = threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGTERM))
    try:
        timer.start()
        with pytest.raises(SystemExit):
            main.monitor(spy, spy, spy)
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, previous)

    assert spy.calls == [
        "start",
        "stop distribution",
        "recorder cleanup",
        "transfer stop",
    ]


def test_a_stalled_camera_ends_the_process_through_the_usual_cleanup() -> None:
    """Regression: on 2026-09-26 capture blocked for 3 h 46 min without raising,
    and the process stayed alive, so systemd never restarted it."""
    spy: Any = MonitorSpy(stalled_for=main.CAPTURE_STALL_SECONDS + 1)

    with pytest.raises(SystemExit) as exit_info:
        main.monitor(spy, spy, spy)

    assert exit_info.value.code == main.EXIT_CAPTURE_STALLED
    assert exit_info.value.code != 0
    assert spy.calls == [
        "start",
        "stop distribution",
        "recorder cleanup",
        "transfer stop",
    ]


def test_the_stall_check_follows_the_camera(
    fake_camera: FakePicamera2, camera_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "CAPTURE_STALL_SECONDS", STALL_LIMIT_SECONDS)
    camera_manager.start_frame_distribution()
    time.sleep(2 * STALL_LIMIT_SECONDS)
    main.check_capture(camera_manager)

    fake_camera.delivering.clear()
    try:
        time.sleep(2 * STALL_LIMIT_SECONDS)
        with pytest.raises(SystemExit):
            main.check_capture(camera_manager)
    finally:
        fake_camera.delivering.set()


class HeartbeatSpy:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def start(self) -> None:
        self.calls.append("heartbeat start")

    def stop(self) -> None:
        self.calls.append("heartbeat stop")


def test_monitor_runs_the_heartbeat_alongside_capture() -> None:
    spy: Any = MonitorSpy(stalled_for=main.CAPTURE_STALL_SECONDS + 1)
    heartbeat: Any = HeartbeatSpy(spy.calls)

    with pytest.raises(SystemExit):
        main.monitor(spy, spy, spy, heartbeat)

    assert spy.calls[:2] == ["start", "heartbeat start"]
    assert spy.calls[-1] == "heartbeat stop"


def test_no_heartbeat_url_means_no_heartbeat(
    camera_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("main.Config.CAPTURE_HEARTBEAT_URL", None)
    assert main.build_heartbeat(camera_manager, RECORDING) is None


def test_an_unusable_heartbeat_url_leaves_the_detector_running(
    camera_manager: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url = "hc-ping.com/0f0f-token"
    monkeypatch.setattr("main.Config.CAPTURE_HEARTBEAT_URL", url)

    assert main.build_heartbeat(camera_manager, RECORDING) is None
    assert "Capture heartbeat off" in caplog.text
    assert url not in caplog.text


def test_a_heartbeat_url_builds_a_heartbeat_on_the_camera(
    camera_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "main.Config.CAPTURE_HEARTBEAT_URL", "https://example.invalid/ping"
    )
    heartbeat = main.build_heartbeat(camera_manager, RECORDING)

    assert heartbeat is not None
    assert heartbeat.stall_seconds == main.CAPTURE_STALL_SECONDS
    assert heartbeat.frame_age() < main.CAPTURE_STALL_SECONDS


def test_a_stall_exit_cannot_hang_in_cleanup(contained_exits: list[str]) -> None:
    """picamera2's close runs at exit and joins its event-loop thread without a
    timeout, so a wedged camera could hold the process up forever."""
    spy: Any = MonitorSpy(stalled_for=main.CAPTURE_STALL_SECONDS + 1)

    with pytest.raises(SystemExit):
        main.check_capture(spy)

    assert wait_for(lambda: contained_exits == ["forced"], timeout=2)


def test_a_sigterm_cannot_interrupt_the_cleanup_after_a_stall() -> None:
    spy: Any = MonitorSpy(stalled_for=main.CAPTURE_STALL_SECONDS + 1)

    with pytest.raises(SystemExit):
        main.check_capture(spy)

    assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN


def test_no_forced_exit_while_frames_arrive(contained_exits: list[str]) -> None:
    spy: Any = MonitorSpy()
    main.check_capture(spy)
    time.sleep(2 * EXIT_DEADLINE_SECONDS)
    assert contained_exits == []


def test_no_heartbeat_without_a_recorder(
    camera_manager: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Frames reach the live feed without a recorder, but no visit is recorded,
    so the watcher must hear nothing."""
    monkeypatch.setattr(
        "main.Config.CAPTURE_HEARTBEAT_URL", "https://example.invalid/ping"
    )
    assert main.build_heartbeat(camera_manager, None) is None
    assert "recorder is not running" in caplog.text


def test_startup_ingest_does_not_hold_up_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stalled share must not delay the camera: the scan runs on its own thread."""
    release = threading.Event()
    scanning = threading.Event()

    def stalled_ingest(self: Any, captures_dir: Path) -> int:
        scanning.set()
        release.wait(timeout=10)
        return 3

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "captures.db"))
    monkeypatch.setattr(Config, "REVIEW_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(CaptureDB, "ingest", stalled_ingest)

    with caplog.at_level(logging.INFO):
        review = main.open_review()
        assert scanning.wait(timeout=5)
        assert "Ingest found" not in caplog.text
        release.set()
        worker = next(t for t in threading.enumerate() if t.name == "startup-ingest")
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert "Ingest found 3 new clip(s)" in caplog.text
    review.db.close()
