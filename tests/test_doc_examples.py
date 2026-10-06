"""The examples in DESIGN.md are what the code writes and reads."""

from __future__ import annotations

import csv
import datetime
import json
import re
from pathlib import Path

from src.capture.motion_metrics import FIELDS
from src.clips.blob import Blob
from src.clips.sidecar import (
    ClipFacts,
    CloseReason,
    read_sidecar,
    sidecar_name,
    write_sidecar,
)

DESIGN = Path(__file__).resolve().parents[1] / "docs" / "DESIGN.md"


def example(language: str) -> str:
    """The one fenced block of this language in DESIGN.md."""
    fence = rf"^```{language}\n(.*?)^```"
    blocks = re.findall(fence, DESIGN.read_text(), re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, f"DESIGN.md should hold one ```{language} example"
    return str(blocks[0])


def keys(value: object) -> object:
    """The shape of a JSON value: its keys, recursively, without the values."""
    if isinstance(value, dict):
        return {key: keys(item) for key, item in value.items()}
    return None


def test_the_sidecar_example_has_the_fields_the_recorder_writes(tmp_path: Path) -> None:
    clip = tmp_path / "cat_video_20260912_080413.h264"
    blob = Blob(area=1, x=2, y=3, w=4, h=5)
    write_sidecar(
        clip,
        ClipFacts(
            started_at=datetime.datetime(2026, 9, 12, 8, 4, 13),
            ended_at=datetime.datetime(2026, 9, 12, 8, 5, 2),
            close_reason=CloseReason.TIMEOUT,
            trigger_blob=blob,
            last_blob=blob,
        ),
    )
    written = json.loads(sidecar_name(clip).read_text())
    assert keys(json.loads(example("json"))) == keys(written)


def test_the_sidecar_example_reads_back_as_its_facts(tmp_path: Path) -> None:
    clip = tmp_path / "cat_video_20260912_080413.h264"
    sidecar_name(clip).write_text(example("json"))
    facts = read_sidecar(clip)
    assert facts is not None
    assert facts.close_reason is CloseReason.TIMEOUT
    assert facts.trigger_blob is not None
    shown = json.loads(example("json"))["trigger_blob"]
    assert facts.trigger_blob.centroid == (shown["cx"], shown["cy"])


def test_the_metrics_example_has_the_columns_the_log_writes() -> None:
    header, *rows = list(csv.reader(example("csv").splitlines()))
    assert header == FIELDS
    assert rows
    assert all(len(row) == len(FIELDS) for row in rows)
