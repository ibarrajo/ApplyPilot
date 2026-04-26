// ApplyPilot Control Panel — popup controller
// Reads worker state from chrome.storage.local (populated by background.js every 3s).
// Each worker server lives at http://localhost:(7380 + workerId).

const BASE_PORT = 7380;
const MAX_WORKERS = 5;
// Seconds before "recently focused" highlight fades
const FOCUS_HIGHLIGHT_TTL = 30;

let workers = [];
let myWorkerId     = null;        // workerId of THIS Chrome instance (set by background.js via storage)

// Synchronous fast-path: config.js is loaded before this script in popup.html,
// so WORKER_CONFIG is available immediately without waiting for async storage.
if (typeof WORKER_CONFIG !== 'undefined' && WORKER_CONFIG.workerId !== null) {
  myWorkerId = WORKER_CONFIG.workerId;
}
let lastFocusedWid = null;        // worker ID the user most recently clicked to focus
let lastFocusedAt  = 0;           // epoch-seconds when that click happened
const expandedHitl   = new Set();   // worker IDs with mini-task panel open
const taskEventSources = {};         // workerId → EventSource
const savedInputs    = {};           // workerId → textarea content (survives re-renders)
const savedOutputs   = {};           // workerId → { text, visible }

// ── Helpers ───────────────────────────────────────────────────────────────────

function workerUrl(wid, path) {
  return `http://localhost:${BASE_PORT + wid}${path}`;
}

async function apiCall(wid, path, opts = {}) {
  return fetch(workerUrl(wid, path), {
    headers: { 'Content-Type': 'application/json' },
    signal: AbortSignal.timeout(3000),
    ...opts,
  });
}

// Last serialized workers state — used to skip redundant re-renders.
let _lastWorkersJson = '';

// Fetch fresh status directly from all worker servers (bypass storage).
// Only re-renders if data actually changed, so open textareas aren't
// interrupted by DOM rebuilds every 2s.
async function refreshFromServers() {
  const results = await Promise.allSettled(
    Array.from({ length: MAX_WORKERS }, (_, i) =>
      fetch(`http://localhost:${BASE_PORT + i}/api/status`, {
        signal: AbortSignal.timeout(1500),
      })
        .then(r => r.ok ? r.json() : null)
        .catch(() => null)
    )
  );
  const fresh = results
    .map((r, i) => (r.status === 'fulfilled' && r.value
      ? { ...r.value, workerId: r.value.workerId ?? i }
      : null))
    .filter(Boolean);

  const newJson = JSON.stringify(fresh);
  if (newJson !== _lastWorkersJson) {
    _lastWorkersJson = newJson;
    renderAll(fresh);
  }

  _updateRefreshAge();
}

