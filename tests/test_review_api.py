"""The review API: walks, labels, counts, grouping corrections and media routes."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import make_clip_file, visit
from flask import Flask

from src.review.api import Review, create_review_blueprint
from src.review.clip_frames import ClipFrames

CATS = ("Ada", "Bea")


def client_for(db: Any, tmp_path: Path, cats: tuple[str, ...] = ()) -> Any:
    """A test client for the review API alone, over this database."""
    app = Flask(__name__)
    review = Review(db, ClipFrames(tmp_path / "cache"), tmp_path / "captures", cats)
    app.register_blueprint(create_review_blueprint(review))
    return app.test_client()


@pytest.fixture
def client(db: Any, clip_dir: Path, tmp_path: Path) -> Any:
    db.ingest(clip_dir)
    return client_for(db, tmp_path)


def test_next_returns_the_oldest_event(client: Any) -> None:
    data = client.get("/api/review/next").get_json()
    assert data["event"]["started_at"] == "2026-08-25T07:24:52.000"
    assert data["counts"]["total"] == 2


def test_labeling_advances_and_counts(client: Any) -> None:
    first = client.get("/api/review/next").get_json()["event"]

    response = client.post(f"/api/review/{first['id']}/label", json={"value": "cat"})
    assert response.status_code == 200

    data = client.get("/api/review/next").get_json()
    assert data["event"]["id"] != first["id"]
    assert data["counts"] == {"total": 2, "labeled": 1, "multi_clip": 0, "cat": 1}


def test_all_labeled_returns_null_event(client: Any) -> None:
    for _ in range(2):
        event = client.get("/api/review/next").get_json()["event"]
        client.post(f"/api/review/{event['id']}/label", json={"value": "not_cat"})

    assert client.get("/api/review/next").get_json()["event"] is None


def test_invalid_label_is_rejected(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    response = client.post(f"/api/review/{event['id']}/label", json={"value": "dog"})
    assert response.status_code == 400
    response = client.post(f"/api/review/{event['id']}/label", json={})
    assert response.status_code == 400


def test_a_cleaning_is_labeled_and_carries_its_poop_counts(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]

    labeled = client.post(f"/api/review/{event['id']}/label", json={"value": "clean"})
    assert labeled.status_code == 200
    assert labeled.get_json()["counts"]["clean"] == 1

    counted = client.post(
        f"/api/review/{event['id']}/counts", json={"counts": {"1": 2, "3": 0}}
    )
    assert counted.status_code == 200
    assert counted.get_json()["event"]["poops"] == {"1": 2, "3": 0}


def test_poop_counts_reject_unknown_boxes_and_impossible_counts(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    bodies: tuple[dict[str, Any], ...] = (
        {"counts": {"4": 1}},
        {"counts": {"1": -1}},
        {"counts": {}},
        {},
    )
    for body in bodies:
        response = client.post(f"/api/review/{event['id']}/counts", json=body)
        assert response.status_code == 400, body
    assert (
        client.post("/api/review/999/counts", json={"counts": {"1": 1}}).status_code
        == 404
    )


def test_unknown_event_404s(client: Any) -> None:
    assert (
        client.post("/api/review/999/label", json={"value": "cat"}).status_code == 404
    )
    assert client.get("/api/review/event/999").status_code == 404
    assert client.get("/review/clip/999/strip.jpg").status_code == 404


def test_the_payload_lists_every_clip_of_the_event(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    assert event["ended_at"] is not None
    assert [c["index"] for c in event["clips"]] == [0]
    assert event["clips"][0]["close_reason"] == "recovered"


# --- review API: grouping ---------------------------------------------------


@pytest.fixture
def grouped_client(db: Any, tmp_path: Path) -> Any:
    """One two-clip visit and, later, a one-clip visit."""
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    visit(directory, 500)
    db.ingest(directory)
    return client_for(db, tmp_path)


def test_the_page_walks_multi_clip_events_on_request(grouped_client: Any) -> None:
    first = grouped_client.get("/api/review/next?filter=multi").get_json()["event"]
    assert len(first["clips"]) == 2

    after = grouped_client.get(f"/api/review/next?filter=multi&after={first['id']}")
    assert after.get_json()["event"] is None


def test_split_and_join_from_the_api(grouped_client: Any) -> None:
    event = grouped_client.get("/api/review/next").get_json()["event"]

    split = grouped_client.post(f"/api/review/{event['id']}/split", json={"index": 1})
    assert split.status_code == 200
    assert len(split.get_json()["event"]["clips"]) == 1
    assert split.get_json()["counts"]["total"] == 3

    joined = grouped_client.post(f"/api/review/{event['id']}/join")
    assert joined.status_code == 200
    assert len(joined.get_json()["event"]["clips"]) == 2
    assert joined.get_json()["event"]["clips"][1]["boundary"] == "join"


def test_split_rejects_the_first_clip_and_bad_input(grouped_client: Any) -> None:
    event = grouped_client.get("/api/review/next").get_json()["event"]
    for body in ({"index": 0}, {"index": 9}, {"index": "1"}, {}):
        response = grouped_client.post(f"/api/review/{event['id']}/split", json=body)
        assert response.status_code == 400, body
    assert (
        grouped_client.post("/api/review/999/split", json={"index": 1}).status_code
        == 404
    )
    assert grouped_client.post("/api/review/999/join").status_code == 404


def test_the_video_route_serves_a_playable_clip(
    db: Any, tmp_path: Path, short_clip: Path
) -> None:
    directory = tmp_path / "captures"
    directory.mkdir()
    shutil.copy(short_clip, directory / "cat_video_20260912_080000.h264")
    db.ingest(directory)
    client = client_for(db, tmp_path)
    event = client.get("/api/review/next").get_json()["event"]

    url = f"/review/clip/{event['clips'][0]['id']}/video.mp4"
    response = client.get(url)
    assert response.status_code == 200
    assert response.mimetype == "video/mp4"

    # Seeking in the player needs byte ranges honoured, not just advertised.
    partial = client.get(url, headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206
    assert len(partial.data) == 10


def test_the_video_route_404s_for_an_unreadable_clip(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ClipFrames, "video", lambda *args: None)
    event = client.get("/api/review/next").get_json()["event"]
    assert (
        client.get(f"/review/clip/{event['clips'][0]['id']}/video.mp4").status_code
        == 404
    )


def test_undo_fetch_shows_the_existing_label(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    client.post(f"/api/review/{event['id']}/label", json={"value": "unsure"})

    data = client.get(f"/api/review/event/{event['id']}").get_json()
    assert data["event"]["label"] == "unsure"


def test_strip_route_for_an_unreadable_clip_404s(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ClipFrames, "strip", lambda *args: None)
    event = client.get("/api/review/next").get_json()["event"]
    assert (
        client.get(f"/review/clip/{event['clips'][0]['id']}/strip.jpg").status_code
        == 404
    )


def test_a_label_is_a_walk_of_its_own(db: Any, tmp_path: Path) -> None:
    for name in ("cat_video_20260917_080000", "cat_video_20260917_090000"):
        make_clip_file(tmp_path / "captures" / f"{name}.h264")
    db.ingest(tmp_path / "captures")
    client = client_for(db, tmp_path)
    first = client.get("/api/review/next").get_json()["event"]["id"]
    client.post(f"/api/review/{first}/label", json={"value": "unsure"})

    labeled = client.get("/api/review/next?filter=labeled").get_json()["event"]
    assert labeled["id"] == first
    assert (
        client.get("/api/review/next?filter=unsure").get_json()["event"]["id"] == first
    )
    assert client.get("/api/review/next?filter=cat").get_json()["event"] is None
    assert client.get("/api/review/next?filter=dog").status_code == 400


def test_cat_names_and_multiple_are_labels_and_count_by_name(
    db: Any, clip_dir: Path, tmp_path: Path
) -> None:
    db.ingest(clip_dir)
    client = client_for(db, tmp_path, CATS)
    first = client.get("/api/review/next").get_json()["event"]["id"]
    assert (
        client.post(f"/api/review/{first}/label", json={"value": "Bea"}).status_code
        == 200
    )

    second = client.get("/api/review/next").get_json()["event"]["id"]
    response = client.post(f"/api/review/{second}/label", json={"value": "multiple"})
    assert response.status_code == 200
    assert response.get_json()["counts"] == {
        "total": 2,
        "labeled": 2,
        "multi_clip": 0,
        "Bea": 1,
        "multiple": 1,
    }
    assert client.get("/api/review/next?filter=Bea").get_json()["event"]["id"] == first
    assert client.get("/api/review/next?filter=Ada").get_json()["event"] is None


def test_without_cat_names_a_name_and_multiple_are_rejected(client: Any) -> None:
    event = client.get("/api/review/next").get_json()["event"]
    for value in ("Ada", "multiple"):
        response = client.post(
            f"/api/review/{event['id']}/label", json={"value": value}
        )
        assert response.status_code == 400, value
    assert client.get("/api/review/next?filter=multiple").status_code == 400


@pytest.mark.parametrize(
    "names",
    [
        ("cat", "Ada"),
        ("Ada", "multiple"),
        ("multi",),
        ("labeled",),
        ("Ada", "Ada"),
        ("",),
    ],
)
def test_a_cat_may_not_take_a_reserved_or_repeated_name(
    db: Any, tmp_path: Path, names: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="CAT_NAMES"):
        Review(db, ClipFrames(tmp_path / "cache"), tmp_path / "captures", names)


# --- review API: one route for every walk -----------------------------------


@pytest.fixture
def walks(db: Any, tmp_path: Path) -> tuple[Any, list[int]]:
    """Five visits in time order, the first and third of two clips each,
    labeled Ada, -, -, not_cat, Ada. Returns the client and the event ids."""
    directory = tmp_path / "captures"
    for start, *offsets in ((0, 40), (500,), (1000, 40), (1500,), (2000,)):
        visit(directory, start, *offsets)
    db.ingest(directory)
    client = client_for(db, tmp_path, CATS)
    ids = walk(client, "")
    for index, value in ((0, "Ada"), (3, "not_cat"), (4, "Ada")):
        client.post(f"/api/review/{ids[index]}/label", json={"value": value})
    return client, ids


def walk(client: Any, walk_filter: str) -> list[int]:
    """Every event of a walk, each request continuing after the last event."""
    seen: list[int] = []
    while len(seen) <= 10:
        after = f"&after={seen[-1]}" if seen else ""
        response = client.get(f"/api/review/next?filter={walk_filter}{after}")
        event = response.get_json()["event"]
        if event is None:
            return seen
        seen.append(event["id"])
    raise AssertionError(f"the {walk_filter!r} walk does not end: {seen}")


@pytest.mark.parametrize(
    ("walk_filter", "positions"),
    [
        ("", [1, 2]),
        ("multi", [0, 2]),
        ("labeled", [0, 3, 4]),
        ("Ada", [0, 4]),
        ("not_cat", [3]),
        ("Bea", []),
    ],
)
def test_every_walk_is_one_route_in_time_order(
    walks: tuple[Any, list[int]], walk_filter: str, positions: list[int]
) -> None:
    client, ids = walks
    assert walk(client, walk_filter) == [ids[i] for i in positions]


def test_the_unlabeled_queue_starts_at_its_oldest_event(
    walks: tuple[Any, list[int]],
) -> None:
    client, ids = walks
    assert client.get("/api/review/next").get_json()["event"]["id"] == ids[1]
    client.post(f"/api/review/{ids[1]}/label", json={"value": "Bea"})
    assert client.get("/api/review/next").get_json()["event"]["id"] == ids[2]


def test_a_walk_continues_after_an_event_it_no_longer_holds(
    walks: tuple[Any, list[int]],
) -> None:
    client, ids = walks
    client.post(f"/api/review/{ids[0]}/label", json={"value": "Bea"})
    response = client.get(f"/api/review/next?filter=Ada&after={ids[0]}")
    assert response.get_json()["event"]["id"] == ids[4]


def test_the_per_walk_routes_are_gone(walks: tuple[Any, list[int]]) -> None:
    client, _ = walks
    for route in ("multi/next", "labeled/next"):
        assert client.get(f"/api/review/{route}").status_code == 404, route


# --- review API: staging ----------------------------------------------------


def stage(client: Any, event_id: int, **body: Any) -> Any:
    return client.post(f"/api/review/{event_id}/stage", json=body)


def test_a_staged_event_is_walked_shown_and_confirmed_with_its_label(
    walks: tuple[Any, list[int]],
) -> None:
    client, ids = walks
    staged = stage(client, ids[3], reason="judged from eight seconds", source="agent")
    assert staged.status_code == 200
    assert staged.get_json()["counts"]["staged"] == 1

    assert walk(client, "") == [ids[1], ids[2], ids[3]]
    shown = client.get(f"/api/review/event/{ids[3]}").get_json()["event"]
    assert shown["label"] == "not_cat"
    assert shown["staged"]["reason"] == "judged from eight seconds"
    assert shown["staged"]["source"] == "agent"

    client.post(f"/api/review/{ids[3]}/label", json={"value": "not_cat"})
    confirmed = client.get(f"/api/review/event/{ids[3]}").get_json()
    assert confirmed["event"]["label"] == "not_cat"
    assert confirmed["event"]["staged"] is None
    assert "staged" not in confirmed["counts"]
    assert walk(client, "") == [ids[1], ids[2]]


def test_staging_needs_a_reason_a_known_source_and_an_event(
    walks: tuple[Any, list[int]],
) -> None:
    client, ids = walks
    for body in ({}, {"reason": " "}, {"reason": 3}, {"reason": "x", "source": "cat"}):
        assert stage(client, ids[0], **body).status_code == 400, body
    for raw in (["recheck"], "recheck", 3):
        response = client.post(f"/api/review/{ids[0]}/stage", json=raw)
        assert response.status_code == 400, raw
    assert stage(client, 999, reason="x").status_code == 404
    assert walk(client, "") == [ids[1], ids[2]]


def test_a_peek_is_marked_shown_and_cleared_without_leaving_the_queue(
    walks: tuple[Any, list[int]],
) -> None:
    client, ids = walks
    marked = client.post(f"/api/review/{ids[1]}/peek", json={"peek": True})
    assert marked.status_code == 200
    assert marked.get_json()["event"]["peek"] is True
    assert walk(client, "") == [ids[1], ids[2]]
    rows = client.get("/api/timeline").get_json()["rows"]
    assert {row["id"] for row in rows if row["peek"]} == {ids[1]}

    client.post(f"/api/review/{ids[1]}/label", json={"value": "Ada"})
    shown = client.get(f"/api/review/event/{ids[1]}").get_json()["event"]
    assert (shown["label"], shown["peek"]) == ("Ada", True)

    cleared = client.post(f"/api/review/{ids[1]}/peek", json={"peek": False})
    assert cleared.get_json()["event"]["peek"] is False


def test_a_label_that_is_not_a_cat_clears_the_peek(
    walks: tuple[Any, list[int]],
) -> None:
    client, ids = walks
    for value, kept in (("Ada", True), ("multiple", True), ("not_cat", False)):
        client.post(f"/api/review/{ids[1]}/peek", json={"peek": True})
        client.post(f"/api/review/{ids[1]}/label", json={"value": value})
        shown = client.get(f"/api/review/event/{ids[1]}").get_json()["event"]
        assert shown["peek"] is kept, value


def test_a_peek_needs_true_or_false_and_an_event(
    walks: tuple[Any, list[int]],
) -> None:
    client, ids = walks
    for body in ({}, {"peek": "yes"}, {"peek": 1}, ["peek"]):
        response = client.post(f"/api/review/{ids[0]}/peek", json=body)
        assert response.status_code == 400, body
    assert client.post("/api/review/999/peek", json={"peek": True}).status_code == 404
    assert (
        client.get(f"/api/review/event/{ids[0]}").get_json()["event"]["peek"] is False
    )


def test_a_cat_may_not_be_called_staged(db: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CAT_NAMES"):
        Review(db, ClipFrames(tmp_path / "cache"), tmp_path / "captures", ("staged",))


# --- review API: findings from a code review -------------------------------


def test_media_urls_name_the_clip_so_a_split_cannot_change_what_they_serve(
    grouped_client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: media URLs named an event and an index, so after a split,
    join or regroup a cached URL could show another clip's tiles."""

    def strip_naming_its_clip(_: Any, clip_id: int, _path: Any) -> Path:
        made = tmp_path / f"strip{clip_id}.jpg"
        made.write_bytes(f"clip {clip_id}".encode())
        return made

    monkeypatch.setattr(ClipFrames, "strip", strip_naming_its_clip)
    event = grouped_client.get("/api/review/next").get_json()["event"]
    second = event["clips"][1]["id"]

    grouped_client.post(f"/api/review/{event['id']}/split", json={"index": 1})
    moved = grouped_client.get(f"/api/review/next?after={event['id']}")
    assert moved.get_json()["event"]["clips"][0]["id"] == second

    served = grouped_client.get(f"/review/clip/{second}/strip.jpg")
    assert served.data == f"clip {second}".encode()


