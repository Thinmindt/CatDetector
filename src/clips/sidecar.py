"""The facts about a clip that its raw H.264 stream cannot carry."""

import datetime
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from src.clips.blob import Blob

SIDECAR_SUFFIX = ".json"


class CloseReason(StrEnum):
    TIMEOUT = "timeout"
    MAX_LENGTH = "max_length"
    SHUTDOWN = "shutdown"
    # Writes failed partway through, so the clip stops short of its end time.
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class ClipFacts:
    started_at: datetime.datetime
    ended_at: datetime.datetime
    close_reason: CloseReason
    trigger_blob: Blob | None
    last_blob: Blob | None


def sidecar_name(clip: Path) -> Path:
    """The sidecar beside a clip, named after the clip's final name."""
    return clip.with_name(clip.name + SIDECAR_SUFFIX)


def write_sidecar(clip: Path, facts: ClipFacts) -> None:
    payload = {
        "started_at": facts.started_at.isoformat(timespec="milliseconds"),
        "ended_at": facts.ended_at.isoformat(timespec="milliseconds"),
        "close_reason": facts.close_reason.value,
        "trigger_blob": _blob_fields(facts.trigger_blob),
        "last_blob": _blob_fields(facts.last_blob),
    }
    sidecar_name(clip).write_text(json.dumps(payload, indent=1))


def read_sidecar(clip: Path) -> ClipFacts | None:
    """The facts shipped with a clip, or None if it has no sidecar."""
    try:
        payload = json.loads(sidecar_name(clip).read_text())
    except FileNotFoundError:
        return None
    return ClipFacts(
        started_at=datetime.datetime.fromisoformat(payload["started_at"]),
        ended_at=datetime.datetime.fromisoformat(payload["ended_at"]),
        close_reason=CloseReason(payload["close_reason"]),
        trigger_blob=_blob_from(payload["trigger_blob"]),
        last_blob=_blob_from(payload["last_blob"]),
    )


def _blob_fields(blob: Blob | None) -> dict[str, int] | None:
    if blob is None:
        return None
    cx, cy = blob.centroid
    return {
        "area": blob.area,
        "x": blob.x,
        "y": blob.y,
        "w": blob.w,
        "h": blob.h,
        "cx": cx,
        "cy": cy,
    }


def _blob_from(fields: dict[str, int] | None) -> Blob | None:
    if fields is None:
        return None
    return Blob(
        area=fields["area"], x=fields["x"], y=fields["y"], w=fields["w"], h=fields["h"]
    )
