"""Label review UI: is there a cat in this capture?"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from config import Config
from src.capture_db import CaptureDB, Event
from src.clip_frames import THUMB_COUNT, ClipFrames

log = logging.getLogger(__name__)

VALID_LABELS = ("cat", "not_cat", "unsure")

_PAGE = """<!DOCTYPE html>
<html><head><title>Capture review</title><style>
body { font-family: system-ui, sans-serif; margin: 24px;
       background: #141414; color: #eee; }
h1 { font-size: 1.2rem; }
#strip { width: 100%; max-width: 1280px; cursor: zoom-in; border-radius: 4px; }
#meta { color: #aaa; margin: 8px 0; }
#counts { color: #aaa; margin-top: 16px; }
.label { display: inline-block; padding: 2px 10px;
         border-radius: 10px; background: #333; }
button { font-size: 1rem; margin-right: 8px; padding: 6px 14px; cursor: pointer; }
kbd { background: #333; border-radius: 3px; padding: 1px 5px; }
#done { display: none; }
</style></head><body>
<h1>Capture review</h1>
<div id="viewer">
  <div id="meta"></div>
  <img id="strip" alt="frame strip unavailable (clip unreadable?)">
  <p>
    <button onclick="label('cat')">cat <kbd>c</kbd></button>
    <button onclick="label('not_cat')">not a cat <kbd>n</kbd></button>
    <button onclick="label('unsure')">unsure <kbd>u</kbd></button>
    <button onclick="undo()">undo <kbd>z</kbd></button>
  </p>
</div>
<div id="done"><p>Nothing left to review.</p>
  <button onclick="rescan()">rescan clip directory</button></div>
<div id="counts"></div>
<script>
let current = null;
const history = [];

async function show(data) {
  current = data.event;
  document.getElementById('viewer').style.display = current ? 'block' : 'none';
  document.getElementById('done').style.display = current ? 'none' : 'block';
  renderCounts(data.counts);
  if (!current) return;
  const badge = current.label ? ` <span class="label">${current.label}</span>` : '';
  document.getElementById('meta').innerHTML =
    `#${current.id} — ${current.started_at || 'unknown time'}${badge}`;
  document.getElementById('strip').src = `/review/${current.id}/strip.jpg`;
}

function renderCounts(c) {
  const parts = Object.entries(c).filter(([k]) => !['total','labeled'].includes(k))
    .map(([k, v]) => `${k}: ${v}`).join(', ');
  document.getElementById('counts').textContent =
    `${c.labeled} of ${c.total} labeled` + (parts ? ` (${parts})` : '');
}

async function load() { show(await (await fetch('/api/review/next')).json()); }

async function label(value) {
  if (!current) return;
  await fetch(`/api/review/${current.id}/label`,
    {method: 'POST', headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({value})});
  history.push(current.id);
  load();
}

async function undo() {
  const id = history.pop();
  if (id === undefined) return;
  show(await (await fetch(`/api/review/event/${id}`)).json());
}

async function rescan() {
  await fetch('/api/review/rescan', {method: 'POST'});
  load();
}

document.getElementById('strip').onclick = (e) => {
  const slot = Math.floor(e.offsetX / e.target.clientWidth * %THUMBS%);
  if (current) window.open(`/review/${current.id}/frame/${slot}.jpg`);
};
document.addEventListener('keydown', (e) => {
  if (e.key === 'c') label('cat');
  else if (e.key === 'n') label('not_cat');
  else if (e.key === 'u') label('unsure');
  else if (e.key === 'z') undo();
});
load();
</script></body></html>"""


class ReviewPages:
    """Route handlers bound to one database and one frame extractor."""

    def __init__(self, db: CaptureDB, frames: ClipFrames) -> None:
        self.db = db
        self.frames = frames

    def page(self) -> str:
        return _PAGE.replace("%THUMBS%", str(THUMB_COUNT))

    def next_event(self) -> Any:
        return self._event_payload(self.db.next_unlabeled())

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

    def rescan(self) -> Any:
        added = self.db.ingest(Path(Config.NETWORK_SHARE_DIR) / "captures")
        return jsonify({"added": added, "counts": self.db.counts()})

    def strip(self, event_id: int) -> Response:
        return self._image(event_id, lambda e: self.frames.strip(e.id, e.clip_path))

    def frame(self, event_id: int, slot: int) -> Response:
        return self._image(
            event_id, lambda e: self.frames.frame(e.id, e.clip_path, slot)
        )

    def _image(
        self, event_id: int, produce: Callable[[Event], Path | None]
    ) -> Response:
        event = self.db.get(event_id)
        path = produce(event) if event is not None else None
        if path is None:
            return Response("unavailable", status=404)
        return send_file(path, mimetype="image/jpeg")

    def _event_payload(self, event: Event | None) -> Any:
        body = None
        if event is not None:
            body = {
                "id": event.id,
                "started_at": event.started_at,
                "label": event.label,
            }
        return jsonify({"event": body, "counts": self.db.counts()})


def create_review_blueprint(db: CaptureDB, frames: ClipFrames) -> Blueprint:
    pages = ReviewPages(db, frames)
    bp = Blueprint("review", __name__)
    bp.add_url_rule("/review", view_func=pages.page)
    bp.add_url_rule("/api/review/next", view_func=pages.next_event)
    bp.add_url_rule("/api/review/event/<int:event_id>", view_func=pages.one_event)
    bp.add_url_rule(
        "/api/review/<int:event_id>/label",
        view_func=pages.set_label,
        methods=["POST"],
    )
    bp.add_url_rule("/api/review/rescan", view_func=pages.rescan, methods=["POST"])
    bp.add_url_rule("/review/<int:event_id>/strip.jpg", view_func=pages.strip)
    bp.add_url_rule(
        "/review/<int:event_id>/frame/<int:slot>.jpg", view_func=pages.frame
    )
    return bp
