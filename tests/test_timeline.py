"""The timeline: its filter, its strips' counts, and the API over the database."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import BOX_1, BOX_2, BOX_3, make_clip_file, make_clip_with_facts, visit
from flask import Flask

from src.review.api import Review, create_review_blueprint
from src.review.clip_frames import ClipFrames
from src.review.timeline import (
    DATE,
    HOURS,
    UNLABELED,
    TimelineFilter,
    TimelineRow,
    build_timeline,
)

CATS = ("Ada", "Bea")
DAY = datetime.date(2026, 9, 20)


def row(
    event_id: int,
    hour: int | None,
    label: str | None = None,
    day: int = 0,
    labeled_at: datetime.datetime | None = None,
    trigger_cx: int | None = None,
) -> TimelineRow:
    started = (
        None
        if hour is None
        else datetime.datetime.combine(
            DAY + datetime.timedelta(days=day), datetime.time(hour)
        )
    )
    return TimelineRow(
        id=event_id,
        started_at=started,
        ended_at=None if started is None else started + datetime.timedelta(seconds=30),
        clips=1,
        label=label,
        labeled_at=labeled_at,
        trigger_cx=trigger_cx,
        staged=False,
    )


def ids(rows: list[TimelineRow]) -> list[int]:
    return [r.id for r in rows]


# --- the filter -------------------------------------------------------------


def test_rows_are_newest_first_with_undated_rows_last() -> None:
    rows = [row(1, 8), row(2, 9, day=1), row(3, None), row(4, 23)]
    assert ids(build_timeline(rows, TimelineFilter()).rows) == [2, 4, 1, 3]


def test_chips_choose_labels_and_the_unlabelled() -> None:
    rows = [row(1, 8, "Ada"), row(2, 9, "not_cat"), row(3, 10)]
    only = TimelineFilter(chips=frozenset({"Ada", UNLABELED}))
    assert ids(build_timeline(rows, only).rows) == [3, 1]


def test_days_are_inclusive_and_drop_undated_rows() -> None:
    rows = [row(1, 8, day=0), row(2, 8, day=1), row(3, 8, day=2), row(4, None)]
    middle = TimelineFilter(
        first_day=DAY + datetime.timedelta(days=1),
        last_day=DAY + datetime.timedelta(days=2),
    )
    assert ids(build_timeline(rows, middle).rows) == [3, 2]


@pytest.mark.parametrize(
    ("hours", "expected"),
    [((8, 10), [2, 1]), ((22, 6), [6, 5, 4]), ((0, 24), [6, 5, 4, 3, 2, 1])],
)
def test_hours_are_half_open_and_may_cross_midnight(
    hours: tuple[int, int], expected: list[int]
) -> None:
    rows = [row(1, 8), row(2, 9), row(3, 10), row(4, 22), row(5, 23), row(6, 5)]
    assert sorted(
        ids(build_timeline(rows, TimelineFilter(hours=hours)).rows)
    ) == sorted(expected)


def test_labelled_before_takes_only_labels_older_than_it() -> None:
    cutoff = datetime.datetime(2026, 9, 20, 12)
    rows = [
        row(1, 8, "not_cat", labeled_at=cutoff - datetime.timedelta(hours=1)),
        row(2, 9, "not_cat", labeled_at=cutoff),
        row(3, 10),
    ]
    assert ids(build_timeline(rows, TimelineFilter(labeled_before=cutoff)).rows) == [1]


def test_each_strip_counts_under_every_filter_but_its_own() -> None:
    rows = [
        row(1, 8, "Ada", day=0),
        row(2, 23, "Ada", day=1),
        row(3, 23, "Bea", day=1),
        row(4, 8, "Ada", day=2),
    ]
    chosen = TimelineFilter(
        chips=frozenset({"Ada"}), first_day=DAY, last_day=DAY, hours=(22, 6)
    )
    timeline = build_timeline(rows, chosen)

    assert timeline.rows == []
    assert {day.day: dict(n) for day, n in timeline.by_day.items()} == {21: {"Ada": 1}}
    assert timeline.by_hour[8] == {"Ada": 1}
    assert sum(sum(n.values()) for n in timeline.by_hour) == 1
    assert (timeline.first_day, timeline.last_day) == (
        DAY,
        DAY + datetime.timedelta(days=2),
    )
    assert chosen.admits(rows[1], ignoring=DATE)
    assert chosen.admits(rows[0], ignoring=HOURS)


@pytest.mark.parametrize(
    ("trigger_cx", "box"), [(0, 1), (213, 1), (214, 2), (426, 2), (427, 3), (639, 3)]
)
def test_the_box_is_the_third_of_the_frame_holding_the_trigger(
    trigger_cx: int, box: int
) -> None:
    assert row(1, 8, trigger_cx=trigger_cx).box == box
    assert row(1, 8).box is None


# --- the API over a database ------------------------------------------------


@pytest.fixture
def timeline_client(db: Any, tmp_path: Path) -> tuple[Any, dict[str, int]]:
    """Visits at 07:00 and 23:30 on two days and one undated clip, labelled.
    Returns the client and the event ids by name."""
    directory = tmp_path / "captures"
    visits = {
        "ada_morning": (datetime.datetime(2026, 9, 20, 7), BOX_1, "Ada"),
        "bea_night": (datetime.datetime(2026, 9, 20, 23, 30), BOX_3, "Bea"),
        "false_morning": (datetime.datetime(2026, 9, 21, 7), BOX_2, "not_cat"),
        "open_night": (datetime.datetime(2026, 9, 21, 23, 30), BOX_1, None),
    }
    for started, place, _ in visits.values():
        make_clip_with_facts(directory, started, seconds=40, place=place)
    make_clip_file(directory / "oddly-named.h264")
    db.ingest(directory)
    app = Flask(__name__)
    review = Review(db, ClipFrames(tmp_path / "cache"), directory, CATS)
    app.register_blueprint(create_review_blueprint(review))
    client = app.test_client()
    by_start = {row.started_at: row.id for row in db.timeline_rows()}
    names = {name: by_start[started] for name, (started, _, _) in visits.items()}
    names["undated"] = by_start[None]
    for name, (_, _, label) in visits.items():
        if label:
            client.post(f"/api/review/{names[name]}/label", json={"value": label})
    return client, names


def shown(client: Any, query: str) -> list[int]:
    response = client.get(f"/api/timeline?{query}")
    assert response.status_code == 200, response.get_json()
    return [r["id"] for r in response.get_json()["rows"]]


def query_db(db: Any, sql: str, *params: Any) -> set[int]:
    """Event ids from SQL written independently of the timeline's own code."""
    connection = sqlite3.connect(db.path)
    try:
        return {int(r[0]) for r in connection.execute(sql, params)}
    finally:
        connection.close()


