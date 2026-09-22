"""The largest foreground region in a frame, as the sidecar and the metrics carry it."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Blob:
    area: int
    x: int
    y: int
    w: int
    h: int

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    @property
    def centroid(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2
