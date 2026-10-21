"""Verified backports: take the fix that closed a bug on main and prove it works on the
maintenance branch of the version the reporter used.

    assessment (fixed_on_main, fixing commit known)
      -> find the maintenance branch for the reported version      (git ls-remote --heads)
      -> sandbox: fresh checkout of that branch head
           a. the reproduction test must FAIL                        else already_fixed_on_branch
           c. git cherry-pick -x <fixing commit>                     (-m 1 for a merge commit)
           d. conflicts: abort and report the files                  the model never resolves them
           e. reinstall; the reproduction test must PASS
           b. the project's own test suite, before and after         (900 s each, no -x)
           f. a test that fails after and did not fail before is a regression
           g. git format-patch of the cherry-pick

The suites (b) run after the cheap checks, from forks of the untouched branch image and of the
cherry-picked image, so a conflict is reported at once instead of after 900 s of tests.
      -> BackportResult

Everything that runs project code runs in the sandbox. The host only lists branch names with
`git ls-remote`. No model is called here, so a backport costs sandbox time and no tokens.

Shell scripts are built by small pure functions and their output is parsed by pure functions, so
tests can check the exact commands and feed canned sandbox output through the parsers.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from fixedyet import envbuild
from fixedyet.agent import emit_progress
from fixedyet.config import Settings, get_settings
from fixedyet.envbuild import REPO_DIR, EnvBuildError
from fixedyet.github import parse_issue_url
from fixedyet.models import (
    Assessment,
    BackportResult,
    BackportStatus,
    ExecResult,
    Outcome,
    ProgressCallback,
    SideRun,
    SuiteRun,
    Usage,
    Verdict,
)
from fixedyet.pipeline import MeteredSandbox, run_scope
from fixedyet.sandbox import get_sandbox
from fixedyet.sandbox.base import Sandbox, SandboxError

log = logging.getLogger(__name__)

SUITE_TIMEOUT_S = 900
PICK_TIMEOUT_S = 1200
LOG_EXCERPT_CHARS = 3000
SUITE_LOG_BYTES = 400_000
MAX_CONFLICT_FILES = 50
GIT_USER_NAME = "FixedYet"
GIT_USER_EMAIL = "fixedyet@users.noreply.github.com"

BranchLister = Callable[[str], Awaitable[dict[str, str]]]
"""``repo`` ("owner/name") to ``{branch name: head commit}``. Tests replace it."""

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./+-]{0,199}$")


# -- finding the target branch ---------------------------------------------------------------

# A maintenance branch name: an optional prefix, an optional v, MAJOR[.MINOR][.x], an optional
# suffix. 8.1.x, v8.1.x, 8.1, 8.x, stable/8.1, release-8.1, release/8_1, maint/8.1,
# 8.1-maintenance, 3.x-line, 1.10.X-fixes. A numeric patch (1.2.3) is a release, not a
# maintenance line, and never matches.
_BRANCH_RE = re.compile(
    r"""^(?:(?P<prefix>stable|releases?|rel|maint(?:enance)?|branch|backports?|version)[/_-])?
        v?(?P<major>\d+)
        (?:[._](?P<minor>\d+|x))?
        (?:[._](?P<patch>x))?
        (?:[/_-](?P<suffix>x|stable|maint(?:enance)?|branch|releases?|line|fixes|patches))?$""",
    re.IGNORECASE | re.VERBOSE,
)
_VERSION_RE = re.compile(
    r"^(?:v|version[-_/]?|release[-_/]?|rel[-_/]?)?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?![\w])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BranchName:
    """A branch name read as a maintenance line: ``minor`` is None for a major-only line."""

    name: str
    major: int
    minor: int | None
    style: int  # sort key: 0 for the plain `8.1.x`, then `8.1`, then decorated forms


def parse_branch(name: str) -> BranchName | None:
    """Read a branch name as a version line, or None when it is not one (main, stable, fix-1)."""
    match = _BRANCH_RE.match(name)
    if match is None:
        return None
    minor_text = match["minor"]
    minor = int(minor_text) if minor_text and minor_text.lower() != "x" else None
    if minor is None and match["patch"]:  # "8.x.x" is not a form anyone uses
        return None
    style = (0 if match["patch"] else 1) + (2 if match["prefix"] else 0)
    style += 4 if match["suffix"] else 0
    return BranchName(name, int(match["major"]), minor, style)


def parse_version(ref: str | None) -> tuple[int, int | None] | None:
    """``(major, minor)`` of a release tag such as ``8.3.3`` or ``v2.31.0``; None for a branch
    name, a commit hash or anything else that is not a version."""
    text = (ref or "").strip()
    if not text or re.fullmatch(r"[0-9a-f]{7,40}", text):
        return None
    match = _VERSION_RE.match(text)
    if match is None:
        return None
    return int(match["major"]), (int(match["minor"]) if match["minor"] is not None else None)


@dataclass
class BranchMatch:
    branch: str | None
    candidates: list[str] = field(default_factory=list)  # every branch that fits the version
    maintenance: list[str] = field(default_factory=list)  # every version-like branch there is


def find_target_branch(version: str | None, branches: Sequence[str]) -> BranchMatch:
    """The maintenance branch for ``version``: the same MAJOR.MINOR first, then a MAJOR-only line.

    ``8.3.3`` fits ``8.3.x``, ``v8.3.x``, ``stable/8.3``, ``release-8.3``, ``maint/8.3`` and, if
    nothing better exists, ``8.x``. It never fits ``8.4.x``: a fix verified on another minor line
    says nothing about this one. Ties go to the plainest name.
    """
    parsed = [b for name in sorted(set(branches)) if (b := parse_branch(name)) is not None]
    maintenance = [b.name for b in parsed]
    wanted = parse_version(version)
    if wanted is None:
        return BranchMatch(None, [], maintenance)
    major, minor = wanted
    tiers: list[tuple[int, int, str]] = []
    for b in parsed:
        if b.major != major:
            continue
        if minor is not None and b.minor == minor:
            tiers.append((0, b.style, b.name))
        elif b.minor is None:
            tiers.append((1, b.style, b.name))
    tiers.sort()
    names = [name for _, _, name in tiers]
    return BranchMatch(names[0] if names else None, names, maintenance)


async def list_remote_branches(repo: str) -> dict[str, str]:
    """``{branch: head sha}`` of github.com/<repo>, from ``git ls-remote --heads``.

    Runs only `git ls-remote` on the host: it reads branch names and executes nothing from the
    project. Raises `EnvBuildError` when the remote cannot be listed.
    """
    envbuild._check_repo(repo)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-remote", "--heads", f"https://github.com/{repo}.git",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
    except (OSError, TimeoutError) as exc:
        raise EnvBuildError(f"could not list the branches of {repo}: {exc}") from exc
    if proc.returncode != 0:
        detail = err.decode("utf-8", errors="replace").strip()[-300:]
        raise EnvBuildError(f"git ls-remote failed for {repo}: {detail}")
    return parse_ls_remote(out.decode("utf-8", errors="replace"))


def parse_ls_remote(text: str) -> dict[str, str]:
    branches: dict[str, str] = {}
    for line in text.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/") and _SHA_RE.match(sha):
            branches[ref.removeprefix("refs/heads/")] = sha.lower()
    return branches


# -- scripts and their parsers ---------------------------------------------------------------

_PREFIX_LINES = envbuild._PREFIX_LINES
_PICK_MARK = {
    name: re.compile(rf"^__{name}__=(.*)$", re.MULTILINE)
    for name in ("HEAD", "MERGE", "PICKED", "MSG_B64", "PATCH_B64", "PICK_FAILED",
                 "SETUP_FAILED", "CONFLICT")
}
_PREFIXED_LINE = re.compile(r"^\| ?(.*)$", re.MULTILINE)
_RC_LINE = re.compile(r"^__RC__=(-?\d+)$", re.MULTILINE)


def pick_script(commit: str) -> str:
    """Cherry-pick ``commit`` onto the checked-out branch head, reinstall, export the patch.

    Markers on their own lines are the machine-readable output; git's own text is prefixed with
    ``| `` so it can never forge one. A conflict aborts the pick and exits 30.
    """
    if not _SHA_RE.match(commit):
        raise EnvBuildError(f"invalid commit {commit!r}")
    repo_dir = REPO_DIR
    return "\n".join(
        [
            "set -u",
            "export GIT_TERMINAL_PROMPT=0 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1",
            f"cd {repo_dir} || {{ echo __SETUP_FAILED__=cd; exit 20; }}",
            f"git config user.name {GIT_USER_NAME}",
            f"git config user.email {GIT_USER_EMAIL}",
            'echo "__HEAD__=$(git rev-parse HEAD)"',
            f"if ! git cat-file -e {commit}^{{commit}} 2>/dev/null; then",
            f"  git fetch --quiet origin {commit} || {{ echo __SETUP_FAILED__=fetch; exit 21; }}",
            "fi",
            f'if [ "$(git rev-list --parents -n 1 {commit} | wc -w)" -gt 2 ]; then',
            "  echo __MERGE__=1; set -- -m 1",
            "else",
            "  set --",
            "fi",
            f'git cherry-pick -x "$@" {commit} > /tmp/ir_pick.log 2>&1',
            "rc=$?",
            'if [ "$rc" -ne 0 ]; then',
            '  echo "__PICK_FAILED__=$rc"',
            "  git -c core.quotepath=off diff --name-only --diff-filter=U"
            f" | head -n {MAX_CONFLICT_FILES} | awk '{{ print \"__CONFLICT__=\" $0 }}'",
            f"  tail -n 40 /tmp/ir_pick.log | {_PREFIX_LINES}",
            "  git cherry-pick --abort > /dev/null 2>&1",
            "  exit 30",
            "fi",
            'echo "__PICKED__=$(git rev-parse HEAD)"',
            'echo "__MSG_B64__=$(git log -1 --format=%B | base64 | tr -d \'\\n\')"',
            f"({envbuild.INSTALL_CHAIN}) > /tmp/ir_reinstall.log 2>&1 "
            "|| { echo __SETUP_FAILED__=reinstall; "
            f"tail -n 40 /tmp/ir_reinstall.log | {_PREFIX_LINES}; exit 22; }}",
            'echo "__PATCH_B64__=$(git format-patch -1 --stdout --no-signature HEAD'
            " | base64 | tr -d '\\n')\"",
            "exit 0",
            "",
        ]
    )


@dataclass
class PickOutcome:
    """What `pick_script` printed, parsed."""

    head: str | None = None
    merge: bool = False
    picked: str | None = None
    message: str = ""
    patch: str = ""
    conflicts: list[str] = field(default_factory=list)
    pick_failed: bool = False
    setup_failed: str | None = None
    log: str = ""


def _b64(text: str | None) -> str:
    if not text:
        return ""
    try:
        return base64.b64decode(text.strip(), validate=False).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


def _last(name: str, stdout: str) -> str | None:
    found = _PICK_MARK[name].findall(stdout)
    return found[-1].strip() if found else None


def parse_pick(result: ExecResult) -> PickOutcome:
    out = result.stdout
    conflicts = [c.strip() for c in _PICK_MARK["CONFLICT"].findall(out) if c.strip()]
    log = "\n".join(_PREFIXED_LINE.findall(out)).strip()
    return PickOutcome(
        head=_last("HEAD", out),
        merge=_last("MERGE", out) == "1",
        picked=_last("PICKED", out),
        message=_b64(_last("MSG_B64", out)).strip("\n"),
        patch=_b64(_last("PATCH_B64", out)),
        conflicts=conflicts[:MAX_CONFLICT_FILES],
        pick_failed=_last("PICK_FAILED", out) is not None,
        setup_failed=_last("SETUP_FAILED", out),
        log=log[-LOG_EXCERPT_CHARS:],
    )


def suite_script(timeout_s: int = SUITE_TIMEOUT_S) -> str:
    """Run the project's own tests once, every test (no ``-x``), and echo the end of the log.

    ``--continue-on-collection-errors`` keeps one module that cannot be imported from hiding the
    rest; ``--maxfail=0`` undoes a ``-x`` the project's own addopts might set.
    """
    return "\n".join(
        [
            "set -u",
            "export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1",
            f"cd {REPO_DIR} || {{ echo __SETUP_FAILED__=cd; exit 20; }}",
            f"timeout -k 10 {int(timeout_s)} python -m pytest -q -p no:cacheprovider --maxfail=0 "
            "--continue-on-collection-errors --tb=line -rfE > /tmp/ir_suite.log 2>&1",
            "rc=$?",
            f"tail -c {SUITE_LOG_BYTES} /tmp/ir_suite.log | {_PREFIX_LINES}",
            'echo "__RC__=$rc"',
            "exit 0",
            "",
        ]
    )


_SUMMARY_LINE = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped)\b")
_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$", re.MULTILINE)
_FAILING_ENTRY = re.compile(r"^(?:FAILED|ERROR) (?P<id>.+?)(?: - .*)?$")
_DURATION = re.compile(r"\bin (\d+(?:\.\d+)?)s\b")


def parse_suite(result: ExecResult) -> SuiteRun:
    """Counts and failing test ids from `suite_script`'s output.

    ``ran`` is True only when pytest printed its summary line and exited 0 or 1. A timeout, a
    crash, "no tests ran" (exit 5) or a missing summary leave it False, and the caller cannot
    compare such a run with another.
    """
    codes = _RC_LINE.findall(result.stdout)
    exit_code = int(codes[-1]) if codes else None
    timed_out = result.timed_out or exit_code in (124, 137)
    lines = _PREFIXED_LINE.findall(result.stdout)
    text = "\n".join(lines)

    summary = next((ln for ln in reversed(lines) if _SUMMARY_LINE.search(ln)), None)
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    duration = 0.0
    if summary is not None:
        for number, word in _COUNT.findall(summary):
            counts["error" if word.startswith("error") else word] += int(number)
        found = _DURATION.search(summary)
        duration = float(found.group(1)) if found else 0.0

    failing: list[str] = []
    headers = list(_SUMMARY_HEADER.finditer(text))
    if headers:
        for line in text[headers[-1].end():].splitlines():
            entry = _FAILING_ENTRY.match(line.strip())
            if entry and entry["id"] not in failing:
                failing.append(entry["id"])

    ran = (
        summary is not None
        and any(counts.values())
        and exit_code in (0, 1)
        and not timed_out
    )
    return SuiteRun(
        ran=ran,
        exit_code=exit_code,
        timed_out=timed_out,
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["error"],
        skipped=counts["skipped"],
        failing_tests=failing,
        duration_s=duration,
        log_excerpt=_suite_excerpt(text, result),
    )


def _suite_excerpt(text: str, result: ExecResult) -> str:
    """The end of the log from a line boundary; stderr when the run printed no log."""
    body = text.strip()
    if not body and result.stderr.strip():
        body = result.stderr.strip()
    if len(body) > LOG_EXCERPT_CHARS:
        cut = body[-LOG_EXCERPT_CHARS:]
        newline = cut.find("\n")
        body = "[...]\n" + (cut[newline + 1 :] if 0 <= newline < len(cut) - 1 else cut)
    if result.timed_out:
        body += "\n[sandbox run timed out]"
    return body


def describe_suite(suite: SuiteRun) -> str:
    """'120 passed, 2 failed, 1 error, 3 skipped' for a suite that ran, else why it did not."""
    if not suite.ran:
        if suite.timed_out:
            return "timed out"
        return f"did not run (pytest exit code {suite.exit_code})"
    parts = [f"{suite.passed} passed"]
    for count, word in ((suite.failed, "failed"), (suite.errors, "error"),
                        (suite.skipped, "skipped")):
        if count:
            parts.append(f"{count} {word}")
    return ", ".join(parts)


# -- the run ---------------------------------------------------------------------------------


def _fail_ids(suite: SuiteRun | None) -> set[str]:
    return set(suite.failing_tests) if suite else set()


def newly_failing(before: SuiteRun, after: SuiteRun) -> list[str]:
    """Test ids that fail after the cherry-pick and did not fail before, in the suite's order."""
    known = _fail_ids(before)
    return [test for test in after.failing_tests if test not in known]


