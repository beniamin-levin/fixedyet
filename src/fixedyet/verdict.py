"""The decision matrix. A pure function: the model never decides a verdict, the sandbox runs do.

Order of the rules, first match wins:

1. the contract is not runnable                       -> needs_info,  low (no run happened)
2. the environment failed, a side did not run,
   or every run on a side ERRORed                     -> unsupported, low
3. the runs on a side disagree with each other        -> inconsistent, low
4. otherwise the two stable outcomes pick the row:
       reported  fix
       FAIL      PASS   -> fixed_on_main
       FAIL      FAIL   -> open_bug, unless the two failures are of different kinds (see below)
       PASS      PASS   -> not_reproduced
       PASS      FAIL   -> inconsistent (a bad test, or a regression on main)

When a side errors, the basis line names the reason the log gives (`error_reason`): the project's
conftest or a plugin could not be imported, the test file could not be collected, or the ref needs
a newer Python than the sandbox has.

Rows of rule 4 are HIGH confidence: "high only when every run on both sides agrees and neither
side errored" holds by construction once rules 2 and 3 have passed.

FAIL on both sides only means "the bug is still there" when it is the same failure (rules v2):

- A test that fails with an AssertionError at the reported version and with a TypeError on main
  has hit a changed API, not the bug: `inconsistent`, low. The failure kind is read from the
  pytest output (`failure_kind`); when either log does not show one, the row stands.
- The same kind with different text (the `E` lines of the last run, `SideRun.failure_detail`,
  after `normalise_failure` has removed addresses, durations, line numbers, temporary paths and
  the `.dYYYYMMDD` version suffix) means the fix changed what the test sees: `inconsistent`, low,
  and the basis quotes the first line that differs on each side.
- An exception other than AssertionError or pytest's `Failed` that the issue thread never names
  (`thread`) is more likely the test's own misuse of the API than the bug: `open_bug`, low.

`issue_state_lines` adds what GitHub says about an open bug: closed as completed, or a merged pull
request that closes or mentions it. Each such line caps the confidence at low.

`RULES_VERSION` names this table. `fixedyet rescore` runs it again over saved assessments; it uses
`is_rule_line` to tell the lines this module wrote from the caveats the pipeline added.
"""

from __future__ import annotations

import re
from datetime import datetime

from fixedyet.models import Confidence, IssueStatus, Outcome, SideRun, Verdict

RULES_VERSION = "v2"
"""v1: the matrix and the failure kind. v2: the failure text, the exception the issue never names,
and the issue's state on GitHub."""

# The line pytest's short summary prints for a failed test: `FAILED path::test - TypeError: msg`.
_SUMMARY_LINE = re.compile(r"^FAILED \S+ - (?P<what>.+)$", re.MULTILINE)
_EXCEPTION_LINE = re.compile(r"^E\s+(?P<what>[A-Za-z_]\S.*)$", re.MULTILINE)
_EXCEPTION_NAME = re.compile(r"(?P<name>[A-Za-z_][\w.]*)(?=:|\s|$)")


def _kind_of(what: str) -> str | None:
    """'AssertionError' for 'assert 1 == 2', the class name for 'pkg.mod.Boom: text', else None."""
    what = what.strip()
    if what.startswith("assert"):
        return "AssertionError"
    match = _EXCEPTION_NAME.match(what)
    if not match:
        return None
    name = match.group("name").rsplit(".", 1)[-1]
    return name if name[:1].isupper() else None


def failure_kind(log: str) -> str | None:
    """The exception class a failed pytest run ended with, from its log; None when unclear.

    The summary line is read first (it survives a log cut at the top), then the first `E` line
    of the traceback. A bare `assert` counts as AssertionError.
    """
    for pattern in (_SUMMARY_LINE, _EXCEPTION_LINE):
        for match in pattern.finditer(log):
            if kind := _kind_of(match.group("what")):
                return kind
    return None


# -- reading an errored run --------------------------------------------------------------------

_LOADING_CONFTEST = re.compile(r"ImportError while loading conftest '(?P<path>[^']+)'")
_LOADING_PLUGIN = re.compile(r"Error importing plugin ['\"]?(?P<name>[\w.-]+)")
_COLLECTING = re.compile(r"ERROR collecting (?P<path>\S+)")
_NEEDS_PYTHON = re.compile(r"requires a different Python: (?P<have>\S+) not in '(?P<need>[^']+)'")
_LAST_EXCEPTION = re.compile(r"^E\s+(?P<what>\S.*)$", re.MULTILINE)
_WORK_DIR = "/work/repo/"


