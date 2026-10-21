/* FixedYet demo page: run form, live progress, example reports, judge access. No build step, no
 * dependencies.
 *
 * The helpers, the markdown renderer and the report renderers are in common.js (FY), which the
 * reports page shares. Everything that comes from the server is untrusted and goes into the page
 * as text, never as HTML.
 *
 * Every URL this file requests is relative and has no leading slash (api/status), so the page
 * works at the root of a host and under a prefix such as https://benlevin.fyi/fixedyet/.
 */
(() => {
  'use strict';

  const {
    $, el, link, gh, linkify, formatDuration, renderReport, VERDICTS, BACKPORT_STATUS, FIX_STATUS,
  } = globalThis.FY;

  const JUDGE_KEY = 'fixedyet-judge-code';

  function showReport(node) {
    $('#report-body').replaceChildren(node);
    const report = $('#report');
    report.hidden = false;
    report.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  }

  // ---------- timeline ----------

  const STAGES = [
    { id: 'intake', label: 'Read the issue', rank: 0 },
    { id: 'environment', label: 'Build the environment', rank: 1 },
    { id: 'agent', label: 'Write the reproduction test', rank: 2 },
    { id: 'run_reported', label: 'Run at the reported version', rank: 3 },
    { id: 'run_fix', label: 'Run on main', rank: 3 },
    { id: 'verdict', label: 'Apply the decision rules', rank: 4 },
    { id: 'evidence', label: 'Search the web for evidence', rank: 5 },
    { id: 'report', label: 'Write the report', rank: 6 },
  ];

  const timeline = {
    state: {}, startedAt: null, baseAt: null, clockTimer: null, finished: false,

    reset() {
      this.state = Object.fromEntries(STAGES.map((s) => [s.id, { status: 'pending', message: '', at: null }]));
      this.startedAt = Date.now();
      this.baseAt = null;
      this.finished = false;
      $('#progress').hidden = false;
      this.draw();
      this.startClock();
    },

    startClock() {
      clearInterval(this.clockTimer);
      const tick = () => { $('#clock').textContent = formatDuration((Date.now() - this.startedAt) / 1000); };
      tick();
      this.clockTimer = setInterval(tick, 1000);
    },

    stopClock() {
      clearInterval(this.clockTimer);
      this.clockTimer = null;
    },

    apply(event) {
      const at = Date.parse(event.at) || Date.now();
      if (event.stage === 'done') {
        for (const s of STAGES) if (this.state[s.id].status === 'active') this.state[s.id].status = 'done';
        for (const s of STAGES) if (this.state[s.id].status === 'pending') this.state[s.id].status = 'skipped';
        this.finished = true;
      } else if (event.stage === 'error') {
        let marked = false;
        for (const s of STAGES) {
          if (this.state[s.id].status === 'active') {
            this.state[s.id].status = 'failed';
            this.state[s.id].message = event.message;
            marked = true;
          }
        }
        if (!marked) {
          const next = STAGES.find((s) => this.state[s.id].status === 'pending');
          if (next) Object.assign(this.state[next.id], { status: 'failed', message: event.message, at });
        }
        this.finished = true;
      } else if (this.state[event.stage]) {
        const rank = STAGES.find((s) => s.id === event.stage).rank;
        for (const s of STAGES) {
          if (s.rank < rank && this.state[s.id].status === 'active') this.state[s.id].status = 'done';
        }
        const st = this.state[event.stage];
        st.status = 'active';
        st.message = event.message;
        st.at = st.at || at;
      }
      if (this.baseAt === null) this.baseAt = at;
      this.draw();
    },

    draw() {
      const base = this.baseAt ?? Date.now();
      const list = $('#timeline');
      list.replaceChildren(...STAGES.map((s) => {
        const st = this.state[s.id];
        const offset = st.at ? formatDuration(Math.max(0, (st.at - base) / 1000)) : '';
        return el('li', { class: st.status },
          el('span', { class: 'dot', 'aria-hidden': 'true' }),
          el('span', { class: 'label' }, s.label, st.message ? el('span', { class: 'msg' }, linkify(st.message)) : null),
          el('span', { class: 'time', text: offset ? `+${offset}` : '' }));
      }));
    },

  };

  // ---------- notices, status, examples ----------

  function notice(message, kind, linkExamples) {
    const box = $('#notice');
    box.className = `notice${kind === 'error' ? ' error' : ''}`;
    box.replaceChildren(document.createTextNode(message));
    if (linkExamples) {
      box.append(document.createTextNode(' '), el('a', { href: '#examples', text: 'See the example reports.' }));
    }
    box.hidden = false;
  }

  function clearNotice() { $('#notice').hidden = true; }

  // localStorage can be blocked or empty (a private window, cleared site data): the page works
  // without it, a judge just types the code again.
  function stored(key) {
    try { return window.localStorage.getItem(key) || ''; } catch { return ''; }
  }

  function store(key, value) {
    try {
      if (value) window.localStorage.setItem(key, value);
      else window.localStorage.removeItem(key);
    } catch { /* nothing to do */ }
  }

  // The maintainer's key, saved by "Maintainer sign-in" on the reports page (same origin). Their
  // runs skip the daily limit; the server checks the key, this only changes the hint and button.
  function maintainerToken() { return stored('fixedyet-maintainer-token'); }

  // A hackathon judge's code: their runs use the judges' own daily quota, not the public one.
  // The server checks it on every request; it is never put into the page.
  function judgeCode() { return stored(JUDGE_KEY); }

  function authHeaders() {
    if (maintainerToken()) return { Authorization: `Bearer ${maintainerToken()}` };
    return judgeCode() ? { 'X-Judge-Code': judgeCode() } : {};
  }

  async function refreshStatus() {
    try {
      const res = await fetch('api/status', { headers: authHeaders() });
      if (!res.ok) return;
      const s = await res.json();
      const hint = $('#form-hint');
      const button = $('#run-button');
      $('#judge-line').hidden = !s.judge_access || Boolean(maintainerToken());
      if (judgeCode() && !s.judge && !maintainerToken()) {
        store(JUDGE_KEY, '');  // the code was withdrawn, or judge access is closed
        $('#judge-state').textContent = s.judge_access ? 'Your judge code is no longer accepted.' : '';
      }
      $('#judge-link').textContent = judgeCode() ? 'Stop using the judge code' : 'Hackathon judge? Enter your code';
      if (maintainerToken()) {
        hint.textContent = `Signed in as maintainer: no daily limit. One run at a time` +
          `${s.busy ? ', and one is in progress right now' : ''}.`;
        button.disabled = false;
      } else if (s.judge) {
        hint.textContent = `Judge code accepted: ${s.judge.remaining} of ${s.judge.daily_limit} judge runs left today. One run at a time` +
          `${s.busy ? ', and one is in progress right now' : ''}.`;
        button.disabled = s.judge.remaining <= 0;
      } else if (s.remaining <= 0) {
        hint.textContent = s.daily_limit <= 0
          ? 'Live runs are paused while the demo has no sandbox backend. The example reports below show what a run produces.'
          : 'Today\'s live runs are used up. The example reports below show what a run produces.';
        button.disabled = true;
      } else {
        hint.textContent = `${s.remaining} of ${s.daily_limit} live runs left today. One run at a time` +
          `${s.busy ? ', and one is in progress right now' : ''}.`;
        button.disabled = false;
      }
    } catch {
      /* the hint is a convenience */
    }
  }

  let examples = [];

  // "pydantic/pydantic#14043" for a pull request URL.
  function prLabel(url) {
    const m = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/.exec(url || '');
    return m ? `${m[1]}#${m[2]}` : 'Pull request';
  }

  // Each example opens on the reports page, which shows the verdict first, then the verified fix
  // or backport, then the two-sided result.
  function drawExamples() {
    const list = $('#example-list');
    list.replaceChildren();
    $('#examples').hidden = examples.length === 0;
    for (const ex of examples) {
      const meta = VERDICTS[ex.verdict] || BACKPORT_STATUS[ex.verdict] || { label: ex.verdict, tone: 'muted' };
      const fix = ex.assessment && ex.assessment.fix;
      const fixMeta = fix ? FIX_STATUS[`fix_${fix.status}`] : null;
      const tags = el('div', { class: 'tags' },
        el('span', { class: `badge ${meta.tone}`, text: meta.label }),
        fixMeta ? el('span', { class: `badge ${fixMeta.tone}`, text: fixMeta.label }) : null,
        el('span', { class: 'badge outline', text: ex.illustrative ? 'Illustrative sample' : 'Real run' }));
      const open = el('a', { class: 'btn small', href: `reports#${encodeURIComponent(ex.id)}`, text: 'Open report' });
      const repo = gh.repoOf(ex.issue_url);
      const issue = /\/issues\/(\d+)/.exec(ex.issue_url);
      const actions = el('div', { class: 'ex-actions' }, open,
        ex.pr_url ? link(ex.pr_url, `PR ${prLabel(ex.pr_url)}`, 'ex-pr') : null);
      list.append(el('div', { class: 'example', 'data-id': ex.id },
        tags,
        el('div', { class: 'ex-title', text: ex.title }),
        ex.label ? el('div', { class: 'ex-label', text: ex.label }) : null,
        link(ex.issue_url, repo && issue ? `${repo}#${issue[1]}` : ex.issue_url, 'ex-repo'),
        actions));
    }
  }

  async function loadExamples() {
    try {
      const res = await fetch('api/examples');
      if (res.ok) examples = await res.json();
    } catch {
      examples = [];
    }
    drawExamples();
  }

  function pointAtExamples() {
    const box = $('#examples');
    if (box.hidden) return;
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    box.querySelectorAll('.example').forEach((n) => n.classList.add('attention'));
    setTimeout(() => box.querySelectorAll('.example').forEach((n) => n.classList.remove('attention')), 2500);
  }

  // ---------- running ----------

  let source = null;
  let pollTimer = null;

  function stopWatching() {
    if (source) { source.close(); source = null; }
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  async function fetchRun(id) {
    const res = await fetch(`api/runs/${encodeURIComponent(id)}`);
    if (!res.ok) return null;
    return res.json();
  }

  async function finishRun(id) {
    stopWatching();
    timeline.stopClock();
    const data = await fetchRun(id);
    $('#run-button').disabled = false;
    if (!data) { notice('Could not load the finished run. Reload the page to try again.', 'error'); return; }
    showReport(renderReport(data));
    refreshStatus();
  }

  function watch(id) {
    stopWatching();
    let seen = 0;
    let finished = false;
    const onDone = () => { if (!finished) { finished = true; finishRun(id); } };

    // Fallback if server-sent events are blocked or drop: poll the run instead.
    const poll = async () => {
      const data = await fetchRun(id).catch(() => null);
      if (data) {
        data.events.slice(seen).forEach((ev) => timeline.apply(ev));
        seen = data.events.length;
        if (data.status !== 'running') { onDone(); return; }
      }
      pollTimer = setTimeout(poll, 2000);
    };

    if (!('EventSource' in window)) { poll(); return; }
    source = new EventSource(`api/runs/${encodeURIComponent(id)}/events`);
    source.addEventListener('progress', (msg) => {
      seen += 1;
      try { timeline.apply(JSON.parse(msg.data)); } catch { /* ignore a malformed frame */ }
    });
    source.addEventListener('done', () => { source.close(); onDone(); });
    source.onerror = () => {
      if (finished) return;
      if (source && source.readyState === EventSource.CLOSED) { source = null; poll(); }
    };
  }

  async function startRun(url) {
    clearNotice();
    $('#report').hidden = true;
    $('#run-button').disabled = true;
    let res;
    try {
      res = await fetch('api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ issue_url: url }),
      });
    } catch {
      notice('Could not reach the server. Check your connection and try again.', 'error');
      $('#run-button').disabled = false;
      return;
    }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const limited = res.status === 429;
      notice(body.message || 'The run could not be started.', limited ? 'warn' : 'error', limited);
      if (limited) pointAtExamples();
      await refreshStatus();
      if (!limited) $('#run-button').disabled = false;
      return;
    }
    history.replaceState(null, '', `?run=${encodeURIComponent(body.id)}`);
    timeline.reset();
    watch(body.id);
  }

  async function openExistingRun(id) {
    const data = await fetchRun(id);
    if (!data) { notice('That run was not found. It may predate the last cleanup.', 'error'); return; }
    if ((data.kind === 'backport' || data.kind === 'fix') && data.report_id) {  // a maintainer's job lives on the reports page
      location.replace(`reports#${encodeURIComponent(data.report_id)}`);
      return;
    }
    $('#issue-url').value = data.issue_url;
    if (data.status === 'running') {
      timeline.reset();
      $('#run-button').disabled = true;
      watch(id);
      return;
    }
    timeline.reset();
    data.events.forEach((ev) => timeline.apply(ev));
    timeline.finished = true;
    timeline.stopClock();
    $('#clock').textContent = data.finished_at
      ? formatDuration((Date.parse(data.finished_at) - Date.parse(data.created_at)) / 1000) : '';
    showReport(renderReport(data));
  }

  // ---------- judge access ----------

  async function checkJudgeCode(code) {
    try {
      const res = await fetch('api/judge/check', { headers: { 'X-Judge-Code': code } });
      return { 200: 'ok', 401: 'unauthorized', 404: 'off' }[res.status] || 'error';
    } catch {
      return 'error';
    }
  }

  function toggleJudgeForm() {
    if (judgeCode()) {  // "Stop using the judge code"
      store(JUDGE_KEY, '');
      $('#judge-state').textContent = 'The judge code is no longer used on this browser.';
      refreshStatus();
      return;
    }
    $('#judge-form').hidden = !$('#judge-form').hidden;
    $('#judge-state').textContent = '';
    if (!$('#judge-form').hidden) $('#judge-code').focus();
  }

  async function submitJudgeCode(event) {
    event.preventDefault();
    const input = $('#judge-code');
    const code = input.value.trim();
    if (!code) return;
    $('#judge-state').textContent = 'Checking…';
    const result = await checkJudgeCode(code);
    if (result === 'ok') {
      store(JUDGE_KEY, code);
      input.value = '';
      $('#judge-form').hidden = true;
      $('#judge-state').textContent = 'Judge code accepted. Your runs use the judges\' own daily quota.';
      refreshStatus();
      return;
    }
    $('#judge-state').textContent = {
      unauthorized: 'That code was not accepted. Check it and try again.',
      off: 'Judge access is not open on this server.',
    }[result] || 'Could not reach the server.';
  }

  // ---------- start-up ----------

  document.addEventListener('DOMContentLoaded', () => {
    $('#run-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const value = $('#issue-url').value.trim();
      if (!/^https:\/\/(www\.)?github\.com\/[^/\s]+\/[^/\s]+\/issues\/\d+/.test(value)) {
        notice('Paste a public GitHub issue URL, like https://github.com/owner/repo/issues/123.', 'error');
        $('#issue-url').focus();
        return;
      }
      startRun(value);
    });

    $('#judge-link').addEventListener('click', toggleJudgeForm);
    $('#judge-form').addEventListener('submit', submitJudgeCode);
    $('#judge-cancel').addEventListener('click', () => { $('#judge-form').hidden = true; $('#judge-code').value = ''; });

    refreshStatus();
    loadExamples();

    const runId = new URLSearchParams(location.search).get('run');
    if (runId && /^[0-9a-f]{12}$/.test(runId)) openExistingRun(runId);
  });
})();
