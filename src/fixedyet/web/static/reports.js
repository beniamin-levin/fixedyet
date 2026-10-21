/* FixedYet reports page: every saved report in a list, one report in detail, and the maintainer's
 * controls (backport a fix to a draft pull request, hide a report). No build step.
 *
 * The helpers and the report renderers are in common.js (FY), shared with the demo page.
 * Everything that comes from the server is untrusted and goes into the page as text, never as HTML.
 *
 * Every URL this file requests is relative and has no leading slash (api/reports), so the page
 * works at the root of a host and under a prefix such as https://benlevin.fyi/fixedyet/.
 * A report is shareable as reports#<id>.
 *
 * Maintainer mode: the token is kept in localStorage (when the browser allows it) and sent as
 * "Authorization: Bearer <token>". It is never written into the page. The server answers 404 to
 * every maintainer endpoint when maintainer mode is off, and this page then says so.
 */
(() => {
  'use strict';

  const {
    $, el, link, gh, refLink, commitLink, linkify, formatCost, formatDuration, formatDate,
    copyText, renderReport, renderBackport, VERDICTS, BACKPORT_STATUS,
  } = globalThis.FY;

  const TOKEN_KEY = 'fixedyet-maintainer-token';
  const JOB_KEY = 'fixedyet-backport-job:';
  const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/;
  const SHOWN_VERDICTS = ['fixed_on_main', 'open_bug', 'not_reproduced'];
  const EXAMPLE_BACKPORT = 'real-backport-pytest-dev-pytest-13784';
  const EXAMPLE_PR = 'https://github.com/beniamin-levin/pytest/pull/1';

  const state = {
    reports: [], filter: 'all', query: '', token: '', admin: false,
    nav: 0, scroll: 0, detail: null, flash: null, source: null, pollTimer: null,
  };

  // ---------- storage (can be blocked or empty: a private window, cleared site data) ----------

  const store = {
    get(kind, key) {
      try { return window[kind].getItem(key) || ''; } catch { return ''; }
    },
    set(kind, key, value) {
      try {
        if (value) window[kind].setItem(key, value);
        else window[kind].removeItem(key);
      } catch { /* nothing to do: the page works without storage */ }
    },
  };

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const res = await fetch(path, { ...options, headers });
    let body = null;
    try { body = await res.json(); } catch { /* an empty or non-JSON body */ }
    return { status: res.status, ok: res.ok, body };
  }

  const notice = (text, kind) => el('div', { class: `notice${kind === 'error' ? ' error' : ''}`, text });

  function verdictMeta(r) {
    return VERDICTS[r.verdict] || BACKPORT_STATUS[r.verdict] || { label: r.verdict, tone: 'muted' };
  }

  // ---------- the list ----------

  function matchesFilter(r) {
    if (state.filter === 'all') return true;
    if (state.filter === 'other') return !SHOWN_VERDICTS.includes(r.verdict);
    return r.verdict === state.filter;
  }

  function matchesQuery(r) {
    const words = state.query.toLowerCase().split(/\s+/).filter(Boolean);
    const haystack = `${r.repo || ''}#${r.number ?? ''} ${r.title || ''}`.toLowerCase();
    return words.every((w) => haystack.includes(w));
  }

  function reportRow(r) {
    const meta = verdictMeta(r);
    const tags = el('div', { class: 'rr-tags' },
      el('span', { class: `badge ${meta.tone}`, text: meta.label }),
      r.confidence ? el('span', { class: 'badge outline', text: `${r.confidence === 'high' ? 'High' : 'Low'} confidence` }) : null,
      r.example ? el('span', { class: 'badge outline', text: 'Example' }) : null,
      r.hidden ? el('span', { class: 'badge muted', text: 'hidden' }) : null);
    const ref = r.repo && r.number ? `${r.repo}#${r.number}` : r.issue_url;
    const sub = el('div', { class: 'rr-sub' },
      link(r.issue_url, ref, 'rr-issue'),
      r.reported_version ? el('span', null, 'reported ', refLink(r.repo, r.reported_version)) : null,
      r.fixing_commit ? el('span', null, 'fix ', commitLink(r.repo, r.fixing_commit)) : null);
    const side = el('div', { class: 'rr-meta' },
      el('span', { class: 'rr-date', text: formatDate(r.created_at) }),
      el('span', { class: 'rr-cost', text: formatCost(r.cost_usd) }),
      r.duration_s ? el('span', { class: 'rr-time', text: formatDuration(r.duration_s) }) : null,
      r.backport && r.backport.pr_url ? link(r.backport.pr_url, 'Draft PR', 'rr-pr') : null);
    const item = el('li', { class: 'report-row', 'data-id': r.id },
      el('div', { class: 'rr-main' },
        tags,
        el('a', { class: 'rr-title', href: `#${encodeURIComponent(r.id)}`, text: r.title || 'Untitled issue' }),
        sub),
      side);
    item.addEventListener('click', (event) => {
      if (!event.target.closest('a, button')) location.hash = encodeURIComponent(r.id);
    });
    return item;
  }

  function drawList() {
    const byQuery = state.reports.filter(matchesQuery);
    for (const chip of document.querySelectorAll('.chip-btn')) {
      const name = chip.dataset.filter;
      chip.setAttribute('aria-pressed', String(name === state.filter));
      const count = byQuery.filter((r) => (name === 'all' ? true : name === 'other' ? !SHOWN_VERDICTS.includes(r.verdict) : r.verdict === name)).length;
      let badge = chip.querySelector('.count');
      if (!badge) { badge = el('span', { class: 'count' }); chip.append(badge); }
      badge.textContent = String(count);
    }
    const rows = byQuery.filter(matchesFilter);
    $('#report-list').replaceChildren(...rows.map(reportRow));
    $('#list-status').textContent = !state.reports.length
      ? 'No reports yet.'
      : (rows.length ? `${rows.length} of ${state.reports.length} reports` : 'No report matches. Try another filter or search.');
  }

  async function loadList() {
    $('#list-status').textContent = 'Loading the reports…';
    try {
      const res = await api('api/reports');
      if (!res.ok || !Array.isArray(res.body)) throw new Error('bad answer');
      state.reports = res.body;
    } catch {
      state.reports = [];
      $('#report-list').replaceChildren();
      $('#list-status').textContent = 'Could not load the reports. Reload the page to try again.';
      return;
    }
    drawList();
    if (state.scroll) { window.scrollTo(0, state.scroll); state.scroll = 0; }
  }

  // ---------- one report ----------

  function prLabel(url) {
    const m = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/.exec(url);
    return m ? `${m[1]}#${m[2]}` : url;
  }

  function stopWatching() {
    if (state.source) { state.source.close(); state.source = null; }
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  function reportNode(d) {
    const note = d.example
      ? (d.note || (d.illustrative
        ? 'Illustrative sample, written by hand to show the report format. It is not the output of a live run.'
        : 'Saved report from a real run of FixedYet.'))
      : '';
    if (d.assessment) return renderReport({ assessment: d.assessment, markdown: d.markdown }, { banner: note });
    if (d.backport && d.backport.result) return renderBackport(d.backport.result, d.markdown, { banner: note });
    return notice('This report has no content to show.', 'error');
  }

  function detailBar(d) {
    const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy link' });
    copy.addEventListener('click', () => copyText(location.href, copy));
    const bar = el('div', { class: 'detail-bar' },
      d.example ? el('span', { class: 'badge outline', text: 'Example' }) : null,
      d.hidden ? el('span', { class: 'badge muted', text: 'hidden' }) : null,
      el('span', { class: 'spacer' }),
      copy);
    if (state.admin) {
      const hide = el('button', { class: 'btn small', type: 'button', text: d.hidden ? 'Unhide' : 'Hide' });
      hide.addEventListener('click', () => setHidden(d, !d.hidden, hide));
      bar.append(hide);
    }
    return bar;
  }

  async function setHidden(d, hidden, button) {
    button.disabled = true;
    try {
      const res = await api(`api/reports/${encodeURIComponent(d.id)}/hide`, { method: hidden ? 'POST' : 'DELETE' });
      if (res.status === 401) { signOut('Your maintainer token was not accepted. Sign in again.'); return; }
      if (!res.ok) { button.disabled = false; button.textContent = 'Failed, try again'; return; }
    } catch {
      button.disabled = false;
      button.textContent = 'Failed, try again';
      return;
    }
    await loadList();
    await showDetail(d.id, { keepScroll: true });
  }

  // What a visitor sees when they ask for a pull request: where to get one, not a server call.
  function visitorPanel(d) {
    const repo = gh.repoOf(d.summary.issue_url);
    const number = /\/issues\/(\d+)$/.exec(d.summary.issue_url || '');
    const url = repo && number ? `https://github.com/${repo}/issues/${number[1]}` : '<issue-url>';
    const command = `fixedyet backport ${url} --open-pr`;
    const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy' });
    copy.addEventListener('click', () => copyText(command, copy));
    const signIn = el('button', { class: 'linklike', type: 'button', text: 'Maintainer sign-in' });
    signIn.addEventListener('click', openSignIn);
    return el('div', { class: 'panel', role: 'region', 'aria-label': 'How to open a pull request' },
      el('p', null, el('strong', { text: 'Opening pull requests on this demo is limited to its maintainer.' })),
      el('p', { text: 'To do it yourself, install FixedYet and run this. It reproduces the backport in your own sandbox and opens a draft pull request in your fork.' }),
      el('div', { class: 'code-head' }, el('span', { class: 'code-path', text: 'shell' }), copy),
      el('pre', { class: 'code', tabindex: '0' }, el('code', { text: command })),
      el('p', null, 'A verified example: ',
        el('a', { href: `#${EXAMPLE_BACKPORT}`, text: 'the backport report' }),
        ' and ', link(EXAMPLE_PR, 'its draft pull request'), '.'),
      el('p', { class: 'muted small' }, 'Are you the maintainer? Use ', signIn, '.'));
  }

  function backportCard(d) {
    const record = d.backport;
    const pr = record && record.pr_url;
    const card = el('section', { class: 'card backport-card' });
    card.append(el('div', { class: 'card-head' }, el('h3', { text: d.kind === 'backport' ? 'Pull request' : 'Backport' })));
    const flashed = state.flash && state.flash.id === d.id;
    if (flashed) {
      card.append(notice(state.flash.text, state.flash.kind));
      state.flash = null;
    }
    if (pr) {
      card.append(el('p', { class: 'pr-line' }, 'Draft PR: ', link(pr, prLabel(pr), 'pr-link')));
      card.append(el('p', { class: 'muted small', text: 'Opened as a draft in the maintainer\'s fork, not in the upstream project. Its description says it was prepared with AI.' }));
    }
    if (d.kind !== 'backport' && !pr) {
      if (!d.can_backport) return null;
      if (record) {
        const meta = BACKPORT_STATUS[`backport_${record.status}`] || { label: record.status, tone: 'muted' };
        card.append(el('p', null, 'Last try: ', el('span', { class: `badge ${meta.tone}`, text: meta.label }),
          record.result && record.result.reason ? ` ${record.result.reason}` : ''));
        if (record.pr_error && !flashed) card.append(notice(record.pr_error, 'error'));
      }
      const button = el('button', { class: 'btn primary', type: 'button', text: 'Backport fix → draft PR' });
      const area = el('div', { class: 'backport-area' });
      card.append(
        el('p', { class: 'muted', text: state.admin
          ? 'Cherry-picks the fix onto the maintenance branch of the reported version, checks it there, and opens a draft pull request in your fork. It takes several minutes.'
          : 'The fix is known. This cherry-picks it onto the maintenance branch of the reported version, checks it there, and opens a draft pull request.' }),
        button, area);
      button.addEventListener('click', () => {
        if (state.admin) startBackport(d, button, area);
        else if (area.firstChild) area.replaceChildren();
        else area.replaceChildren(visitorPanel(d));
      });
    }
    if (record && record.result && d.kind !== 'backport') {
      card.append(el('details', null,
        el('summary', { text: 'Show the backport report' }),
        renderBackport(record.result, null, {})));
    }
    return card.childElementCount > 1 ? card : null;
  }

  function drawDetail(d) {
    state.detail = d;
    const parts = [detailBar(d)];
    const card = backportCard(d);
    if (card) parts.push(card);
    parts.push(reportNode(d));
    $('#detail').replaceChildren(...parts);
    if (state.admin && d.can_backport) resumeJob(d);
  }

  async function showDetail(id, { keepScroll = false } = {}) {
    const nav = ++state.nav;
    stopWatching();
    $('#list-view').hidden = true;
    $('#detail-view').hidden = false;
    const box = $('#detail');
    if (!keepScroll) {
      box.replaceChildren(el('p', { class: 'muted', text: 'Loading the report…' }));
      window.scrollTo(0, 0);
    }
    let res = null;
    try { res = await api(`api/reports/${encodeURIComponent(id)}`); } catch { /* shown below */ }
    if (nav !== state.nav) return;  // the visitor already went somewhere else
    if (!res) { box.replaceChildren(notice('Could not reach the server. Check your connection and try again.', 'error')); return; }
    if (res.status === 404) { box.replaceChildren(notice('No such report. It may have been removed.', 'error')); return; }
    if (!res.ok || !res.body) { box.replaceChildren(notice('The report could not be loaded.', 'error')); return; }
    drawDetail(res.body);
  }

  function showList() {
    state.nav += 1;
    stopWatching();
    state.detail = null;
    $('#detail-view').hidden = true;
    $('#list-view').hidden = false;
    if (state.scroll) { window.scrollTo(0, state.scroll); state.scroll = 0; }
  }

  function route() {
    let id = '';
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { id = '!'; }
    if (!id) { showList(); return; }
    if (!$('#list-view').hidden) state.scroll = window.scrollY;
    if (!ID_RE.test(id) || id.includes('..')) {
      $('#list-view').hidden = true;
      $('#detail-view').hidden = false;
      $('#detail').replaceChildren(notice('No such report.', 'error'));
      return;
    }
    showDetail(id);
  }

  // ---------- the maintainer's backport job ----------

  async function startBackport(d, button, area) {
    button.disabled = true;
    area.replaceChildren(el('p', { class: 'muted', text: 'Starting…' }));
    let res;
    try {
      res = await api(`api/reports/${encodeURIComponent(d.id)}/backport`, { method: 'POST' });
    } catch {
      button.disabled = false;
      area.replaceChildren(notice('Could not reach the server. Check your connection and try again.', 'error'));
      return;
    }
    if (res.status === 401) { signOut('Your maintainer token was not accepted. Sign in again.'); return; }
    if (res.status === 404) { signOut('Maintainer mode is not available on this server.'); return; }
    if (res.status === 200) { await showDetail(d.id, { keepScroll: true }); return; }  // a pull request already exists
    if (res.status === 202 && res.body && res.body.id) { watchJob(res.body.id, d, area); return; }
    button.disabled = false;
    area.replaceChildren(notice((res.body && res.body.message) || 'The backport could not be started.', 'error'));
  }

  async function resumeJob(d) {
    const jobId = store.get('sessionStorage', JOB_KEY + d.id);
    if (!jobId) return;
    let res = null;
    try { res = await api(`api/runs/${encodeURIComponent(jobId)}`); } catch { /* try again on reload */ }
    const area = $('.backport-area');
    if (res && res.ok && res.body && res.body.status === 'running' && area && state.detail === d) {
      watchJob(jobId, d, area);
    } else {
      store.set('sessionStorage', JOB_KEY + d.id, '');
    }
  }

  function watchJob(jobId, d, area) {
    stopWatching();
    store.set('sessionStorage', JOB_KEY + d.id, jobId);
    const button = area.parentElement && area.parentElement.querySelector('button.primary');
    if (button) button.disabled = true;
    const log = el('ol', { class: 'job-log', 'aria-live': 'polite' });
    area.replaceChildren(el('p', { class: 'job-title', text: 'Backport in progress. This can take 30 minutes or more.' }), log);
    let seen = 0;
    let finished = false;
    const add = (event) => {
      log.append(el('li', null, el('span', { class: 'job-stage', text: event.stage }), el('span', { class: 'job-msg' }, linkify(event.message))));
    };
    const finish = async (error) => {
      if (finished) return;
      finished = true;
      stopWatching();
      store.set('sessionStorage', JOB_KEY + d.id, '');
      state.flash = error ? { id: d.id, kind: 'error', text: error } : null;
      await showDetail(d.id, { keepScroll: true });
    };
    const poll = async () => {
      let res = null;
      try { res = await api(`api/runs/${encodeURIComponent(jobId)}`); } catch { /* retry */ }
      if (res && res.ok && res.body) {
        res.body.events.slice(seen).forEach(add);
        seen = res.body.events.length;
        if (res.body.status !== 'running') { finish(res.body.error); return; }
      }
      state.pollTimer = setTimeout(poll, 2000);
    };
    if (!('EventSource' in window)) { poll(); return; }
    // The events endpoint is public; the job id is a random value that only the maintainer got.
    const source = new EventSource(`api/runs/${encodeURIComponent(jobId)}/events`);
    state.source = source;
    source.addEventListener('progress', (msg) => {
      seen += 1;
      try { add(JSON.parse(msg.data)); } catch { /* ignore a malformed frame */ }
    });
    source.addEventListener('done', (msg) => {
      let error = null;
      try { error = JSON.parse(msg.data).error; } catch { /* the job record has the details */ }
      source.close();
      finish(error);
    });
    source.onerror = () => {
      if (!finished && source.readyState === EventSource.CLOSED) { state.source = null; poll(); }
    };
  }

  // ---------- maintainer sign-in ----------

  async function probeAdmin() {
    try {
      const res = await api('api/admin/check');
      return { 200: 'ok', 401: 'unauthorized', 404: 'off' }[res.status] || 'error';
    } catch {
      return 'error';
    }
  }

  function setAdminText(text) { $('#admin-state').textContent = text; }

  function setAdmin(on) {
    state.admin = on;
    $('#admin-link').textContent = on ? 'Maintainer sign-out' : 'Maintainer sign-in';
    setAdminText(on ? 'Maintainer mode is on.' : '');
    $('#admin-form').hidden = true;
  }

  async function refreshViews() {
    await loadList();
    if (state.detail) await showDetail(state.detail.id, { keepScroll: true });
  }

  function signOut(message) {
    state.token = '';
    store.set('localStorage', TOKEN_KEY, '');
    setAdmin(false);
    if (message) setAdminText(message);
    refreshViews();
  }

  async function openSignIn() {
    if (state.admin) { signOut(); return; }
    state.token = '';
    setAdminText('Checking…');
    const result = await probeAdmin();
    if (result === 'off') { setAdminText('Maintainer mode is not available on this server.'); return; }
    if (result === 'error') { setAdminText('Could not reach the server.'); return; }
    setAdminText('');
    $('#admin-form').hidden = false;
    $('#admin-token').focus();
    $('#admin-form').scrollIntoView({ block: 'nearest' });
  }

  async function submitSignIn(event) {
    event.preventDefault();
    const input = $('#admin-token');
    const token = input.value.trim();
    if (!token) return;
    state.token = token;
    setAdminText('Checking…');
    const result = await probeAdmin();
    if (result === 'ok') {
      store.set('localStorage', TOKEN_KEY, token);
      input.value = '';
      setAdmin(true);
      refreshViews();
      return;
    }
    state.token = '';
    setAdminText(result === 'off' ? 'Maintainer mode is not available on this server.'
      : result === 'error' ? 'Could not reach the server.' : 'That token was not accepted.');
  }

  async function restoreAdmin() {
    const saved = store.get('localStorage', TOKEN_KEY);
    if (!saved) return;
    state.token = saved;
    if (await probeAdmin() === 'ok') { setAdmin(true); return; }
    state.token = '';
    store.set('localStorage', TOKEN_KEY, '');
  }

  // ---------- start-up ----------

  document.addEventListener('DOMContentLoaded', async () => {
    $('#chips').addEventListener('click', (event) => {
      const chip = event.target.closest('.chip-btn');
      if (!chip) return;
      state.filter = chip.dataset.filter;
      drawList();
    });
    $('#report-search').addEventListener('input', (event) => {
      state.query = event.target.value;
      drawList();
    });
    $('#admin-link').addEventListener('click', openSignIn);
    $('#admin-form').addEventListener('submit', submitSignIn);
    $('#admin-cancel').addEventListener('click', () => { $('#admin-form').hidden = true; $('#admin-token').value = ''; });
    window.addEventListener('hashchange', route);

    await restoreAdmin();
    await loadList();
    route();
  });
})();
