// The Review tab: one event at a time from a walk, labeled with one key or tap.
import {CONFIG, escaped, get, onActions, post, timeTag} from './common.js';

const FILTERS = ['multi', 'labeled', ...CONFIG.labels];
const asked = new URLSearchParams(location.search).get('filter');
const FILTER = FILTERS.includes(asked) ? asked : null;  // null = the review queue
const MULTI = FILTER === 'multi';
const WALKING = FILTER !== null;
const BOXES = [1, 2, 3];
const SNACK_MS = 5000;

let current = null;
const undoable = [];
let busy = false;  // a label request is in flight; the bar and the keys wait
let snackTimer = null;
let strips = [];  // the shown event's strip images; empty once canceled

// --- the address ------------------------------------------------------------

// The review address is /review/<event id>?filter=<walk>, so a reload, Back or
// a pasted link lands on the same event in the same walk.
function eventFromPath() {
  const match = location.pathname.match(/^\/review\/(\d+)$/);
  return match ? Number(match[1]) : null;
}

function reviewUrl(id) {
  const query = FILTER ? `?filter=${encodeURIComponent(FILTER)}` : '';
  return (id ? `/review/${id}` : '/review') + query;
}

// Where the Review tab returns to.
export function reviewAddress() { return reviewUrl(current?.id); }

// A move to another event is a new history entry; the first event shown
// replaces the bare /review it was reached by.
function followInAddress(id) {
  const shown = eventFromPath();
  if (shown === id) return;
  const url = reviewUrl(id);
  if (shown === null) history.replaceState(null, '', url);
  else history.pushState(null, '', url);
}

// --- showing an event -------------------------------------------------------

function hms(iso) { return iso ? iso.slice(11, 19) : '?'; }
function clipUrl(clip, file) { return `/review/clip/${clip.id}/${file}`; }
function secs(a, b) { return Math.round((new Date(b) - new Date(a)) / 1000); }

function clipBlock(c, prev) {
  const length = c.started_at && c.ended_at
    ? `${secs(c.started_at, c.ended_at)} s` : '?';
  const gap = prev && c.started_at && prev.ended_at
    ? `, ${secs(prev.ended_at, c.started_at)} s after the previous clip` : '';
  const override = c.boundary
    ? ` <span class="warn">reviewer: ${c.boundary}</span>` : '';
  const boundary = c.index > 0
    ? `<div class="boundary"><button data-action="split" data-index="${c.index}">` +
      `a new visit starts here</button></div>`
    : '';
  return `${boundary}<div class="clip">
    <div class="clipmeta">clip ${c.index + 1}:
      ${hms(c.started_at)} &rarr; ${hms(c.ended_at)}
      (${length}, closed: ${c.close_reason}${gap})${override}
      <button data-action="watch" data-index="${c.index}">watch</button></div>
    <div class="tiles" id="t${c.index}"></div>
    <video id="v${c.index}" controls preload="none" hidden></video>
  </div>`;
}

function show(data) {
  current = data.event;
  document.getElementById('viewer').hidden = !current;
  document.getElementById('done').hidden = !!current;
  document.getElementById('bar').hidden = !current;
  renderCounts(data.counts);
  showCleanForm(false);
  cancelStrips();
  followInAddress(current ? current.id : null);
  if (!current) {
    showDone(data.counts);
    return;
  }
  showMeta();
  document.getElementById('clips').innerHTML =
    current.clips.map((c, i) => clipBlock(c, i ? current.clips[i - 1] : null)).join('');
  restoreTiles();
  padForBar();
}

