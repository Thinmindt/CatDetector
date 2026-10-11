// The Timeline tab: every event, newest first, filtered by label chips, a
// brushed range of days, a brushed range of hours and a labelled-before date.
// The filter is the address's query string and the API's, unchanged.
import {CONFIG, escaped, get, onActions, post, timeTag} from './common.js';

const {d3} = window;
const UNLABELED = 'unlabeled';
const CHIPS = [...CONFIG.labels, UNLABELED];
const FIXED_COLOURS = {cat: '#bbbbbb', not_cat: '#5a5a5a', unsure: '#9a8430',
                       clean: '#4a7fa8', multiple: '#8c6bb1', [UNLABELED]: '#e8e8e8'};
const CAT_COLOURS = ['#e15759', '#59a14f', '#f28e2b', '#76b7b2', '#edc948',
                     '#ff9da7', '#9c755f', '#4e79a7', '#b07aa1', '#d4a6c8'];
const HOURS_IN_DAY = 24;
const HOUR_TICKS = [0, 3, 6, 9, 12, 15, 18, 21, 24];
const STRIP_HEIGHT = 72;
const AXIS_HEIGHT = 18;
const HANDLE_PX = 28;  // the brush's grab width
const EDGE_PX = 22;  // room at each end for the first and last axis labels
const DAY_TICK_PX = 90;
const RESIZE_MS = 150;

const colour = (chip) => FIXED_COLOURS[chip]
  ?? CAT_COLOURS[CONFIG.cats.indexOf(chip) % CAT_COLOURS.length] ?? '#999999';

// chips: the labels shown (empty: all); days: [first, last] inclusive, as
// YYYY-MM-DD; hours: [first, last) as brushed, and outside: whether the
// filter is the rest of the day; labeledBefore: YYYY-MM-DD.
const filter = {chips: new Set(), days: null, hours: null, outside: false, labeledBefore: ''};
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
let data = null;
let asked = 0;
let answered = 0;
let resizeTimer = null;

// --- the filter and the address ---------------------------------------------

function readAddress() {
  const params = new URLSearchParams(location.search);
  filter.chips = new Set(params.getAll('label').filter(chip => CHIPS.includes(chip)));
  const from = params.get('from');
  const to = params.get('to');
  filter.days = ISO_DAY.test(from) && ISO_DAY.test(to) ? [from, to] : null;
  const hours = (params.get('hours') || '').match(/^(\d+)-(\d+)$/);
  const inDay = (h) => Number(h) >= 0 && Number(h) <= HOURS_IN_DAY;
  if (hours && hours[1] !== hours[2] && inDay(hours[1]) && inDay(hours[2])) {
    const [first, last] = [Number(hours[1]), Number(hours[2])];
    filter.outside = first > last;
    filter.hours = filter.outside ? [last, first] : [first, last];
  } else {
    filter.hours = null;
    filter.outside = false;
  }
  const before = params.get('labeled_before') || '';
  filter.labeledBefore = ISO_DAY.test(before) ? before : '';
}

function query() {
  const params = new URLSearchParams();
  for (const chip of CHIPS) if (filter.chips.has(chip)) params.append('label', chip);
  if (filter.days) {
    params.set('from', filter.days[0]);
    params.set('to', filter.days[1]);
  }
  if (filter.hours) {
    const [first, last] = filter.hours;
    params.set('hours', filter.outside ? `${last}-${first}` : `${first}-${last}`);
  }
  if (filter.labeledBefore) params.set('labeled_before', filter.labeledBefore);
  return params.toString();
}

export function timelineAddress() {
  const q = query();
  return q ? `/timeline?${q}` : '/timeline';
}

// Staging waits for the answer to the filter on screen.
function stagingAllowed() {
  document.getElementById('stageshown').disabled =
    answered !== asked || !data || data.stageable.length === 0;
}

async function refresh() {
  if (location.pathname === '/timeline') history.replaceState(null, '', timelineAddress());
  const mine = ++asked;
  stagingAllowed();
  const body = await get(`/api/timeline?${query()}`);
  if (mine !== asked) return;  // a newer filter was asked for meanwhile
  answered = mine;
  if (body.error) {
    data = null;
    renderRefused(body.error);
    return;
  }
  data = body;
  render();
}

function renderRefused(message) {
  renderChips();
  document.getElementById('summary').textContent =
    `This filter could not be read (${message}); clear it to start again.`;
  document.querySelector('#rows tbody').innerHTML = '';
  stagingAllowed();
}

// --- days as dates ----------------------------------------------------------

