"""Sidecars carry the facts a raw H.264 stream cannot."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from src.clip_sidecar import (
    ClipFacts,
    CloseReason,
    read_sidecar,
    sidecar_name,
    write_sidecar,
)
from src.motion_metrics import Blob


def facts() -> ClipFacts:
    return ClipFacts(
        started_at=datetime.datetime(2026, 9, 12, 8, 4, 13, 120000),
        ended_at=datetime.datetime(2026, 9, 12, 8, 4, 26, 500000),
        close_reason=CloseReason.TIMEOUT,
        trigger_blob=Blob(area=900, x=100, y=50, w=40, h=60),
        last_blob=None,
    )


def test_sidecar_sits_beside_the_clip_under_its_final_name(tmp_path: Path) -> None:
    clip = tmp_path / "cat_video_20260912_080413.h264"
    assert sidecar_name(clip) == tmp_path / "cat_video_20260912_080413.h264.json"


def test_facts_survive_a_round_trip(tmp_path: Path) -> None:
    clip = tmp_path / "cat_video_20260912_080413.h264"
    write_sidecar(clip, facts())
    assert read_sidecar(clip) == facts()


def test_a_clip_without_a_sidecar_reads_as_none(tmp_path: Path) -> None:
    assert read_sidecar(tmp_path / "cat_video_20260912_080413.h264") is None


def test_the_json_is_readable_without_this_code(tmp_path: Path) -> None:
    """Ingest and ad-hoc scripts read the file directly, so the fields that
    matter for grouping are plain values, centroid included."""
    clip = tmp_path / "cat_video_20260912_080413.h264"
    write_sidecar(clip, facts())

    payload = json.loads(sidecar_name(clip).read_text())

    assert payload["started_at"] == "2026-09-12T08:04:13.120"
    assert payload["ended_at"] == "2026-09-12T08:04:26.500"
    assert payload["close_reason"] == "timeout"
    assert payload["trigger_blob"]["cx"] == 120
    assert payload["trigger_blob"]["cy"] == 80
    assert payload["last_blob"] is None