// The line above the clips, and the sheet's buttons that depend on the event.
function showMeta() {
  const poops = Object.entries(current.poops || {});
  const found = poops.length
    ? ` <span class="label">poops ${poops.map(([b, n]) => `box${b}: ${n}`).join(', ')}</span>`
    : '';
  const badge = current.label
    ? ` <span class="label">${escaped(current.label)}</span>${found}` : '';
  const peek = current.peek ? ' <span class="label">peek, not a visit</span>' : '';
  const staged = current.staged
    ? ` <span class="warn">back for review (${escaped(current.staged.source)}):` +
      ` ${escaped(current.staged.reason)}</span>` : '';
  const n = current.clips.length;
  const grouped = n > 1
    ? ` <span class="warn">${n} clips grouped as one visit</span>` : '';
  document.getElementById('meta').innerHTML =
    `#${current.id} &mdash; ${timeTag(current.started_at)} &rarr; ` +
    `${hms(current.ended_at)}${badge}${peek}${staged}${grouped}`;
  document.getElementById('restage').hidden = !current.label;
  document.getElementById('peektext').textContent = current.peek
    ? 'a visit after all, not a peek' : 'a peek: looked in, used no box';
}

// An empty walk says which walk it is and how many events wait in the queue.
function walkName() {
  if (MULTI) return 'multi-clip events';
  if (FILTER === 'labeled') return 'labeled events';
  return `events labeled ${FILTER.replace('_', ' ')}`;
}

function showDone(counts) {
  const waiting = counts.total - counts.labeled + (counts.staged || 0);
  document.getElementById('donetext').textContent =
    WALKING ? `No ${walkName()} left in this walk.` : 'Nothing left to review.';
  const toQueue = document.getElementById('toqueue');
  toQueue.textContent = `go to the review queue (${waiting} waiting)`;
  toQueue.hidden = !WALKING || waiting === 0;
}

// One frame per tile, cut from the clip's strip image so the tiles wrap to the
// page instead of running off it. The strip's width says how many frames it holds.
function loadTiles(eventId, clip) {
  const grid = document.getElementById(`t${clip.index}`);
  const strip = new Image();
  strip.onload = () => {
    if (current?.id !== eventId) return;
    const slots = Math.max(1, Math.round(strip.naturalWidth / CONFIG.thumbWidth));
    grid.innerHTML = '';
    for (let slot = 0; slot < slots; slot++) {
      const tile = document.createElement('div');
      tile.className = 'tile';
      tile.title = 'click for the full frame';
      tile.style.backgroundImage = `url(${strip.src})`;
      tile.style.backgroundPosition = `${slots > 1 ? slot / (slots - 1) * 100 : 0}% 0`;
      tile.onclick = () => window.open(clipUrl(clip, `frame/${slot}.jpg`));
      grid.appendChild(tile);
    }
  };
  strip.onerror = () => {
    if (current?.id !== eventId) return;
    grid.innerHTML = '<span class="note warn">frames unavailable (clip unreadable?)</span>';
  };
  strip.src = clipUrl(clip, 'strip.jpg');
  return strip;
}

// Everything that leaves an event cancels its pending strips first, and
// restores them if it ends up staying.
function cancelStrips() {
  for (const strip of strips) {
    strip.onload = strip.onerror = null;
    strip.removeAttribute('src');
  }
  strips = [];
}

function restoreTiles() {
  if (current && !strips.length && eventFromPath() === current.id) {
    strips = current.clips.map(c => loadTiles(current.id, c));
  }
}

function renderCounts(c) {
  const link = (filter, text) =>
    `<a href="/review?filter=${encodeURIComponent(filter)}">${escaped(text)}</a>`;
  const parts = Object.entries(c).filter(([k]) => !['total', 'labeled'].includes(k))
    .map(([k, v]) => k === 'multi_clip' ? link('multi', `${k}: ${v}`)
                   : CONFIG.labels.includes(k) ? link(k, `${k}: ${v}`)
                   : escaped(`${k}: ${v}`))
    .join(', ');
  document.getElementById('counts').innerHTML =
    `${link('labeled', `${c.labeled} of ${c.total} labeled`)}` +
    (parts ? ` (${parts})` : '') +
    (FILTER ? ' &middot; <a href="/review">back to the review queue</a>' : '');
}

// --- the bar, the sheet and the snackbar ------------------------------------