async def backport(
    assessment: Assessment,
    *,
    target_branch: str | None = None,
    settings: Settings | None = None,
    sandbox: Sandbox | None = None,
    progress: ProgressCallback | None = None,
    list_branches: BranchLister | None = None,
    python_version: str | None = None,
    extra_packages: Sequence[str] = (),
    suite_timeout_s: int = SUITE_TIMEOUT_S,
) -> BackportResult:
    """Prepare and verify a backport of the fix named in ``assessment``.

    Never raises for anything that can go wrong in the world (no branch, a conflict, a sandbox
    failure): the answer is always a `BackportResult` whose ``status`` and ``reason`` say what
    happened. ``sandbox`` and ``list_branches`` are injection points for tests.
    """
    started = time.monotonic()
    usage = Usage()
    rep = assessment.reproduction
    result = BackportResult(
        status=BackportStatus.ERROR,
        issue_url=assessment.issue_url,
        title=assessment.title,
        fixing_commit=rep.fixing_commit,
        repro_test_path=rep.repro_test_path,
        repro_test_code=rep.repro_test_code,
        usage=usage,
    )

    async def say(message: str) -> None:
        await emit_progress(progress, "backport", message)

    settings = settings or get_settings()
    real: list[Sandbox] = []  # the backend, once something needs one (not_applicable never does)
    try:
        async with run_scope(settings, lambda: real[0] if real else None):
            await _backport(
                assessment, result, usage, say,
                target_branch=target_branch, settings=settings, sandbox=sandbox, real=real,
                list_branches=list_branches, python_version=python_version,
                extra_packages=extra_packages, suite_timeout_s=suite_timeout_s,
                progress=progress,
            )
    except (EnvBuildError, SandboxError) as exc:
        _conclude(result, BackportStatus.ERROR, f"the sandbox run could not be made: {exc}"[:600])
    usage.wall_seconds = round(time.monotonic() - started, 2)
    await say(f"{result.status.value}: {result.reason}")
    return result


