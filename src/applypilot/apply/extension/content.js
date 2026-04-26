// ApplyPilot content script — runs on every http(s) page.
//
// Two responsibilities:
//   1. (always) On in-set tabs, capture filtered user events to the
//      action-log ring buffer. Spec §3.2 / §4.4.
//   2. (job pages only, in-set) Show the "Add to ApplyPilot" pill so the
//      user can capture new jobs they're browsing.
//
// "In-set" = this tab is part of a worker's per-job tab set, tracked by
// background.js via openerTabId chains.

const BASE_WORKER_PORT = 7380;
const MAX_WORKERS = 5;

let _inSet = false;          // is this tab in a worker's in-set?
let _myWorkerId = null;      // which worker owns us (null if not in-set)
let _workerPort = null;      // port of the live worker we found
let _pill = null;
let _state = 'idle';

// --- Job page detection --------------------------------------------------
const JOB_URL_PATTERNS = [
  /\/jobs?\//i, /\/careers?\//i, /\/job-detail/i, /\/position/i,
  /\/openings?\//i, /\/apply/i, /\/posting/i, /\/vacancy/i,
  /workday\.com/, /greenhouse\.io/, /lever\.co/, /icims\.com/,
  /myworkdayjobs\.com/, /taleo\.net/, /ashbyhq\.com/, /jobvite\.com/,
  /linkedin\.com\/jobs/, /indeed\.com\/viewjob/, /dice\.com\/job-detail/,
];

function looksLikeJobPage() {
  const url = location.href;
  if (JOB_URL_PATTERNS.some(re => re.test(url))) return true;
  const title = document.title.toLowerCase();
  return /engineer|developer|devops|platform|backend|frontend|staff|principal|senior/.test(title)
    && /job|position|opening|career|apply/.test(title);
}

// --- Action-log event capture (spec §3.2 / §4.4) -------------------------
//
// Filtered to "meaningful" events only — button-target clicks, anchor
// navigations, form submits, tab opens. Body/scroll/focus/keystroke
// noise is dropped. Per-event SW IPC via chrome.runtime.sendMessage —
// the SW maintains the actual ring buffer in chrome.storage.local.

function _send(event) {
  try {
    chrome.runtime.sendMessage({ type: 'append-action', event });
  } catch (_e) { /* SW restarting — drop event */ }
}

function _captureClick(e) {
  // Only meaningful clicks: buttons, anchors, role=button, [type=submit].
  let el = e.target;
  while (el && el !== document.body) {
    const tag = (el.tagName || '').toLowerCase();
    const role = el.getAttribute && el.getAttribute('role');
    const type = el.getAttribute && el.getAttribute('type');
    if (tag === 'button' || tag === 'a' || role === 'button' ||
        type === 'submit' || type === 'button') {
      _send({
        type: 'click',
        target: tag,
        text: (el.innerText || '').trim().slice(0, 80),
        href: el.href || null,
        url: location.href,
      });
      return;
    }
    el = el.parentElement;
  }
}

function _captureSubmit(e) {
  const form = e.target;
  const fields = [];
  if (form && form.elements) {
    for (const el of form.elements) {
      if (!el.name && !el.id) continue;
      // Don't log password values.
      const val = el.type === 'password' ? '***' : (el.value || '').slice(0, 64);
      fields.push({ name: el.name || el.id, value: val });
    }
  }
  _send({ type: 'submit', fields, url: location.href });
}

function _captureNavigation(navType) {
  _send({ type: 'nav', mode: navType, url: location.href });
}

function _installEventCapture() {
  // Click: bubble phase, document-level (catches everything not
  // stop-propagated by the page).
  document.addEventListener('click', _captureClick, { capture: true, passive: true });
  document.addEventListener('submit', _captureSubmit, { capture: true, passive: true });

  // Navigation: history pushState/replaceState + popstate. Initial
  // pageload is logged via the script bootstrap below.
  const _ps = history.pushState;
  history.pushState = function () {
    _ps.apply(this, arguments);
    _captureNavigation('pushState');
  };
  const _rs = history.replaceState;
  history.replaceState = function () {
    _rs.apply(this, arguments);
    _captureNavigation('replaceState');
  };
  window.addEventListener('popstate', () => _captureNavigation('popstate'));

  // Initial page entry.
  _captureNavigation('initial');
}

