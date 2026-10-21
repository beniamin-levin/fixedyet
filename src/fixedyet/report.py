"""Render an `Assessment` as a markdown report.

Everything that came from an issue, a web page or a model is treated as untrusted text: it is put
on one line where it sits in a table or list, and code goes into fenced blocks whose fence is
longer than any backtick run inside.
"""

from __future__ import annotations

import re

from fixedyet.models import (
    Assessment,
    BackportResult,
    BackportStatus,
    Evidence,
    FixResult,
    FixStatus,
    SideRun,
    SuiteRun,
    Verdict,
)

HEADLINES: dict[Verdict, str] = {
    Verdict.FIXED_ON_MAIN: "Fixed on main: fails at the reported version, passes on the fix side",
    Verdict.OPEN_BUG: "Open bug: fails at the reported version and still fails on the fix side",
    Verdict.NOT_REPRODUCED: "Not reproduced: the test passes at the reported version",
    Verdict.INCONSISTENT: "Inconsistent: the results cannot be compared, so nothing is claimed",
    Verdict.NEEDS_INFO: "Needs info: the issue has no runnable reproduction",
    Verdict.UNSUPPORTED: "Unsupported: the environment or the test could not be run",
}
BACKPORT_HEADLINES: dict[BackportStatus, str] = {
    BackportStatus.VERIFIED: "Verified backport: the fix applies to {branch} and the tests agree",
    BackportStatus.CONFLICTS: "Backport blocked: the fix conflicts with {branch}",
    BackportStatus.REGRESSION: "Backport not safe: the fix breaks other tests on {branch}",
    BackportStatus.ALREADY_FIXED_ON_BRANCH: "No backport needed: {branch} already has the fix",
    BackportStatus.NOT_APPLICABLE: "No backport possible",
    BackportStatus.ERROR: "Backport not verified: a step could not be completed",
}
FIX_HEADLINES: dict[FixStatus, str] = {
    FixStatus.VERIFIED: "Verified fix: the test fails on {branch} without it and passes with it",
    FixStatus.NOT_FIXED: "No verified fix: the attempts did not pass the checks",
    FixStatus.ALREADY_FIXED: "Nothing to fix: the test already passes on {branch}",
    FixStatus.NOT_APPLICABLE: "No fix attempted",
    FixStatus.ERROR: "Fix not verified: a step could not be completed",
}
LOG_EXCERPT_CHARS = 1_500
PATCH_MARKDOWN_CHARS = 12_000
ROOT_CAUSE_NOTE = (
    "Written by the model from the issue, the test and the logs. The sandbox runs did not check it."
)


def _log_tail(text: str, limit: int = LOG_EXCERPT_CHARS) -> str:
    """The last ``limit`` characters of a log, without the half line the cut would leave."""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    newline = cut.find("\n")
    return cut[newline + 1 :] if 0 <= newline < len(cut) - 1 else cut


def _line(text: str) -> str:
    """Collapse to one line so untrusted text cannot break out of a table row or list item."""
    return " ".join(str(text).split())


def _cell(text: str) -> str:
    return _line(text).replace("|", "\\|")