function esc(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function renderAll(workerData) {
  // Snapshot textarea content and output areas before wiping DOM
  document.querySelectorAll('textarea[id^="ta-"]').forEach(ta => {
    const wid = parseInt(ta.id.slice(3), 10);
    savedInputs[wid] = ta.value;
  });
  document.querySelectorAll('.output-area[id^="out-"]').forEach(el => {
    const wid = parseInt(el.id.slice(4), 10);
    savedOutputs[wid] = { text: el.textContent, visible: el.classList.contains('visible') };
  });

  workers = workerData || [];
  const list = document.getElementById('worker-list');

  if (!workers.length) {
    list.innerHTML = `<div class="empty">
      No active workers.<br>
      Run <code>applypilot apply</code> to start.
    </div>`;
    updateSummary();
    return;
  }

  // Sort: HITL first, then resuming, then paused, then applying, then other
  const priority = w => {
    const s = w.status;
    if (s === 'waiting_human' || s === 'needs_human') return 0;
    if (s === 'resuming') return 1;
    if (s === 'paused_by_user') return 2;
    if (s === 'applying') return 3;
    return 4;
  };

  list.innerHTML = [...workers]
    .sort((a, b) => priority(a) - priority(b))
    .map(renderCard)
    .join('');

  // Restore textarea values and output content
  workers.forEach(w => {
    const wid = w.workerId;

    const ta = document.getElementById(`ta-${wid}`);
    if (ta) {
      // Prefer previously typed value; fall back to server's saved instruction
      if (savedInputs[wid] !== undefined && savedInputs[wid] !== '') {
        ta.value = savedInputs[wid];
      } else if (w.savedInstruction) {
        ta.value = w.savedInstruction;
      }
    }

    const outEl = document.getElementById(`out-${wid}`);
    if (outEl && savedOutputs[wid]?.visible) {
      outEl.textContent = savedOutputs[wid].text;
      outEl.classList.add('visible');
    }
  });

  attachListeners();
  updateSummary();
  updateWindowLabel();
}

function renderCard(w) {
  const wid = w.workerId;
  const s   = w.status || 'idle';

  // Show yellow highlight on the WID label if this window was recently focused.
  // Uses lastFocusedWid (set when the user clicks a card) or the server's
  // lastFocused timestamp (set when /api/focus was called).
  const now = Date.now() / 1000;
  const serverFocused = w.lastFocused && (now - w.lastFocused) < FOCUS_HIGHLIGHT_TTL;
  const clientFocused = lastFocusedWid === wid && (now - lastFocusedAt) < FOCUS_HIGHLIGHT_TTL;
  const isFocused = serverFocused || clientFocused;

  let dotClass, statusText, statusClass, borderClass = '';
  if (s === 'applying') {
    dotClass = 'dot-green';  statusText = 'Applying';           statusClass = 'c-green';
  } else if (s === 'paused_by_user') {
    dotClass = 'dot-yellow'; statusText = 'You Have Control';   statusClass = 'c-yellow';
    borderClass = 'card-paused';
  } else if (s === 'waiting_human' || s === 'needs_human') {
    dotClass = 'dot-purple'; statusText = 'Human Review Needed'; statusClass = 'c-purple';
    borderClass = 'card-hitl';
  } else if (s === 'resuming') {
    dotClass = 'dot-purple'; statusText = 'Agent Taking Over…';  statusClass = 'c-purple';
    borderClass = 'card-hitl';
  } else {
    dotClass = 'dot-gray';   statusText = s;                    statusClass = 'c-gray';
  }

  const scoreHtml = w.score
    ? `<div class="score-badge">${w.score}/10</div>` : '';

  let bodyHtml = '';

  if (s === 'applying') {
    bodyHtml = `
      <div class="card-body">
        <button class="btn btn-takeover" data-action="takeover" data-wid="${wid}">⏸ Take Over</button>
      </div>`;

  } else if (s === 'paused_by_user') {
    const streamActive = !!taskEventSources[wid];
    bodyHtml = `
      <div class="card-body">
        <div class="task-area">
          <textarea id="ta-${wid}" placeholder="Tell the assistant what to do (or leave blank to just resume)…"></textarea>
          <div class="btn-row mt6">
            <button class="btn btn-run btn-sm" data-action="run-task" data-wid="${wid}"
              ${streamActive ? 'disabled' : ''}>▶ Run</button>
            <div class="save-row flex1" style="justify-content:flex-end">
              <input type="checkbox" id="save-${wid}" checked>
              <label for="save-${wid}">Save</label>
            </div>
          </div>
          <div id="out-${wid}" class="output-area"></div>
        </div>
        <div class="divider"></div>
        <button class="btn btn-handback" style="width:100%"
          data-action="handback" data-wid="${wid}">⏭ Give Back Control</button>
      </div>`;

  } else if (s === 'resuming') {
    bodyHtml = `
      <div class="card-body">
        <div class="hitl-instructions">Agent is taking over the browser…</div>
        <div class="btn-row">
          <button class="btn btn-done flex1" disabled style="opacity:0.6;cursor:default">⏳ Resuming…</button>
        </div>
      </div>`;

  } else if (s === 'waiting_human' || s === 'needs_human') {
    const reasonHtml = w.reason
      ? `<div class="hitl-reason">⚠ ${esc(w.reason)}</div>` : '';
    const instrHtml = w.instructions
      ? `<div class="hitl-instructions">${esc(w.instructions)}</div>`
      : `<div class="hitl-instructions">Complete the action in the Chrome window, then click Done.</div>`;

    const miniOpen    = expandedHitl.has(wid);
    const streamActive = !!taskEventSources[wid];
    const miniHtml = miniOpen ? `
      <div class="task-area mt8">
        <textarea id="ta-${wid}" placeholder="Describe what the mini assistant should do…"></textarea>
        <div class="btn-row mt6">
          <button class="btn btn-run btn-sm" data-action="run-task" data-wid="${wid}"
            ${streamActive ? 'disabled' : ''}>▶ Run</button>
          <button class="btn btn-mini" data-action="collapse-mini" data-wid="${wid}">✕ Close</button>
        </div>
        <div id="out-${wid}" class="output-area"></div>
      </div>` : '';

    bodyHtml = `
      <div class="card-body">
        ${reasonHtml}
        ${instrHtml}
        <div class="btn-row">
          <button class="btn btn-done flex1" data-action="done" data-wid="${wid}">✓ Done — Resume Agent</button>
          <button class="btn btn-mini" data-action="toggle-mini" data-wid="${wid}">▶ Mini Task</button>
        </div>
        ${miniHtml}
      </div>`;
  }

  // Yellow = "this is the Chrome window I'm currently looking at" (permanent, from storage)
  // Purple = "this window was recently brought to front via the focus button" (fades after TTL)
  const isSelf   = myWorkerId !== null && myWorkerId === wid;
  const widStyle = isSelf
    ? ' style="background:#eab308;color:#000;border-color:#ca9a08"'
    : isFocused
      ? ' style="background:#7c3aed;color:#fff;border-color:#a855f7"'
      : '';

  return `
    <div class="worker-card ${borderClass}">
      <div class="card-top" data-action="focus" data-wid="${wid}" title="Click to bring Chrome window to front">
        <div class="wid-label"${widStyle}>W${wid}</div>
        <div class="status-dot ${dotClass}"></div>
        <div class="job-info">
          <div class="job-title">${esc(w.jobTitle || '—')}</div>
          <div class="job-meta">
            <span class="status-label ${statusClass}">${statusText}</span>
            ${w.noHitl ? ' · <span title="--no-hitl mode: pauses auto-park instead of waiting" style="color:#fbbf24">⏭ no-hitl</span>' : ''}
            ${w.jobCompany ? ` · ${esc(w.jobCompany)}` : ''}${w.jobSite ? ` · ${esc(w.jobSite)}` : ''}
          </div>
        </div>
        ${scoreHtml}
        <div class="focus-hint">↗</div>
      </div>
      ${bodyHtml}
    </div>`;
}

function updateWindowLabel() {
  const el = document.getElementById('window-label');
  if (!el) return;
  if (myWorkerId !== null) {
    el.textContent = `on: W${myWorkerId}`;
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
}

function updateSummary() {
  const el = document.getElementById('summary');
  if (!workers.length) { el.textContent = 'no workers'; return; }

  const hitl    = workers.filter(w => w.status === 'waiting_human' || w.status === 'needs_human').length;
  const paused  = workers.filter(w => w.status === 'paused_by_user').length;
  const applying = workers.filter(w => w.status === 'applying').length;

  const parts = [];
  if (hitl)    parts.push(`${hitl} waiting`);
  if (paused)  parts.push(`${paused} paused`);
  if (applying) parts.push(`${applying} applying`);
  el.textContent = parts.join(', ') || `${workers.length} online`;
}

// ── Event delegation ──────────────────────────────────────────────────────────

function attachListeners() {
  document.querySelectorAll('[data-action]').forEach(el => {
    el.addEventListener('click', handleAction);
  });
}

async function handleAction(e) {
  const btn    = e.currentTarget;
  const action = btn.dataset.action;
  const wid    = parseInt(btn.dataset.wid, 10);

  // Focus: bring the worker's Chrome window to foreground.
  // Track locally so the WID label highlights immediately (no need to wait
  // for the next poll to get the server's lastFocused timestamp).
  if (action === 'focus') {
    lastFocusedWid = wid;
    lastFocusedAt  = Date.now() / 1000;
    renderAll(workers);   // immediate highlight update
    apiCall(wid, '/api/focus').catch(() => {});
    return;
  }

  // Expand/collapse toggles (no API call needed)
  if (action === 'toggle-mini') {
    if (expandedHitl.has(wid)) expandedHitl.delete(wid);
    else expandedHitl.add(wid);
    renderAll(workers);
    return;
  }
  if (action === 'collapse-mini') {
    expandedHitl.delete(wid);
    renderAll(workers);
    return;
  }

  btn.disabled = true;

  try {
    if (action === 'takeover') {
      await apiCall(wid, '/api/takeover', { method: 'POST', body: '{}' });
      triggerPoll();

    } else if (action === 'done') {
      // Fire the HITL done event — agent resumes from where it was parked
      await apiCall(wid, '/api/done', { method: 'POST', body: '{}' });
      expandedHitl.delete(wid);
      delete savedInputs[wid];
      triggerPoll();

    } else if (action === 'handback') {
      const ta      = document.getElementById(`ta-${wid}`);
      const saveChk = document.getElementById(`save-${wid}`);
      const instructions = ta ? ta.value.trim() : '';
      const save         = saveChk ? saveChk.checked : false;

      closeStream(wid);
      delete savedInputs[wid];
      delete savedOutputs[wid];

      await apiCall(wid, '/api/handback', {
        method: 'POST',
        body: JSON.stringify({ instructions, save }),
      });
      triggerPoll();

    } else if (action === 'run-task') {
      const ta           = document.getElementById(`ta-${wid}`);
      const instructions = ta ? ta.value.trim() : '';
      if (!instructions) { btn.disabled = false; return; }

      closeStream(wid);

      const outEl = document.getElementById(`out-${wid}`);
      if (outEl) {
        outEl.textContent = '';
        outEl.classList.add('visible');
        savedOutputs[wid] = { text: '', visible: true };
      }

      const resp = await apiCall(wid, '/api/run-task', {
        method: 'POST',
        body: JSON.stringify({ instructions }),
      });
      if (!resp.ok) {
        if (outEl) outEl.textContent = '[error starting task]';
        btn.disabled = false;
        return;
      }

      const es = new EventSource(workerUrl(wid, '/api/task-stream'));
      taskEventSources[wid] = es;

      es.onmessage = ev => {
        if (ev.data === '[DONE]') {
          appendOutput(wid, '\n[Task complete]');
          closeStream(wid);
          if (btn.isConnected) btn.disabled = false;
        } else {
          appendOutput(wid, ev.data.replace(/\\n/g, '\n') + '\n');
        }
      };
      es.onerror = () => {
        closeStream(wid);
        if (btn.isConnected) btn.disabled = false;
      };
      return; // keep button disabled until stream ends
    }

  } catch (err) {
    console.error(`Action "${action}" on W${wid} failed:`, err);
  }

  btn.disabled = false;
}

function appendOutput(wid, text) {
  // Update the live DOM element (if present) and the persisted buffer
  const outEl = document.getElementById(`out-${wid}`);
  if (outEl) {
    outEl.textContent += text;
    outEl.scrollTop = outEl.scrollHeight;
  }
  if (!savedOutputs[wid]) savedOutputs[wid] = { text: '', visible: true };
  savedOutputs[wid].text += text;
  savedOutputs[wid].visible = true;
}

function closeStream(wid) {
  if (taskEventSources[wid]) {
    taskEventSources[wid].close();
    delete taskEventSources[wid];
  }
}

// ── Add to Pipeline ───────────────────────────────────────────────────────────

/**
 * POST the given URL+title to the job capture endpoint on any live worker.
 * Returns the parsed JSON response.
 */
async function captureJobUrl(url, title) {
  for (const w of workers) {
    try {
      const resp = await apiCall(w.workerId, '/api/add-job', {
        method: 'POST',
        body: JSON.stringify({ url, title }),
      });
      if (resp.ok) return resp.json();
    } catch { /* skip */ }
  }
  throw new Error('No active workers. Run: applypilot apply');
}

document.getElementById('btn-add-job').addEventListener('click', async () => {
  const btn = document.getElementById('btn-add-job');
  const fb  = document.getElementById('add-feedback');
  btn.disabled = true;
  fb.className = 'feedback hidden';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const url   = tab?.url   || '';
    const title = tab?.title || '';

    if (!url.startsWith('http')) {
      fb.className = 'feedback error';
      fb.textContent = 'Not a valid job page URL';
      btn.disabled = false;
      return;
    }

    const data = await captureJobUrl(url, title);

    if (data.status === 'queued') {
      fb.className = 'feedback success';
      fb.textContent = '✓ Added to pipeline';
    } else if (data.status === 'exists') {
      fb.className = 'feedback info';
      fb.textContent = `Already in pipeline (${data.applyStatus || 'pending'})`;
      btn.disabled = false;
    } else {
      throw new Error('Unexpected response');
    }
  } catch (err) {
    fb.className = 'feedback error';
    fb.textContent = err.message;
    btn.disabled = false;
  }
});

// ── Polling ───────────────────────────────────────────────────────────────────

function triggerPoll() {
  chrome.runtime.sendMessage({ type: 'poll' });
}

// ── Live refresh indicator ─────────────────────────────────────────────────────

let _lastRefreshAt = 0;

function _updateRefreshAge() {
  _lastRefreshAt = Date.now();
  const el = document.getElementById('btn-refresh');
  if (el) el.title = 'Auto-refreshing every 2s (last: just now)';
}

// Updates the refresh button tooltip with the elapsed time since last fetch.
// Called every second so the "Ns ago" label stays current.
setInterval(() => {
  if (!_lastRefreshAt) return;
  const el = document.getElementById('btn-refresh');
  if (!el) return;
  const secs = Math.round((Date.now() - _lastRefreshAt) / 1000);
  el.title = secs <= 1
    ? 'Auto-refreshing every 2s (last: just now)'
    : `Auto-refreshing every 2s (last: ${secs}s ago)`;
}, 1000);

document.getElementById('btn-refresh').addEventListener('click', async () => {
  // Manual refresh: bypass the 2s timer and fetch immediately.
  await refreshFromServers();
  triggerPoll(); // also tell background to update its storage cache
});

// ── Storage sync ──────────────────────────────────────────────────────────────

chrome.storage.onChanged.addListener(changes => {
  if (changes.myWorkerId) {
    myWorkerId = changes.myWorkerId.newValue ?? null;
    updateWindowLabel();
  }
  if (changes.workers) renderAll(changes.workers.newValue);
});

// Initial load — read both worker state and own worker ID
chrome.storage.local.get(['workers', 'myWorkerId'], data => {
  // Only update myWorkerId from storage if it has a value — don't overwrite
  // the WORKER_CONFIG sync fast-path with null if background.js hasn't run yet.
  if (data.myWorkerId != null) myWorkerId = data.myWorkerId;
  renderAll(data.workers);
});

// ── Auto-refresh while popup is open ──────────────────────────────────────────
// Poll directly from worker servers every 2s, bypassing chrome.storage and the
// background service worker entirely. Chrome MV3 service workers are killed
// after ~30s of inactivity, so relying on storage.onChanged for live updates is
// unreliable. This loop keeps the popup always current regardless of SW state.
// Smart re-render (above) ensures textareas are not interrupted mid-typing.
refreshFromServers();                    // immediate fresh fetch when popup opens
setInterval(refreshFromServers, 2000);  // keep refreshing every 2s

// ── Jobs tab ───────────────────────────────────────────────────────────────────

let activeTab = 'workers';

function setTab(name) {
  activeTab = name;
  document.getElementById('tab-workers').classList.toggle('active', name === 'workers');
  document.getElementById('tab-jobs').classList.toggle('active', name === 'jobs');
  document.getElementById('workers-panel').style.display = name === 'workers' ? '' : 'none';
  document.getElementById('jobs-panel').style.display    = name === 'jobs'    ? '' : 'none';
  if (name === 'jobs') loadJobs();
}

document.getElementById('tab-workers').addEventListener('click', () => setTab('workers'));
document.getElementById('tab-jobs').addEventListener('click',    () => setTab('jobs'));

// Stage badge helper
function stageBadge(job) {
  const cat = job.apply_category || '';
  const st  = job.apply_status   || '';
  const hasResume = !!job.tailored_resume_path;
  const hasCover  = !!job.cover_letter_path;

  if (cat === 'needs_human' || st === 'needs_human')
    return ['stage-hitl', 'HITL'];
  if (cat === 'blocked_auth' || cat === 'blocked_technical')
    return ['stage-blocked', 'Blocked'];
  if (st === 'failed' || cat.startsWith('archived'))
    return ['stage-error', 'Error'];
  if (hasResume && hasCover)
    return ['stage-ready', 'Ready'];
  if (hasResume)
    return ['stage-pending', 'Tailored'];
  return ['stage-pending', 'Pending'];
}

function renderJobCard(job) {
  const [badgeCls, badgeTxt] = stageBadge(job);
  const score = job.fit_score ? `${job.fit_score}/10` : '?';
  const company = job.company || job.site || '';
  const errNote = job.apply_error
    ? `<div class="job-card-meta" style="color:#fca5a5">${esc(job.apply_error.slice(0, 60))}</div>` : '';
  const encodedUrl = encodeURIComponent(job.url);
  return `
    <div class="job-card" id="jcard-${encodedUrl}">
      <div class="job-card-top">
        <div class="job-card-info">
          <div class="job-card-title">
            <a href="${esc(job.url)}" target="_blank">${esc(job.title || 'Unknown')}</a>
          </div>
          <div class="job-card-meta">${esc(company)}</div>
          ${errNote}
        </div>
        <div class="score-badge">${esc(score)}</div>
        <span class="stage-badge ${badgeCls}">${badgeTxt}</span>
      </div>
      <div class="btn-row">
        <button class="btn btn-sm btn-applied flex1" data-job-action="applied" data-url="${esc(job.url)}">✓ Applied</button>
        <button class="btn btn-sm btn-skip    flex1" data-job-action="skip"    data-url="${esc(job.url)}">✕ Skip</button>
        <button class="btn btn-sm btn-reset   flex1" data-job-action="reset"   data-url="${esc(job.url)}">↺ Reset</button>
        <button class="btn btn-sm btn-error   flex1" data-job-action="error"   data-url="${esc(job.url)}">⚠ Error</button>
      </div>
      <div id="jfb-${encodedUrl}" class="job-feedback"></div>
    </div>`;
}

async function loadJobs() {
  const list = document.getElementById('job-list');
  list.innerHTML = '<div class="jobs-loading">Loading…</div>';
  try {
    let resp = null;
    for (const w of workers) {
      try {
        const r = await apiCall(w.workerId, '/api/jobs?limit=50');
        if (r.ok) { resp = r; break; }
      } catch { /* skip */ }
    }
    if (!resp) throw new Error('No active workers. Run: applypilot apply');
    const data = await resp.json();
    const jobs = data.jobs || [];
    if (!jobs.length) {
      list.innerHTML = '<div class="jobs-empty">No actionable jobs found.<br><code>applypilot run score tailor cover</code></div>';
    } else {
      list.innerHTML = jobs.map(renderJobCard).join('');
      list.querySelectorAll('[data-job-action]').forEach(btn => {
        btn.addEventListener('click', handleJobAction);
      });
    }
  } catch (err) {
    list.innerHTML = `<div class="jobs-empty">Could not load jobs.<br><code>applypilot apply</code><br><span style="color:#475569;font-size:10px">${esc(err.message)}</span></div>`;
  }
}

async function handleJobAction(e) {
  const btn    = e.currentTarget;
  const action = btn.dataset.jobAction;
  const url    = btn.dataset.url;
  const encodedUrl = encodeURIComponent(url);
  const card   = document.getElementById(`jcard-${encodedUrl}`);
  const fb     = document.getElementById(`jfb-${encodedUrl}`);

  // Disable all buttons on this card while the request is in flight
  card.querySelectorAll('[data-job-action]').forEach(b => b.disabled = true);

  const labels = { applied: 'Applied ✓', skip: 'Skipped', reset: 'Reset ↺', error: 'Marked Error' };
  const colors = { applied: '#6ee7b7', skip: '#fca5a5', reset: '#93c5fd', error: '#fcd34d' };

  try {
    let ok = false;
    for (const w of workers) {
      try {
        const r = await apiCall(w.workerId, '/api/jobs/mark', {
          method: 'POST',
          body: JSON.stringify({ url, action }),
        });
        if (r.ok) { ok = true; break; }
      } catch { /* skip */ }
    }

    if (!ok) throw new Error('No active workers. Run: applypilot apply');

    // Visual feedback — hide card after brief delay for destructive actions
    if (fb) {
      fb.textContent = labels[action] || action;
      fb.style.color = colors[action] || '#e2e8f0';
      fb.style.background = 'rgba(255,255,255,0.05)';
      fb.classList.add('visible');
    }
    if (action !== 'reset') {
      setTimeout(() => { if (card) card.style.display = 'none'; }, 1200);
    } else {
      // Reset: re-enable buttons so user can take another action
      setTimeout(() => {
        card.querySelectorAll('[data-job-action]').forEach(b => b.disabled = false);
        if (fb) fb.classList.remove('visible');
      }, 1500);
    }
  } catch (err) {
    if (fb) {
      fb.textContent = `Error: ${err.message}`;
      fb.style.color = '#fca5a5';
      fb.style.background = 'rgba(255,0,0,0.1)';
      fb.classList.add('visible');
    }
    card.querySelectorAll('[data-job-action]').forEach(b => b.disabled = false);
  }
}
