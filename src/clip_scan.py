"""Reading finished clips off the share: names, sizes and sidecars. No database."""

import datetime
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from src.clip_format import CLIP_SUFFIX, H264_BITRATE
from src.clip_sidecar import ClipFacts, read_sidecar

log = logging.getLogger(__name__)

CLIP_TIMESTAMP = re.compile(r"_(\d{8})_(\d{6})(?:_\d+)?\.h264$")

# A clip with no sidecar was cut off by a crash; its end is estimated from size.
RECOVERED = "recovered"
BYTES_PER_SECOND = H264_BITRATE / 8


@dataclass(frozen=True)
class FoundClip:
    """A clip on the share and what its name, size and sidecar say about it."""

    path: Path
    size: int
    facts: ClipFacts | None

    @property
    def started_at(self) -> str | None:
        if self.facts is not None:
            return self.facts.started_at.isoformat(timespec="milliseconds")
        return _started_at_from_name(self.path.name)

    @property
    def ended_at(self) -> str | None:
        if self.facts is not None:
            return self.facts.ended_at.isoformat(timespec="milliseconds")
        return _estimated_end(self.started_at, self.size)

    @property
    def close_reason(self) -> str:
        return RECOVERED if self.facts is None else self.facts.close_reason.value

    @property
    def trigger(self) -> tuple[int, int] | None:
        blob = self.facts.trigger_blob if self.facts is not None else None
        return blob.centroid if blob is not None else None

    @property
    def last(self) -> tuple[int, int] | None:
        blob = self.facts.last_blob if self.facts is not None else None
        return blob.centroid if blob is not None else None


def scan_clips(directory: Path, skip: Collection[str]) -> list[FoundClip]:
    """Every readable clip in the directory, in name order, skipping known paths.

    Reads the share, so it can stall for minutes; never call it under a lock.
    """
    if not directory.is_dir():
        log.warning("Clip directory %s does not exist; nothing ingested", directory)
        return []
    clips = sorted(directory.glob(f"*{CLIP_SUFFIX}"))
    found = [_read_clip(clip) for clip in clips if str(clip) not in skip]
    return [clip for clip in found if clip is not None]


def _read_clip(clip: Path) -> FoundClip | None:
    """A clip's size and sidecar facts, or None if it cannot be read."""
    try:
        size = clip.stat().st_size
    except OSError as error:
        log.warning("Skipping %s: %s", clip, error)
        return None
    return FoundClip(clip, size, _facts_for(clip))


def _facts_for(clip: Path) -> ClipFacts | None:
    try:
        return read_sidecar(clip)
    except (OSError, ValueError, KeyError) as error:
        log.warning("Ignoring the sidecar of %s: %s", clip.name, error)
        return None


def _started_at_from_name(name: str) -> str | None:
    """ISO timestamp parsed from a clip filename, or None if it has none."""
    match = CLIP_TIMESTAMP.search(name)
    if match is None:
        return None
    try:
        stamp = datetime.datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.isoformat()


def _estimated_end(started: str | None, size: int) -> str | None:
    if started is None:
        return None
    end = datetime.datetime.fromisoformat(started) + datetime.timedelta(
        seconds=size / BYTES_PER_SECOND
    )
    return end.isoformat(timespec="milliseconds")
