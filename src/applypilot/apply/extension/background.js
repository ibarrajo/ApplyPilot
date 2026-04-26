// ApplyPilot Control Panel — background service worker
// Polls all worker servers (7380-7384) every 3s and updates the extension badge.

const BASE_PORT = 7380;
const MAX_WORKERS = 5;
const POLL_MS = 3000;

async function fetchWorker(workerId) {
  const port = BASE_PORT + workerId;
  try {
    const resp = await fetch(`http://localhost:${port}/api/status`, {
      signal: AbortSignal.timeout(1500),
    });
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

async function pollAll() {
  const results = await Promise.all(
    Array.from({ length: MAX_WORKERS }, (_, i) => fetchWorker(i))
  );

  const workers = results
    .map((d, i) => (d ? { ...d, workerId: d.workerId ?? i } : null))
    .filter(Boolean);

  await chrome.storage.local.set({ workers, lastPoll: Date.now() });
  updateBadge(workers);
}

function updateBadge(workers) {
  const hitl = workers.filter(
    (w) => w.status === 'waiting_human' || w.status === 'needs_human'
  );
  const applying = workers.filter((w) => w.status === 'applying');
  const paused = workers.filter((w) => w.status === 'paused_by_user');

  if (hitl.length > 0) {
    chrome.action.setBadgeText({ text: '!' });
    chrome.action.setBadgeBackgroundColor({ color: [168, 85, 247, 255] }); // purple
  } else if (paused.length > 0) {
    chrome.action.setBadgeText({ text: String(paused.length) });
    chrome.action.setBadgeBackgroundColor({ color: [234, 179, 8, 255] }); // yellow
  } else if (applying.length > 0) {
    chrome.action.setBadgeText({ text: String(applying.length) });
    chrome.action.setBadgeBackgroundColor({ color: [34, 197, 94, 255] }); // green
  } else if (workers.length > 0) {
    chrome.action.setBadgeText({ text: '' });
  } else {
    chrome.action.setBadgeText({ text: '' });
  }
}

// Per-worker colored icon: each Chrome window gets a distinct color+number icon.
// Colors: W0=blue, W1=green, W2=orange, W3=purple, W4=red
const _WORKER_COLORS = ['#3b82f6', '#22c55e', '#f97316', '#a855f7', '#ef4444'];

function makeWorkerIcon(workerId, size) {
  const color = _WORKER_COLORS[workerId % _WORKER_COLORS.length];
  const canvas = new OffscreenCanvas(size, size);
  const ctx = canvas.getContext('2d');

  // Colored circle background
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2, 0, Math.PI * 2);
  ctx.fill();

  // White worker number
  ctx.fillStyle = '#ffffff';
  ctx.font = `bold ${Math.round(size * 0.6)}px system-ui, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(String(workerId), size / 2, size / 2 + 1);

  return ctx.getImageData(0, 0, size, size);
}

// Persist own worker ID so popup.js can identify which card to highlight.
// WORKER_CONFIG is prepended to this file by setup_worker_profile() with the
// correct per-worker values — this is more reliable than popup.html loading
// config.js (which depends on the user installing from the right directory).
if (typeof WORKER_CONFIG !== 'undefined' && WORKER_CONFIG.workerId !== null) {
  const _wid = WORKER_CONFIG.workerId;
  chrome.storage.local.set({ myWorkerId: _wid });

  // Set distinct colored icon for this worker window
  try {
    chrome.action.setIcon({
      imageData: {
        16:  makeWorkerIcon(_wid, 16),
        48:  makeWorkerIcon(_wid, 48),
        128: makeWorkerIcon(_wid, 128),
      },
    });
  } catch (_e) {
    // Icon generation is non-critical — fail silently
  }
}

// ---------------------------------------------------------------------------
// Per-job tab tracking (spec §3.2 — audit #3 fix)
// ---------------------------------------------------------------------------
// Each worker has a "primary tab" (the first tab loaded under this profile).
// Any tab opened from the primary or its descendants — tracked via
// `openerTabId` — joins the worker's in-set. Tabs the user opens manually
// (no opener) stay out-of-set. Used by content.js to gate event capture
// and (eventually) banner rendering.
//
// State shape in chrome.storage.local:
//   tabSet: { [workerId]: number[] }   // tab IDs currently in-set
//
// In-memory mirror loaded on SW init and kept synchronized with storage.

const _tabSet = new Map(); // workerId -> Set<tabId>

async function loadTabSet() {
  try {
    const { tabSet } = await chrome.storage.local.get(['tabSet']);
    if (tabSet) {
      for (const [wid, tabs] of Object.entries(tabSet)) {
        _tabSet.set(Number(wid), new Set(tabs));
      }
    }
  } catch (_e) { /* SW init race — fine, will be empty */ }
}

async function persistTabSet() {
  const obj = {};
  for (const [wid, tabs] of _tabSet) {
    obj[wid] = Array.from(tabs);
  }
  try { await chrome.storage.local.set({ tabSet: obj }); } catch (_e) {}
}

function findOwningWorker(openerTabId) {
  if (openerTabId == null) return null;
  for (const [wid, tabs] of _tabSet) {
    if (tabs.has(openerTabId)) return wid;
  }
  return null;
}

chrome.tabs.onCreated.addListener(async (tab) => {
  if (tab.openerTabId == null) return;
  const owner = findOwningWorker(tab.openerTabId);
  if (owner == null) return;
  _tabSet.get(owner).add(tab.id);
  await persistTabSet();
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  let changed = false;
  for (const tabs of _tabSet.values()) {
    if (tabs.delete(tabId)) changed = true;
  }
  if (changed) await persistTabSet();
});

// On SW init: if WORKER_CONFIG identifies us as a specific worker's profile,
// seed the in-set with the currently-active tab as the primary.
async function seedPrimaryTab() {
  if (typeof WORKER_CONFIG === 'undefined' || WORKER_CONFIG.workerId == null) {
    return;
  }
  const wid = WORKER_CONFIG.workerId;
  if (!_tabSet.has(wid)) _tabSet.set(wid, new Set());
  // Only seed if the set is currently empty (avoid clobbering on SW restart).
  if (_tabSet.get(wid).size > 0) return;
  try {
    const tabs = await chrome.tabs.query({ currentWindow: true });
    for (const t of tabs) _tabSet.get(wid).add(t.id);
    await persistTabSet();
  } catch (_e) { /* fine */ }
}

// Expose tab-set membership to content scripts via runtime.sendMessage.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'is-in-set') {
    const tabId = sender.tab && sender.tab.id;
    if (tabId == null) { sendResponse({ inSet: false }); return false; }
    let inSet = false;
    for (const tabs of _tabSet.values()) {
      if (tabs.has(tabId)) { inSet = true; break; }
    }
    sendResponse({ inSet, workerId: WORKER_CONFIG && WORKER_CONFIG.workerId });
    return false;
  }
  if (msg.type === 'append-action') {
    appendAction(msg.event).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === 'get-action-log') {
    const wid = msg.workerId ?? (typeof WORKER_CONFIG !== 'undefined' ? WORKER_CONFIG.workerId : null);
    chrome.storage.local.get(['actionLog']).then(({ actionLog }) => {
      const events = (actionLog && actionLog[wid]) || [];
      sendResponse({ events });
    });
    return true;
  }
});

loadTabSet().then(seedPrimaryTab);

// ---------------------------------------------------------------------------
// Action-log ring buffer (spec §4.4)
// ---------------------------------------------------------------------------
// Filtered events captured by content.js (clicks, navs, form submits, tab
// opens). Stored per-worker as a ring buffer in chrome.storage.local.
// Capped at 200 events / 64KB total per worker per pause cycle.
//
// Shape:
//   actionLog: { [workerId]: Array<{type, t, ...}> }

const ACTION_LOG_MAX_EVENTS = 200;
const ACTION_LOG_MAX_BYTES  = 64 * 1024;

async function appendAction(event) {
  if (typeof WORKER_CONFIG === 'undefined' || WORKER_CONFIG.workerId == null) return;
  const wid = WORKER_CONFIG.workerId;
  try {
    const { actionLog } = await chrome.storage.local.get(['actionLog']);
    const logs = actionLog || {};
    const buf = logs[wid] || [];
    buf.push({ ...event, t: Date.now() });
    // Cap by event count.
    while (buf.length > ACTION_LOG_MAX_EVENTS) buf.shift();
    // Cap by serialized size.
    while (buf.length > 1) {
      const size = new TextEncoder().encode(JSON.stringify(buf)).length;
      if (size <= ACTION_LOG_MAX_BYTES) break;
      buf.shift();
    }
    logs[wid] = buf;
    await chrome.storage.local.set({ actionLog: logs });
  } catch (_e) { /* SW write race — fine, drop event */ }
}

// Poll on alarm — 1-minute period (Chrome's minimum for alarms).
// The setInterval below handles 3s polling while the SW is alive.
// The alarm acts as a restart trigger: Chrome wakes the SW to fire the alarm,
// at which point setInterval restarts from scratch in the new SW context.
chrome.alarms.create('poll', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'poll') pollAll();
});

// Keep the service worker alive by making a harmless runtime API call every 25s.
// Chrome MV3 terminates idle SWs after ~30s; this extends the window so the
// 3s setInterval below stays active during normal use. The 1-min alarm above
// handles the case where the SW does get terminated between popup opens.
setInterval(() => chrome.runtime.getPlatformInfo(() => {}), 25000);

// Also poll on install/startup
chrome.runtime.onInstalled.addListener(pollAll);
chrome.runtime.onStartup.addListener(pollAll);

// Poll immediately and on message from popup
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'poll') {
    pollAll().then(() => sendResponse({ ok: true }));
    return true; // async response
  }
});

// Start polling loop via setInterval (alarms have 1-min minimum)
setInterval(pollAll, POLL_MS);
pollAll();
