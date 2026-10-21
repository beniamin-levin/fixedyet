"""Render an `Assessment` as a markdown report.

Everything that came from an issue, a web page or a model is treated as untrusted text: it is put
on one line where it sits in a table or list, and code goes into fenced blocks whose fence is
longer than any backtick run inside.
"""

from __future__ import annotations

import re

from fixedyet.models import Assessment, Evidence, SideRun, Verdict

HEADLINES: dict[Verdict, str] = {
    Verdict.FIXED_ON_MAIN: "Fixed on main: fails at the reported version, passes on the fix side",
    Verdict.OPEN_BUG: "Open bug: fails at the reported version and still fails on the fix side",
    Verdict.NOT_REPRODUCED: "Not reproduced: the test passes at the reported version",
    Verdict.INCONSISTENT: "Inconsistent: the results cannot be compared, so nothing is claimed",
    Verdict.NEEDS_INFO: "Needs info: the issue has no runnable reproduction",
    Verdict.UNSUPPORTED: "Unsupported: the environment or the test could not be run",
}
LOG_EXCERPT_CHARS = 1_500
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

    out += ["", "## Usage", "", _usage_line(a)]

    images = [
        (label, side)
        for label, side in (("Reported version", rep.reported_side), ("Fix side", rep.fix_side))
        if side and side.checkpoint_image
    ]
    if images:
        out += ["", "## Rerun this yourself", "", "Checkpoint images (runs fork from them):", ""]
        for label, side in images:
            out.append(f"- {label} (`{_cell(side.ref)}`): `{_cell(side.checkpoint_image or '')}`")
        out += [
            "",
            "Start a sandbox from either image and run:",
            "",
            _fence(f"cd /work/repo && python -m pytest -x -q {rep.repro_test_path or '.'}", "sh"),
            "",
            f"Or repeat the whole assessment: `fixedyet run {_cell(a.issue_url)}`",
        ]
    return "\n".join(out).rstrip() + "\n"
