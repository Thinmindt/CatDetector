"""Ships finished clips from local disk to the network share."""

import logging
import os
import shutil
import threading
from pathlib import Path

from src.clip_format import CLIP_SUFFIX, partial_name
from src.clip_sidecar import sidecar_name

log = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 60
STOP_TIMEOUT_SECONDS = 5


class ClipTransfer:
    """Moves completed clips to the share, retrying until they land.

    Every call here can block on the share, so it runs on its own thread. A clip
    is only deleted locally once it has arrived under its final name, and the
    scan is stateless: clips left behind by a crash ship on the next pass.
    """

    def __init__(
        self,
        local_directory: str | Path,
        destination: str | Path,
        share_root: str | Path,
        scan_interval: float = SCAN_INTERVAL_SECONDS,
    ) -> None:
        self.local_directory = Path(local_directory)
        self.destination = Path(destination)
        self.share_root = Path(share_root)
        self.scan_interval = scan_interval
        self.local_directory.mkdir(parents=True, exist_ok=True)
        self._share_was_ready = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Shipping clips from %s to %s", self.local_directory, self.destination)

    def stop(self) -> None:
        """Stop the worker. Anything pending ships on the next run."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_TIMEOUT_SECONDS)
            self._thread = None

    def pending(self) -> list[Path]:
        """Finished clips waiting to be shipped, oldest first."""
        return sorted(self.local_directory.glob(f"*{CLIP_SUFFIX}"))

    def transfer_once(self) -> int:
        """Ship whatever is waiting. Returns how many clips landed."""
        waiting = self.pending()
        if not waiting or not self._share_is_ready(len(waiting)):
            return 0
        return sum(self._ship(clip) for clip in waiting)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.transfer_once()
            except Exception:
                log.exception("Clip transfer cycle failed")
            self._stop.wait(self.scan_interval)

    def _share_is_ready(self, waiting: int) -> bool:
        """Whether the share is mounted, logging only when that changes.

        Writing to an absent mountpoint would put clips on the SD card, where the
        share hides them the moment it mounts.
        """
        ready = os.path.ismount(self.share_root)
        if ready and not self._share_was_ready:
            log.info("%s is back; shipping %d clip(s)", self.share_root, waiting)
        elif not ready and self._share_was_ready:
            log.warning(
                "%s is not mounted; %d clip(s) held on local disk",
                self.share_root,
                waiting,
            )
        self._share_was_ready = ready
        return ready

    def _ship(self, clip: Path) -> bool:
        """Copy the sidecar, then the clip, so the facts land first."""
        sidecar = sidecar_name(clip)
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
            if sidecar.exists():
                self._copy(sidecar)
            self._copy(clip)
        except OSError as error:
            log.warning("Could not ship %s, keeping it local: %s", clip.name, error)
            return False

        sidecar.unlink(missing_ok=True)
        clip.unlink()
        log.info("Shipped %s to %s", clip.name, self.destination)
        return True

    def _copy(self, source: Path) -> None:
        """Copy under the partial name, then rename within the share.

        The rename is atomic, so the final name never appears until every
        byte is there.
        """
        staged = partial_name(self.destination / source.name)
        try:
            shutil.copyfile(source, staged)
            self._check_size(source, staged)
            staged.replace(self.destination / source.name)
        except OSError:
            self._discard(staged)
            raise

    @staticmethod
    def _check_size(source: Path, staged: Path) -> None:
        if staged.stat().st_size != source.stat().st_size:
            raise OSError(f"short copy of {source.name}")

    @staticmethod
    def _discard(staged: Path) -> None:
        try:
            staged.unlink(missing_ok=True)
        except OSError as error:
            log.warning("Could not clear %s: %s", staged, error)
