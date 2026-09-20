"""The grouping rule: which clips form one visit."""

from __future__ import annotations

import datetime

from conftest import BOX_1, BOX_2, BOX_3, DISTANCE, GAP, T0

from src.review.event_grouping import Boundary, ClipRow, group_clips


def clip(
    name: str,
    start: int,
    end: int,
    place: tuple[int, int] | None = BOX_1,
    boundary: Boundary | None = None,
    joins: str | None = None,
) -> ClipRow:
    """A clip running from T0+start to T0+end seconds, seen at one place."""
    return ClipRow(
        path=name,
        started_at=T0 + datetime.timedelta(seconds=start),
        ended_at=T0 + datetime.timedelta(seconds=end),
        trigger=place,
        last=place,
        boundary=boundary,
        joins=joins,
    )


def names(groups: list[list[ClipRow]]) -> list[list[str]]:
    return [[c.path for c in group] for group in groups]


def test_a_clip_soon_after_the_last_one_continues_the_visit() -> None:
    groups = group_clips([clip("a", 0, 20), clip("b", 40, 55)], GAP, DISTANCE)
    assert names(groups) == [["a", "b"]]


def test_any_number_of_clips_can_form_one_visit() -> None:
    clips = [
        clip("a", 0, 20),
        clip("b", 40, 55),
        clip("c", 90, 100),
        clip("d", 130, 140),
    ]
    assert names(group_clips(clips, GAP, DISTANCE)) == [["a", "b", "c", "d"]]


def test_a_gap_longer_than_the_threshold_starts_a_new_visit() -> None:
    groups = group_clips([clip("a", 0, 20), clip("b", 81, 90)], GAP, DISTANCE)
    assert names(groups) == [["a"], ["b"]]


def test_the_gap_is_measured_from_the_previous_end_not_its_start() -> None:
    """A long clip followed quickly by another is one visit even when the
    starts are far apart."""
    groups = group_clips([clip("a", 0, 300), clip("b", 320, 330)], GAP, DISTANCE)
    assert names(groups) == [["a", "b"]]


def test_a_different_box_is_a_different_visit_even_when_close_in_time() -> None:
    groups = group_clips(
        [clip("a", 0, 20, BOX_1), clip("b", 30, 40, BOX_3)], GAP, DISTANCE
    )
    assert names(groups) == [["a"], ["b"]]


def test_visits_in_different_boxes_can_interleave() -> None:
    clips = [
        clip("a1", 0, 20, BOX_1),
        clip("b1", 25, 35, BOX_2),
        clip("a2", 50, 60, BOX_1),
        clip("b2", 70, 80, BOX_2),
    ]
    assert names(group_clips(clips, GAP, DISTANCE)) == [["a1", "a2"], ["b1", "b2"]]


def test_an_unknown_position_matches_on_time_alone() -> None:
    """A clip recovered after a crash has no centroids and must still join."""
    groups = group_clips(
        [clip("a", 0, 20, BOX_1), clip("b", 30, 40, None)], GAP, DISTANCE
    )
    assert names(groups) == [["a", "b"]]


def test_a_split_boundary_starts_a_new_visit_regardless_of_the_rule() -> None:
    groups = group_clips(
        [clip("a", 0, 20), clip("b", 30, 40, boundary=Boundary.SPLIT)], GAP, DISTANCE
    )
    assert names(groups) == [["a"], ["b"]]


def test_a_join_boundary_continues_the_named_clip_regardless_of_the_rule() -> None:
    clips = [
        clip("a", 0, 20, BOX_1),
        clip("c", 30, 40, BOX_3),
        clip("b", 600, 610, BOX_1, boundary=Boundary.JOIN, joins="a"),
    ]
    assert names(group_clips(clips, GAP, DISTANCE)) == [["a", "b"], ["c"]]


def test_a_join_to_an_unknown_clip_falls_back_to_the_rule() -> None:
    groups = group_clips(
        [clip("a", 0, 20), clip("b", 600, 610, boundary=Boundary.JOIN, joins="nope")],
        GAP,
        DISTANCE,
    )
    assert names(groups) == [["a"], ["b"]]


def test_input_order_does_not_matter_and_output_is_oldest_first() -> None:
    clips = [
        clip("a", 0, 20),
        clip("b", 40, 55),
        clip("z", 500, 510),
        clip("y", 530, 540),
    ]
    newest_first = list(reversed(clips))
    assert names(group_clips(newest_first, GAP, DISTANCE)) == [["a", "b"], ["z", "y"]]
