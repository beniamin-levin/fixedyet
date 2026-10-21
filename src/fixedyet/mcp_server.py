"""MCP server for FixedYet (stdio transport).

Four tools:

``reproduce_issue(issue_url, fix_ref=None)``
    Runs the full pipeline on a public GitHub issue and returns the markdown report followed
    by a JSON block with the whole Assessment.

``check_fixed(issue_url, ref)``
    Runs the same pipeline with ``ref`` (a branch, tag or commit) as the fix side and returns
    ``{"fixed": bool | None, "verdict", "confidence", "summary"}``.

``propose_backport(issue_url, target_branch=None)``
    Dry run only: reproduces the issue, and when it is fixed on main cherry-picks the fix onto
    the maintenance branch of the reported version in a sandbox. Returns the markdown summary and
    a JSON block with the BackportResult (status, patch, test results before and after). It
    never pushes anything and never opens a pull request; that is `fixedyet backport --open-pr`.

``propose_fix(issue_url)``
    Dry run only: reproduces the issue, and when the bug is still open on main asks the model
    for the smallest change that makes the reproduction test pass, then verifies it in the
    sandbox (the test fails twice without it and passes twice with it, and the project's suite
    has no new failure). Returns the markdown summary and a JSON block with the FixResult (status,
    diff, attempts, suite results). It never pushes anything and never opens a pull request;
    that is `fixedyet fix --open-pr`.

A run takes a few minutes and spends Token Factory, Sandboxes and Tavily credits, so clients
should call these tools on purpose. The server reads its keys from the environment or from a
``.env`` file in its working directory (see ``.env.example``).

Register it in Claude Code::

    claude mcp add fixedyet -- fixedyet mcp
    # or, without installing the console script:
    claude mcp add fixedyet -- /path/to/fixedyet/.venv/bin/python -m fixedyet.mcp_server
    # pass keys explicitly instead of using a .env file (one -e KEY=value per key, for
    # NEBIUS_API_KEY and TAVILY_API_KEY):
    claude mcp add fixedyet -e KEY=value -- fixedyet mcp

Register it in any other MCP client (Claude Desktop, Cursor, ...) with the usual stdio entry::

    {"mcpServers": {"fixedyet": {"command": "fixedyet", "args": ["mcp"],
                                    "env": {"NEBIUS_API_KEY": "...", "TAVILY_API_KEY": "..."}}}}

Nothing here may write to stdout: on stdio transport stdout carries the protocol.
"""

from __future__ import annotations

import re
from typing import Any

from fixedyet.models import Assessment, BackportResult, FixResult, Verdict
from fixedyet.web import parse_issue_url

try:  # mcp >= 2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

_REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/@^~+-]{0,199}$")

mcp = _Server(
    "fixedyet",
    instructions=(
        "FixedYet reproduces a public GitHub bug report in a sandbox at the reported "
        "version, runs the same test against a fix side (main by default), and returns a "
        "rule-based verdict with evidence. Runs take a few minutes and cost money, so call "
        "the tools only when the user wants a bug checked. propose_backport and propose_fix are "
        "dry runs: they never push or open a pull request."
    ),
)


# --- seams to the core modules (imported lazily; tests replace these) -----------------


async def run_pipeline(issue_url: str, fix_ref: str | None) -> Assessment:
    from fixedyet.pipeline import reproduce

    return await reproduce(issue_url, fix_ref=fix_ref)


def render_report(assessment: Assessment) -> str:
    from fixedyet.report import render_markdown

    return render_markdown(assessment)


async def run_backport(assessment: Assessment, target_branch: str | None) -> BackportResult:
    from fixedyet.backport import backport

    return await backport(assessment, target_branch=target_branch)


def render_backport(result: BackportResult) -> str:
    from fixedyet.report import render_backport_markdown

    return render_backport_markdown(result)


async def run_fix(assessment: Assessment) -> FixResult:
    from fixedyet.fixmode import attempt_fix

    return await attempt_fix(assessment)