def test_a_join_onto_a_clip_with_no_start_time_is_refused(
    db: Any, tmp_path: Path
) -> None:
    """Regression: the join was recorded, but grouping leaves undated clips
    alone, so the page showed a merge that never happened."""
    directory = tmp_path / "captures"
    visit(directory, 0)
    make_clip_file(directory / "oddly-named.h264")
    db.ingest(directory)
    client = client_for(db, tmp_path)
    dated = client.get("/api/review/next").get_json()["event"]["id"]

    refused = client.post(f"/api/review/{dated}/join")

    assert refused.status_code == 409
    assert "no start time" in refused.get_json()["error"]
    assert db.counts()["total"] == 2
    assert all(clip.boundary is None for clip in db.get(dated).clips)


class CountingAhead:
    """Stands in for StripsAhead, counting how often the queue changed."""

    def __init__(self) -> None:
        self.calls = 0

    def wanted(self) -> None:
        self.calls += 1


def test_every_change_to_the_queue_readies_the_next_strips(
    db: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "captures"
    visit(directory, 0, 40)
    visit(directory, 500)
    db.ingest(directory)
    clip_dir = directory
    ahead = CountingAhead()
    app = Flask(__name__)
    review = Review(
        db,
        ClipFrames(tmp_path / "cache"),
        clip_dir,
        (),
        ahead,  # type: ignore[arg-type]
    )
    app.register_blueprint(create_review_blueprint(review))
    client = app.test_client()
    event = client.get("/api/review/next").get_json()["event"]["id"]

    client.post(f"/api/review/{event}/label", json={"value": "cat"})
    stage(client, event, reason="check")
    client.post(f"/api/review/{event}/split", json={"index": 1})
    client.post(f"/api/review/{event}/join")
    client.post("/api/review/rescan")

    assert ahead.calls == 5
