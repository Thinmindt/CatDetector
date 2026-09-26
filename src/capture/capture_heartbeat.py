"""Tells an outside watcher that the camera is delivering frames."""

import http.client
import logging
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60.0
PING_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 5.0


def http_ping(url: str) -> Callable[[], None]:
    """A send function that GETs url. Raises ValueError unless it is http(s)."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("the heartbeat URL must start with http:// or https://")

    def send() -> None:
        with urllib.request.urlopen(url, timeout=PING_TIMEOUT_SECONDS):  # noqa: S310 -- scheme checked above
            pass

    return send


class CaptureHeartbeat:
    """Calls send once an interval, only while frames are arriving.

    A stall, a restart loop or a stopped detector all go quiet, which is what
    a dead man's switch alerts on. Runs on its own thread, never the capture one.
    """

    def __init__(
        self,
        frame_age: Callable[[], float],
        send: Callable[[], None],
        stall_seconds: float,
        interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.frame_age = frame_age
        self.send = send
        self.stall_seconds = stall_seconds
        self.interval = interval
        self._failing = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="capture-heartbeat", daemon=True
        )
        self._thread.start()
        log.info("Capture heartbeat every %.0f s while frames arrive", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_TIMEOUT_SECONDS)
            self._thread = None

    def beat(self) -> bool:
        """Ping if frames are arriving. Returns whether a ping was sent."""
        if self.frame_age() > self.stall_seconds:
            return False
        try:
            self.send()
        except (OSError, http.client.HTTPException) as error:
            if not self._failing:
                log.warning(
                    "Capture heartbeat failed (%s); retrying each interval", error
                )
            self._failing = True
            return False
        if self._failing:
            log.info("Capture heartbeat is getting through again")
        self._failing = False
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.beat()
            except Exception:
                log.exception("Capture heartbeat failed")
