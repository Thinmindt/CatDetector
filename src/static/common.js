// What every tab shares: the page's settings, fetching, escaping, readable
// times, and one click handler per section in place of inline onclick.

// {review, labels, cats, thumbWidth}, written into the page by the template.
export const CONFIG = JSON.parse(document.getElementById('config').textContent);

export async function get(url) { return (await fetch(url)).json(); }

export async function post(url, body) {
  return (await fetch(url, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})})).json();
}

const ENTITIES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};

// Safe in text and in a quoted attribute alike.
export function escaped(text) {
  return String(text).replace(/[&<>"']/g, (c) => ENTITIES[c]);
}

// Stored times carry no zone and are the Pi's local time, which a browser on
// the same network shares; new Date() reads such a string as local.
const READABLE = {weekday: 'short', day: 'numeric', month: 'short',
                  hour: '2-digit', minute: '2-digit'};
const UNITS = [['year', 31536000], ['month', 2592000], ['week', 604800],
               ['day', 86400], ['hour', 3600], ['minute', 60], ['second', 1]];

export function readable(iso) {
  return iso ? new Date(iso).toLocaleString(undefined, READABLE) : 'unknown time';
}

export function relative(iso) {
  if (!iso) return '';
  const seconds = (new Date(iso) - Date.now()) / 1000;
  const [unit, size] = UNITS.find(([, s]) => Math.abs(seconds) >= s) || UNITS.at(-1);
  return new Intl.RelativeTimeFormat(undefined, {numeric: 'auto'})
    .format(Math.round(seconds / size), unit);
}

// A readable time with the relative one on hover, as HTML.
export function timeTag(iso) {
  return `<time datetime="${escaped(iso || '')}" title="${escaped(relative(iso))}">` +
    `${escaped(readable(iso))}</time>`;
}

// Clicks on [data-action] elements inside root call actions[name](element).
export function onActions(root, actions) {
  root.addEventListener('click', (e) => {
    const target = e.target.closest('[data-action]');
    if (!target || !root.contains(target)) return;
    const action = actions[target.dataset.action];
    if (!action) return;
    e.preventDefault();
    action(target, e);
  });
}