def _last_exception(log: str) -> str:
    lines = _LAST_EXCEPTION.findall(log)
    return lines[-1].strip() if lines else ""


def is_setup_error(log: str) -> bool:
    """True when pytest stopped while loading the project's own configuration (its conftest or a
    plugin), or the ref cannot be installed here. The test file is not what failed, so editing it
    cannot help."""
    return bool(
        _LOADING_CONFTEST.search(log) or _LOADING_PLUGIN.search(log) or _NEEDS_PYTHON.search(log)
    )


def error_reason(log: str) -> str | None:
    """One line on why a pytest run errored before any test decided, from its log; None when the
    log does not say.

    Four causes are told apart: the ref needs a newer Python than the sandbox has, the project's
    conftest cannot be imported, a pytest plugin cannot be imported, and the test file itself
    cannot be collected. The reason names the exception pytest ended with.
    """
    if needs := _NEEDS_PYTHON.search(log):
        return (
            f"the code needs Python {needs['need']} and the sandbox has {needs['have']}, so it "
            "cannot be installed there"
        )
    what = _last_exception(log)
    suffix = f" ({what})" if what else ""
    if conftest := _LOADING_CONFTEST.search(log):
        path = conftest["path"].removeprefix(_WORK_DIR)
        return f"pytest could not load the project's {path}{suffix}"
    if plugin := _LOADING_PLUGIN.search(log):
        return f"pytest could not import the plugin {plugin['name']}{suffix}"
    if collecting := _COLLECTING.search(log):
        return f"pytest could not collect {collecting['path']}{suffix}"
    return None


# -- the failure text (rules v2) -----------------------------------------------------------------

FAILURE_DETAIL_CHARS = 4_000
# pytest prints these after the traceback of a failed test; `E` lines below them are not its own.
_AFTER_TRACEBACK = re.compile(
    r"^(?:-{3,} Captured |={3,} (?:short test summary info|warnings summary|slowest) )"
)
_RUN_HEADER = re.compile(r"^--- (?:last of \d+ runs.*|run \d+: .*) ---$", re.MULTILINE)
_PYTEST_HINT = re.compile(r"^E\s+(?:Use -v+ to get|\.\.\.Full output truncated)")
_NORMALISE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"pytest-of-[^/\s]+/pytest-\d+"), "pytest-of-?/pytest-?"),
    (re.compile(r"/tmp/[^\s'\":,)\]]*"), "/tmp/?"),  # noqa: S108 - text, not a file
    (re.compile(r"\b0x[0-9a-fA-F]{4,}\b"), "0x?"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:s|ms|us|sec|secs|seconds)\b"), "?s"),
    (re.compile(r"\.d(?:19|20)\d{6}\b"), ""),  # setuptools-scm's local version of a dirty tree
    (re.compile(r"(\.py['\"]?(?::|, line ))\d+"), r"\1?"),
)


def last_run(log_excerpt: str) -> str:
    """The text of the last run in a side's `log_excerpt` (see `envbuild.format_log`)."""
    return _RUN_HEADER.split(log_excerpt)[-1]


def failure_lines(log: str) -> str | None:
    """The `E` lines of a pytest failure report: the exception and pytest's explanation of it.

    Lines after the traceback (captured output, the warnings and short summaries) are not read.
    None when the log has no `E` line with text.
    """
    lines: list[str] = []
    for line in log.splitlines():
        if _AFTER_TRACEBACK.match(line):
            break
        if line == "E" or line.startswith(("E ", "E\t")):
            lines.append(line.rstrip())
    text = "\n".join(lines)
    return text[:FAILURE_DETAIL_CHARS] if text.strip("E \t\n") else None


def normalise_failure(line: str) -> str:
    """What may differ between two runs of the same failure, replaced by placeholders."""
    for pattern, replacement in _NORMALISE:
        line = pattern.sub(replacement, line)
    return " ".join(line.split())


def _detail(side: SideRun) -> str | None:
    if side.failure_detail is not None:
        return side.failure_detail
    return failure_lines(last_run(side.log_excerpt))  # a report saved before failure_detail


def _comparable(detail: str) -> list[tuple[str, str]]:
    """(normalised, as printed) for each `E` line that carries text, pytest's hints left out."""
    out = []
    for line in detail.splitlines():
        if _PYTEST_HINT.match(line):
            continue
        text = line[1:].strip()
        if text:
            out.append((normalise_failure(text), text))
    return out