def _conclude(result: BackportResult, status: BackportStatus, reason: str) -> BackportResult:
    result.status = status
    result.reason = reason
    return result


async def _backport(
    assessment: Assessment,
    result: BackportResult,
    usage: Usage,
    say: Callable[[str], Awaitable[None]],
    *,
    target_branch: str | None,
    settings: Settings,
    sandbox: Sandbox | None,
    real: list[Sandbox],
    list_branches: BranchLister | None,
    python_version: str | None,
    extra_packages: Sequence[str],
    suite_timeout_s: int,
    progress: ProgressCallback | None,
) -> None:
    na, err = BackportStatus.NOT_APPLICABLE, BackportStatus.ERROR
    rep = assessment.reproduction

    # -- is there anything to backport? -----------------------------------------------------
    if assessment.verdict is not Verdict.FIXED_ON_MAIN:
        _conclude(result, na, f"the verdict is {assessment.verdict.value}; a backport needs a bug "
                  "that is fixed on main")
        return
    commit = (rep.fixing_commit or "").strip()
    if not commit:
        _conclude(result, na, "the commit that fixed the bug is unknown (the fix may have landed "
                  "as a squashed pull request that could not be traced), so there is nothing to "
                  "cherry-pick")
        return
    if not _SHA_RE.match(commit):
        _conclude(result, err, f"the fixing commit {commit!r} is not a commit hash")
        return
    if not rep.repro_test_code or not rep.repro_test_path:
        _conclude(result, err, "the assessment carries no reproduction test to verify with")
        return
    try:
        owner, repo, _ = parse_issue_url(assessment.issue_url)
    except ValueError:
        _conclude(result, na, "the assessment is not about a GitHub issue, so there is no "
                  "repository to backport in")
        return
    full_name = result.repo = f"{owner}/{repo}"
    reported_ref = rep.reported_side.ref if rep.reported_side else None
    result.reported_version = reported_ref

    # -- which branch? ----------------------------------------------------------------------
    await say(f"Listing the branches of {full_name}")
    branches = await (list_branches or list_remote_branches)(full_name)
    match = find_target_branch(reported_ref, list(branches))
    result.candidate_branches = match.maintenance
    if target_branch:
        if not _BRANCH_NAME_RE.match(target_branch) or ".." in target_branch:
            _conclude(result, err, f"{target_branch!r} is not a valid branch name")
            return
        if target_branch not in branches:
            _conclude(result, na, f"{full_name} has no branch named {target_branch}")
            return
        branch = target_branch
    else:
        if parse_version(reported_ref) is None:
            _conclude(result, na, "the reported side ran "
                      f"{reported_ref or 'no ref'}, not a release, so the reported version is "
                      "unknown; pass a target branch to choose one")
            return
        if match.branch is None:
            others = sorted(b for b in branches if b not in {"main", "master"})
            seen = ", ".join(others[:12]) + (", ..." if len(others) > 12 else "")
            hint = f" Other branches: {seen}." if others else ""
            if match.maintenance:
                hint = f" Maintenance branches for other versions: {', '.join(match.maintenance)}."
            _conclude(result, na, f"{full_name} has no maintenance branch for the reported "
                      f"version {reported_ref}.{hint} Pass --target-branch to choose one.")
            return
        branch = match.branch
    head = branches[branch]
    result.target_branch, result.target_branch_head = branch, head
    if len(match.candidates) > 1 and not target_branch:
        await say(f"Several branches fit {reported_ref} ({', '.join(match.candidates)}); "
                  f"using {branch}")

    # -- the sandbox ------------------------------------------------------------------------
    real.append(sandbox or get_sandbox(settings))
    sb = MeteredSandbox(real[0], usage)
    test_path, test_code = rep.repro_test_path, rep.repro_test_code

    await say(f"Building {branch} at {head[:10]} in the sandbox")
    image, _ = await envbuild.build_baseline(
        sb, repo=full_name, ref=head, python_version=python_version, progress=progress,
        extra_packages=list(extra_packages),
    )

    await say(f"Running the reproduction test on {branch} before the cherry-pick")
    before = await envbuild.run_test_on_side(
        sb, image, test_path=test_path, test_code=test_code, ref=f"{branch} (before)",
    )
    result.repro_before = before
    if before.stable_outcome is Outcome.PASS:
        _conclude(result, BackportStatus.ALREADY_FIXED_ON_BRANCH,
                  f"the reproduction test already passes on {branch}, so there is nothing to "
                  "backport")
        return
    if before.stable_outcome is not Outcome.FAIL:
        outcomes = ", ".join(o.value for o in before.outcomes) or "none"
        _conclude(result, err, f"the reproduction test did not give a clean failure on {branch} "
                  f"(runs: {outcomes}), so the cherry-pick cannot be judged")
        result.log_excerpt = before.log_excerpt
        return

    await say(f"Cherry-picking {commit[:10]} onto {branch}")
    picked = await sb.run(
        image, pick_script(commit), workdir=REPO_DIR, timeout_s=PICK_TIMEOUT_S,
        keep=True, network=True,
    )
    pick = parse_pick(picked)
    result.log_excerpt = pick.log
    if pick.head and pick.head.lower() != head.lower():
        log.warning("branch head in the sandbox %s differs from ls-remote %s", pick.head, head)
    if picked.timed_out:
        _conclude(result, err, "the cherry-pick step timed out")
        return
    if pick.pick_failed:
        result.conflicting_files = pick.conflicts
        if pick.conflicts:
            shown = ", ".join(pick.conflicts[:5]) + (", ..." if len(pick.conflicts) > 5 else "")
            _conclude(result, BackportStatus.CONFLICTS,
                      f"the cherry-pick conflicts in {len(pick.conflicts)} file(s): {shown}. It "
                      "was aborted; resolve the conflicts by hand")
        else:
            _conclude(result, err, "git cherry-pick failed without a conflict (an empty pick "
                      "means the branch already holds this change); see the log")
        return
    if pick.setup_failed or not pick.picked or not pick.patch or not picked.image:
        step = pick.setup_failed or ("export" if not pick.patch else "checkpoint")
        _conclude(result, err, f"the cherry-pick step failed at '{step}'; see the log")
        return
    result.cherry_picked_commit = pick.picked
    result.commit_message = pick.message
    result.patch = pick.patch

    await say(f"Rerunning the reproduction test on {branch} after the cherry-pick")
    after = await envbuild.run_test_on_side(
        sb, picked.image, test_path=test_path, test_code=test_code, ref=f"{branch} (after)",
    )
    result.repro_after = after
    if after.stable_outcome is not Outcome.PASS:
        outcomes = ", ".join(o.value for o in after.outcomes) or "none"
        _conclude(result, err, f"the cherry-pick applied cleanly but the reproduction test does "
                  f"not pass on {branch} afterwards (runs: {outcomes}); the fix probably depends "
                  "on other changes that the branch does not have")
        result.log_excerpt = after.log_excerpt
        return

    # The suites come last so that a conflict or a still-failing test is reported without
    # waiting for up to 900 s of the project's tests. The "before" run is still a fork of the
    # untouched branch image, so it is the baseline of the branch as it was.
    await say(f"Running the project's test suite on {branch} before the cherry-pick (baseline)")
    result.suite_before = suite_before = await _run_suite(sb, image, suite_timeout_s)
    if not suite_before.ran:
        _conclude(result, err, "the project's test suite could not be run on "
                  f"{branch} ({describe_suite(suite_before)}), so regressions cannot be ruled "
                  "out; the patch is attached unverified")
        result.log_excerpt = suite_before.log_excerpt
        return
    await say("Running the project's test suite after the cherry-pick")
    result.suite_after = suite_after = await _run_suite(sb, picked.image, suite_timeout_s)
    if not suite_after.ran:
        _conclude(result, err, "the project's test suite could not be run after the cherry-pick "
                  f"({describe_suite(suite_after)}), so regressions cannot be ruled out; the "
                  "patch is attached unverified")
        result.log_excerpt = suite_after.log_excerpt
        return

    result.newly_failing = newly_failing(suite_before, suite_after)
    failing_before = suite_before.failed + suite_before.errors
    failing_after = suite_after.failed + suite_after.errors
    if result.newly_failing or failing_after > failing_before:
        if result.newly_failing:
            shown = ", ".join(result.newly_failing[:3])
            shown += ", ..." if len(result.newly_failing) > 3 else ""
            what = f"{len(result.newly_failing)} test(s) fail now that passed before: {shown}"
        else:  # the count rose but the ids could not be read: do not call that verified
            what = (f"the number of failing tests rose from {failing_before} to {failing_after}")
        _conclude(result, BackportStatus.REGRESSION,
                  f"the cherry-pick fixes the reproduction test but {what}")
        result.log_excerpt = suite_after.log_excerpt
        return
    _conclude(result, BackportStatus.VERIFIED,
              f"the reproduction test fails on {branch} before the cherry-pick and passes after; "
              f"the project's suite has no newly failing tests ({describe_suite(suite_before)} "
              f"before, {describe_suite(suite_after)} after)")


async def _run_suite(sb: Sandbox, image: str, timeout_s: int) -> SuiteRun:
    """The suite in a fork of ``image``, without network: project tests are untrusted code."""
    outcome = await sb.run(
        image, suite_script(timeout_s), workdir=REPO_DIR, timeout_s=timeout_s + 120,
        network=False,
    )
    return parse_suite(outcome)


def side_outcomes(side: SideRun | None) -> str:
    """'fail, fail' for a side's runs, '-' when it did not run."""
    return ", ".join(o.value for o in side.outcomes) if side and side.outcomes else "-"