function padForBar() {
  const bar = document.getElementById('bar');
  document.body.style.paddingBottom = bar.hidden ? '' : `${bar.offsetHeight + 16}px`;
  document.getElementById('snack').style.bottom = `${bar.offsetHeight + 12}px`;
}

function setBusy(on) {
  busy = on;
  document.getElementById('bar').classList.toggle('busy', on);
}

function showCleanForm(open) {
  document.getElementById('clean').hidden = !open;
  document.getElementById('save').hidden = !open;
  document.getElementById('cleaning').hidden = open;
  padForBar();
}

function openSheet() { document.getElementById('sheet').hidden = false; }
function closeSheet() { document.getElementById('sheet').hidden = true; }

function snack(text) {
  const box = document.getElementById('snack');
  document.getElementById('snacktext').textContent = text;
  box.classList.remove('gone');
  clearTimeout(snackTimer);
  snackTimer = setTimeout(() => box.classList.add('gone'), SNACK_MS);
}

// --- actions ----------------------------------------------------------------

// The event the address names, or the walk's next if it names none or one
// that is gone (a regroup renumbers events).
export async function showReview() {
  const id = eventFromPath();
  if (id === null) {
    if (!current) await load();
    return;
  }
  if (current?.id === id) {
    restoreTiles();
    return;
  }
  cancelStrips();
  const data = await get(`/api/review/event/${id}`);
  if (eventFromPath() !== id) return;  // the address moved on while this loaded
  if (data.event) {
    show(data);
  } else {
    snack(`#${id} no longer exists`);
    history.replaceState(null, '', reviewUrl(null));
    await load();
  }
}

// Leaving the tab cancels the strips; showReview() brings them back.
export function hideReview() { cancelStrips(); }

async function load() {
  closeSheet();
  cancelStrips();
  const params = new URLSearchParams();
  if (FILTER) params.set('filter', FILTER);
  if (WALKING && current) params.set('after', current.id);
  const from = location.pathname;
  const data = await get(`/api/review/next?${params}`);
  if (location.pathname === from) show(data);  // else Back or Forward moved on
}

// Records the label, says so, and moves on; nothing else is accepted meanwhile.
async function label(value) {
  if (!current || busy) return;
  closeSheet();
  cancelStrips();
  setBusy(true);
  const id = current.id;
  try {
    await post(`/api/review/${id}/label`, {value});
    undoable.push(id);
    snack(`#${id}: ${value.replace('_', ' ')}`);
    if (eventFromPath() === id) await load();  // else Back or Forward moved on
  } finally {
    setBusy(false);
    restoreTiles();
  }
}

async function startClean() {
  if (!current || busy) return;
  const poops = current.poops || {};
  await post(`/api/review/${current.id}/label`, {value: 'clean'});
  showCleanForm(true);
  for (const box of BOXES) {
    document.getElementById(`box${box}`).value = poops[box] ?? 0;
  }
  document.getElementById('box1').focus();
  document.getElementById('box1').select();
}

async function saveCounts() {
  if (!current || busy) return;
  cancelStrips();
  setBusy(true);
  const id = current.id;
  try {
    const counts = {};
    for (const box of BOXES) {
      counts[box] = Number(document.getElementById(`box${box}`).value) || 0;
    }
    await post(`/api/review/${id}/counts`, {counts});
    undoable.push(id);
    snack(`#${id}: clean, ${Object.values(counts).join('/')}`);
    if (eventFromPath() === id) await load();
  } finally {
    setBusy(false);
    restoreTiles();
  }
}

async function undo() {
  const id = undoable.pop();
  if (id === undefined) return;
  document.getElementById('snack').classList.add('gone');
  cancelStrips();
  show(await get(`/api/review/event/${id}`));
}

async function split(index) {
  if (!current) return;
  show(await post(`/api/review/${current.id}/split`, {index}));
}

async function join() {
  if (!current) return;
  closeSheet();
  const data = await post(`/api/review/${current.id}/join`);
  if (data.event) show(data);
  else alert(data.error || 'There is no later event to join.');
}