def changed_failure(reported: SideRun, fix: SideRun) -> tuple[str, str] | None:
    """The first failure line that differs between the sides, as printed on each; None when the
    failures read the same after `normalise_failure`, or when either side shows no `E` line."""
    reported_detail, fix_detail = _detail(reported), _detail(fix)
    if not reported_detail or not fix_detail:
        return None
    left, right = _comparable(reported_detail), _comparable(fix_detail)
    if [n for n, _ in left] == [n for n, _ in right]:
        return None
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a is None or b is None or a[0] != b[0]:
            none = "(no further failure line)"
            return (a[1] if a else none), (b[1] if b else none)
    return None  # pragma: no cover - the lists differ, so some index differs


_EXPECTED_KINDS = frozenset({"AssertionError", "Failed"})


def _kind(side: SideRun) -> str | None:
    return failure_kind(side.log_excerpt) or failure_kind(side.failure_detail or "")


def _named_in(name: str, text: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(name)}(?!\w)", text) is not None


def _quote(text: str, start: int = 0, limit: int = 160) -> str:
    """``text`` on one line, at most ``limit`` characters, from about ``start`` on."""
    flat = " ".join(text.split())
    if start > 0:
        flat = "..." + flat[start:]
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _quote_pair(a: str, b: str, limit: int = 160) -> tuple[str, str]:
    """Both lines, cut the same way so the place where they first differ stays in view."""
    flat_a, flat_b = " ".join(a.split()), " ".join(b.split())
    same = 0
    while same < min(len(flat_a), len(flat_b)) and flat_a[same] == flat_b[same]:
        same += 1
    start = same - 60 if same > limit - 40 else 0
    return _quote(flat_a, start, limit), _quote(flat_b, start, limit)


# -- the issue on GitHub (rules v2) ---------------------------------------------------------------


