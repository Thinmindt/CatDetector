"""Startup wiring: a missing share or a broken recorder must not cost the stream."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest


def test_unmounted_share_skips_recording(
    camera_manager: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: mkdir(parents=True) on an unmounted mountpoint happily writes
    to the SD card, and clips vanish the moment the share mounts over them."""
    import main

    # tmp_path is a real directory but not a mount point.
    with caplog.at_level(logging.INFO):
        assert main.build_recorder(camera_manager, tmp_path) is None
    assert "not mounted" in caplog.text


def test_a_broken_recorder_still_leaves_the_stream(
    camera_manager: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: _setup_circular_recording no longer swallows failures, so an
    OSError propagated out of __init__ and killed startup before WebStreamer
    was ever constructed -- leaving no web feed at all."""
    import main

    def explode(**kwargs: object) -> None:
        raise OSError("share went away mid-startup")

    monkeypatch.setattr("os.path.ismount", lambda path: True)
    monkeypatch.setattr(main, "MotionRecorder", explode)

    with caplog.at_level(logging.INFO):
        assert main.build_recorder(camera_manager, tmp_path) is None
    assert "stream only" in caplog.text


def test_a_mounted_share_builds_a_recorder(
    camera_manager: Any,
    fake_encoders: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    monkeypatch.setattr("os.path.ismount", lambda path: True)

    recorder = main.build_recorder(camera_manager, tmp_path)

    assert recorder is not None
    assert (tmp_path / "captures").is_dir()