// Puts a labeled event back in the queue with a reason, keeping its label.
async function restage() {
  if (!current) return;
  closeSheet();
  const reason = prompt(`Why should #${current.id} come back for review?`);
  if (!reason || !reason.trim()) return;
  const data = await post(`/api/review/${current.id}/stage`, {reason});
  if (data.event) {
    cancelStrips();
    show(data);
    snack(`#${current.id}: back for review`);
  } else {
    alert(data.error || 'Could not stage this event.');
  }
}

// A peek keeps its label but is not a visit. The page stays on the event, its
// tiles and any video untouched, while the reviewer picks the cat.
async function togglePeek() {
  if (!current || busy) return;
  closeSheet();
  setBusy(true);
  const id = current.id;
  try {
    const data = await post(`/api/review/${id}/peek`, {peek: !current.peek});
    if (!data.event) {
      alert(data.error || 'Could not mark this event.');
      return;
    }
    if (current?.id !== id) return;  // the page moved on meanwhile
    current = data.event;
    showMeta();
    snack(`#${id}: ${current.peek ? 'a peek, not a visit' : 'a visit'}`);
  } finally {
    setBusy(false);
  }
}

function watch(index) {
  const video = document.getElementById(`v${index}`);
  if (!video.src) video.src = clipUrl(current.clips[index], 'video.mp4');
  video.hidden = false;
  video.play();
}

async function rescan() { closeSheet(); await post('/api/review/rescan'); load(); }

function catButton(text, value, key) {
  const button = document.createElement('button');
  button.dataset.action = 'label';
  button.dataset.value = value;
  button.textContent = `${text} `;
  const kbd = document.createElement('kbd');
  kbd.textContent = key;
  button.appendChild(kbd);
  return button;
}

function onKey(e) {
  if (document.getElementById('review').hidden || e.target.tagName === 'VIDEO') return;
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;  // shortcuts are bare keys
  if (e.key === 'Escape') { closeSheet(); return; }
  if (e.target.tagName === 'INPUT') {
    if (e.key === 'Enter') saveCounts();
    return;  // digits belong in the field, not to the shortcuts
  }
  const digit = Number(e.key);
  if (e.key === 'c') label('cat');
  else if (digit >= 1 && digit <= CONFIG.cats.length) label(CONFIG.cats[digit - 1]);
  else if (e.key === 'm' && CONFIG.cats.length) label('multiple');
  else if (e.key === 'l') startClean();
  else if (e.key === 'n') label('not_cat');
  else if (e.key === 'p') togglePeek();
  else if (e.key === 'u') label('unsure');
  else if (e.key === 'z') undo();
  else if (e.key === 'ArrowRight' && WALKING) load();
}

export function initReview() {
  const cats = document.getElementById('cats');
  CONFIG.cats.forEach((name, i) => cats.appendChild(catButton(name, name, String(i + 1))));
  if (CONFIG.cats.length) {
    cats.appendChild(catButton('multiple', 'multiple', 'm'));
  } else {
    cats.appendChild(catButton('cat', 'cat', 'c'));  // no names yet: cat is the primary label
    document.getElementById('plaincat').hidden = true;
  }
  onActions(document.getElementById('review'), {
    label: (el) => label(el.dataset.value),
    split: (el) => split(Number(el.dataset.index)),
    watch: (el) => watch(Number(el.dataset.index)),
    clean: startClean,
    save: saveCounts,
    undo,
    join,
    restage,
    peek: togglePeek,
    next: load,
    rescan,
    more: openSheet,
    close: closeSheet,
    backdrop: (el, e) => { if (e.target === el) closeSheet(); },
  });
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', padForBar);
  window.addEventListener('beforeunload', cancelStrips);
  window.addEventListener('pageshow', restoreTiles);
  if (WALKING) {
    document.getElementById('mode').textContent =
      MULTI ? '(multi-clip)' : `(${FILTER.replace('_', ' ')})`;
    document.getElementById('skip').hidden = false;
  }
}

