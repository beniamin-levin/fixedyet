/* FixedYet demo page: run form, live progress, example reports. No build step, no dependencies.
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
    $, el, link, gh, linkify, formatDuration, renderReport, renderBackport, VERDICTS, BACKPORT_STATUS,
  } = globalThis.FY;

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

  // The maintainer's key, saved by "Maintainer sign-in" on the reports page (same origin). Their
  // runs skip the daily limit; the server checks the key, this only changes the hint and button.
  function maintainerToken() {
    try { return window.localStorage.getItem('fixedyet-maintainer-token') || ''; } catch { return ''; }
  }

  async function refreshStatus() {
    try {
      const res = await fetch('api/status');
      if (!res.ok) return;
      const s = await res.json();
      const hint = $('#form-hint');
      const button = $('#run-button');
      if (maintainerToken()) {
        hint.textContent = `Signed in as maintainer: no daily limit. One run at a time` +
          `${s.busy ? ', and one is in progress right now' : ''}.`;
        button.disabled = false;
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

  function drawExamples() {
    const list = $('#example-list');
    list.replaceChildren();
    $('#examples').hidden = examples.length === 0;
    for (const ex of examples) {
      const meta = VERDICTS[ex.verdict] || BACKPORT_STATUS[ex.verdict] || { label: ex.verdict, tone: 'muted' };
      const tags = el('div', { class: 'tags' },
        el('span', { class: `badge ${meta.tone}`, text: meta.label }),
        el('span', { class: 'badge outline', text: ex.illustrative ? 'Illustrative sample' : 'Real run' }));
      const open = el('button', { class: 'btn small', type: 'button', text: 'Open report' });
      open.addEventListener('click', () => showExample(ex));
      const repo = gh.repoOf(ex.issue_url);
      const issue = /\/issues\/(\d+)/.exec(ex.issue_url);
      const actions = el('div', { class: 'ex-actions' }, open,
        ex.pr_url ? link(ex.pr_url, 'Draft PR', 'ex-pr') : null);
      list.append(el('div', { class: 'example', 'data-id': ex.id },
        tags,
        el('div', { class: 'ex-title', text: ex.title }),
        link(ex.issue_url, repo && issue ? `${repo}#${issue[1]}` : ex.issue_url, 'ex-repo'),
        actions));
    }
  }

  function showExample(ex) {
    stopWatching();
    timeline.stopClock();
    $('#progress').hidden = true;
    const banner = ex.illustrative
      ? (ex.note || 'Illustrative sample, written by hand to show the report format. It is not the output of a live run.')
      : (ex.note || 'Saved report from a real run of FixedYet.');
    if (ex.backport) {
      showReport(renderBackport(ex.backport, ex.markdown, { banner }));
      return;
    }
    showReport(renderReport({ assessment: ex.assessment, markdown: ex.markdown }, { banner }));
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
        headers: {
          'Content-Type': 'application/json',
          ...(maintainerToken() ? { Authorization: `Bearer ${maintainerToken()}` } : {}),
        },
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
      if (res.status !== 429) $('#run-button').disabled = false;
      return;
    }
    history.replaceState(null, '', `?run=${encodeURIComponent(body.id)}`);
    timeline.reset();
    watch(body.id);
  }

  async function openExistingRun(id) {
    const data = await fetchRun(id);
    if (!data) { notice('That run was not found. It may predate the last cleanup.', 'error'); return; }
    if (data.kind === 'backport' && data.report_id) {  // a maintainer's backport job lives on the reports page
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

    refreshStatus();
    loadExamples();

    const runId = new URLSearchParams(location.search).get('run');
    if (runId && /^[0-9a-f]{12}$/.test(runId)) openExistingRun(runId);
  });
})();