// Read form-field final values for the snapshot Done payload (spec §4.1
// step 5). Skips passwords. Capped at 64 chars per value to keep the
// snapshot small.
window.__ap_form_snapshot = function () {
  const fields = {};
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (!el.name && !el.id) continue;
    if (el.type === 'password') continue;
    if (el.type === 'hidden') continue;
    const key = el.name || el.id;
    const val = (el.value || '').slice(0, 200);
    if (val) fields[key] = val;
  }
  return fields;
};

// --- "Add to ApplyPilot" pill (existing feature, preserved) -------------

function createPill() {
  const pill = document.createElement('div');
  pill.id = 'applypilot-capture-pill';
  pill.style.cssText = `
    position: fixed;
    bottom: 18px;
    right: 18px;
    z-index: 2147483647;
    background: #312e81;
    color: #e0e7ff;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 12px;
    font-weight: 500;
    padding: 7px 14px 7px 10px;
    border-radius: 20px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.4);
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: background 0.15s, opacity 0.15s;
    user-select: none;
    border: 1px solid #4f46e5;
  `;
  pill.addEventListener('mouseenter', () => {
    if (_state === 'idle') pill.style.background = '#3730a3';
  });
  pill.addEventListener('mouseleave', () => {
    if (_state === 'idle') pill.style.background = '#312e81';
  });
  pill.addEventListener('click', captureJob);
  return pill;
}

function setPillState(state, msg) {
  _state = state;
  if (!_pill) return;
  const configs = {
    idle:    { bg: '#312e81', border: '#4f46e5', text: '📥 Add to ApplyPilot' },
    loading: { bg: '#1e1b4b', border: '#3730a3', text: '⏳ Adding…' },
    success: { bg: '#064e3b', border: '#059669', text: '✓ Added to pipeline' },
    exists:  { bg: '#1e3a5f', border: '#2563eb', text: msg || '↩ Already in pipeline' },
    error:   { bg: '#450a0a', border: '#b91c1c', text: '✗ ' + (msg || 'Error') },
  };
  const cfg = configs[state] || configs.idle;
  _pill.style.background = cfg.bg;
  _pill.style.borderColor = cfg.border;
  _pill.textContent = cfg.text;

  if (state !== 'idle' && state !== 'loading') {
    setTimeout(() => setPillState('idle'), 4000);
  }
}

async function captureJob() {
  if (_state === 'loading') return;
  setPillState('loading');

  const url   = location.href;
  const title = document.title.replace(/\s*[-|].*$/, '').trim();

  if (!_workerPort) await findWorker();
  if (!_workerPort) {
    setPillState('error', 'No active workers');
    return;
  }

  try {
    const resp = await fetch(`http://localhost:${_workerPort}/api/add-job`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, title }),
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    if (data.status === 'queued') {
      setPillState('success');
    } else if (data.status === 'exists') {
      const label = data.applyStatus ? `Already ${data.applyStatus}` : 'Already in pipeline';
      setPillState('exists', label);
    } else {
      setPillState('error', 'Unexpected response');
    }
  } catch (err) {
    _workerPort = null;
    setPillState('error', err.message.slice(0, 40));
  }
}

async function findWorker() {
  for (let i = 0; i < MAX_WORKERS; i++) {
    const port = BASE_WORKER_PORT + i;
    try {
      const resp = await fetch(`http://localhost:${port}/api/status`, {
        signal: AbortSignal.timeout(1000),
      });
      if (resp.ok) { _workerPort = port; return port; }
    } catch { /* skip */ }
  }
  return null;
}

// --- Bootstrap ----------------------------------------------------------

async function init() {
  // Ask the SW whether this tab belongs to a worker's in-set. If yes,
  // install the action-log event capture (regardless of whether the
  // page looks like a job posting). The pill stays job-page-gated.
  try {
    const reply = await chrome.runtime.sendMessage({ type: 'is-in-set' });
    _inSet = !!(reply && reply.inSet);
    _myWorkerId = (reply && reply.workerId) ?? null;
  } catch (_e) {
    // SW unavailable — silent fallback (no event capture, no pill).
    return;
  }

  if (_inSet) _installEventCapture();

  // Pill is job-page-gated as before.
  if (!looksLikeJobPage()) return;

  const port = await findWorker();
  if (!port) return;

  _pill = createPill();
  setPillState('idle');
  if (document.body) document.body.appendChild(_pill);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
