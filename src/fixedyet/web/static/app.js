/* FixedYet demo page. No build step, no dependencies.
 *
 * Everything that comes from the server (issue titles, logs, markdown, test code) is untrusted.
 * Nothing is ever put into the page as HTML: text goes through textContent, and the tiny
 * markdown and Python renderers below build DOM nodes directly.
 *
 * Every URL this file requests is relative and has no leading slash (api/status), so the page
 * works at the root of a host and under a prefix such as https://benlevin.fyi/fixedyet/.
 */
(() => {
  'use strict';

  // ---------- helpers ----------

  const $ = (sel, root = document) => root.querySelector(sel);

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'onclick') node.addEventListener('click', value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid);
    }
    return node;
  }

  function safeUrl(value) {
    try {
      const url = new URL(String(value));
      return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
    } catch {
      return null;
    }
  }

  function link(href, text, cls) {
    const url = safeUrl(href);
    if (!url) return el('span', { class: cls, text });
    return el('a', { href: url, class: cls, target: '_blank', rel: 'noopener noreferrer', text });
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    if (total < 60) return `${total}s`;
    return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`;
  }

  function formatCost(usd) {
    if (!usd) return '$0';
    return usd < 0.1 ? `$${usd.toFixed(3)}` : `$${usd.toFixed(2)}`;
  }

  async function copyText(text, button) {
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = 'Copied';
    } catch {
      button.textContent = 'Copy failed';
    }
    setTimeout(() => { button.textContent = original; }, 1600);
  }

  // ---------- Python syntax colouring (light theme, DOM nodes only) ----------

  const PY_KEYWORDS = new Set((
    'False None True and as assert async await break class continue def del elif else except ' +
    'finally for from global if import in is lambda nonlocal not or pass raise return try while ' +
    'with yield match case'
  ).split(' '));
  const PY_BUILTINS = new Set((
    'print len range list dict set tuple str int float bool bytes type isinstance issubclass ' +
    'super self cls open enumerate zip map filter sorted sum min max any all repr getattr ' +
    'setattr hasattr iter next abs round format vars dir id hash input object Exception ' +
    'ValueError TypeError KeyError IndexError AttributeError RuntimeError OSError ' +
    'AssertionError StopIteration NotImplementedError'
  ).split(' '));

  const PY_TOKEN = new RegExp([
    '(?<comment>#[^\\n]*)',
    '(?<string>[rRbBuUfF]{0,2}(?:"""[\\s\\S]*?(?:"""|$)|\'\'\'[\\s\\S]*?(?:\'\'\'|$)|"(?:\\\\.|[^"\\\\\\n])*"?|\'(?:\\\\.|[^\'\\\\\\n])*\'?))',
    '(?<deco>^[ \\t]*@[\\w.]+)',
    '(?<number>0[xX][0-9a-fA-F_]+|\\b\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b)',
    '(?<word>[A-Za-z_]\\w*)',
  ].join('|'), 'gm');

  function highlightPython(code) {
    const frag = document.createDocumentFragment();
    let last = 0;
    let prevWord = '';
    const push = (text, cls) => {
      if (!text) return;
      if (cls) frag.append(el('span', { class: cls, text }));
      else frag.append(document.createTextNode(text));
    };
    for (const match of code.matchAll(PY_TOKEN)) {
      push(code.slice(last, match.index));
      const g = match.groups;
      const text = match[0];
      if (g.comment !== undefined) push(text, 'tok-c');
      else if (g.string !== undefined) push(text, 'tok-s');
      else if (g.deco !== undefined) push(text, 'tok-a');
      else if (g.number !== undefined) push(text, 'tok-n');
      else if (PY_KEYWORDS.has(text)) push(text, 'tok-k');
      else if (prevWord === 'def' || prevWord === 'class') push(text, 'tok-d');
      else if (PY_BUILTINS.has(text)) push(text, 'tok-b');
      else push(text);
      if (g.word !== undefined) prevWord = text;
      else if (!/^\s+$/.test(text)) prevWord = '';
      last = match.index + text.length;
    }
    push(code.slice(last));
    return frag;
  }

  function codeBlock(code, language) {
    const inner = el('code');
    if (language === 'python' || language === 'py') inner.append(highlightPython(code));
    else inner.textContent = code;
    return el('pre', { class: 'code', tabindex: '0' }, inner);
  }

  // ---------- tiny, safe markdown renderer ----------

  function renderInline(text) {
    const frag = document.createDocumentFragment();
    const pattern = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>)]*[^\s<>).,;:])/g;
    let last = 0;
    for (const m of text.matchAll(pattern)) {
      if (m.index > last) frag.append(document.createTextNode(text.slice(last, m.index)));
      if (m[2] !== undefined) frag.append(el('code', { text: m[2].trim() }));
      else if (m[3] !== undefined || m[4] !== undefined) frag.append(el('strong', { text: m[3] ?? m[4] }));
      else if (m[5] !== undefined) frag.append(el('em', { text: m[5] }));
      else if (m[6] !== undefined) frag.append(link(m[7], m[6]));
      else frag.append(link(m[8], m[8]));
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.append(document.createTextNode(text.slice(last)));
    return frag;
  }

  function renderMarkdown(source) {
    const root = el('div', { class: 'md' });
    const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
    let i = 0;
    const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line);
    const cells = (line) => line.trim().replace(/^\||\|$/g, '').split(/(?<!\\)\|/)
      .map((c) => c.trim().replace(/\\\|/g, '|'));

    while (i < lines.length) {
      const line = lines[i];
      let m;
      if (!line.trim()) { i += 1; continue; }

      if ((m = line.match(/^\s*(```+|~~~+)\s*([\w+-]*)/))) {
        const fence = m[1];
        const body = [];
        i += 1;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) { body.push(lines[i]); i += 1; }
        i += 1;
        root.append(codeBlock(body.join('\n'), m[2].toLowerCase()));
      } else if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
        root.append(el(`h${m[1].length}`, null, renderInline(m[2])));
        i += 1;
      } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        root.append(el('hr'));
        i += 1;
      } else if (isTableRow(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && isTableRow(lines[i])) { rows.push(cells(lines[i])); i += 1; }
        const table = el('table', null,
          el('thead', null, el('tr', null, head.map((h) => el('th', null, renderInline(h))))),
          el('tbody', null, rows.map((r) => el('tr', null, r.map((c) => el('td', null, renderInline(c)))))));
        root.append(el('div', { class: 'table-scroll' }, table));
      } else if (/^\s*>/.test(line)) {
        const body = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { body.push(lines[i].replace(/^\s*>\s?/, '')); i += 1; }
        root.append(el('blockquote', null, el('p', null, renderInline(body.join(' ')))));
      } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const list = el(ordered ? 'ol' : 'ul', ordered ? null : { class: 'plain' });
        while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          list.append(el('li', null, renderInline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ''))));
          i += 1;
        }
        root.append(list);
      } else {
        const body = [];
        while (i < lines.length && lines[i].trim() && !/^\s*(```|~~~|#{1,4}\s|>|[-*+]\s|\d+[.)]\s)/.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        if (!body.length) { body.push(line); i += 1; }
        const para = el('p');
        body.forEach((text, n) => {
          para.append(renderInline(text.trim()));
          if (n < body.length - 1) para.append(/ {2,}$/.test(text) ? el('br') : document.createTextNode(' '));
        });
        root.append(para);
      }
    }
    return root;
  }

  // ---------- verdict vocabulary ----------

  const VERDICTS = {
    fixed_on_main: {
      label: 'Fixed on main', tone: 'good',
      blurb: 'The reproduction test fails at the reported version and passes on the fix side. The bug was real and has been fixed.',
    },
    open_bug: {
      label: 'Open bug', tone: 'bad',
      blurb: 'The reproduction test fails at the reported version and still fails on main. The bug is real and not fixed yet.',
    },
    not_reproduced: {
      label: 'Not reproduced', tone: 'neutral',
      blurb: 'The reproduction test passes on both sides, so the reported behaviour did not appear.',
    },
    inconsistent: {
      label: 'Inconsistent', tone: 'warn',
      blurb: 'The results contradict each other, changed between runs, or the test failed for a different reason on main. This is never reported as a reproduction.',
    },
    needs_info: {
      label: 'Needs info', tone: 'info',
      blurb: 'The issue has no runnable reproduction, so nothing was run.',
    },
    unsupported: {
      label: 'Unsupported', tone: 'muted',
      blurb: 'The environment could not be built, so there is no verdict about the bug.',
    },
  };

  const BACKPORT_STATUS = {
    backport_verified: {
      label: 'Verified backport', tone: 'good',
      blurb: 'The fix from main applies cleanly to the maintenance branch of the reported version. The reproduction test fails there before it and passes after it, and the project\'s own tests show no new failures.',
    },
    backport_conflicts: {
      label: 'Backport conflicts', tone: 'warn',
      blurb: 'The fix does not apply cleanly to the maintenance branch. The cherry-pick was aborted and nothing was resolved by the model.',
    },
    backport_regression: {
      label: 'Backport breaks tests', tone: 'bad',
      blurb: 'The fix makes the reproduction test pass but newly breaks other tests of the project on that branch.',
    },
    backport_already_fixed_on_branch: {
      label: 'Already fixed on branch', tone: 'neutral',
      blurb: 'The reproduction test already passes on the maintenance branch, so there is nothing to backport.',
    },
    backport_not_applicable: {
      label: 'No backport possible', tone: 'muted',
      blurb: 'There is no maintenance branch for the reported version, or the fixing commit is unknown.',
    },
    backport_error: {
      label: 'Backport not verified', tone: 'muted',
      blurb: 'A step could not be completed, so the backport is not verified.',
    },
  };

  const OUTCOME_NOTE = {
    pass: 'The test passes here: the behaviour is as expected.',
    fail: 'The test fails here: the bug is present.',
    error: 'The test did not run to a result here.',
  };

  const REASONS = {
    environment: 'Likely cause: the reporter\'s environment',
    configuration: 'Likely cause: configuration',
    not_an_issue: 'Likely cause: expected behaviour, not a bug',
    needs_info: 'The report needs more information',
  };

  const EVIDENCE_KINDS = {
    duplicate: 'Duplicate', upstream: 'Upstream', changelog: 'Changelog', log: 'Log',
  };

  // ---------- report ----------

  function sideCard(title, side, isFix) {
    const card = el('div', { class: 'side' });
    card.append(el('div', { class: 'side-title', text: title }));
    if (!side) {
      card.append(el('div', { class: 'side-ref', text: 'not run' }));
      card.append(el('p', { class: 'side-note', text: 'This side was not reached.' }));
      return card;
    }
    card.append(el('div', { class: 'side-ref', text: side.ref || (isFix ? 'main' : 'reported') }));
    const outcomes = side.outcomes || [];
    const runs = el('div', { class: 'runs' });
    if (outcomes.length) {
      outcomes.forEach((o, n) => runs.append(el('span', { class: `chip ${o}`, text: `run ${n + 1}: ${String(o).toUpperCase()}` })));
    } else {
      runs.append(el('span', { class: 'chip none', text: 'no runs' }));
    }
    card.append(runs);
    const stable = outcomes.length && outcomes.every((o) => o === outcomes[0]) ? outcomes[0] : null;
    card.append(el('p', {
      class: 'side-note',
      text: stable ? OUTCOME_NOTE[stable] : (outcomes.length ? 'The runs disagree, so this side is flaky.' : ''),
    }));
    if (side.log_excerpt) {
      card.append(el('details', null, el('summary', { text: 'Log excerpt' }), codeBlock(side.log_excerpt, 'text')));
    }
    return card;
  }

  function section(title, ...kids) {
    return el('section', { class: 'card' }, el('h3', { text: title }), kids);
  }

  function renderReport(data, options = {}) {
    const a = data.assessment;
    const body = $('#report-body');
    body.replaceChildren();
    if (!a) {
      body.append(el('div', { class: 'notice error', text: data.error || 'This run ended without a report.' }));
      showReport();
      return;
    }

    const meta = VERDICTS[a.verdict] || { label: a.verdict, tone: 'muted', blurb: '' };
    const head = el('section', { class: `card verdict-card ${meta.tone}` });
    head.append(el('div', { class: 'verdict-row' },
      el('span', { class: `badge big ${meta.tone}`, text: meta.label }),
      el('span', { class: 'badge outline', text: `${a.confidence === 'high' ? 'High' : 'Low'} confidence` })));
    head.append(el('h3', { class: 'issue-title', text: a.title || 'Untitled issue' }));
    head.append(link(a.issue_url, a.issue_url, 'issue-link'));
    if (meta.blurb) head.append(el('p', { class: 'blurb', text: meta.blurb }));
    if ((a.confidence_basis || []).length) {
      head.append(el('ul', { class: 'basis' }, a.confidence_basis.map((b) => el('li', { text: b }))));
    }
    if (options.banner) head.append(el('div', { class: 'banner', text: options.banner }));
    body.append(head);

    const repro = a.reproduction || {};
    const fixRef = repro.fix_side && repro.fix_side.ref;
    const fixTitle = /^(main|master)$/.test(fixRef || '') || !fixRef ? 'On main' : 'With the fix';
    body.append(section('Two-sided result', el('div', { class: 'sides' },
      sideCard('At the reported version', repro.reported_side, false),
      sideCard(fixTitle, repro.fix_side, true))));

    if (repro.repro_test_code) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy' });
      copy.addEventListener('click', () => copyText(repro.repro_test_code, copy));
      body.append(section('Reproduction test',
        el('div', { class: 'code-head' },
          el('span', { class: 'code-path', text: repro.repro_test_path || 'test_reproduction.py' }), copy),
        codeBlock(repro.repro_test_code, 'python')));
    }

    const findings = [];
    if (a.root_cause) {
      findings.push(el('p', { text: a.root_cause }));
      findings.push(el('p', { class: 'muted small', text: 'Written by the model from the issue, the test and the logs. The sandbox runs did not check it.' }));
    }
    if (a.not_reproduced_reason && REASONS[a.not_reproduced_reason]) {
      findings.push(el('p', { class: 'muted', text: REASONS[a.not_reproduced_reason] }));
    }
    const facts = [];
    if (repro.fixing_commit) facts.push(el('div', null, el('dt', { text: 'Fixing commit' }), el('dd', { text: repro.fixing_commit.slice(0, 12) })));
    if (repro.fixed_in_release) facts.push(el('div', null, el('dt', { text: 'Fixed in release' }), el('dd', { text: repro.fixed_in_release })));
    if (facts.length) findings.push(el('dl', { class: 'facts' }, facts));
    if (findings.length) body.append(section('What is going on', findings));

    if ((a.evidence || []).length) {
      body.append(section('Evidence', el('ul', { class: 'evidence' }, a.evidence.map((e) => el('li', null,
        el('span', { class: 'badge outline', text: EVIDENCE_KINDS[e.kind] || e.kind }),
        e.url ? link(e.url, e.title || e.url, 'e-title') : el('span', { class: 'e-title', text: e.title }),
        e.why ? el('span', { class: 'e-why', text: e.why }) : null)))));
    }

    if ((a.next_actions || []).length) {
      body.append(section('Next actions', el('ul', { class: 'plain' }, a.next_actions.map((n) => el('li', { text: n })))));
    }

    const u = a.usage || {};
    const facts2 = [
      ['Cost', formatCost(u.cost_usd)],
      ['Time', formatDuration(u.wall_seconds || 0)],
      ['Model calls', String(u.llm_calls ?? 0)],
      ['Sandbox runs', String(u.sandbox_runs ?? 0)],
      ['Search credits', String(u.search_credits ?? 0)],
    ];
    const usage = [el('dl', { class: 'facts' }, facts2.map(([k, v]) => el('div', null, el('dt', { text: k }), el('dd', { text: v }))))];
    if (a.model) usage.push(el('p', { class: 'muted small', text: `Model: ${a.model}` }));
    body.append(section('Cost and time', usage));

    if (data.markdown) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy markdown' });
      copy.addEventListener('click', () => copyText(data.markdown, copy));
      body.append(el('section', { class: 'card' },
        el('div', { class: 'card-head' }, el('h3', { text: 'Markdown report' }), copy),
        el('details', null, el('summary', { text: 'Show the report as rendered markdown' }), renderMarkdown(data.markdown))));
    }

    showReport();
  }

  function suiteCell(suite, key) {
    return suite && suite.ran ? String(suite[key]) : (suite ? 'did not finish' : 'not run');
  }

  function renderBackport(b, markdown, options = {}) {
    const body = $('#report-body');
    body.replaceChildren();
    const meta = BACKPORT_STATUS[`backport_${b.status}`] || { label: b.status, tone: 'muted', blurb: '' };
    const head = el('section', { class: `card verdict-card ${meta.tone}` });
    head.append(el('div', { class: 'verdict-row' },
      el('span', { class: `badge big ${meta.tone}`, text: meta.label }),
      b.target_branch ? el('span', { class: 'badge outline', text: `branch ${b.target_branch}` }) : null));
    head.append(el('h3', { class: 'issue-title', text: b.title || 'Untitled issue' }));
    head.append(link(b.issue_url, b.issue_url, 'issue-link'));
    if (meta.blurb) head.append(el('p', { class: 'blurb', text: meta.blurb }));
    if (b.reason) head.append(el('p', { class: 'muted', text: b.reason }));
    if (options.banner) head.append(el('div', { class: 'banner', text: options.banner }));
    body.append(head);

    const facts = [];
    if (b.reported_version) facts.push(el('div', null, el('dt', { text: 'Reported version' }), el('dd', { text: b.reported_version })));
    if (b.target_branch) facts.push(el('div', null, el('dt', { text: 'Target branch' }), el('dd', { text: b.target_branch })));
    if (b.target_branch_head) facts.push(el('div', null, el('dt', { text: 'Branch head' }), el('dd', { text: b.target_branch_head.slice(0, 12) })));
    if (b.fixing_commit) facts.push(el('div', null, el('dt', { text: 'Fix on main' }), el('dd', { text: b.fixing_commit.slice(0, 12) })));
    if (facts.length) body.append(section('The backport', el('dl', { class: 'facts' }, facts)));

    if (b.repro_before || b.repro_after) {
      body.append(section(`Reproduction test on ${b.target_branch || 'the branch'}`, el('div', { class: 'sides' },
        sideCard('Before the cherry-pick', b.repro_before, false),
        sideCard('After the cherry-pick', b.repro_after, true))));
    }

    if (b.suite_before || b.suite_after) {
      const rows = [['Passed', 'passed'], ['Failed', 'failed'], ['Errors', 'errors'], ['Skipped', 'skipped']]
        .map(([label, key]) => el('tr', null,
          el('td', { text: label }), el('td', { text: suiteCell(b.suite_before, key) }), el('td', { text: suiteCell(b.suite_after, key) })));
      const newly = (b.newly_failing || []).length ? b.newly_failing.join(', ') : (b.suite_after && b.suite_after.ran ? 'none' : '-');
      rows.push(el('tr', null, el('td', { text: 'Newly failing tests' }), el('td', { text: '-' }), el('td', { text: newly })));
      body.append(section('The project\'s own test suite', el('div', { class: 'table-scroll md' },
        el('table', null,
          el('thead', null, el('tr', null, el('th', { text: '' }), el('th', { text: 'Before the cherry-pick' }), el('th', { text: 'After the cherry-pick' }))),
          el('tbody', null, rows)))));
    }

    if ((b.conflicting_files || []).length) {
      body.append(section('Conflicting files', el('ul', { class: 'plain' }, b.conflicting_files.map((f) => el('li', { text: f })))));
    }
    if (b.patch) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy patch' });
      copy.addEventListener('click', () => copyText(b.patch, copy));
      body.append(section('Patch (git format-patch)',
        el('div', { class: 'code-head' }, el('span', { class: 'code-path', text: 'git cherry-pick -x ' + (b.fixing_commit || '').slice(0, 12) }), copy),
        codeBlock(b.patch, 'diff')));
    }
    if (b.log_excerpt && b.status !== 'verified') {
      body.append(section('Output of the step that stopped it', codeBlock(b.log_excerpt, 'text')));
    }
    const u = b.usage || {};
    body.append(section('Cost and time', el('dl', { class: 'facts' }, [
      ['Model cost', formatCost(u.cost_usd)], ['Time', formatDuration(u.wall_seconds || 0)],
      ['Sandbox runs', String(u.sandbox_runs ?? 0)],
    ].map(([k, v]) => el('div', null, el('dt', { text: k }), el('dd', { text: v }))))));

    if (markdown) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy markdown' });
      copy.addEventListener('click', () => copyText(markdown, copy));
      body.append(el('section', { class: 'card' },
        el('div', { class: 'card-head' }, el('h3', { text: 'Markdown summary' }), copy),
        el('details', null, el('summary', { text: 'Show the summary as rendered markdown' }), renderMarkdown(markdown))));
    }
    showReport();
  }

  function showReport() {
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
          el('span', { class: 'label' }, s.label, st.message ? el('span', { class: 'msg', text: st.message }) : null),
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

  async function refreshStatus() {
    try {
      const res = await fetch('api/status');
      if (!res.ok) return;
      const s = await res.json();
      const hint = $('#form-hint');
      const button = $('#run-button');
      if (s.remaining <= 0) {
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
      list.append(el('div', { class: 'example', 'data-id': ex.id },
        tags,
        el('div', { class: 'ex-title', text: ex.title }),
        el('div', { class: 'ex-repo', text: ex.issue_url.replace(/^https?:\/\//, '') }),
        open));
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
      renderBackport(ex.backport, ex.markdown, { banner });
      return;
    }
    renderReport({ assessment: ex.assessment, markdown: ex.markdown }, { banner });
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
    renderReport(data);
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
        headers: { 'Content-Type': 'application/json' },
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
    renderReport(data);
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