def _day(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "an unknown date"


def issue_state_lines(verdict: Verdict, status: IssueStatus | None) -> list[str]:
    """Basis lines for an open bug that GitHub says may be fixed; each caps confidence at low.

    The issue closed as completed, or a merged pull request of the same repository that closes
    or mentions it. Other verdicts, and an issue whose state is unknown, get no line.
    """
    if verdict is not Verdict.OPEN_BUG or status is None:
        return []
    lines: list[str] = []
    named: set[str] = set()
    if status.state == "closed" and (status.state_reason or "").lower() == "completed":
        by = ""
        if status.closed_by and status.closed_by.startswith("#"):
            by = f" by pull request {status.closed_by}"
            named.add(status.closed_by)
        elif status.closed_by:
            by = f" by commit {status.closed_by[:12]}"
        lines.append(
            f"the issue was closed as completed on {_day(status.closed_at)}{by}: the project "
            "considers it fixed, so a test that still fails on main may check something else"
        )
    merged = [
        pr for pr in status.linked_prs if pr.state == "merged" and f"#{pr.number}" not in named
    ]
    for pr in sorted(merged, key=lambda pr: (not pr.closes, pr.number))[:3]:
        how = "closes" if pr.closes else "mentions"
        lines.append(
            f"pull request #{pr.number}, merged on {_day(pr.merged_at)}, {how} this issue: the "
            "fix may be in place in a form this test does not see"
        )
    return lines


# -- reading a basis back (fixedyet rescore) ------------------------------------------------------

_RULE_LINES = re.compile(
    "|".join(
        (
            # v1
            r"^the issue has no runnable reproduction, so no sandbox run was made$",
            r"^the environment at the reported version could not be built$",
            r"^the test never ran on the (?:reported|fix) side$",
            r"^every run on the (?:reported|fix) side \(.*\) errored before the test could decide",
            r"^runs on the (?:reported|fix) side \(.*\) disagree: ",
            r"^(?:both runs agree|the single run|all \d+ runs agree): \w+ on the (?:reported|fix) "
            r"side$",
            r"^no run errored$",
            r"^the failures differ: ",
            r"^a different failure on the fix side does not show the bug is still there",
            r"^the same kind of failure on both sides \(",
            r"^passing at the reported version but failing on the fix side means",
            # v2
            r"^the failure text changes between the sides",
            r"^failure at the reported version: ",
            r"^failure on the fix side: ",
            r"^the test fails with \S+ on both sides and the issue never names it",
            r"^the issue was closed as completed on ",
            r"^pull request #\d+, merged on ",
        )
    )
)
NOTE_PREFIXES = ("the root-cause write-up was skipped", "re-scored by rules ")
"""Basis lines that are neither rules nor caveats: they never change the confidence."""


def is_rule_line(line: str) -> bool:
    """True for a basis line this module writes (any rules version), which a re-score replaces."""
    return _RULE_LINES.match(line) is not None


def decide(
    reported: SideRun | None,
    fix: SideRun | None,
    *,
    runnable: bool,
    env_ok: bool,
    thread: str | None = None,
) -> tuple[Verdict, Confidence, list[str]]:
    """The verdict from the two sides. ``thread`` is the issue's text (title, body, comments);
    without it the rule about an exception the issue never names is skipped."""
    if not runnable:
        return (
            Verdict.NEEDS_INFO,
            Confidence.LOW,
            ["the issue has no runnable reproduction, so no sandbox run was made"],
        )
    if not env_ok:
        return (
            Verdict.UNSUPPORTED,
            Confidence.LOW,
            ["the environment at the reported version could not be built"],
        )

    sides = (("reported", reported), ("fix", fix))
    for label, side in sides:
        if side is None or not side.outcomes:
            return (
                Verdict.UNSUPPORTED,
                Confidence.LOW,
                [f"the test never ran on the {label} side"],
            )

    # Both sides have at least one outcome from here on.
    assert reported is not None and fix is not None
    errored = [
        f"every run on the {label} side ({side.ref}) errored before the test could decide"
        + (f": {reason}" if (reason := error_reason(side.log_excerpt)) else "")
        for label, side in sides
        if side.stable_outcome is Outcome.ERROR
    ]
    if errored:
        return Verdict.UNSUPPORTED, Confidence.LOW, errored

    flaky = [
        f"runs on the {label} side ({side.ref}) disagree: "
        + ", ".join(o.value for o in side.outcomes)
        for label, side in sides
        if side.stable_outcome is None
    ]
    if flaky:
        return Verdict.INCONSISTENT, Confidence.LOW, flaky

    # Neither stable_outcome is None here (rule 3) and neither is ERROR (rule 2).
    reported_outcome = reported.stable_outcome
    fix_outcome = fix.stable_outcome
    basis = [
        f"{_describe(reported)} on the reported side",
        f"{_describe(fix)} on the fix side",
        "no run errored",
    ]
    if reported_outcome is Outcome.FAIL and fix_outcome is Outcome.PASS:
        return Verdict.FIXED_ON_MAIN, Confidence.HIGH, basis
    if reported_outcome is Outcome.FAIL and fix_outcome is Outcome.FAIL:
        reported_kind, fix_kind = _kind(reported), _kind(fix)
        if reported_kind and fix_kind and reported_kind != fix_kind:
            return (
                Verdict.INCONSISTENT,
                Confidence.LOW,
                [
                    *basis[:2],
                    f"the failures differ: {reported_kind} at the reported version, {fix_kind} "
                    "on the fix side",
                    "a different failure on the fix side does not show the bug is still there; "
                    "the test may rely on something that changed between the two versions",
                ],
            )
        if changed := changed_failure(reported, fix):
            at_reported, on_fix = _quote_pair(*changed)
            return (
                Verdict.INCONSISTENT,
                Confidence.LOW,
                [
                    *basis[:2],
                    "the failure text changes between the sides (addresses, durations, line "
                    "numbers and temporary paths ignored), so the fix side may not show the same "
                    "bug",
                    f"failure at the reported version: {at_reported}",
                    f"failure on the fix side: {on_fix}",
                ],
            )
        if reported_kind and reported_kind == fix_kind:
            same = f"the same kind of failure on both sides ({reported_kind})"
            basis = [*basis[:2], same, basis[2]]
        if (
            thread is not None
            and reported_kind
            and reported_kind not in _EXPECTED_KINDS
            and not _named_in(reported_kind, thread)
        ):
            return (
                Verdict.OPEN_BUG,
                Confidence.LOW,
                [
                    *basis,
                    f"the test fails with {reported_kind} on both sides and the issue never "
                    "names it: the test may stop on its own use of the project's API before it "
                    "reaches the bug",
                ],
            )
        return Verdict.OPEN_BUG, Confidence.HIGH, basis
    if reported_outcome is Outcome.PASS and fix_outcome is Outcome.PASS:
        return Verdict.NOT_REPRODUCED, Confidence.HIGH, basis
    note = (
        "passing at the reported version but failing on the fix side means a bad test "
        "or a regression, never a reproduction"
    )
    return Verdict.INCONSISTENT, Confidence.HIGH, [*basis, note]


def _describe(side: SideRun) -> str:
    """'both runs agree: fail' / 'the single run: pass' / 'all 3 runs agree: fail'."""
    outcome = side.stable_outcome
    assert outcome is not None
    count = len(side.outcomes)
    if count == 1:
        return f"the single run: {outcome.value}"
    return f"{'both' if count == 2 else f'all {count}'} runs agree: {outcome.value}"