def render_fix(result: FixResult) -> str:
    from fixedyet.report import render_fix_markdown

    return render_fix_markdown(result)


# --- helpers --------------------------------------------------------------------------


def _clean_url(issue_url: str) -> str:
    return parse_issue_url(issue_url).url  # ValueError carries a friendly message


def _clean_ref(ref: str) -> str:
    ref = ref.strip()
    if not ref:
        raise ValueError("ref is required: give a branch, tag or commit to check against.")
    if not _REF.match(ref) or ".." in ref:
        raise ValueError(
            "That does not look like a git ref. Use a branch, tag or commit hash, "
            "for example main, v2.4.0 or 3f9c2ab."
        )
    return ref


def fixed_flag(verdict: Verdict) -> bool | None:
    """True when the bug is gone on the fix side, False when it is still there, else None.

    "Not reproduced" is None on purpose: if the test never failed at the reported version,
    it says nothing about whether the ref fixed anything.
    """
    if verdict is Verdict.FIXED_ON_MAIN:
        return True
    if verdict is Verdict.OPEN_BUG:
        return False
    return None


def summarise(assessment: Assessment, ref: str) -> str:
    """One or two sentences for a caller that only wants the answer."""
    verdict = assessment.verdict
    if verdict is Verdict.FIXED_ON_MAIN:
        text = f"The bug reproduces at the reported version and is fixed at {ref}."
        commit = assessment.reproduction.fixing_commit
        release = assessment.reproduction.fixed_in_release
        if commit:
            text += f" Fixing commit: {commit}."
        if release:
            text += f" First release with the fix: {release}."
    elif verdict is Verdict.OPEN_BUG:
        text = f"The bug reproduces at the reported version and still reproduces at {ref}."
    elif verdict is Verdict.NOT_REPRODUCED:
        text = (
            "The bug did not reproduce at the reported version, "
            f"so nothing can be said about {ref}."
        )
        if assessment.not_reproduced_reason:
            text += f" Likely reason: {assessment.not_reproduced_reason.value}."
    elif verdict is Verdict.INCONSISTENT:
        text = (
            "The results contradict each other, changed between runs, or the test failed for a "
            "different reason on the fix side, so no claim is made about whether the bug is fixed."
        )
    elif verdict is Verdict.NEEDS_INFO:
        text = "The issue has no runnable reproduction, so nothing was run."
    else:
        text = "The environment could not be built, so there is no verdict about the bug."
    if assessment.confidence.value == "low":
        text += " Confidence is low."
    return text


# --- tools ----------------------------------------------------------------------------


@mcp.tool()
async def reproduce_issue(issue_url: str, fix_ref: str | None = None) -> str:
    """Reproduce a public GitHub issue in a sandbox and return a verdict.

    Writes a pytest reproduction, runs it at the version the issue reports and again on the
    fix side, and applies fixed rules to the results. Takes a few minutes and costs money.

    Args:
        issue_url: URL of a public GitHub issue, like https://github.com/owner/repo/issues/123.
        fix_ref: Branch, tag or commit to use as the fix side. Defaults to the default branch.

    Returns:
        The markdown report, then a fenced JSON block with the full Assessment.
    """
    url = _clean_url(issue_url)
    ref = _clean_ref(fix_ref) if fix_ref and fix_ref.strip() else None
    assessment = await run_pipeline(url, ref)
    try:
        report = render_report(assessment)
    except Exception:  # a rendering bug must not hide the verdict
        report = f"# {assessment.title}\n\nVerdict: {assessment.verdict.value}"
    return f"{report.rstrip()}\n\n```json\n{assessment.model_dump_json(indent=2)}\n```\n"


