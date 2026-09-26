"""CaptureHeartbeat: a watcher hears from the detector only while frames arrive."""

from __future__ import annotations

import http.server
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import ClassVar

import pytest

from src.capture.capture_heartbeat import CaptureHeartbeat, http_ping

STALL_SECONDS = 10.0
FRESH = 0.1
STALE = STALL_SECONDS + 1
TICK_SECONDS = 0.02
JOIN_TIMEOUT_SECONDS = 0.2
SHUTDOWN_BUDGET_SECONDS = 2


class FrameClock:
    """Plays CameraManager.seconds_since_last_frame."""

    def __init__(self) -> None:
        self.age = FRESH

    def __call__(self) -> float:
        return self.age


class Recorder:
    """A send function that notes which thread called it, or raises."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []
        self.error: Exception | None = None

    def __call__(self) -> None:
        if self.error is not None:
            raise self.error
        self.threads.append(threading.current_thread())


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def heartbeat(clock: FrameClock, send: Recorder) -> CaptureHeartbeat:
    return CaptureHeartbeat(clock, send, STALL_SECONDS, interval=TICK_SECONDS)


def test_pings_stop_when_frames_stop_and_resume_when_they_resume() -> None:
    clock, send = FrameClock(), Recorder()
    beat = heartbeat(clock, send)

    assert beat.beat()
    clock.age = STALE
    assert not beat.beat()
    clock.age = FRESH
    assert beat.beat()
    assert len(send.threads) == 2


def test_pings_run_on_their_own_thread() -> None:
    """Never on the caller's thread: a hung network call must not reach capture."""
    clock, send = FrameClock(), Recorder()
    beat = heartbeat(clock, send)
    beat.start()
    try:
        assert wait_for(lambda: len(send.threads) >= 2)
    finally:
        beat.stop()

    assert {t.name for t in send.threads} == {"capture-heartbeat"}
    assert threading.current_thread() not in send.threads


def test_a_hung_send_does_not_hold_up_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.capture.capture_heartbeat.STOP_TIMEOUT_SECONDS", JOIN_TIMEOUT_SECONDS
    )
    release = threading.Event()
    entered = threading.Event()

    def hung() -> None:
        entered.set()
        release.wait(timeout=30)

    beat = CaptureHeartbeat(FrameClock(), hung, STALL_SECONDS, interval=TICK_SECONDS)
    beat.start()
    assert entered.wait(timeout=5)

    started = time.monotonic()
    beat.stop()
    release.set()
    assert time.monotonic() - started < SHUTDOWN_BUDGET_SECONDS


def test_stop_ends_the_thread() -> None:
    beat = heartbeat(FrameClock(), Recorder())
    beat.start()
    thread = beat._thread
    assert thread is not None
    beat.stop()
    assert not thread.is_alive()


def test_a_failing_watcher_is_logged_once_per_streak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, send = FrameClock(), Recorder()
    beat = heartbeat(clock, send)
    send.error = OSError("network unreachable")

    with caplog.at_level(logging.INFO):
        assert not beat.beat()
        assert not beat.beat()
        send.error = None
        assert beat.beat()

    assert caplog.text.count("Capture heartbeat failed") == 1
    assert "getting through again" in caplog.text


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    status = 200
    paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- signature fixed by the base class
        pass


@pytest.fixture
def watcher() -> Iterator[tuple[str, type[_CountingHandler]]]:
    """A local HTTP server standing in for the dead man's switch."""
    handler = type("Handler", (_CountingHandler,), {"paths": [], "status": 200})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/ping/abc", handler
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_ping_gets_the_url(
    watcher: tuple[str, type[_CountingHandler]],
) -> None:
    url, handler = watcher
    http_ping(url)()
    assert handler.paths == ["/ping/abc"]


def test_an_error_status_counts_as_a_failed_ping(
    watcher: tuple[str, type[_CountingHandler]],
) -> None:
    url, handler = watcher
    handler.status = 500
    beat = CaptureHeartbeat(FrameClock(), http_ping(url), STALL_SECONDS)
    assert not beat.beat()


@pytest.mark.parametrize("url", ["hc-ping.com/abc", "file:///etc/passwd", ""])
def test_http_ping_refuses_anything_but_http(url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        http_ping(url)
