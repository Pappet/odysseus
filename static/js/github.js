// static/js/github.js — GitHub panel: connect, browse repos / issues / PRs / files.
// Self-contained `.modal` module (mirrors calendar.js), no build step.

let _modal = null;
let _state = { connected: false, user: null, repos: [], activeRepo: null, tab: 'issues' };

function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function _api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  let data = null;
  try { data = await r.json(); } catch { /* no body */ }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `HTTP ${r.status}`);
  return data;
}

function _getModal() {
  if (_modal) return _modal;
  _modal = document.createElement('div');
  _modal.id = 'github-modal';
  _modal.className = 'modal';
  _modal.style.display = 'none';
  _modal.innerHTML = `
    <div class="modal-content" style="max-width:860px;width:92vw;height:80vh;display:flex;flex-direction:column">
      <div class="modal-header">
        <h4><svg width="15" height="15" viewBox="0 0 16 16" fill="currentColor" style="vertical-align:-2px;margin-right:6px"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>GitHub</h4>
        <button class="close-btn" id="gh-close">✖</button>
      </div>
      <div class="modal-body" id="gh-body" style="flex:1;overflow:auto;padding:16px"></div>
    </div>`;
  document.body.appendChild(_modal);
  _modal.querySelector('#gh-close').addEventListener('click', closeGithub);
  _modal.addEventListener('click', (e) => { if (e.target === _modal) closeGithub(); });
  return _modal;
}

function _body() { return document.getElementById('gh-body'); }

function _renderConnect(status) {
  const dev = status && status.device_flow_available;
  _body().innerHTML = `
    <div style="max-width:520px;margin:0 auto">
      <h3 style="margin-top:0">Connect GitHub</h3>
      <p style="opacity:.8;line-height:1.5">
        Paste a <b>fine-grained Personal Access Token</b>. Recommended for write
        access (create branches, commits, PRs, comments). Scope it per-repo with
        an expiry in GitHub → Settings → Developer settings → Personal access tokens.
        Suggested permissions: <code>Contents</code>, <code>Issues</code>,
        <code>Pull requests</code> (read/write).
      </p>
      <div style="display:flex;gap:8px;margin:12px 0">
        <input id="gh-token" type="password" placeholder="github_pat_..."
          style="flex:1;padding:8px;border-radius:6px;border:1px solid var(--border,#444);background:var(--input-bg,#1c1c1c);color:inherit">
        <button class="btn" id="gh-connect-btn">Connect</button>
      </div>
      ${dev ? `<div style="margin-top:8px"><button class="btn-secondary" id="gh-device-btn">Or connect with one-click OAuth</button></div>` : ''}
      <p style="opacity:.55;font-size:12px;margin-top:18px">Tokens are stored encrypted at rest and never shown again.</p>
      <div id="gh-connect-err" style="color:#e66;margin-top:8px"></div>
    </div>`;
  document.getElementById('gh-connect-btn').addEventListener('click', async () => {
    const token = document.getElementById('gh-token').value.trim();
    const err = document.getElementById('gh-connect-err');
    err.textContent = '';
    if (!token) { err.textContent = 'Enter a token.'; return; }
    try {
      await _api('/api/github/connect', { method: 'POST', body: JSON.stringify({ token }) });
      await _load();
    } catch (e) { err.textContent = e.message; }
  });
  const devBtn = document.getElementById('gh-device-btn');
  if (devBtn) devBtn.addEventListener('click', () => _startDeviceFlow());
}

async function _startDeviceFlow() {
  const err = document.getElementById('gh-connect-err');
  try {
    const fd = new FormData();
    const start = await fetch('/api/github/device/start', { method: 'POST', body: fd }).then(r => r.json());
    if (start.verification_uri_complete) window.open(start.verification_uri_complete, '_blank');
    err.style.color = ''; err.textContent =
      `Authorize in the opened tab. Code: ${start.user_code || ''} — waiting...`;
    const poll = async () => {
      const f = new FormData(); f.append('poll_id', start.poll_id);
      const res = await fetch('/api/github/device/poll', { method: 'POST', body: f }).then(r => r.json());
      if (res.status === 'authorized') { await _load(); return; }
      if (res.status === 'failed') { err.style.color = '#e66'; err.textContent = `Failed: ${res.error}`; return; }
      setTimeout(poll, (start.interval || 5) * 1000);
    };
    setTimeout(poll, (start.interval || 5) * 1000);
  } catch (e) { err.style.color = '#e66'; err.textContent = e.message; }
}

