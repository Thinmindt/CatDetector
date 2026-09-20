"""CameraManager owns the one camera and fans frames out to consumers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from conftest import LORES_SIZE, MAIN_SIZE, FakePicamera2

from src.clips.frame import Frame

# The bound that matters is "gave up on the wedged consumer", not the exact join
# timeout, which the test shortens.
JOIN_TIMEOUT_SECONDS = 0.2
SHUTDOWN_BUDGET_SECONDS = 2


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_configures_both_streams_and_starts(
    fake_camera: FakePicamera2, camera_manager: Any
) -> None:
    config = fake_camera.configured
    assert config["main"]["size"] == (1280, 720)
    assert config["lores"]["size"] == (640, 480)
    assert fake_camera.started


def test_uses_the_full_sensor(fake_camera: FakePicamera2, camera_manager: Any) -> None:
    """The whole array binned 2x2, not the cropped mode picamera2 picks by size."""
    assert fake_camera.configured["sensor"]["output_size"] == (2304, 1296)


def test_both_streams_come_from_one_request(
    fake_camera: FakePicamera2, camera_manager: Any
) -> None:
    """Regression: two capture_array() calls consumed two separate requests,
    which halved the frame rate (measured 15fps vs 30fps) and left main and
    lores a frame apart in time."""
    camera_manager.add_consumer(lambda main, lores: None)
    camera_manager.start_frame_distribution()

    assert wait_for(lambda: fake_camera.capture_arrays_calls >= 3)
    assert fake_camera.capture_array_calls == 0


def test_consumers_receive_main_and_lores_frames(camera_manager: Any) -> None:
    seen: list[tuple[Any, Any]] = []
    camera_manager.add_consumer(
        lambda main, lores: seen.append((main.shape, lores.shape))
    )
    camera_manager.start_frame_distribution()

    assert wait_for(lambda: len(seen) >= 1)
    assert seen[0] == ((*MAIN_SIZE, 3), (*LORES_SIZE, 3))


def test_a_raising_consumer_does_not_stop_the_others(camera_manager: Any) -> None:
    calls: list[int] = []

    def boom(main: Frame, lores: Frame) -> None:
        raise RuntimeError("consumer blew up")

    def healthy(main: Frame, lores: Frame) -> None:
        calls.append(1)

    camera_manager.add_consumer(boom)
    camera_manager.add_consumer(healthy)
    camera_manager.start_frame_distribution()

    # The loop must keep delivering frames despite boom() raising every time.
    assert wait_for(lambda: len(calls) >= 3)


def test_consumers_run_outside_the_frame_lock(camera_manager: Any) -> None:
    """Regression: the lock used to span the whole consumer loop and the sleep.

    threading.Lock is not reentrant, so if the capture loop still held it while
    calling consumers, this non-blocking acquire would fail.
    """
    acquired: list[bool] = []

    def probe(main: Frame, lores: Frame) -> None:
        got = camera_manager._frame_lock.acquire(blocking=False)
        acquired.append(got)
        if got:
            camera_manager._frame_lock.release()

    camera_manager.add_consumer(probe)
    camera_manager.start_frame_distribution()

    assert wait_for(lambda: len(acquired) >= 2)
    assert all(acquired)


def test_start_frame_distribution_is_idempotent(camera_manager: Any) -> None:
    camera_manager.start_frame_distribution()
    thread = camera_manager._frame_thread
    camera_manager.start_frame_distribution()
    assert camera_manager._frame_thread is thread


def test_stop_frame_distribution_ends_the_thread(camera_manager: Any) -> None:
    """The join is bounded, so this assertion can actually fail.

    With an unbounded join it was a tautology: the thread had necessarily
    exited by the time it ran, and a regression hung the suite instead.
    """
    camera_manager.start_frame_distribution()
    thread = camera_manager._frame_thread
    assert thread is not None

    camera_manager.stop_frame_distribution()
    assert not thread.is_alive()


def test_a_wedged_consumer_cannot_hang_shutdown(
    camera_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer that never returns must not make stop() block forever."""
    monkeypatch.setattr(
        "src.capture.camera_manager.JOIN_TIMEOUT_SECONDS", JOIN_TIMEOUT_SECONDS
    )
    release = threading.Event()
    entered = threading.Event()

    def wedged(main: Frame, lores: Frame) -> None:
        entered.set()
        release.wait(timeout=30)

    camera_manager.add_consumer(wedged)
    camera_manager.start_frame_distribution()
    assert entered.wait(timeout=5)

    started = time.monotonic()
    camera_manager.stop_frame_distribution()
    elapsed = time.monotonic() - started

    assert elapsed < SHUTDOWN_BUDGET_SECONDS
    release.set()
