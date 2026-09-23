// The review and live tabs. The constants it reads (REVIEW, LABELS, CATS,
// THUMB_WIDTH) are filled by the template before this file loads.
const FILTERS = ['multi', 'labeled', ...LABELS];
const asked = new URLSearchParams(location.search).get('filter');
const FILTER = FILTERS.includes(asked) ? asked : null;  // null = the unlabeled queue
const MULTI = FILTER === 'multi';
const WALKING = FILTER !== null;
let current = null;
const history_ = [];
let statusTimer = null;
let busy = false;  // a label request is in flight; the bar and the keys wait
let snackTimer = null;

// --- tabs -------------------------------------------------------------------

function tabFromPath() { return location.pathname === '/review' ? 'review' : 'live'; }

function activate(tab, push) {
  for (const name of ['live', 'review']) {
    document.getElementById(name).hidden = name !== tab;
    document.querySelector(`nav a[data-tab=${name}]`)
      .classList.toggle('active', name === tab);
  }
  const feed = document.getElementById('feed');
  if (tab === 'live') {
    feed.src = '/video_feed';
    pollStatus();
    statusTimer = setInterval(pollStatus, 2000);
  } else {
    feed.removeAttribute('src');  // closes the MJPEG connection
    clearInterval(statusTimer);
    statusTimer = null;
    if (REVIEW && !current) load();
  }
  if (push) {
    const url = tab === 'live' ? '/' : '/review' + (FILTER ? `?filter=${FILTER}` : '');
    history.pushState(null, '', url);
  }
}

document.querySelectorAll('nav a').forEach(a => a.addEventListener('click', e => {
  e.preventDefault();
  activate(a.dataset.tab, true);
}));
window.addEventListener('popstate', () => activate(tabFromPath(), false));

// --- live -------------------------------------------------------------------

async function pollStatus() {
  const s = await get('/api/status');
  const box = document.getElementById('status');
  const feed = document.getElementById('feed');
  feed.hidden = !s.detector;
  if (!s.detector) {
    box.textContent = 'The detector is not running; this is the review-only server.';
  } else if (!s.recorder) {
    box.textContent = 'Camera up; the recorder is unavailable, so this is stream only.';
  } else {
    const state = s.recording ? `RECORDING ${s.clip || ''}` : 'watching';
    box.textContent =
      `${state} · threshold ${s.motion_threshold} px · timeout ${s.motion_timeout} s`;
  }
}

// --- review -----------------------------------------------------------------

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
  if (!current) return;
  const poops = Object.entries(current.poops || {});
  const found = poops.length
    ? ` <span class="label">poops ${poops.map(([b, n]) => `box${b}: ${n}`).join(', ')}</span>`
    : '';
  const badge = current.label
    ? ` <span class="label">${current.label}</span>${found}` : '';
  const n = current.clips.length;
  const grouped = n > 1
    ? ` <span class="warn">${n} clips grouped as one visit</span>` : '';
  document.getElementById('meta').innerHTML =
    `#${current.id} &mdash; ${current.started_at || 'unknown time'} &rarr; ` +
    `${hms(current.ended_at)}${badge}${grouped}`;
  document.getElementById('clips').innerHTML =
    current.clips.map((c, i) => clipBlock(c, i ? current.clips[i - 1] : null)).join('');
  current.clips.forEach(c => loadTiles(current.id, c.index));
  padForBar();
}

// One frame per tile, cut from the clip's strip image so the tiles wrap to the
// page instead of running off it. The strip's width says how many frames it holds.
function loadTiles(eventId, index) {
  const grid = document.getElementById(`t${index}`);
  const strip = new Image();
  strip.onload = () => {
    if (current?.id !== eventId) return;
    const slots = Math.max(1, Math.round(strip.naturalWidth / THUMB_WIDTH));
    grid.innerHTML = '';
    for (let slot = 0; slot < slots; slot++) {
      const tile = document.createElement('div');
      tile.className = 'tile';
      tile.title = 'click for the full frame';
      tile.style.backgroundImage = `url(${strip.src})`;
      tile.style.backgroundPosition = `${slots > 1 ? slot / (slots - 1) * 100 : 0}% 0`;
      tile.onclick = () => window.open(`/review/${eventId}/clip/${index}/frame/${slot}.jpg`);
      grid.appendChild(tile);
    }
  };
  strip.onerror = () => {
    grid.innerHTML = '<span class="note warn">frames unavailable (clip unreadable?)</span>';
  };
  strip.src = `/review/${eventId}/clip/${index}/strip.jpg`;
}

