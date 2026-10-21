/* Shared by the demo page (app.js) and the reports page (reports.js). No build step, no dependencies.
 *
 * Everything that comes from the server (issue titles, logs, markdown, test code) is untrusted.
 * Nothing is ever put into the page as HTML: text goes through textContent, and the tiny
 * markdown and Python renderers below build DOM nodes directly.
 *
 * Every URL this file requests or builds is relative or an https://github.com link made from
 * validated parts (see `gh`), so the pages work at the root of a host and under a prefix such as
 * https://benlevin.fyi/fixedyet/. Nothing here touches the document while it loads.
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

  function formatDate(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
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

  // ---------- GitHub links ----------
  // Every link to GitHub is built from a validated owner/repo and a validated ref, with each
  // path segment encoded. A value that does not validate gives null, and `link` then draws plain
  // text. Nothing here can produce a javascript: or data: URL.

  const OWNER_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
  const REPO_RE = /^(?!\.+$)[A-Za-z0-9._-]{1,100}$/;
  const REF_RE = /^[A-Za-z0-9][A-Za-z0-9_.\/+-]{0,199}$/;
  const SHA_RE = /^[0-9a-f]{7,40}$/i;
  const VERSION_RE = /^v?\d+(?:[.\-_+][0-9A-Za-z]+)*$/;

  function repoOf(value) {
    const text = String(value || '').trim();
    const issue = text.match(/^https:\/\/(?:www\.)?github\.com\/([^/]+)\/([^/]+)\/(?:issues|pull)\/\d+\/?$/);
    const parts = issue ? [issue[1], issue[2]] : text.split('/');
    if (parts.length !== 2 || !OWNER_RE.test(parts[0]) || !REPO_RE.test(parts[1])) return null;
    return `${parts[0]}/${parts[1]}`;
  }

  function refPath(name) {
    const text = String(name || '');
    const segments = text.split('/');
    if (!REF_RE.test(text) || text.includes('..') || segments.some((s) => !s || s === '.')) return null;
    return segments.map(encodeURIComponent).join('/');
  }

  // A hash of 7+ hex digits with both a digit and a letter: the shape of a git sha in running
  // text, which "added" or "1234567" are not.
  function looksLikeSha(text) {
    return SHA_RE.test(text) && /\d/.test(text) && /[a-f]/i.test(text);
  }

  // 8.4.1, v2.31.0, 1.0-rc1: a release tag. 8.4.x is a maintenance branch.
  function isVersion(name) {
    return VERSION_RE.test(name) && !/[.\-_]x$/i.test(name);
  }

  const gh = {
    repoOf,
    repo(repo) {
      const full = repoOf(repo);
      return full ? `https://github.com/${full}` : null;
    },
    issue(repo, number) {
      const base = gh.repo(repo);
      const n = String(number);
      return base && /^[1-9]\d{0,8}$/.test(n) ? `${base}/issues/${n}` : null;
    },
    commit(repo, sha) {
      const base = gh.repo(repo);
      return base && SHA_RE.test(String(sha)) ? `${base}/commit/${sha}` : null;
    },
    blob(repo, sha, path, line) {
      const base = gh.repo(repo);
      const file = refPath(path);
      const n = String(line);
      return base && file && SHA_RE.test(String(sha)) && /^[1-9]\d{0,6}$/.test(n)
        ? `${base}/blob/${sha}/${file}#L${n}` : null;
    },
    branch(repo, name) {
      const base = gh.repo(repo);
      const path = base && refPath(name);
      return path ? `${base}/tree/${path}` : null;
    },
    tag(repo, name) {
      const base = gh.repo(repo);
      const path = base && refPath(name);
      return path ? `${base}/releases/tag/${path}` : null;
    },
    // Whatever a report calls a ref: a sha, a release tag, a branch, or a label such as
    // "8.4.x (before)" from a backport.
    ref(repo, ref) {
      const text = String(ref || '').trim();
      if (!text || text === 'gold_patch') return null;
      if (looksLikeSha(text)) return gh.commit(repo, text);
      const labelled = text.match(/^(\S+) \((?:before|after)\)$/);
      if (labelled) return gh.branch(repo, labelled[1]);
      return isVersion(text) ? gh.tag(repo, text) : gh.branch(repo, text);
    },
  };

  // A reference as a link to GitHub, or as plain text when it cannot be linked.
  function refLink(repo, ref, text, cls) {
    return link(gh.ref(repo, ref), text ?? String(ref), cls);
  }

  function commitLink(repo, sha, cls) {
    return link(gh.commit(repo, sha), String(sha).slice(0, 12), cls);
  }

  // Plain text with its bare URLs, owner/repo#123 references and (when the repository is
  // known) commit hashes turned into links.
  const LINKIFY = new RegExp([
    '(https?:\\/\\/[^\\s<>)]*[^\\s<>).,;:])',
    '(?<![\\w/.-])([A-Za-z0-9][A-Za-z0-9-]{0,38}\\/[A-Za-z0-9][A-Za-z0-9._-]{0,99})#(\\d{1,9})\\b',
    '\\b([0-9a-fA-F]{7,40})\\b',
  ].join('|'), 'g');

  function linkify(text, repo) {
    const source = String(text ?? '');
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of source.matchAll(LINKIFY)) {
      let node = null;
      if (m[1] !== undefined) node = link(m[1], m[1]);
      else if (m[2] !== undefined) node = link(gh.issue(m[2], m[3]), m[0]);
      else if (repo && looksLikeSha(m[4])) node = link(gh.commit(repo, m[4]), m[4]);
      if (!node) continue;
      if (m.index > last) frag.append(document.createTextNode(source.slice(last, m.index)));
      frag.append(node);
      last = m.index + m[0].length;
    }
    if (last < source.length) frag.append(document.createTextNode(source.slice(last)));
    return frag;
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

  // A unified diff with a light tint per line: green for added, red for removed. Light theme only.
  function highlightDiff(code) {
    const frag = document.createDocumentFragment();
    const lines = String(code).replace(/\n$/, '').split('\n');
    let inHunk = false;
    for (const line of lines) {
      let cls = 'd-ctx';
      if (/^(diff --git |index |new file mode|deleted file mode|similarity |rename |From |Subject: |Date: )/.test(line)) { cls = 'd-meta'; inHunk = false; }
      else if (/^(---|\+\+\+) /.test(line) && !inHunk) cls = 'd-meta';
      else if (line.startsWith('@@')) { cls = 'd-hunk'; inHunk = true; }
      else if (inHunk && line.startsWith('+')) cls = 'd-add';
      else if (inHunk && line.startsWith('-')) cls = 'd-del';
      frag.append(el('span', { class: `d-line ${cls}`, text: line || ' ' }));
    }
    return frag;
  }

  function codeBlock(code, language) {
    const inner = el('code');
    if (language === 'python' || language === 'py') inner.append(highlightPython(code));
    else if (language === 'diff' || language === 'patch') inner.append(highlightDiff(code));
    else inner.textContent = code;
    return el('pre', { class: `code${language === 'diff' || language === 'patch' ? ' diff' : ''}`, tabindex: '0' }, inner);
  }

  // ---------- tiny, safe markdown renderer ----------

  // `repo` ("owner/repo", optional) is the repository the text is about: with it, a code span
  // holding a commit hash links to that commit. A bare owner/repo#123 always links to the issue.
  function renderInline(text, repo) {
    const frag = document.createDocumentFragment();
    const pattern = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>)]*[^\s<>).,;:])|(?<![\w/.-])([A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9][A-Za-z0-9._-]{0,99})#(\d{1,9})\b/g;
    let last = 0;
    for (const m of text.matchAll(pattern)) {
      if (m.index > last) frag.append(document.createTextNode(text.slice(last, m.index)));
      if (m[2] !== undefined) {
        const code = m[2].trim();
        const url = repo && looksLikeSha(code) ? safeUrl(gh.commit(repo, code)) : null;
        frag.append(url
          ? el('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, el('code', { text: code }))
          : el('code', { text: code }));
      } else if (m[3] !== undefined || m[4] !== undefined) frag.append(el('strong', { text: m[3] ?? m[4] }));
      else if (m[5] !== undefined) frag.append(el('em', { text: m[5] }));
      else if (m[6] !== undefined) frag.append(link(m[7], m[6]));
      else if (m[8] !== undefined) frag.append(link(m[8], m[8]));
      else frag.append(link(gh.issue(m[9], m[10]), m[0]));
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.append(document.createTextNode(text.slice(last)));
    return frag;
  }

  function renderMarkdown(source, repo) {
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
        root.append(el(`h${m[1].length}`, null, renderInline(m[2], repo)));
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
          el('thead', null, el('tr', null, head.map((h) => el('th', null, renderInline(h, repo))))),
          el('tbody', null, rows.map((r) => el('tr', null, r.map((c) => el('td', null, renderInline(c, repo)))))));
        root.append(el('div', { class: 'table-scroll' }, table));
      } else if (/^\s*>/.test(line)) {
        const body = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { body.push(lines[i].replace(/^\s*>\s?/, '')); i += 1; }
        root.append(el('blockquote', null, el('p', null, renderInline(body.join(' '), repo))));
      } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const list = el(ordered ? 'ol' : 'ul', ordered ? null : { class: 'plain' });
        while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          list.append(el('li', null, renderInline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ''), repo)));
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
          para.append(renderInline(text.trim(), repo));
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

  const FIX_STATUS = {
    fix_verified: {
      label: 'Verified fix', tone: 'good',
      blurb: 'The model wrote a change; in the sandbox the reproduction test fails on the default branch without it and passes with it, twice each, and the project\'s own tests show no new failures.',
    },
    fix_not_fixed: {
      label: 'No verified fix', tone: 'warn',
      blurb: 'None of the model\'s attempts passed the checks. Each attempt and the reason it failed are listed.',
    },
    fix_already_fixed: {
      label: 'Nothing to fix', tone: 'neutral',
      blurb: 'The reproduction test already passes on the default branch.',
    },
    fix_not_applicable: {
      label: 'No fix possible', tone: 'muted',
      blurb: 'Only an open bug with a reproduction test can get a fix attempt.',
    },
    fix_error: {
      label: 'Fix not verified', tone: 'muted',
      blurb: 'A step could not be completed, so there is no verified fix.',
    },
  };

  const ATTEMPT_OUTCOMES = {
    verified: 'verified', invalid_edits: 'edits rejected', repro_still_fails: 'test still fails',
    regression: 'breaks other tests', suite_not_run: 'suite did not finish',
    apply_failed: 'could not apply', no_edits: 'no edits', gave_up: 'model declined',
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

  // A reference line that links to GitHub where it can: `ref` is a tag, branch, sha or label.
  function refNode(repo, ref, fallback, cls) {
    return ref ? refLink(repo, ref, ref, cls) : el('span', { class: cls, text: fallback });
  }

  function sideCard(title, side, isFix, repo) {
    const card = el('div', { class: 'side' });
    card.append(el('div', { class: 'side-title', text: title }));
    if (!side) {
      card.append(el('div', { class: 'side-ref', text: 'not run' }));
      card.append(el('p', { class: 'side-note', text: 'This side was not reached.' }));
      return card;
    }
    card.append(el('div', { class: 'side-ref' }, refNode(repo, side.ref, isFix ? 'main' : 'reported')));
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

  function fact(label, value) {
    return el('div', null, el('dt', { text: label }), el('dd', null, value));
  }

  // A note (an example's banner) is markdown: bullets, **bold**, `code` and links.
  function banner(note, repo) {
    return el('div', { class: 'banner' }, renderMarkdown(note, repo));
  }

  // The report of an assessment as a DOM node. `data` is {assessment, markdown, error}.
  function renderReport(data, options = {}) {
    const a = data.assessment;
    const root = el('div');
    if (!a) {
      root.append(el('div', { class: 'notice error', text: data.error || 'This run ended without a report.' }));
      return root;
    }
    const repo = gh.repoOf(a.issue_url);

    const meta = VERDICTS[a.verdict] || { label: a.verdict, tone: 'muted', blurb: '' };
    const head = el('section', { class: `card verdict-card ${meta.tone}` });
    head.append(el('div', { class: 'verdict-row' },
      el('span', { class: `badge big ${meta.tone}`, text: meta.label }),
      el('span', { class: 'badge outline', text: `${a.confidence === 'high' ? 'High' : 'Low'} confidence` })));
    head.append(el('h3', { class: 'issue-title', text: a.title || 'Untitled issue' }));
    head.append(link(a.issue_url, a.issue_url, 'issue-link'));
    if (meta.blurb) head.append(el('p', { class: 'blurb', text: meta.blurb }));
    if ((a.confidence_basis || []).length) {
      head.append(el('ul', { class: 'basis' }, a.confidence_basis.map((b) => el('li', null, linkify(b, repo)))));
    }
    if (options.banner) head.append(banner(options.banner, repo));
    root.append(head);

    const repro = a.reproduction || {};
    const fixRef = repro.fix_side && repro.fix_side.ref;
    const fixTitle = /^(main|master)$/.test(fixRef || '') || !fixRef ? 'On main' : 'With the fix';
    root.append(section('Two-sided result', el('div', { class: 'sides' },
      sideCard('At the reported version', repro.reported_side, false, repo),
      sideCard(fixTitle, repro.fix_side, true, repo))));

    if (repro.repro_test_code) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy' });
      copy.addEventListener('click', () => copyText(repro.repro_test_code, copy));
      root.append(section('Reproduction test',
        el('div', { class: 'code-head' },
          el('span', { class: 'code-path', text: repro.repro_test_path || 'test_reproduction.py' }), copy),
        codeBlock(repro.repro_test_code, 'python')));
    }

    const findings = [];
    if (a.root_cause) {
      findings.push(el('p', null, linkify(a.root_cause, repo)));
      findings.push(el('p', { class: 'muted small', text: 'Written by the model from the issue, the test and the logs. The sandbox runs did not check it.' }));
    }
    if (a.not_reproduced_reason && REASONS[a.not_reproduced_reason]) {
      findings.push(el('p', { class: 'muted', text: REASONS[a.not_reproduced_reason] }));
    }
    const facts = [];
    if (repro.fixing_commit) facts.push(fact('Fixing commit', commitLink(repo, repro.fixing_commit)));
    if (repro.fixed_in_release) facts.push(fact('Fixed in release', refNode(repo, repro.fixed_in_release, '')));
    if (facts.length) findings.push(el('dl', { class: 'facts' }, facts));
    if (findings.length) root.append(section('What is going on', findings));

    if ((a.evidence || []).length) {
      root.append(section('Evidence', el('ul', { class: 'evidence' }, a.evidence.map((e) => el('li', null,
        el('span', { class: 'badge outline', text: EVIDENCE_KINDS[e.kind] || e.kind }),
        e.url ? link(e.url, e.title || e.url, 'e-title') : el('span', { class: 'e-title', text: e.title }),
        e.why ? el('span', { class: 'e-why' }, linkify(e.why, repo)) : null)))));
    }

    if ((a.next_actions || []).length) {
      root.append(section('Next actions', el('ul', { class: 'plain' }, a.next_actions.map((n) => el('li', null, linkify(n, repo))))));
    }

    const u = a.usage || {};
    const facts2 = [
      ['Cost', formatCost(u.cost_usd)],
      ['Time', formatDuration(u.wall_seconds || 0)],
      ['Model calls', String(u.llm_calls ?? 0)],
      ['Sandbox runs', String(u.sandbox_runs ?? 0)],
      ['Search credits', String(u.search_credits ?? 0)],
    ];
    const usage = [el('dl', { class: 'facts' }, facts2.map(([k, v]) => fact(k, v)))];
    if (a.model) usage.push(el('p', { class: 'muted small', text: `Model: ${a.model}` }));
    root.append(section('Cost and time', usage));

    if (data.markdown) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy markdown' });
      copy.addEventListener('click', () => copyText(data.markdown, copy));
      root.append(el('section', { class: 'card' },
        el('div', { class: 'card-head' }, el('h3', { text: 'Markdown report' }), copy),
        el('details', null, el('summary', { text: 'Show the report as rendered markdown' }), renderMarkdown(data.markdown, repo))));
    }
    return root;
  }

  function suiteCell(suite, key) {
    return suite && suite.ran ? String(suite[key]) : (suite ? 'did not finish' : 'not run');
  }

  // The report of a backport result as a DOM node.
  function renderBackport(b, markdown, options = {}) {
    const repo = gh.repoOf(b.repo) || gh.repoOf(b.issue_url);
    const root = el('div');
    const meta = BACKPORT_STATUS[`backport_${b.status}`] || { label: b.status, tone: 'muted', blurb: '' };
    const head = el('section', { class: `card verdict-card ${meta.tone}` });
    head.append(el('div', { class: 'verdict-row' },
      el('span', { class: `badge big ${meta.tone}`, text: meta.label }),
      b.target_branch ? el('span', { class: 'badge outline', text: `branch ${b.target_branch}` }) : null));
    head.append(el('h3', { class: 'issue-title', text: b.title || 'Untitled issue' }));
    head.append(link(b.issue_url, b.issue_url, 'issue-link'));
    if (meta.blurb) head.append(el('p', { class: 'blurb', text: meta.blurb }));
    if (b.reason) head.append(el('p', { class: 'muted' }, linkify(b.reason, repo)));
    if (options.banner) head.append(banner(options.banner, repo));
    root.append(head);

    const facts = [];
    if (b.reported_version) facts.push(fact('Reported version', refNode(repo, b.reported_version, '')));
    if (b.target_branch) facts.push(fact('Target branch', link(gh.branch(repo, b.target_branch), b.target_branch)));
    if (b.target_branch_head) facts.push(fact('Branch head', commitLink(repo, b.target_branch_head)));
    if (b.fixing_commit) facts.push(fact('Fix on main', commitLink(repo, b.fixing_commit)));
    if (facts.length) root.append(section('The backport', el('dl', { class: 'facts' }, facts)));

    if (b.repro_before || b.repro_after) {
      root.append(section(`Reproduction test on ${b.target_branch || 'the branch'}`, el('div', { class: 'sides' },
        sideCard('Before the cherry-pick', b.repro_before, false, repo),
        sideCard('After the cherry-pick', b.repro_after, true, repo))));
    }

    if (b.suite_before || b.suite_after) {
      const rows = [['Passed', 'passed'], ['Failed', 'failed'], ['Errors', 'errors'], ['Skipped', 'skipped']]
        .map(([label, key]) => el('tr', null,
          el('td', { text: label }), el('td', { text: suiteCell(b.suite_before, key) }), el('td', { text: suiteCell(b.suite_after, key) })));
      const newly = (b.newly_failing || []).length ? b.newly_failing.join(', ') : (b.suite_after && b.suite_after.ran ? 'none' : '-');
      rows.push(el('tr', null, el('td', { text: 'Newly failing tests' }), el('td', { text: '-' }), el('td', { text: newly })));
      root.append(section('The project\'s own test suite', el('div', { class: 'table-scroll md' },
        el('table', null,
          el('thead', null, el('tr', null, el('th', { text: '' }), el('th', { text: 'Before the cherry-pick' }), el('th', { text: 'After the cherry-pick' }))),
          el('tbody', null, rows)))));
    }

    if ((b.conflicting_files || []).length) {
      root.append(section('Conflicting files', el('ul', { class: 'plain' }, b.conflicting_files.map((f) => el('li', { text: f })))));
    }
    if (b.patch) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy patch' });
      copy.addEventListener('click', () => copyText(b.patch, copy));
      root.append(section('Patch (git format-patch)',
        el('div', { class: 'code-head' }, el('span', { class: 'code-path', text: 'git cherry-pick -x ' + (b.fixing_commit || '').slice(0, 12) }), copy),
        codeBlock(b.patch, 'diff')));
    }
    if (b.log_excerpt && b.status !== 'verified') {
      root.append(section('Output of the step that stopped it', codeBlock(b.log_excerpt, 'text')));
    }
    const u = b.usage || {};
    root.append(section('Cost and time', el('dl', { class: 'facts' }, [
      ['Model cost', formatCost(u.cost_usd)], ['Time', formatDuration(u.wall_seconds || 0)],
      ['Sandbox runs', String(u.sandbox_runs ?? 0)],
    ].map(([k, v]) => fact(k, v)))));

    if (markdown) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy markdown' });
      copy.addEventListener('click', () => copyText(markdown, copy));
      root.append(el('section', { class: 'card' },
        el('div', { class: 'card-head' }, el('h3', { text: 'Markdown summary' }), copy),
        el('details', null, el('summary', { text: 'Show the summary as rendered markdown' }), renderMarkdown(markdown, repo))));
    }
    return root;
  }

  // The other callers of what a verified fix changes: code the reproduction test did not run,
  // which a reviewer should check before trusting "verified". Null when there is nothing to say.
  function otherCallers(f, repo) {
    if (f.status !== 'verified' || !(f.changed_functions || []).length) return null;
    const names = f.changed_functions.join(', ');
    const sites = f.other_callers || [];
    if (!sites.length) {
      return el('p', { class: 'muted small', text: `Other callers of ${names}: none found (searched by name).` });
    }
    const items = sites.map((s) => el('li', null,
      link(gh.blob(repo, f.base_sha, s.path, s.line), `${s.path}:${s.line}`), ` uses ${s.symbol}`,
      s.code ? codeBlock(s.code, 'text') : null));
    const more = (f.other_callers_total || 0) - sites.length;
    if (more > 0) items.push(el('li', { text: `and ${more} more` }));
    return el('div', { class: 'other-callers' },
      el('p', null, el('strong', { text: 'Other callers of the changed code, not covered by the reproduction' }),
        ` (${names}; searched by name). The suite passing does not show that they behave as before.`),
      el('ul', null, items));
  }

  // The report of a fix result (fix mode on an open bug) as a DOM node.
  function renderFix(f, markdown, options = {}) {
    const repo = gh.repoOf(f.repo) || gh.repoOf(f.issue_url);
    const branch = f.base_branch || 'main';
    const root = el('div');
    const meta = FIX_STATUS[`fix_${f.status}`] || { label: f.status, tone: 'muted', blurb: '' };
    const head = el('section', { class: `card verdict-card ${meta.tone}` });
    head.append(el('div', { class: 'verdict-row' },
      el('span', { class: `badge big ${meta.tone}`, text: meta.label }),
      el('span', { class: 'badge outline', text: `on ${branch}` })));
    head.append(el('h3', { class: 'issue-title', text: f.title || 'Untitled issue' }));
    head.append(link(f.issue_url, f.issue_url, 'issue-link'));
    if (meta.blurb) head.append(el('p', { class: 'blurb', text: meta.blurb }));
    if (f.reason) head.append(el('p', { class: 'muted' }, linkify(f.reason, repo)));
    if (options.banner) head.append(banner(options.banner, repo));
    root.append(head);

    const facts = [];
    if (f.base_sha) facts.push(fact('Verified on', el('span', null, `${branch} at `, commitLink(repo, f.base_sha))));
    if ((f.files_changed || []).length) facts.push(fact('Change', `${f.files_changed.length} file${f.files_changed.length === 1 ? '' : 's'}, +${f.lines_added} −${f.lines_removed}`));
    if ((f.context_files || []).length) facts.push(fact('Code shown to the model', f.context_files.join(', ')));
    if (f.regression_test_path) facts.push(fact('Regression test in the PR', f.regression_test_path));
    else if (f.status === 'verified') facts.push(fact('Regression test in the PR', `none (${f.regression_test_note || 'the repository has no tests'})`));
    if (facts.length) root.append(section('The fix', el('dl', { class: 'facts' }, facts)));

    if (f.summary || f.explanation) {
      root.append(section('What the change does',
        f.summary ? el('p', null, el('strong', { text: f.summary })) : null,
        f.explanation ? el('p', null, linkify(f.explanation, repo)) : null,
        el('p', { class: 'muted small', text: 'Written by the model. The sandbox checked the behaviour, not this text.' })));
    }
    if (f.diff) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy diff' });
      copy.addEventListener('click', () => copyText(f.diff, copy));
      root.append(section(f.status === 'verified' ? 'Verified diff' : 'Diff (not verified)',
        el('div', { class: 'code-head' }, el('span', { class: 'code-path', text: `git diff on ${branch}` }), copy),
        codeBlock(f.diff, 'diff'), otherCallers(f, repo)));
    }
    if (f.repro_before || f.repro_after) {
      root.append(section(`Reproduction test on ${branch}`, el('div', { class: 'sides' },
        sideCard('Without the change', f.repro_before, false, repo),
        sideCard('With the change', f.repro_after, true, repo))));
    }
    if (f.suite_before || f.suite_after) {
      const rows = [['Passed', 'passed'], ['Failed', 'failed'], ['Errors', 'errors'], ['Skipped', 'skipped']]
        .map(([label, key]) => el('tr', null,
          el('td', { text: label }), el('td', { text: suiteCell(f.suite_before, key) }), el('td', { text: suiteCell(f.suite_after, key) })));
      const newly = (f.newly_failing || []).length ? f.newly_failing.join(', ') : (f.suite_after && f.suite_after.ran ? 'none' : '-');
      rows.push(el('tr', null, el('td', { text: 'Newly failing tests' }), el('td', { text: '-' }), el('td', { text: newly })));
      root.append(section('The project\'s own test suite', el('div', { class: 'table-scroll md' },
        el('table', null,
          el('thead', null, el('tr', null, el('th', { text: '' }), el('th', { text: 'Without the change' }), el('th', { text: 'With the change' }))),
          el('tbody', null, rows)))));
    }
    if ((f.attempts || []).length) {
      const rows = f.attempts.map((a) => el('tr', null,
        el('td', { text: String(a.number) }),
        el('td', { text: ATTEMPT_OUTCOMES[a.outcome] || a.outcome }),
        el('td', { text: String(a.lines_changed || 0) }),
        el('td', { text: formatCost(a.cost_usd) }),
        el('td', { class: 'attempt-detail', text: a.reason || '-' })));
      root.append(section('Attempts', el('div', { class: 'table-scroll md' },
        el('table', null,
          el('thead', null, el('tr', null, ['#', 'Outcome', 'Changed lines', 'Model cost', 'Detail'].map((h) => el('th', { text: h })))),
          el('tbody', null, rows)))));
    }
    if (f.repro_test_code) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy' });
      copy.addEventListener('click', () => copyText(f.repro_test_code, copy));
      root.append(section('Regression test',
        el('div', { class: 'code-head' }, el('span', { class: 'code-path', text: f.repro_test_path || 'test.py' }), copy),
        codeBlock(f.repro_test_code, 'python')));
    }
    if (f.log_excerpt && f.status !== 'verified') {
      root.append(section('Output of the step that stopped it', codeBlock(f.log_excerpt, 'text')));
    }
    const u = f.usage || {};
    root.append(section('Cost and time', el('dl', { class: 'facts' }, [
      ['Model cost', `${formatCost(u.cost_usd)} of ${formatCost(f.budget_usd)}`], ['Time', formatDuration(u.wall_seconds || 0)],
      ['Model calls', String(u.llm_calls ?? 0)], ['Sandbox runs', String(u.sandbox_runs ?? 0)],
    ].map(([k, v]) => fact(k, v)))));

    if (markdown) {
      const copy = el('button', { class: 'btn small', type: 'button', text: 'Copy markdown' });
      copy.addEventListener('click', () => copyText(markdown, copy));
      root.append(el('section', { class: 'card' },
        el('div', { class: 'card-head' }, el('h3', { text: 'Markdown summary' }), copy),
        el('details', null, el('summary', { text: 'Show the summary as rendered markdown' }), renderMarkdown(markdown, repo))));
    }
    return root;
  }

  globalThis.FY = {
    $, el, safeUrl, link, gh, refLink, commitLink, linkify, formatDuration, formatCost, formatDate,
    copyText, codeBlock, renderInline, renderMarkdown, renderReport, renderBackport, renderFix, banner,
    otherCallers,
    VERDICTS, BACKPORT_STATUS, FIX_STATUS,
  };
})();
