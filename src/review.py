"""Label review UI: is there a cat in this visit, and is the grouping right?"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from config import Config
from src.capture_db import CaptureDB, Clip, Event
from src.clip_frames import THUMB_WIDTH, ClipFrames

log = logging.getLogger(__name__)

VALID_LABELS = ("cat", "not_cat", "unsure")

_PAGE = """<!DOCTYPE html>
<html><head><title>Capture review</title><style>
body { font-family: system-ui, sans-serif; margin: 24px;
       background: #141414; color: #eee; }
h1 { font-size: 1.2rem; }
#meta, #counts, .clipmeta { color: #aaa; }
#meta { margin: 8px 0; }
#counts { margin-top: 16px; }
.clip { margin: 12px 0; padding: 10px; background: #1e1e1e; border-radius: 6px; }
.clipmeta { margin-bottom: 6px; }
.strip { width: 100%; max-width: 1280px; cursor: zoom-in; border-radius: 4px;
         display: block; }
video { width: 100%; max-width: 1280px; margin-top: 8px; display: none; }
.boundary { margin: 4px 0 4px 12px; }
.boundary button, .clipmeta button { font-size: 0.85rem; padding: 3px 10px; }
.label { display: inline-block; padding: 2px 10px;
         border-radius: 10px; background: #333; }
.warn { color: #f0b429; }
button { font-size: 1rem; margin-right: 8px; padding: 6px 14px; cursor: pointer; }
kbd { background: #333; border-radius: 3px; padding: 1px 5px; }
#done, #skip { display: none; }
</style></head><body>
<h1>Capture review <span id="mode"></span></h1>
<div id="viewer">
  <div id="meta"></div>
  <div id="clips"></div>
  <p>
    <button onclick="label('cat')">cat <kbd>c</kbd></button>
    <button onclick="label('not_cat')">not a cat <kbd>n</kbd></button>
    <button onclick="label('unsure')">unsure <kbd>u</kbd></button>
    <button onclick="undo()">undo <kbd>z</kbd></button>
    <button id="skip" onclick="load()">next <kbd>&rarr;</kbd></button>
    <button onclick="join()">same visit as the next event</button>
  </p>
</div>
<div id="done"><p>Nothing left to review.</p>
  <button onclick="rescan()">rescan clip directory</button></div>
<div id="counts"></div>
<script>
const MULTI = new URLSearchParams(location.search).get('filter') === 'multi';
const THUMB_WIDTH = %THUMB_WIDTH%;
let current = null;
const history = [];

function hms(iso) { return iso ? iso.slice(11, 19) : '?'; }
function secs(a, b) { return Math.round((new Date(b) - new Date(a)) / 1000); }

function clipBlock(c, prev) {
  const length = c.started_at && c.ended_at
    ? `${secs(c.started_at, c.ended_at)} s` : '?';
  const gap = prev && c.started_at && prev.ended_at
    ? `, ${secs(prev.ended_at, c.started_at)} s after the previous clip` : '';
  const override = c.boundary
    ? ` <span class="warn">reviewer: ${c.boundary}</span>` : '';
  const boundary = c.index > 0
    ? `<div class="boundary"><button onclick="split(${c.index})">` +
      `a new visit starts here</button></div>`
    : '';
  return `${boundary}<div class="clip">
    <div class="clipmeta">clip ${c.index + 1}:
      ${hms(c.started_at)} &rarr; ${hms(c.ended_at)}
      (${length}, closed: ${c.close_reason}${gap})${override}
      <button onclick="watch(${c.index})">watch</button></div>
    <img class="strip" data-index="${c.index}"
         src="/review/${current.id}/clip/${c.index}/strip.jpg"
         alt="frame strip unavailable (clip unreadable?)">
    <video id="v${c.index}" controls preload="none"></video>
  </div>`;
}

function show(data) {
  current = data.event;
  document.getElementById('viewer').style.display = current ? 'block' : 'none';
  document.getElementById('done').style.display = current ? 'none' : 'block';
  renderCounts(data.counts);
  if (!current) return;
  const badge = current.label ? ` <span class="label">${current.label}</span>` : '';
  const n = current.clips.length;
  const grouped = n > 1
    ? ` <span class="warn">${n} clips grouped as one visit</span>` : '';
  document.getElementById('meta').innerHTML =
    `#${current.id} &mdash; ${current.started_at || 'unknown time'} &rarr; ` +
    `${hms(current.ended_at)}${badge}${grouped}`;
  document.getElementById('clips').innerHTML =
    current.clips.map((c, i) => clipBlock(c, i ? current.clips[i - 1] : null)).join('');
}

function renderCounts(c) {
  const parts = Object.entries(c).filter(([k]) => !['total','labeled'].includes(k))
    .map(([k, v]) => `${k}: ${v}`).join(', ');
  document.getElementById('counts').textContent =
    `${c.labeled} of ${c.total} labeled` + (parts ? ` (${parts})` : '');
}

async function get(url) { return (await fetch(url)).json(); }
async function post(url, body) {
  return (await fetch(url, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})})).json();
}

async function load() {
  const after = current ? `?after=${current.id}` : '';
  if (MULTI) show(await get(`/api/review/multi/next${after}`));
  else show(await get('/api/review/next'));
}

async function label(value) {
  if (!current) return;
  await post(`/api/review/${current.id}/label`, {value});
  history.push(current.id);
  load();
}

async function undo() {
  const id = history.pop();
  if (id === undefined) return;
  show(await get(`/api/review/event/${id}`));
}

async function split(index) {
  if (!current) return;
  show(await post(`/api/review/${current.id}/split`, {index}));
}

async function join() {
  if (!current) return;
  const data = await post(`/api/review/${current.id}/join`);
  if (data.event) show(data); else alert('There is no later event to join.');
}

function watch(index) {
  const video = document.getElementById(`v${index}`);
  if (!video.src) video.src = `/review/${current.id}/clip/${index}/video.mp4`;
  video.style.display = 'block';
  video.play();
}

async function rescan() { await post('/api/review/rescan'); load(); }

document.getElementById('clips').addEventListener('click', (e) => {
  if (!e.target.classList.contains('strip')) return;
  const img = e.target;
  const slots = Math.max(1, Math.round(img.naturalWidth / THUMB_WIDTH));
  const slot = Math.min(slots - 1, Math.floor(e.offsetX / img.clientWidth * slots));
  window.open(`/review/${current.id}/clip/${img.dataset.index}/frame/${slot}.jpg`);
});
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'VIDEO') return;
  if (e.key === 'c') label('cat');
  else if (e.key === 'n') label('not_cat');
  else if (e.key === 'u') label('unsure');
  else if (e.key === 'z') undo();
  else if (e.key === 'ArrowRight' && MULTI) load();
});
if (MULTI) {
  document.getElementById('mode').textContent = '— multi-clip events';
  document.getElementById('skip').style.display = 'inline-block';
}
load();
</script></body></html>"""


class ReviewPages:
    """Route handlers bound to one database and one frame extractor."""

    def __init__(self, db: CaptureDB, frames: ClipFrames) -> None:
        self.db = db
        self.frames = frames

    def page(self) -> str:
        """The review page, with the strip's thumbnail width baked in."""
        return _PAGE.replace("%THUMB_WIDTH%", str(THUMB_WIDTH))

    def next_event(self) -> Any:
        return self._event_payload(self.db.next_unlabeled())

    def next_multi(self) -> Any:
        after = request.args.get("after", type=int)
        return self._event_payload(self.db.next_multi(after))

    def one_event(self, event_id: int) -> Any:
        event = self.db.get(event_id)
        if event is None:
            return jsonify({"error": "no such event"}), 404
        return self._event_payload(event)

    def set_label(self, event_id: int) -> Any:
        value = (request.get_json(silent=True) or {}).get("value")
        if value not in VALID_LABELS:
            return jsonify({"error": f"value must be one of {VALID_LABELS}"}), 400
        if self.db.get(event_id) is None:
            return jsonify({"error": "no such event"}), 404
        self.db.set_label(event_id, value)
        return jsonify({"ok": True, "counts": self.db.counts()})

    def split(self, event_id: int) -> Any:
        """A new visit starts at the index-th clip; the rest stay in this event."""
        event = self.db.get(event_id)
        if event is None:
            return jsonify({"error": "no such event"}), 404
        index = (request.get_json(silent=True) or {}).get("index")
        if not isinstance(index, int) or not 0 < index < len(event.clips):
            return jsonify({"error": "index must name a clip after the first"}), 400
        self.db.split(event.clips[index].id)
        return self._event_payload(self.db.get(event_id))

    def join(self, event_id: int) -> Any:
        if self.db.get(event_id) is None:
            return jsonify({"error": "no such event"}), 404
        return self._event_payload(self.db.join(event_id))

    def rescan(self) -> Any:
        added = self.db.ingest(Path(Config.NETWORK_SHARE_DIR) / "captures")
        return jsonify({"added": added, "counts": self.db.counts()})

    def strip(self, event_id: int, index: int) -> Response:
        return self._media(
            event_id, index, "image/jpeg", lambda c: self.frames.strip(c.id, c.path)
        )

    def frame(self, event_id: int, index: int, slot: int) -> Response:
        return self._media(
            event_id,
            index,
            "image/jpeg",
            lambda c: self.frames.frame(c.id, c.path, slot),
        )

    def video(self, event_id: int, index: int) -> Response:
        return self._media(
            event_id, index, "video/mp4", lambda c: self.frames.video(c.id, c.path)
        )

    def _media(
        self,
        event_id: int,
        index: int,
        mimetype: str,
        produce: Callable[[Clip], Path | None],
    ) -> Response:
        """A file for the index-th clip of an event, or 404.

        send_file honours Range requests, which is what lets the player seek.
        """
        event = self.db.get(event_id)
        clip = event.clips[index] if event and 0 <= index < len(event.clips) else None
        path = produce(clip) if clip is not None else None
        if path is None:
            return Response("unavailable", status=404)
        return send_file(path, mimetype=mimetype)

    def _event_payload(self, event: Event | None) -> Any:
        body = None
        if event is not None:
            body = {
                "id": event.id,
                "started_at": event.started_at,
                "ended_at": event.ended_at,
                "label": event.label,
                "clips": [
                    {
                        "index": index,
                        "started_at": clip.started_at,
                        "ended_at": clip.ended_at,
                        "close_reason": clip.close_reason,
                        "boundary": clip.boundary,
                    }
                    for index, clip in enumerate(event.clips)
                ],
            }
        return jsonify({"event": body, "counts": self.db.counts()})


def create_review_blueprint(db: CaptureDB, frames: ClipFrames) -> Blueprint:
    pages = ReviewPages(db, frames)
    bp = Blueprint("review", __name__)
    bp.add_url_rule("/review", view_func=pages.page)
    bp.add_url_rule("/api/review/next", view_func=pages.next_event)
    bp.add_url_rule("/api/review/multi/next", view_func=pages.next_multi)
    bp.add_url_rule("/api/review/event/<int:event_id>", view_func=pages.one_event)
    for action, view in (
        ("label", pages.set_label),
        ("split", pages.split),
        ("join", pages.join),
    ):
        bp.add_url_rule(
            f"/api/review/<int:event_id>/{action}", view_func=view, methods=["POST"]
        )
    bp.add_url_rule("/api/review/rescan", view_func=pages.rescan, methods=["POST"])
    media = "/review/<int:event_id>/clip/<int:index>"
    bp.add_url_rule(f"{media}/strip.jpg", view_func=pages.strip)
    bp.add_url_rule(f"{media}/frame/<int:slot>.jpg", view_func=pages.frame)
    bp.add_url_rule(f"{media}/video.mp4", view_func=pages.video)
    return bp
