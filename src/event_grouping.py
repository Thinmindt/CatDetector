"""Which clips belong to one litter-box visit."""

import datetime
import math
from collections.abc import Sequence
from dataclasses import dataclass

Centroid = tuple[int, int]


@dataclass(frozen=True)
class ClipRow:
    """What the grouping rule needs to know about one clip."""

    path: str
    started_at: datetime.datetime
    ended_at: datetime.datetime
    trigger: Centroid | None
    last: Centroid | None
    boundary: str | None = None  # "split" or "join", set by a reviewer
    joins: str | None = None  # path of the clip a "join" continues


@dataclass
class _Chain:
    clips: list[ClipRow]

    @property
    def ended_at(self) -> datetime.datetime:
        return self.clips[-1].ended_at

    @property
    def place(self) -> Centroid | None:
        tail = self.clips[-1]
        return tail.last or tail.trigger


def group_clips(
    clips: Sequence[ClipRow], gap_seconds: float, box_distance_px: float
) -> list[list[ClipRow]]:
    """Chains of clips that form one visit each, oldest first.

    A clip continues the most recent chain that ended within gap_seconds of
    its start in the same place. A reviewer's boundary overrides the rule.
    """
    chains: list[_Chain] = []
    by_path: dict[str, _Chain] = {}
    for clip in sorted(clips, key=lambda c: (c.started_at, c.path)):
        chain = _chain_for(clip, chains, by_path, gap_seconds, box_distance_px)
        if chain is None:
            chain = _Chain([])
            chains.append(chain)
        chain.clips.append(clip)
        by_path[clip.path] = chain
    return [chain.clips for chain in chains]


def _chain_for(
    clip: ClipRow,
    chains: list[_Chain],
    by_path: dict[str, _Chain],
    gap_seconds: float,
    box_distance_px: float,
) -> _Chain | None:
    if clip.boundary == "split":
        return None
    if clip.boundary == "join" and clip.joins in by_path:
        return by_path[clip.joins]

    earliest = clip.started_at - datetime.timedelta(seconds=gap_seconds)
    candidates = [
        chain
        for chain in chains
        if chain.ended_at >= earliest
        and _same_place(clip.trigger or clip.last, chain.place, box_distance_px)
    ]
    return max(candidates, key=lambda chain: chain.ended_at, default=None)


def _same_place(a: Centroid | None, b: Centroid | None, box_distance_px: float) -> bool:
    """Whether two centroids are in one box. Unknown positions never separate."""
    if a is None or b is None:
        return True
    return math.dist(a, b) <= box_distance_px
