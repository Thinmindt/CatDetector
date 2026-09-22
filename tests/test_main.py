"""Startup wiring: a missing share or a broken recorder must not cost the stream."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any

import pytest

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


class MonitorSpy:
    """Plays camera manager, recorder and transfer, noting each call in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

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