def _fence(text: str, lang: str = "") -> str:
    """A fenced block that cannot be closed early by backticks inside `text`."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def _link(title: str, url: str | None) -> str:
    title = _line(title).replace("[", "(").replace("]", ")")
    if url and url.startswith(("http://", "https://")) and " " not in url and ")" not in url:
        return f"[{title}]({url})"
    return title


def _results_table(a: Assessment) -> str | None:
    rep = a.reproduction
    sides: list[tuple[str, SideRun]] = []
    if rep.reported_side:
        sides.append(("Reported version", rep.reported_side))
    if rep.fix_side:
        sides.append(("Fix side", rep.fix_side))
    if not sides:
        return None
    runs = max(len(side.outcomes) for _, side in sides) or 1
    header = "| Side | " + " | ".join(f"Run {i}" for i in range(1, runs + 1)) + " |"
    divider = "|---|" + "---|" * runs
    rows = []
    for label, side in sides:
        cells = [o.value.upper() for o in side.outcomes]
        cells += ["-"] * (runs - len(cells))
        rows.append(f"| {label} (`{_cell(side.ref)}`) | " + " | ".join(cells) + " |")
    return "\n".join([header, divider, *rows])


def _evidence_item(item: Evidence) -> str:
    text = f"- **{item.kind}**: {_link(item.title, item.url)}"
    return text + (f" - {_line(item.why)}" if item.why else "")


def _usage_line(a: Assessment) -> str:
    u = a.usage
    total = u.input_tokens + u.output_tokens
    parts = [
        f"{u.llm_calls} LLM calls",
        f"{total:,} tokens ({u.input_tokens:,} in, {u.output_tokens:,} out)",
        f"${u.cost_usd:.4f}",
        f"{u.sandbox_runs} sandbox runs",
        f"{u.search_credits} search credits",
        f"{u.wall_seconds:.0f} s wall time",
    ]
    if a.model:
        parts.append(f"model {a.model}")
    return ", ".join(parts)


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./+@-]*$")


def _repo_of(url: str) -> str | None:
    match = re.match(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/\d+$", url)
    return match.group(1) if match and _REPO_RE.match(match.group(1)) else None


def _rerun_section(a: Assessment) -> list[str]:
    """Commands that repeat each side from scratch: a clone at the commit the side ran on.

    Checkpoint images are not offered: both sides fork the same baseline (the fix side checks out
    its ref inside the fork), and old images are pruned within minutes when runs overlap.
    """
    rep = a.reproduction
    repo = _repo_of(a.issue_url)
    sides = [(label, side) for label, side in (("Reported version", rep.reported_side),
                                               ("Fix side", rep.fix_side)) if side]
    if not repo or not sides or not rep.repro_test_code:
        return []
    test_path = rep.repro_test_path or "tests/test_fixedyet_repro.py"
    out = ["", "## Rerun this yourself", "",
           "Each side is a fresh clone at the commit it ran on, installed in editable mode with "
           f"pytest, with the reproduction test above saved as `{_cell(test_path)}`."]
    for label, side in sides:
        commit = side.commit if side.commit and re.fullmatch(r"[0-9a-f]{40}", side.commit) else None
        ref = side.ref if (_GIT_REF_RE.match(side.ref or "") and ".." not in side.ref
                           and "gold_patch" not in side.ref) else None
        target = commit or ref
        if target is None:  # "gold_patch" and the like: nothing a checkout can repeat
            out += ["", f"{label} (`{_cell(side.ref)}`): not a git ref, so no command is given."]
            continue
        what = f"`{_cell(side.ref)}` at `{commit[:12]}`" if commit and ref and ref != commit \
            else f"`{_cell(target)}`"
        note = "" if commit else " The commit was not recorded, so this is the ref as it is now."
        out += ["", f"{label} ({what}):{note}", "", _fence("\n".join([
            f"git clone https://github.com/{repo}.git fixedyet-rerun && cd fixedyet-rerun",
            f"git checkout {target}",
            "python -m pip install -e '.[test]' || python -m pip install -e .",
            "python -m pip install pytest",
            f"# save the reproduction test as {test_path}, then:",
            f"python -m pytest -x -q -p no:cacheprovider {test_path}",
        ]), "sh")]
    out += ["", f"Or repeat the whole assessment: `fixedyet run {_cell(a.issue_url)}`"]
    return out


def render_markdown(a: Assessment) -> str:
    rep = a.reproduction
    out: list[str] = [f"# {HEADLINES[a.verdict]}", ""]
    out.append(f"**Issue:** {_link(a.title or a.issue_url, a.issue_url)}  ")
    out.append(f"**Verdict:** `{a.verdict.value}`  ")
    out.append(f"**Confidence:** {a.confidence.value}")
    if a.confidence_basis:
        out += ["", "Basis:"] + [f"- {_line(b)}" for b in a.confidence_basis]
    if a.not_reproduced_reason:
        out += ["", f"**Likely reason it was not reproduced:** `{a.not_reproduced_reason.value}`"]

    table = _results_table(a)
    if table:
        out += ["", "## Results", "", table]
    facts = []
    if rep.fixing_commit:
        facts.append(f"- Fixing commit: `{_cell(rep.fixing_commit)}`")
    if rep.fixed_in_release:
        facts.append(f"- Fixed in release: `{_cell(rep.fixed_in_release)}`")
    if facts:
        out += [""] + facts

    if rep.repro_test_code:
        out += ["", f"## Reproduction test (`{_cell(rep.repro_test_path or 'test.py')}`)", ""]
        out.append(_fence(rep.repro_test_code, "python"))
    if rep.reported_side and rep.reported_side.log_excerpt:
        out += ["", "## Output at the reported version (end of the log)", ""]
        out.append(_fence(_log_tail(rep.reported_side.log_excerpt)))
    # The fix side's log matters when it failed: it is the proof that the bug is still there, or
    # the reason the two sides cannot be compared.
    if (
        a.verdict in (Verdict.OPEN_BUG, Verdict.INCONSISTENT)
        and rep.fix_side
        and rep.fix_side.log_excerpt
    ):
        out += ["", "## Output on the fix side (end of the log)", ""]
        out.append(_fence(_log_tail(rep.fix_side.log_excerpt)))

    if a.evidence:
        out += ["", "## Evidence", ""] + [_evidence_item(item) for item in a.evidence]
    if a.root_cause:
        out += ["", "## Root cause", "", a.root_cause.strip()]
        out += ["", f"_{ROOT_CAUSE_NOTE}_"]
    if a.next_actions:
        out += ["", "## Next actions", ""]
        out += [f"{i}. {_line(action)}" for i, action in enumerate(a.next_actions, 1)]

    if a.backport is not None:
        out += ["", *backport_section(a.backport)]
    if a.fix is not None:
        out += ["", *fix_section(a.fix)]

    out += ["", "## Usage", "", _usage_line(a)]

    out += _rerun_section(a)
    return "\n".join(out).rstrip() + "\n"


# -- verified backport ----------------------------------------------------------------------------


def _suite_cell(suite: SuiteRun | None, field_name: str) -> str:
    if suite is None or not suite.ran:
        return "not run" if suite is None else "did not finish"
    return str(getattr(suite, field_name))


def _suite_table(r: BackportResult) -> str | None:
    if r.suite_before is None and r.suite_after is None:
        return None
    rows = [
        "| Project test suite | Before the cherry-pick | After the cherry-pick |",
        "|---|---|---|",
    ]
    for label, name in (("Passed", "passed"), ("Failed", "failed"), ("Errors", "errors"),
                        ("Skipped", "skipped")):
        before, after = _suite_cell(r.suite_before, name), _suite_cell(r.suite_after, name)
        rows.append(f"| {label} | {before} | {after} |")
    both_ran = bool(r.suite_before and r.suite_before.ran and r.suite_after and r.suite_after.ran)
    rows.append(
        "| Newly failing tests | - | "
        + (", ".join(f"`{_cell(t)}`" for t in r.newly_failing) if r.newly_failing
           else ("none" if both_ran else "-"))
        + " |"
    )
    return "\n".join(rows)


def _repro_table(r: BackportResult) -> str | None:
    sides = [("Before the cherry-pick", r.repro_before), ("After the cherry-pick", r.repro_after)]
    sides = [(label, side) for label, side in sides if side is not None]
    if not sides:
        return None
    runs = max(len(side.outcomes) for _, side in sides) or 1
    header = f"| Reproduction test on `{_cell(r.target_branch or '?')}` | " + " | ".join(
        f"Run {i}" for i in range(1, runs + 1)) + " |"
    rows = [header, "|---|" + "---|" * runs]
    for label, side in sides:
        cells = [o.value.upper() for o in side.outcomes] + ["-"] * (runs - len(side.outcomes))
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def backport_section(r: BackportResult, level: int = 2) -> list[str]:
    """The "Verified backport" section as markdown lines.

    It is shown whatever the status, so a conflict or a regression is reported just as plainly as
    a success; the first line says which one it was.
    """
    hashes = "#" * level
    out = [f"{hashes} Verified backport", "", f"**Status:** `{r.status.value}`  "]
    if r.reason:
        out.append(f"**Result:** {_line(r.reason)}  ")
    if r.target_branch:
        head = f" (head `{_cell(r.target_branch_head[:12])}`)" if r.target_branch_head else ""
        out.append(f"**Target branch:** `{_cell(r.target_branch)}`{head}  ")
    if r.reported_version:
        out.append(f"**Reported version:** `{_cell(r.reported_version)}`  ")
    if r.fixing_commit:
        url = (
            f"https://github.com/{r.repo}/commit/{r.fixing_commit}"
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", r.repo or "") else None
        )
        out.append(f"**Fixing commit on main:** {_link(r.fixing_commit[:12], url)}  ")
    out[-1] = out[-1].rstrip()

    repro = _repro_table(r)
    if repro:
        out += ["", repro]
    suite = _suite_table(r)
    if suite:
        out += ["", suite]
    if r.conflicting_files:
        out += ["", "Conflicting files (the cherry-pick was aborted, nothing was resolved):", ""]
        out += [f"- `{_cell(name)}`" for name in r.conflicting_files]
    if r.commit_message:
        out += ["", "Cherry-picked commit message:", "", _fence(r.commit_message)]
    if r.patch:
        patch = r.patch
        note = ""
        if len(patch) > PATCH_MARKDOWN_CHARS:
            patch = patch[:PATCH_MARKDOWN_CHARS].rsplit("\n", 1)[0]
            note = f"\n\n_The patch is cut here; the whole patch is {len(r.patch):,} characters._"
        out += ["", "Patch (`git format-patch` of the cherry-pick):", "", _fence(patch, "diff")]
        out[-1] += note
    if r.log_excerpt and r.status not in (BackportStatus.VERIFIED,):
        out += ["", "Output of the step that stopped it (end of the log):", "",
                _fence(_log_tail(r.log_excerpt))]
    u = r.usage
    out += ["", f"_{u.sandbox_runs} sandbox runs, {u.wall_seconds:.0f} s wall time, "
            f"${u.cost_usd:.4f} model cost._"]
    return out


def render_backport_markdown(r: BackportResult) -> str:
    """A standalone markdown summary of a backport run (what `fixedyet backport` prints)."""
    headline = BACKPORT_HEADLINES[r.status].format(branch=r.target_branch or "the branch")
    out = [f"# {headline}", ""]
    if r.issue_url:
        out.append(f"**Issue:** {_link(r.title or r.issue_url, r.issue_url)}")
        out.append("")
    out += backport_section(r)
    return "\n".join(out).rstrip() + "\n"


# -- fix mode -------------------------------------------------------------------------------------


def _fix_suite_table(r: FixResult) -> str | None:
    if r.suite_before is None and r.suite_after is None:
        return None
    rows = ["| Project test suite | Without the change | With the change |", "|---|---|---|"]
    for label, name in (("Passed", "passed"), ("Failed", "failed"), ("Errors", "errors"),
                        ("Skipped", "skipped")):
        rows.append(f"| {label} | {_suite_cell(r.suite_before, name)} | "
                    f"{_suite_cell(r.suite_after, name)} |")
    both_ran = bool(r.suite_before and r.suite_before.ran and r.suite_after and r.suite_after.ran)
    newly = (", ".join(f"`{_cell(t)}`" for t in r.newly_failing) if r.newly_failing
             else ("none" if both_ran else "-"))
    rows.append(f"| Newly failing tests | - | {newly} |")
    return "\n".join(rows)


def _fix_repro_table(r: FixResult) -> str | None:
    sides = [(label, side) for label, side in (("Without the change", r.repro_before),
                                                ("With the change", r.repro_after)) if side]
    if not sides:
        return None
    runs = max(len(side.outcomes) for _, side in sides) or 1
    rows = [f"| Reproduction test on `{_cell(r.base_branch or 'main')}` | "
            + " | ".join(f"Run {i}" for i in range(1, runs + 1)) + " |", "|---|" + "---|" * runs]
    for label, side in sides:
        cells = [o.value.upper() for o in side.outcomes] + ["-"] * (runs - len(side.outcomes))
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _callers_lines(r: FixResult) -> list[str]:
    """The other callers of what the verified change edits: the code the test did not run."""
    if r.status is not FixStatus.VERIFIED or not r.changed_functions:
        return []
    names = ", ".join(f"`{_cell(n)}`" for n in r.changed_functions)
    head = "**Other callers of the changed code, not covered by the reproduction**"
    if not r.other_callers:
        return ["", f"{head}: none found for {names} (searched by name, best effort)."]
    out = ["", f"{head} (searched by name, best effort; the suite passing does not show that "
           "they behave as before):", ""]
    out += [f"- `{_cell(s.path)}:{s.line}` uses `{_cell(s.symbol)}`" for s in r.other_callers]
    if r.other_callers_total > len(r.other_callers):
        out.append(f"- and {r.other_callers_total - len(r.other_callers)} more")
    return out


def fix_section(r: FixResult, level: int = 2) -> list[str]:
    """The "Fix" section as markdown lines, whatever the status: the attempts are always listed,
    so a fix that failed is reported as plainly as one that worked."""
    hashes = "#" * level
    out = [f"{hashes} Fix", "", f"**Status:** `{r.status.value}`  "]
    if r.reason:
        out.append(f"**Result:** {_line(r.reason)}  ")
    if r.base_branch:
        head = f" at `{_cell((r.base_sha or '')[:12])}`" if r.base_sha else ""
        out.append(f"**Verified on:** `{_cell(r.base_branch)}`{head}  ")
    if r.context_files:
        shown = ", ".join(f"`{_cell(p)}`" for p in r.context_files)
        out.append(f"**Code shown to the model:** {shown}  ")
    if r.regression_test_path:
        out.append(f"**Regression test in the pull request:** `{_cell(r.regression_test_path)}`  ")
    elif r.status is FixStatus.VERIFIED:
        why = r.regression_test_note or "the repository has no tests"
        out.append(f"**Regression test in the pull request:** none ({_line(why)})  ")
    out[-1] = out[-1].rstrip()
    if r.summary:
        out += ["", f"**Change:** {_line(r.summary)}"]
    if r.explanation:
        out += ["", f"**The model's account** (not checked by the sandbox): "
                f"{_line(r.explanation)}"]
    repro = _fix_repro_table(r)
    if repro:
        out += ["", repro]
    out += _callers_lines(r)
    suite = _fix_suite_table(r)
    if suite:
        out += ["", suite]
    if r.attempts:
        out += ["", "| Attempt | Outcome | Changed lines | Model cost | Detail |",
                "|---|---|---|---|---|"]
        for a in r.attempts:
            out.append(f"| {a.number} | `{a.outcome}` | {a.lines_changed} | "
                       f"${a.cost_usd:.4f} | {_cell(a.reason) or '-'} |")
    if r.diff:
        diff = r.diff
        note = ""
        if len(diff) > PATCH_MARKDOWN_CHARS:
            diff = diff[:PATCH_MARKDOWN_CHARS].rsplit("\n", 1)[0]
            note = f"\n\n_The diff is cut here; the whole diff is {len(r.diff):,} characters._"
        label = "Verified diff" if r.status is FixStatus.VERIFIED else "Diff (not verified)"
        out += ["", f"{label}:", "", _fence(diff, "diff")]
        out[-1] += note
    if r.log_excerpt and r.status is not FixStatus.VERIFIED:
        out += ["", "Output of the step that stopped it (end of the log):", "",
                _fence(_log_tail(r.log_excerpt))]
    u = r.usage
    out += ["", f"_{len(r.attempts)} attempt(s), {u.llm_calls} model calls, {u.sandbox_runs} "
            f"sandbox runs, {u.wall_seconds:.0f} s wall time, ${u.cost_usd:.4f} model cost "
            f"(cap ${r.budget_usd:.2f})._"]
    return out


def render_fix_markdown(r: FixResult) -> str:
    """A standalone markdown summary of a fix run (what `fixedyet fix` prints)."""
    headline = FIX_HEADLINES[r.status].format(branch=r.base_branch or "main")
    out = [f"# {headline}", ""]
    if r.issue_url:
        out += [f"**Issue:** {_link(r.title or r.issue_url, r.issue_url)}", ""]
    out += fix_section(r)
    return "\n".join(out).rstrip() + "\n"