function dayDate(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function isoDay(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// --- the strips -------------------------------------------------------------

// A strip: stacked bars per slot, an axis, and a brush that snaps to whole
// slots. place(slot) gives a slot's [left, right) in pixels; slotAt(px) the
// nearest slot boundary; select([a, b]) or select(null) records a release.
// widen(edge) is the one-slot range a brush too short to span a slot becomes.
function drawStrip(svg, {slots, counts, x, axis, place, slotAt, widen, chosen, select}) {
  const width = svg.node().clientWidth;
  const top = STRIP_HEIGHT - AXIS_HEIGHT;
  svg.attr('viewBox', `0 0 ${width} ${STRIP_HEIGHT}`).selectAll('*').remove();
  x.range([EDGE_PX, width - EDGE_PX]);
  const totals = slots.map((_, i) => d3.sum(Object.values(counts(i))));
  const y = d3.scaleLinear().domain([0, d3.max(totals) || 1]).range([top, 2]);
  const bars = svg.append('g');
  slots.forEach((_, i) => {
    const [left, right] = place(i);
    const slot = bars.append('g').attr('class', 'slot').datum(i);
    let base = 0;
    for (const chip of CHIPS) {
      const n = counts(i)[chip] || 0;
      if (!n) continue;
      slot.append('rect').attr('x', left).attr('width', Math.max(1, right - left - 1))
        .attr('y', y(base + n)).attr('height', y(base) - y(base + n))
        .attr('fill', colour(chip));
      base += n;
    }
  });
  svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${top})`).call(axis);
  const dim = (range) => bars.selectAll('.slot').classed('out', (i) => {
    if (!range) return false;
    const [left, right] = place(i);
    return right <= range[0] || left >= range[1];
  });
  const brush = d3.brushX().extent([[EDGE_PX, 0], [width - EDGE_PX, top]])
    .handleSize(HANDLE_PX)
    .on('brush', (e) => dim(e.selection))
    .on('end', (e) => {
      if (!e.sourceEvent) return;  // a move made here, not by the reviewer
      if (!e.selection) {
        dim(null);
        select(null);
        return;
      }
      const [a, b] = e.selection.map(slotAt);
      const range = b > a ? [a, b] : widen(a);
      e.target.move(brushed, range.map((edge) => x(edge)));
      select(range);
    });
  const brushed = svg.append('g').attr('class', 'brush').call(brush);
  if (chosen) {
    const pixels = chosen.map((edge) => x(edge));
    brushed.call(brush.move, pixels);
    dim(pixels);
  }
  return {dim};
}

// Every nth day counted from the first.
function everyNthDay(first, count, width) {
  const step = Math.ceil(count / Math.max(2, Math.floor(width / DAY_TICK_PX)));
  return d3.timeDay.filter((day) => d3.timeDay.count(first, day) % step === 0);
}

function drawDays() {
  const svg = d3.select('#days');
  if (!data.first_day) {
    svg.selectAll('*').remove();
    return;
  }
  const first = dayDate(data.first_day);
  const end = d3.timeDay.offset(dayDate(data.last_day), 1);
  const days = d3.timeDay.range(first, end);
  const x = d3.scaleTime().domain([first, end]);
  const width = svg.node().clientWidth;
  drawStrip(svg, {
    slots: days,
    counts: (i) => data.by_day[isoDay(days[i])] || {},
    x,
    axis: d3.axisBottom(x).ticks(everyNthDay(first, days.length, width))
      .tickFormat(d3.timeFormat('%-d %b')).tickSizeOuter(0),
    place: (i) => [x(days[i]), x(d3.timeDay.offset(days[i], 1))],
    slotAt: (px) => d3.timeDay.round(x.invert(px)),
    widen: (day) => (day < end
      ? [day, d3.timeDay.offset(day, 1)] : [d3.timeDay.offset(end, -1), end]),
    chosen: filter.days
      ? [dayDate(filter.days[0]), d3.timeDay.offset(dayDate(filter.days[1]), 1)] : null,
    select: (range) => {
      filter.days = range
        ? [isoDay(range[0]), isoDay(d3.timeDay.offset(range[1], -1))] : null;
      refresh();
    },
  });
}

function drawHours() {
  const svg = d3.select('#hours');
  const hours = d3.range(HOURS_IN_DAY);
  const x = d3.scaleLinear().domain([0, HOURS_IN_DAY]);
  drawStrip(svg, {
    slots: hours,
    counts: (i) => data.by_hour[i] || {},
    x,
    axis: d3.axisBottom(x).tickValues(HOUR_TICKS).tickFormat((h) => `${h}h`).tickSizeOuter(0),
    place: (i) => [x(i), x(i + 1)],
    slotAt: (px) => Math.round(x.invert(px)),
    widen: (hour) => (hour < HOURS_IN_DAY ? [hour, hour + 1] : [HOURS_IN_DAY - 1, HOURS_IN_DAY]),
    chosen: filter.hours,
    select: (range) => {
      filter.hours = range && range[1] - range[0] < HOURS_IN_DAY ? range : null;
      if (!filter.hours) filter.outside = false;
      refresh();
    },
  });
  d3.select('#hours').classed('outside', filter.outside);
}

// --- the rest of the page ---------------------------------------------------

function describe() {
  const parts = [];
  if (filter.days) parts.push(filter.days[0] === filter.days[1]
    ? filter.days[0] : `${filter.days[0]} to ${filter.days[1]}`);
  if (filter.hours) {
    const [first, last] = filter.hours;
    parts.push(filter.outside ? `outside ${first}h to ${last}h` : `${first}h to ${last}h`);
  }
  if (filter.labeledBefore) parts.push(`labelled before ${filter.labeledBefore}`);
  return parts.join(', ');
}

function renderChips() {
  const chips = document.getElementById('chips');
  chips.classList.toggle('all', filter.chips.size === 0);
  chips.innerHTML = CHIPS.map((chip) =>
    `<button class="chip${filter.chips.has(chip) ? ' on' : ''}" data-action="chip"` +
    ` data-value="${escaped(chip)}"><span class="swatch" style="background:` +
    `${colour(chip)}"></span>${escaped(chip.replace('_', ' '))}</button>`).join('');
  document.getElementById('outside').classList.toggle('on', filter.outside);
  document.getElementById('outside').disabled = !filter.hours;
  document.getElementById('labeledbefore').value = filter.labeledBefore;
}

