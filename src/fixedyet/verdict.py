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

FAIL on both sides only means "the bug is still there" when it is the same failure. A test that
fails with an AssertionError at the reported version and with a TypeError on main has hit a
changed API, not the bug, so that case is `inconsistent` with low confidence. The failure kind is
read from the pytest output (`failure_kind`); when either log does not show one, the row stands.
"""

from __future__ import annotations

import re

from fixedyet.models import Confidence, Outcome, SideRun, Verdict

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


def decide(
    reported: SideRun | None,
    fix: SideRun | None,
    *,
    runnable: bool,
    env_ok: bool,
) -> tuple[Verdict, Confidence, list[str]]:
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
        reported_kind, fix_kind = failure_kind(reported.log_excerpt), failure_kind(fix.log_excerpt)
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
        if reported_kind and reported_kind == fix_kind:
            same = f"the same kind of failure on both sides ({reported_kind})"
            basis = [*basis[:2], same, basis[2]]
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
