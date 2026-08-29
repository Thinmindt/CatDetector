"""ClipTransfer: finished clips reach the share, partial ones never do."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from src.clip_transfer import ClipTransfer
from src.motion_recorder import partial_name


@pytest.fixture
def mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the share root as mounted; tmp_path never is."""
    monkeypatch.setattr("os.path.ismount", lambda path: True)


@pytest.fixture
def transfer(tmp_path: Path) -> ClipTransfer:
    share = tmp_path / "share"
    share.mkdir()
    return ClipTransfer(
        local_directory=tmp_path / "local",
        destination=share / "captures",
        share_root=share,
    )


def write_clip(transfer: ClipTransfer, name: str, body: bytes = b"clip") -> Path:
    path = transfer.local_directory / name
    path.write_bytes(body)
    return path


def test_finished_clips_ship_and_leave_local_disk(
    transfer: ClipTransfer, mounted: None
) -> None:
    clip = write_clip(transfer, "cat_video_20260829_120000.h264", b"footage")

    assert transfer.transfer_once() == 1

    landed = transfer.destination / clip.name
    assert landed.read_bytes() == b"footage"
    assert not clip.exists()
    assert transfer.pending() == []


def test_a_clip_still_being_written_is_not_shipped(
    transfer: ClipTransfer, mounted: None
) -> None:
    """The partial name is the whole signal that a clip is unfinished."""
    in_progress = partial_name(
        transfer.local_directory / "cat_video_20260829_120000.h264"
    )
    in_progress.parent.mkdir(parents=True, exist_ok=True)
    in_progress.write_bytes(b"half a clip")

    assert transfer.pending() == []
    assert transfer.transfer_once() == 0
    assert in_progress.exists()
    assert not (transfer.destination / in_progress.name).exists()


def test_a_short_copy_never_lands_under_the_final_name(
    transfer: ClipTransfer, mounted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated copy must be caught before the rename, not after."""
    clip = write_clip(transfer, "cat_video_20260829_120000.h264", b"the whole clip")

    def truncated_copy(src: Any, dst: Any, **kwargs: Any) -> None:
        Path(dst).write_bytes(b"the whole")

    monkeypatch.setattr(shutil, "copyfile", truncated_copy)

    assert transfer.transfer_once() == 0
    assert not (transfer.destination / clip.name).exists()
    assert clip.exists()


def test_a_failed_transfer_keeps_the_clip_and_retries(
    transfer: ClipTransfer, mounted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = write_clip(transfer, "cat_video_20260829_120000.h264", b"footage")
    real_copy = shutil.copyfile
    failing = {"now": True}

    def flaky(src: Any, dst: Any, **kwargs: Any) -> None:
        if failing["now"]:
            raise OSError("share went away mid-copy")
        real_copy(src, dst)

    monkeypatch.setattr(shutil, "copyfile", flaky)
    assert transfer.transfer_once() == 0
    assert clip.exists()
    assert not partial_name(transfer.destination / clip.name).exists()

    failing["now"] = False
    assert transfer.transfer_once() == 1
    assert (transfer.destination / clip.name).read_bytes() == b"footage"


def test_an_unmounted_share_holds_clips_on_local_disk(
    transfer: ClipTransfer, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: writing to an absent mountpoint puts clips on the SD card,
    where the share hides them the moment it mounts over the top."""
    clip = write_clip(transfer, "cat_video_20260829_120000.h264")

    with caplog.at_level(logging.WARNING):
        assert transfer.transfer_once() == 0

    assert clip.exists()
    assert not transfer.destination.exists()
    assert "not mounted" in caplog.text


def test_the_worker_survives_a_cycle_that_raises(
    transfer: ClipTransfer, mounted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad cycle must not kill the thread and strand every later clip."""
    calls: list[int] = []

    def sometimes_explode() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("share went away mid-scan")
        return 0

    monkeypatch.setattr(transfer, "transfer_once", sometimes_explode)
    transfer.scan_interval = 0.01
    transfer.start()
    try:
        deadline = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        transfer.stop()

    assert len(calls) >= 3