function length(seconds) {
  if (seconds === null) return '?';
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function rowHtml(row) {
  const chip = row.label || UNLABELED;
  let stage = '';
  if (row.staged) stage = '<span class="warn">staged</span>';
  else if (row.label) {
    stage = `<button class="small" data-action="stage-row" data-id="${row.id}"` +
      ' title="back for review">&#8617;</button>';
  }
  return `<tr><td><a href="/review/${row.id}">${timeTag(row.started_at)}</a></td>` +
    `<td><span class="swatch" style="background:${colour(chip)}"></span>` +
    `${escaped(chip.replace('_', ' '))}${row.peek ? '<br><span class="note">peek</span>' : ''}</td>` +
    `<td>${row.box ?? '?'}</td>` +
    `<td>${length(row.seconds)}</td><td>${row.clips}</td><td>${stage}</td></tr>`;
}

function renderRows() {
  const {rows, total} = data;
  const listed = total > rows.length ? ` (the newest ${rows.length} listed)` : '';
  const scope = describe();
  document.getElementById('summary').textContent =
    `${total} event${total === 1 ? '' : 's'}${scope ? `, ${scope}` : ''}${listed}`;
  document.querySelector('#rows tbody').innerHTML = rows.map(rowHtml).join('');
  stagingAllowed();
}

function render() {
  renderChips();
  drawDays();
  drawHours();
  renderRows();
}

// --- actions ----------------------------------------------------------------

async function stage(ids, question) {
  const reason = prompt(question);
  if (!reason || !reason.trim()) return;
  const result = await post('/api/review/stage', {event_ids: ids, reason});
  if (result.error) alert(result.error);
  await refresh();
}

function toggleChip(chip) {
  if (filter.chips.has(chip)) filter.chips.delete(chip);
  else filter.chips.add(chip);
  refresh();
}

function clearFilter() {
  filter.chips.clear();
  filter.days = null;
  filter.hours = null;
  filter.outside = false;
  filter.labeledBefore = '';
  refresh();
}

export function initTimeline() {
  readAddress();
  onActions(document.getElementById('timeline'), {
    chip: (el) => toggleChip(el.dataset.value),
    outside: () => {
      filter.outside = !filter.outside;
      refresh();
    },
    clear: clearFilter,
    'stage-row': (el) => stage([Number(el.dataset.id)],
      `Why should #${el.dataset.id} come back for review?`),
    'stage-shown': () => stage(data.stageable,
      `Put the ${data.stageable.length} labelled events shown that are not staged yet` +
      ' back in the review queue, keeping their labels? Say why:'),
  });
  document.getElementById('labeledbefore').addEventListener('change', (e) => {
    filter.labeledBefore = e.target.value;
    refresh();
  });
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (data && !document.getElementById('timeline').hidden) render();
    }, RESIZE_MS);
  });
}

export function showTimeline() {
  if (location.pathname === '/timeline') readAddress();
  refresh();
}
