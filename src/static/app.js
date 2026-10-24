// The page's tabs. Each tab is a module; this one shows and hides them and
// keeps the address in step.
import {CONFIG} from './common.js';
import {showLive} from './live.js';
import {hideReview, initReview, reviewAddress, showReview} from './review.js';
import {initTimeline, showTimeline, timelineAddress} from './timeline.js';

const TABS = ['live', 'review', 'timeline'];

function tabFromPath() {
  if (location.pathname.startsWith('/review')) return 'review';
  if (location.pathname.startsWith('/timeline')) return 'timeline';
  return 'live';
}

function address(tab) {
  if (tab === 'review') return reviewAddress();
  if (tab === 'timeline') return timelineAddress();
  return '/';
}

function activate(tab, push) {
  for (const name of TABS) {
    document.getElementById(name).hidden = name !== tab;
    document.querySelector(`nav a[data-tab=${name}]`).classList.toggle('active', name === tab);
  }
  showLive(tab === 'live');
  if (tab !== 'review' && CONFIG.review) hideReview();
  if (push) history.pushState(null, '', address(tab));
  if (tab === 'review' && CONFIG.review) showReview();
  if (tab === 'timeline' && CONFIG.review) showTimeline();
}

document.querySelectorAll('nav a').forEach((a) => a.addEventListener('click', (e) => {
  e.preventDefault();
  activate(a.dataset.tab, true);
}));
window.addEventListener('popstate', () => activate(tabFromPath(), false));

if (CONFIG.review) {
  initReview();
  initTimeline();
}
activate(tabFromPath(), false);