function renderCounts(c) {
  const link = (filter, text) => `<a href="/review?filter=${filter}">${text}</a>`;
  const parts = Object.entries(c).filter(([k]) => !['total','labeled'].includes(k))
    .map(([k, v]) => k === 'multi_clip' ? link('multi', `${k}: ${v}`)
                   : LABELS.includes(k) ? link(k, `${k}: ${v}`) : `${k}: ${v}`)
    .join(', ');
  document.getElementById('counts').innerHTML =
    `${link('labeled', `${c.labeled} of ${c.total} labeled`)}` +
    (parts ? ` (${parts})` : '') +
    (FILTER ? ` &middot; <a href="/review">back to the unlabeled queue</a>` : '');
}

async function get(url) { return (await fetch(url)).json(); }
async function post(url, body) {
  return (await fetch(url, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})})).json();
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
  snackTimer = setTimeout(() => box.classList.add('gone'), 5000);
}

// --- actions ----------------------------------------------------------------

// The walk being taken: the unlabeled queue, the multi-clip events, or the
// labeled ones (all, or one label). A walk resumes after the current event.
const NEXT_ROUTE = MULTI ? 'multi/next' : WALKING ? 'labeled/next' : 'next';

async function load() {
  closeSheet();
  const params = new URLSearchParams();
  if (LABELS.includes(FILTER)) params.set('value', FILTER);
  if (WALKING && current) params.set('after', current.id);
  show(await get(`/api/review/${NEXT_ROUTE}?${params}`));
}

// Records the label, says so, and moves on; nothing else is accepted meanwhile.
async function label(value) {
  if (!current || busy) return;
  closeSheet();
  setBusy(true);
  try {
    await post(`/api/review/${current.id}/label`, {value});
    history_.push(current.id);
    snack(`#${current.id}: ${value.replace('_', ' ')}`);
    await load();
  } finally {
    setBusy(false);
  }
}

async function startClean() {
  if (!current || busy) return;
  const poops = current.poops || {};
  await post(`/api/review/${current.id}/label`, {value: 'clean'});
  showCleanForm(true);
  for (const box of [1, 2, 3]) {
    document.getElementById(`box${box}`).value = poops[box] ?? 0;
  }
  document.getElementById('box1').focus();
  document.getElementById('box1').select();
}

async function saveCounts() {
  if (!current || busy) return;
  setBusy(true);
  try {
    const counts = {};
    for (const box of [1, 2, 3]) {
      counts[box] = Number(document.getElementById(`box${box}`).value) || 0;
    }
    await post(`/api/review/${current.id}/counts`, {counts});
    history_.push(current.id);
    snack(`#${current.id}: clean, ${Object.values(counts).join('/')}`);
    await load();
  } finally {
    setBusy(false);
  }
}

async function undo() {
  const id = history_.pop();
  if (id === undefined) return;
  document.getElementById('snack').classList.add('gone');
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
  if (data.event) show(data); else alert('There is no later event to join.');
}

function watch(index) {
  const video = document.getElementById(`v${index}`);
  if (!video.src) video.src = `/review/${current.id}/clip/${index}/video.mp4`;
  video.hidden = false;
  video.play();
}

async function rescan() { closeSheet(); await post('/api/review/rescan'); load(); }

function catButton(text, value, key) {
  const button = document.createElement('button');
  button.onclick = () => label(value);
  button.textContent = `${text} `;
  const kbd = document.createElement('kbd');
  kbd.textContent = key;
  button.appendChild(kbd);
  return button;
}

if (REVIEW) {
  const cats = document.getElementById('cats');
  CATS.forEach((name, i) => cats.appendChild(catButton(name, name, String(i + 1))));
  if (CATS.length) {
    cats.appendChild(catButton('multiple', 'multiple', 'm'));
  } else {
    cats.appendChild(catButton('cat', 'cat', 'c'));  // no names yet: cat is the primary label
    document.getElementById('plaincat').hidden = true;
  }
  document.addEventListener('keydown', (e) => {
    if (document.getElementById('review').hidden || e.target.tagName === 'VIDEO') return;
    if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;  // shortcuts are bare keys
    if (e.key === 'Escape') { closeSheet(); return; }
    if (e.target.tagName === 'INPUT') {
      if (e.key === 'Enter') saveCounts();
      return;  // digits belong in the field, not to the shortcuts
    }
    const digit = Number(e.key);
    if (e.key === 'c') label('cat');
    else if (digit >= 1 && digit <= CATS.length) label(CATS[digit - 1]);
    else if (e.key === 'm' && CATS.length) label('multiple');
    else if (e.key === 'l') startClean();
    else if (e.key === 'n') label('not_cat');
    else if (e.key === 'u') label('unsure');
    else if (e.key === 'z') undo();
    else if (e.key === 'ArrowRight' && WALKING) load();
  });
  window.addEventListener('resize', padForBar);
  if (WALKING) {
    document.getElementById('mode').textContent =
      MULTI ? '(multi-clip)' : `(${FILTER.replace('_', ' ')})`;
    document.getElementById('skip').hidden = false;
  }
}
activate(tabFromPath(), false);
