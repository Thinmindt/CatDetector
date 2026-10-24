// The Live tab: the MJPEG feed and the recorder's status line.
import {get} from './common.js';

const STATUS_EVERY_MS = 2000;
let statusTimer = null;

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

// The feed holds an MJPEG connection only while the tab is shown.
export function showLive(shown) {
  const feed = document.getElementById('feed');
  clearInterval(statusTimer);
  statusTimer = null;
  if (shown) {
    feed.src = '/video_feed';
    pollStatus();
    statusTimer = setInterval(pollStatus, STATUS_EVERY_MS);
  } else {
    feed.removeAttribute('src');  // closes the MJPEG connection
  }
}