function _renderRepoList() {
  const u = _state.user || {};
  const rateTxt = _state.rate ? ` · API ${_state.rate.remaining}/${_state.rate.limit}` : '';
  const rows = _state.repos.map(r => `
    <div class="gh-repo-row" data-repo="${_esc(r.full_name)}"
      style="padding:10px 8px;border-bottom:1px solid var(--border,#333);cursor:pointer">
      <div style="display:flex;align-items:center;gap:8px">
        <b>${_esc(r.full_name)}</b>
        <span style="font-size:11px;opacity:.6">${r.private ? '🔒 private' : 'public'}</span>
        ${r.fork ? '<span style="font-size:11px;opacity:.5">fork</span>' : ''}
      </div>
      <div style="opacity:.7;font-size:13px;margin-top:2px">${_esc(r.description || '')}</div>
      <div style="opacity:.5;font-size:11px;margin-top:3px">
        ${r.language ? _esc(r.language) + ' · ' : ''}★ ${r.stars} · ${r.open_issues} open issues
      </div>
    </div>`).join('');
  _body().innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div>Signed in as <b>${_esc(u.login || '?')}</b><span style="opacity:.5;font-size:12px">${rateTxt}</span></div>
      <button class="btn-secondary" id="gh-disconnect">Disconnect</button>
    </div>
    <input id="gh-filter" placeholder="Filter repositories..."
      style="width:100%;padding:8px;margin-bottom:8px;border-radius:6px;border:1px solid var(--border,#444);background:var(--input-bg,#1c1c1c);color:inherit">
    <div id="gh-repo-list">${rows || '<p style="opacity:.6">No repositories found.</p>'}</div>`;
  document.getElementById('gh-disconnect').addEventListener('click', async () => {
    await _api('/api/github/connect', { method: 'DELETE' });
    await _load();
  });
  document.getElementById('gh-filter').addEventListener('input', (e) => {
    const q = e.target.value.toLowerCase();
    _body().querySelectorAll('.gh-repo-row').forEach(row => {
      row.style.display = row.dataset.repo.toLowerCase().includes(q) ? '' : 'none';
    });
  });
  _body().querySelectorAll('.gh-repo-row').forEach(row =>
    row.addEventListener('click', () => _openRepo(row.dataset.repo)));
}

async function _openRepo(fullName) {
  _state.activeRepo = fullName;
  _state.tab = 'issues';
  await _renderRepo();
}

async function _renderRepo() {
  const repo = _state.activeRepo;
  _body().innerHTML = `
    <button class="btn-secondary" id="gh-back" style="margin-bottom:10px">← Repos</button>
    <h3 style="margin:4px 0">${_esc(repo)}</h3>
    <div style="display:flex;gap:6px;margin:10px 0">
      ${['issues', 'pulls', 'files'].map(t =>
        `<button class="btn-secondary gh-tab" data-tab="${t}" style="${t === _state.tab ? 'font-weight:700;opacity:1' : 'opacity:.6'}">${t === 'pulls' ? 'Pull Requests' : t[0].toUpperCase() + t.slice(1)}</button>`).join('')}
    </div>
    <div id="gh-tab-body"><div style="opacity:.6">Loading…</div></div>`;
  document.getElementById('gh-back').addEventListener('click', () => _renderRepoList());
  _body().querySelectorAll('.gh-tab').forEach(b =>
    b.addEventListener('click', () => { _state.tab = b.dataset.tab; _renderRepo(); }));
  const tb = document.getElementById('gh-tab-body');
  try {
    if (_state.tab === 'issues') {
      const d = await _api(`/api/github/repos/${repo}/issues`);
      tb.innerHTML = (d.issues || []).map(i =>
        `<div style="padding:8px 0;border-bottom:1px solid var(--border,#333)">
           <a href="${_esc(i.html_url)}" target="_blank" style="color:inherit"><b>#${i.number}</b> ${_esc(i.title)}</a>
           <div style="opacity:.5;font-size:11px">${_esc(i.user || '')} · ${i.comments || 0} comments</div>
         </div>`).join('') || '<p style="opacity:.6">No open issues.</p>';
    } else if (_state.tab === 'pulls') {
      const d = await _api(`/api/github/repos/${repo}/pulls`);
      tb.innerHTML = (d.pulls || []).map(p =>
        `<div style="padding:8px 0;border-bottom:1px solid var(--border,#333)">
           <a href="${_esc(p.html_url)}" target="_blank" style="color:inherit"><b>#${p.number}</b> ${_esc(p.title)}</a>
           <div style="opacity:.5;font-size:11px">${_esc(p.user || '')} · ${_esc(p.head)} → ${_esc(p.base)}${p.draft ? ' · draft' : ''}</div>
         </div>`).join('') || '<p style="opacity:.6">No open pull requests.</p>';
    } else {
      await _renderFiles(repo, '');
    }
  } catch (e) {
    tb.innerHTML = `<p style="color:#e66">${_esc(e.message)}</p>`;
  }
}

async function _renderFiles(repo, path) {
  const tb = document.getElementById('gh-tab-body');
  const d = await _api(`/api/github/repos/${repo}/contents/${path}`);
  if (d.type === 'dir') {
    const up = path ? `<div class="gh-entry" data-path="${_esc(path.split('/').slice(0, -1).join('/'))}" style="cursor:pointer;padding:5px 0">📁 ..</div>` : '';
    tb.innerHTML = up + d.entries.map(e =>
      `<div class="gh-entry" data-path="${_esc(e.path)}" data-type="${e.type}" style="cursor:pointer;padding:5px 0">
         ${e.type === 'dir' ? '📁' : '📄'} ${_esc(e.name)}</div>`).join('');
    tb.querySelectorAll('.gh-entry').forEach(el => el.addEventListener('click', () => {
      if (el.dataset.type === 'file') _renderFiles(repo, el.dataset.path);
      else _renderFiles(repo, el.dataset.path);
    }));
  } else {
    tb.innerHTML = `<button class="btn-secondary" id="gh-file-back">← back</button>
      <h4 style="margin:8px 0">${_esc(d.path)}</h4>
      <pre style="white-space:pre-wrap;background:var(--input-bg,#1c1c1c);padding:12px;border-radius:6px;max-height:50vh;overflow:auto;font-size:12px">${_esc((d.content || '').slice(0, 50000))}</pre>`;
    document.getElementById('gh-file-back').addEventListener('click', () =>
      _renderFiles(repo, d.path.split('/').slice(0, -1).join('/')));
  }
}

async function _load() {
  _body().innerHTML = '<div style="opacity:.6;text-align:center;margin-top:40px">Loading…</div>';
  let status;
  try { status = await _api('/api/github/status'); }
  catch (e) { _body().innerHTML = `<p style="color:#e66">${_esc(e.message)}</p>`; return; }
  _state.connected = !!status.connected;
  if (!status.connected) { _renderConnect(status); return; }
  _state.user = status.user; _state.rate = status.rate_limit;
  try {
    const d = await _api('/api/github/repos');
    _state.repos = d.repos || [];
  } catch (e) { _body().innerHTML = `<p style="color:#e66">${_esc(e.message)}</p>`; return; }
  _renderRepoList();
}

export function openGithub() {
  const m = _getModal();
  m.style.display = 'flex';
  document.getElementById('tool-github-btn')?.classList.add('active');
  if (window.Modals && window.Modals.register) {
    window.Modals.register('github-modal', { sidebarBtnId: 'tool-github-btn', closeFn: closeGithub, restoreFn: () => {} });
  }
  _load();
}

export function closeGithub() {
  if (_modal) _modal.style.display = 'none';
  document.getElementById('tool-github-btn')?.classList.remove('active');
}

function _init() {
  document.getElementById('tool-github-btn')?.addEventListener('click', openGithub);
  // Deep link: /github opens the panel on load.
  if (location.pathname.replace(/\/$/, '') === '/github') setTimeout(openGithub, 300);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _init);
else _init();

window.openGithub = openGithub;