@mcp.tool()
async def check_fixed(issue_url: str, ref: str) -> dict[str, Any]:
    """Check whether a bug from a GitHub issue is fixed at a given branch, tag or commit.

    Reproduces the issue at the reported version, then runs the same test at `ref`.

    Args:
        issue_url: URL of a public GitHub issue, like https://github.com/owner/repo/issues/123.
        ref: Branch, tag or commit to check, for example main, v2.4.0 or a commit hash.

    Returns:
        fixed: true if the bug reproduces at the reported version and not at ref, false if it
            still reproduces at ref, null if that cannot be said (not reproduced, inconsistent,
            needs info or unsupported).
        verdict, confidence: the rule-based result.
        summary: one or two sentences.
    """
    url = _clean_url(issue_url)
    checked_ref = _clean_ref(ref)
    assessment = await run_pipeline(url, checked_ref)
    return {
        "fixed": fixed_flag(assessment.verdict),
        "verdict": assessment.verdict.value,
        "confidence": assessment.confidence.value,
        "summary": summarise(assessment, checked_ref),
    }


@mcp.tool()
async def propose_backport(issue_url: str, target_branch: str | None = None) -> str:
    """Prepare a verified backport of a fix, as a dry run (nothing is pushed or opened).

    Reproduces the issue; if the bug is fixed on main and the fixing commit is known, finds the
    maintenance branch of the reported version (for example 8.1.x, stable/8.1, release-8.1), and
    in a sandbox checks that the reproduction test fails there, cherry-picks the fix (conflicts
    are reported, never resolved), checks that the test passes, and compares the project's own
    test suite before and after. Takes several minutes and costs money.

    Args:
        issue_url: URL of a public GitHub issue, like https://github.com/owner/repo/issues/123.
        target_branch: Branch to backport onto. Default: found from the repository's branches.

    Returns:
        The markdown summary, then a fenced JSON block with the BackportResult. Its status is
        one of verified, conflicts, regression, already_fixed_on_branch, not_applicable, error.
    """
    url = _clean_url(issue_url)
    branch = _clean_ref(target_branch) if target_branch and target_branch.strip() else None
    assessment = await run_pipeline(url, None)
    result = await run_backport(assessment, branch)
    try:
        report = render_backport(result)
    except Exception:  # a rendering bug must not hide the status
        report = f"# Backport: {result.status.value}\n\n{result.reason}"
    return f"{report.rstrip()}\n\n```json\n{result.model_dump_json(indent=2)}\n```\n"


@mcp.tool()
async def propose_fix(issue_url: str) -> str:
    """Propose a fix for an open bug and verify it in a sandbox, as a dry run (nothing is pushed
    or opened).

    Reproduces the issue; if the bug is still open on main (verdict open_bug), asks the model for
    the smallest source change that makes the reproduction test pass, at most 3 attempts. A change
    counts as verified only when, in the sandbox, the test fails twice without it and passes twice
    with it, and the project's own test suite has no newly failing test. Only existing source
    files may change (never a test, CI or packaging file), at most 80 lines. Takes several minutes
    and costs money; the model cost of the fix is capped by FIXEDYET_FIX_BUDGET_USD.

    Args:
        issue_url: URL of a public GitHub issue, like https://github.com/owner/repo/issues/123.

    Returns:
        A line with the reproduction verdict, the markdown summary, then a fenced JSON block with
        the FixResult. Its status is one of verified, not_fixed, already_fixed, not_applicable
        (the bug is not an open bug), error. A verified diff is evidence, not a review.
    """
    url = _clean_url(issue_url)
    assessment = await run_pipeline(url, None)
    result = await run_fix(assessment)
    try:
        report = render_fix(result)
    except Exception:  # a rendering bug must not hide the status
        report = f"# Fix: {result.status.value}\n\n{result.reason}"
    verdict = (
        f"Reproduction verdict: {assessment.verdict.value} "
        f"({assessment.confidence.value} confidence). Nothing was pushed and no pull request "
        "was opened."
    )
    return (
        f"{verdict}\n\n{report.rstrip()}\n\n```json\n{result.model_dump_json(indent=2)}\n```\n"
    )


def main() -> None:
    """Run the server on stdio. This is what ``fixedyet mcp`` calls."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