FIRST_START = (
    "SELECT e.id FROM event e JOIN clip c ON c.event_id = e.id"
    " LEFT JOIN label l ON l.event_id = e.id GROUP BY e.id"
)


@pytest.mark.parametrize(
    ("query", "sql"),
    [
        ("", FIRST_START),
        ("label=Ada", "SELECT event_id FROM label WHERE value = 'Ada'"),
        (
            "label=Ada&label=not_cat",
            "SELECT event_id FROM label WHERE value IN ('Ada', 'not_cat')",
        ),
        ("label=unlabeled", FIRST_START + " HAVING MAX(l.value) IS NULL"),
        (
            "from=2026-09-21&to=2026-09-21",
            FIRST_START + " HAVING date(MIN(c.started_at)) = '2026-09-21'",
        ),
        (
            "hours=22-6",
            FIRST_START + " HAVING CAST(strftime('%H', MIN(c.started_at)) AS INT)"
            " NOT BETWEEN 6 AND 21",
        ),
        (
            "hours=22-6&label=unlabeled",
            FIRST_START + " HAVING CAST(strftime('%H', MIN(c.started_at)) AS INT)"
            " NOT BETWEEN 6 AND 21 AND MAX(l.value) IS NULL",
        ),
        (
            "hours=6-12&from=2026-09-21",
            FIRST_START + " HAVING CAST(strftime('%H', MIN(c.started_at)) AS INT)"
            " BETWEEN 6 AND 11 AND date(MIN(c.started_at)) >= '2026-09-21'",
        ),
        (
            "labeled_before=2999-01-01&label=Bea",
            "SELECT event_id FROM label WHERE value = 'Bea'",
        ),
        ("labeled_before=2000-01-01", "SELECT event_id FROM label WHERE 0"),
    ],
)
def test_the_api_returns_exactly_what_the_database_holds(
    db: Any, timeline_client: tuple[Any, dict[str, int]], query: str, sql: str
) -> None:
    client, _ = timeline_client
    got = shown(client, query)
    assert len(got) == len(set(got))
    assert set(got) == query_db(db, sql)


def test_the_api_lists_newest_first_with_the_columns(
    timeline_client: tuple[Any, dict[str, int]],
) -> None:
    client, names = timeline_client
    body = client.get("/api/timeline").get_json()
    order = ["open_night", "false_morning", "bea_night", "ada_morning", "undated"]
    assert [r["id"] for r in body["rows"]] == [names[n] for n in order]
    first = body["rows"][0]
    assert (first["label"], first["box"], first["clips"], first["seconds"]) == (
        None,
        1,
        1,
        40,
    )
    assert body["rows"][2]["box"] == 3
    assert (body["first_day"], body["last_day"]) == ("2026-09-20", "2026-09-21")
    assert body["by_day"]["2026-09-20"] == {"Ada": 1, "Bea": 1}
    assert body["by_hour"][23] == {"Bea": 1, "unlabeled": 1}


@pytest.mark.parametrize(
    "query",
    ["label=dog", "from=yesterday", "hours=5", "hours=6-6", "hours=3-25", "hours=a-b"],
)
def test_a_filter_the_api_cannot_read_is_a_400(
    timeline_client: tuple[Any, dict[str, int]], query: str
) -> None:
    client, _ = timeline_client
    assert client.get(f"/api/timeline?{query}").status_code == 400


def test_stage_what_is_shown_stages_exactly_those_ids(
    db: Any, timeline_client: tuple[Any, dict[str, int]]
) -> None:
    client, names = timeline_client
    mornings = shown(client, "hours=6-12")
    response = client.post(
        "/api/review/stage", json={"event_ids": mornings, "reason": "recheck"}
    )
    assert response.status_code == 200
    assert response.get_json()["staged"] == 2
    staged = {
        r["id"] for r in client.get("/api/timeline").get_json()["rows"] if r["staged"]
    }
    assert staged == {names["ada_morning"], names["false_morning"]}
    assert db.get(names["ada_morning"]).label == "Ada"


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "x"},
        {"event_ids": "1", "reason": "x"},
        {"event_ids": [1, "2"], "reason": "x"},
        {"event_ids": [1]},
        {"event_ids": [1], "reason": "x", "source": "cat"},
    ],
)
def test_stage_many_refuses_what_it_cannot_read(
    timeline_client: tuple[Any, dict[str, int]], body: dict[str, Any]
) -> None:
    client, _ = timeline_client
    assert client.post("/api/review/stage", json=body).status_code == 400


def test_a_visit_of_several_clips_is_one_row_spanning_them(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    db.ingest(directory)
    (only,) = build_timeline(db.timeline_rows(), TimelineFilter()).rows
    assert (only.clips, only.seconds) == (2, 55)
