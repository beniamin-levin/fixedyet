"""The decision matrix, checked row by row and then exhaustively against an independent oracle."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from fixedyet.models import Confidence, IssueStatus, LinkedPullRequest, Outcome, SideRun, Verdict
from fixedyet.verdict import (
    NOTE_PREFIXES,
    changed_failure,
    decide,
    error_reason,
    failure_kind,
    failure_lines,
    is_rule_line,
    is_setup_error,
    issue_state_lines,
    normalise_failure,
)

P, F, E = Outcome.PASS, Outcome.FAIL, Outcome.ERROR
HIGH, LOW = Confidence.HIGH, Confidence.LOW


def side(*outcomes: Outcome, ref: str = "ref") -> SideRun:
    return SideRun(ref=ref, outcomes=list(outcomes))


def run(
    reported: SideRun | None, fix: SideRun | None, *, runnable: bool = True, env_ok: bool = True
):
    return decide(reported, fix, runnable=runnable, env_ok=env_ok)


# -- the documented rows ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "fix", "verdict"),
    [
        ((F, F), (P, P), Verdict.FIXED_ON_MAIN),
        ((F, F), (F, F), Verdict.OPEN_BUG),
        ((P, P), (P, P), Verdict.NOT_REPRODUCED),
        ((P, P), (F, F), Verdict.INCONSISTENT),
    ],
)
def test_matrix_rows_with_stable_runs_are_high_confidence(reported, fix, verdict) -> None:
    got_verdict, confidence, basis = run(side(*reported), side(*fix))
    assert (got_verdict, confidence) == (verdict, HIGH)
    assert basis and all(isinstance(line, str) and line for line in basis)


def test_basis_of_a_high_confidence_verdict_names_both_sides() -> None:
    _, _, basis = run(side(F, F, ref="v1.2.0"), side(P, P, ref="main"))
    text = " ".join(basis)
    assert "both runs agree: fail" in text and "both runs agree: pass" in text
    assert "no run errored" in text


def test_contract_not_runnable_is_needs_info_whatever_else_is_passed() -> None:
    expected = (Verdict.NEEDS_INFO, LOW)
    assert run(None, None, runnable=False)[:2] == expected
    assert run(side(F, F), side(P, P), runnable=False)[:2] == expected
    assert run(None, None, runnable=False, env_ok=False)[:2] == expected  # needs_info wins


def test_env_build_failure_is_unsupported_even_with_runs_present() -> None:
    verdict, confidence, basis = run(side(F, F), side(P, P), env_ok=False)
    assert (verdict, confidence) == (Verdict.UNSUPPORTED, LOW)
    assert "environment" in basis[0]


@pytest.mark.parametrize(
    ("reported", "fix"),
    [
        (None, None),
        (None, side(P, P)),
        (side(F, F), None),
        (side(), side(P, P)),
        (side(F, F), side()),
    ],
)
def test_a_side_that_never_ran_is_unsupported(reported, fix) -> None:
    verdict, confidence, basis = run(reported, fix)
    assert (verdict, confidence) == (Verdict.UNSUPPORTED, LOW)
    assert "never ran" in basis[0]


def test_reported_side_error_is_unsupported_whatever_the_fix_side_did() -> None:
    for fix in ((P, P), (F, F), (E, E), (P, F)):
        verdict, confidence, _ = run(side(E, E), side(*fix))
        assert (verdict, confidence) == (Verdict.UNSUPPORTED, LOW)


def test_fix_side_error_is_unsupported_even_if_the_reported_side_reproduced() -> None:
    verdict, confidence, basis = run(side(F, F), side(E, E, ref="main"))
    assert (verdict, confidence) == (Verdict.UNSUPPORTED, LOW)
    assert "fix side (main)" in basis[0]


def test_disagreeing_runs_are_inconsistent_with_low_confidence() -> None:
    verdict, confidence, basis = run(side(F, P, ref="v1"), side(P, P))
    assert (verdict, confidence) == (Verdict.INCONSISTENT, LOW)
    assert "reported side (v1)" in basis[0] and "fail, pass" in basis[0]


def test_both_sides_flaky_lists_both_in_the_basis() -> None:
    _, _, basis = run(side(F, P), side(P, F))
    assert len(basis) == 2


def test_an_error_mixed_with_a_result_counts_as_disagreement_not_unsupported() -> None:
    assert run(side(F, E), side(P, P))[:2] == (Verdict.INCONSISTENT, LOW)


def test_single_run_per_side_is_taken_at_face_value() -> None:
    assert run(side(F), side(P))[:2] == (Verdict.FIXED_ON_MAIN, HIGH)


def test_three_runs_must_all_agree() -> None:
    assert run(side(F, F, F), side(P, P, P))[:2] == (Verdict.FIXED_ON_MAIN, HIGH)
    assert run(side(F, F, P), side(P, P, P))[:2] == (Verdict.INCONSISTENT, LOW)
    assert "all 3 runs agree" in " ".join(run(side(F, F, F), side(P, P, P))[2])


def test_pass_then_fail_basis_says_never_a_reproduction() -> None:
    _, confidence, basis = run(side(P, P), side(F, F))
    assert confidence is HIGH and "never a reproduction" in basis[-1]


# -- exhaustive: every pair of two-run sides ---------------------------------------------------


def oracle(reported: tuple[Outcome, ...], fix: tuple[Outcome, ...]) -> tuple[Verdict, Confidence]:
    """The matrix from docs/architecture.md, written independently of verdict.py."""
    if set(reported) == {E} or set(fix) == {E}:
        return Verdict.UNSUPPORTED, LOW
    if len(set(reported)) > 1 or len(set(fix)) > 1:
        return Verdict.INCONSISTENT, LOW
    table = {
        (F, P): Verdict.FIXED_ON_MAIN,
        (F, F): Verdict.OPEN_BUG,
        (P, P): Verdict.NOT_REPRODUCED,
        (P, F): Verdict.INCONSISTENT,
    }
    return table[(reported[0], fix[0])], HIGH


PAIRS = list(itertools.product(itertools.product((P, F, E), repeat=2), repeat=2))


@pytest.mark.parametrize(("reported", "fix"), PAIRS, ids=lambda o: "".join(x.value[0] for x in o))
def test_all_81_combinations_match_the_oracle(reported, fix) -> None:
    verdict, confidence, basis = run(side(*reported), side(*fix))
    assert (verdict, confidence) == oracle(reported, fix)
    assert basis, "every decision must explain itself"


def test_high_confidence_never_coexists_with_an_error_or_a_disagreement() -> None:
    for reported, fix in PAIRS:
        _, confidence, _ = run(side(*reported), side(*fix))
        if confidence is HIGH:
            assert len(set(reported)) == len(set(fix)) == 1
            assert E not in reported and E not in fix


def test_decide_is_pure() -> None:
    reported, fix = side(F, F), side(P, P)
    before = (reported.model_copy(deep=True), fix.model_copy(deep=True))
    assert run(reported, fix) == run(reported, fix)
    assert (reported, fix) == before


# -- FAIL on both sides must be the same failure ------------------------------------------------

ASSERT_LOG = """\
>       assert 'fnord' in result.stderr
E       AssertionError: assert 'fnord' in ''
E        +  where '' = <Result okay>.stderr
=========================== short test summary info ============================
FAILED tests/test_fixedyet_repro.py::test_reproduction - AssertionError: assert 'fnord' in ''
1 failed in 0.01s
"""
TYPE_ERROR_LOG = """\
E       TypeError: CliRunner.__init__() got an unexpected keyword argument 'mix_stderr'
=========================== short test summary info ============================
FAILED tests/test_fixedyet_repro.py::test_reproduction - TypeError: CliRunner.__init__() got un...
"""


def with_log(*outcomes: Outcome, log: str, ref: str = "ref") -> SideRun:
    return SideRun(ref=ref, outcomes=list(outcomes), log_excerpt=log)


@pytest.mark.parametrize(
    ("log", "kind"),
    [
        pytest.param(ASSERT_LOG, "AssertionError", id="assertion-log"),
        pytest.param(TYPE_ERROR_LOG, "TypeError", id="type-error-log"),
        # the summary line wins and survives a log cut off above the traceback
        ("FAILED t.py::test_a - click.exceptions.UsageError: no such option", "UsageError"),
        ("FAILED t.py::test_a - assert 1 == 2", "AssertionError"),
        ("FAILED t.py::test_a - Failed: DID NOT RAISE <class 'KeyError'>", "Failed"),
        # no summary line: the first exception line of the traceback
        ("E   KeyError: 'a'\nE   +  where x = 1\n", "KeyError"),
        ("E   assert 1 == 2\n", "AssertionError"),
        ("E   ValueError\n", "ValueError"),
        # nothing readable
        ("", None),
        ("1 failed in 0.01s\n", None),
        ("E    +  where '' = <Result okay>.stderr\n", None),
        ("E   where x = 1\n", None),
        ("FAILED t.py::test_a\n", None),
    ],
)
def test_failure_kind(log: str, kind: str | None) -> None:
    assert failure_kind(log) == kind


def test_the_same_failure_on_both_sides_is_an_open_bug_and_says_so() -> None:
    verdict, confidence, basis = run(
        with_log(F, F, log=ASSERT_LOG), with_log(F, F, log=ASSERT_LOG.replace("0.01s", "0.4s"))
    )
    assert (verdict, confidence) == (Verdict.OPEN_BUG, HIGH)
    assert any("the same kind of failure on both sides (AssertionError)" in b for b in basis)
    assert basis[-1] == "no run errored"


def test_the_same_kind_with_other_text_is_inconsistent_and_quotes_both_lines() -> None:
    verdict, confidence, basis = run(
        with_log(F, F, log=ASSERT_LOG), with_log(F, F, log=ASSERT_LOG.replace("fnord", "x"))
    )
    assert (verdict, confidence) == (Verdict.INCONSISTENT, LOW)
    assert "failure at the reported version: AssertionError: assert 'fnord' in ''" in basis
    assert "failure on the fix side: AssertionError: assert 'x' in ''" in basis


def test_different_failures_on_the_two_sides_are_not_an_open_bug() -> None:
    verdict, confidence, basis = run(
        with_log(F, F, log=ASSERT_LOG, ref="8.1.3"), with_log(F, F, log=TYPE_ERROR_LOG, ref="main")
    )
    assert (verdict, confidence) == (Verdict.INCONSISTENT, LOW)
    text = " ".join(basis)
    assert "AssertionError at the reported version, TypeError on the fix side" in text
    assert "does not show the bug is still there" in text


def test_a_log_without_a_readable_failure_leaves_the_row_alone() -> None:
    for fix_log in ("", "1 failed in 0.01s"):
        verdict, confidence, _ = run(with_log(F, F, log=ASSERT_LOG), with_log(F, F, log=fix_log))
        assert (verdict, confidence) == (Verdict.OPEN_BUG, HIGH)


def test_failure_kinds_do_not_matter_when_a_side_passes() -> None:
    assert run(with_log(F, F, log=TYPE_ERROR_LOG), with_log(P, P, log=""))[:2] == (
        Verdict.FIXED_ON_MAIN,
        HIGH,
    )


# -- why a run errored -------------------------------------------------------------------------

CLICK_CONFTEST = """\
ImportError while loading conftest '/work/repo/tests/conftest.py'.
tests/conftest.py:2: in <module>
    from click.testing import CliRunner
E   ModuleNotFoundError: No module named 'click'
"""
TRIO_CONFTEST = """\
ImportError while loading conftest '/work/repo/tests/conftest.py'.
tests/conftest.py:5: in <module>
    import trio
/usr/local/lib/python3.13/site-packages/trio/_path.py:102: in generate_forwards
    raise TypeError(attr_name, type(attr))
E   TypeError: ('parser', <class 'module'>)
"""


@pytest.mark.parametrize(
    ("log", "reason"),
    [
        (
            CLICK_CONFTEST,
            "pytest could not load the project's tests/conftest.py "
            "(ModuleNotFoundError: No module named 'click')",
        ),
        (
            TRIO_CONFTEST,
            "pytest could not load the project's tests/conftest.py "
            "(TypeError: ('parser', <class 'module'>))",
        ),
        (
            "ImportError while loading conftest '/work/repo/conftest.py'.",
            "pytest could not load the project's conftest.py",
        ),
        (
            "Error importing plugin 'pytest_foo': nope\nE   ImportError: cannot import name 'x'",
            "pytest could not import the plugin pytest_foo (ImportError: cannot import name 'x')",
        ),
        (
            "_____ ERROR collecting tests/test_fixedyet_repro.py _____\nE   SyntaxError: bad",
            "pytest could not collect tests/test_fixedyet_repro.py (SyntaxError: bad)",
        ),
        (
            "ERROR: Package 'jinja2' requires a different Python: 3.9.18 not in '>=3.10'",
            "the code needs Python >=3.10 and the sandbox has 3.9.18, "
            "so it cannot be installed there",
        ),
        ("1 failed in 0.1s\nE   AssertionError", None),
        ("", None),
    ],
)
def test_error_reason(log: str, reason: str | None) -> None:
    assert error_reason(log) == reason


def test_a_conftest_plugin_or_python_problem_is_a_setup_error_and_a_broken_test_is_not() -> None:
    assert is_setup_error(CLICK_CONFTEST) and is_setup_error(TRIO_CONFTEST)
    assert is_setup_error("Error importing plugin 'x'")
    assert is_setup_error("requires a different Python: 3.9.1 not in '>=3.10'")
    assert not is_setup_error("ERROR collecting tests/t.py\nE   NameError: name 'x'")
    assert not is_setup_error("1 failed")


def test_unsupported_basis_gives_the_reason_each_side_errored() -> None:
    reported = SideRun(ref="v1.2.3", outcomes=[E, E], log_excerpt=CLICK_CONFTEST)
    fix = SideRun(ref="main", outcomes=[E, E], log_excerpt=TRIO_CONFTEST)
    verdict, confidence, basis = run(reported, fix)
    assert (verdict, confidence) == (Verdict.UNSUPPORTED, LOW)
    assert basis == [
        "every run on the reported side (v1.2.3) errored before the test could decide: "
        "pytest could not load the project's tests/conftest.py "
        "(ModuleNotFoundError: No module named 'click')",
        "every run on the fix side (main) errored before the test could decide: "
        "pytest could not load the project's tests/conftest.py "
        "(TypeError: ('parser', <class 'module'>))",
    ]


def test_unsupported_basis_is_unchanged_when_the_log_says_nothing() -> None:
    _, _, basis = run(side(E, E, ref="v1"), side(P, P))
    assert basis == ["every run on the reported side (v1) errored before the test could decide"]


# -- rules v2: the failure text --------------------------------------------------------------------

PYPARSING_REPORTED = """\
>           assert tokens.target == [['x']]
E           AssertionError: assert ParseResults(.../'], {})], {}) == [['x']]
E             
E             At index 0 diff: ParseResults(['x', '/* */'], {}) != ['x']
E             Use -v to get more diff

tests/test_fixedyet_repro.py:10: AssertionError
=========================== short test summary info ============================
FAILED tests/test_fixedyet_repro.py::test_nested_expr_ignore_comment - Assert...
1 failed in 0.40s
"""
PYPARSING_FIX = (
    PYPARSING_REPORTED.replace("(.../'], {})], {})", "(...x'], {})], {})")
    .replace("['x', '/* */']", "['x']")
    .replace("0.40s", "0.10s")
)


def test_a_failure_that_changes_on_the_fix_side_is_inconsistent() -> None:
    """pyparsing#693: the comment is gone on main, the test still compares with a list."""
    verdict, confidence, basis = run(
        with_log(F, F, log=PYPARSING_REPORTED), with_log(F, F, log=PYPARSING_FIX)
    )
    assert (verdict, confidence) == (Verdict.INCONSISTENT, LOW)
    assert basis[:2] == ["both runs agree: fail on the reported side",
                         "both runs agree: fail on the fix side"]
    assert basis[2].startswith("the failure text changes between the sides")
    assert basis[3] == (
        "failure at the reported version: AssertionError: assert ParseResults(.../'], {})], {}) "
        "== [['x']]"
    )
    assert basis[4].startswith("failure on the fix side: AssertionError: assert ParseResults(...x")


def test_extra_failure_lines_on_one_side_count_as_a_change() -> None:
    """pytest-mock#639: the introspection the issue is about is gone on main."""
    reported = SideRun(ref="v3.16.1", outcomes=[F, F], failure_detail=(
        "E       AssertionError: Calls not found.\nE       Expected: [call(2), call(4)]\n"
        "E       \nE       pytest introspection follows:\nE       Args:"
    ))
    fix = SideRun(ref="main", outcomes=[F, F], failure_detail=(
        "E       AssertionError: Calls not found.\nE       Expected: [call(2), call(4)]"
    ))
    assert changed_failure(reported, fix) == (
        "pytest introspection follows:", "(no further failure line)"
    )
    assert run(reported, fix)[:2] == (Verdict.INCONSISTENT, LOW)


def test_failure_detail_is_read_before_the_excerpt() -> None:
    reported = SideRun(ref="a", outcomes=[F, F], log_excerpt=ASSERT_LOG,
                       failure_detail="E   AssertionError: assert 1 == 2")
    fix = SideRun(ref="b", outcomes=[F, F], log_excerpt=ASSERT_LOG,
                  failure_detail="E   AssertionError: assert 1 == 3")
    assert run(reported, fix)[0] is Verdict.INCONSISTENT
    same = fix.model_copy(update={"failure_detail": "E   AssertionError: assert 1 == 2"})
    assert run(reported, same)[:2] == (Verdict.OPEN_BUG, HIGH)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("<Foo object at 0x7f3a2b1c9d00>", "<Foo object at 0x7dbaecab29d0>"),
        ("timed out after 1.52s", "timed out after 0.3 s"),
        ("/tmp/pytest-of-fixedyet/pytest-3/test_a0/x.py",
         "/tmp/pytest-of-root/pytest-0/test_a0/x.py"),
        ("/tmp/tmpab12cd/out.txt missing", "/tmp/tmpzz99/out.txt missing"),
        ("version 1.2.3.dev4+g0a1b2c3.d20261021", "version 1.2.3.dev4+g0a1b2c3"),
        ("src/_pytest/skipping.py:239: unconditional skip",
         "src/_pytest/skipping.py:241: unconditional skip"),
        ('File "/work/repo/a.py", line 12, in f', 'File "/work/repo/a.py", line 14, in f'),
        ("a   b", "a b"),
    ],
)
def test_normalise_failure_removes_what_differs_between_runs(a: str, b: str) -> None:
    assert normalise_failure(a) == normalise_failure(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("skipping.py:239: unconditional skip", "test_skip_location.py:2: unconditional skip"),
        ("assert 3 == 4", "assert 3 == 5"),
        ("ParseResults(['x', '/* */'], {})", "ParseResults(['x'], {})"),
    ],
)
def test_normalise_failure_keeps_what_tells_failures_apart(a: str, b: str) -> None:
    assert normalise_failure(a) != normalise_failure(b)


def test_failure_lines_stop_at_the_sections_after_the_traceback() -> None:
    log = (
        "E   ValueError: boom\nE   \nE     Use -v to get more diff\n"
        "----------------------------- Captured stdout call -----------------------------\n"
        "E   not pytest's: printed by the test\n"
        "=========================== short test summary info ===========================\n"
    )
    assert failure_lines(log) == "E   ValueError: boom\nE\nE     Use -v to get more diff"
    assert failure_lines("1 passed in 0.01s") is None
    assert failure_lines("E\nE   \n") is None


def test_pytest_hints_do_not_count_as_a_change() -> None:
    reported = SideRun(ref="a", outcomes=[F],
                       failure_detail="E   assert 1 == 2\nE   Use -v to get the full diff")
    fix = SideRun(ref="b", outcomes=[F],
                  failure_detail="E   assert 1 == 2\nE   Use -v to get more diff")
    assert changed_failure(reported, fix) is None


# -- rules v2: an exception the issue never names --------------------------------------------------

OPTION_ERROR_LOG = """\
E   optparse.OptionError: no such option: --min-similarity-lines
=========================== short test summary info ============================
FAILED tests/test_fixedyet_repro.py::test_reproduction - optparse.OptionError: no such option...
"""


def test_an_exception_the_issue_never_names_lowers_an_open_bug() -> None:
    sides = with_log(F, F, log=OPTION_ERROR_LOG), with_log(F, F, log=OPTION_ERROR_LOG)
    verdict, confidence, basis = decide(*sides, runnable=True, env_ok=True,
                                        thread="Setting min-similarity-lines to 0 should stop it")
    assert (verdict, confidence) == (Verdict.OPEN_BUG, LOW)
    assert basis[-1].startswith("the test fails with OptionError on both sides and the issue never")


@pytest.mark.parametrize(
    ("log", "thread"),
    [
        (OPTION_ERROR_LOG, "pylint raises OptionError when I pass it"),  # the issue names it
        (OPTION_ERROR_LOG, None),  # no thread: the rule is skipped
        (ASSERT_LOG, "nothing about exceptions"),  # an assertion is the expected failure
        ("FAILED t.py::test_a - Failed: DID NOT RAISE <class 'KeyError'>\n", "no error named"),
    ],
)
def test_an_expected_or_named_failure_keeps_an_open_bug_high(log: str, thread: str | None) -> None:
    verdict, confidence, _ = decide(with_log(F, F, log=log), with_log(F, F, log=log),
                                    runnable=True, env_ok=True, thread=thread)
    assert (verdict, confidence) == (Verdict.OPEN_BUG, HIGH)


def test_the_exception_rule_needs_a_whole_word() -> None:
    log = OPTION_ERROR_LOG
    verdict, confidence, _ = decide(with_log(F, F, log=log), with_log(F, F, log=log),
                                    runnable=True, env_ok=True, thread="MyOptionErrors happen")
    assert confidence is LOW


# -- rules v2: the issue's state on GitHub ---------------------------------------------------------

DAY = datetime(2026, 10, 17, 16, 11, tzinfo=UTC)


def pr(number: int, state: str, *, closes: bool = False) -> LinkedPullRequest:
    return LinkedPullRequest(number=number, url=f"https://github.com/o/r/pull/{number}",
                             state=state, closes=closes,
                             merged_at=DAY if state == "merged" else None)


def test_an_open_bug_closed_as_completed_names_the_date_and_the_pull_request() -> None:
    status = IssueStatus(state="closed", state_reason="completed", closed_at=DAY, closed_by="#643",
                         linked_prs=[pr(643, "merged", closes=True), pr(642, "merged")])
    lines = issue_state_lines(Verdict.OPEN_BUG, status)
    assert lines[0].startswith(
        "the issue was closed as completed on 2026-10-17 by pull request #643"
    )
    assert lines[1].startswith("pull request #642, merged on 2026-10-17, mentions this issue")
    assert len(lines) == 2  # #643 is named once


def test_a_merged_closing_pull_request_on_an_open_issue_is_named() -> None:
    status = IssueStatus(linked_prs=[pr(7, "merged", closes=True), pr(8, "open", closes=True)])
    assert issue_state_lines(Verdict.OPEN_BUG, status) == [
        "pull request #7, merged on 2026-10-17, closes this issue: the fix may be in place in a "
        "form this test does not see"
    ]


@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        (Verdict.OPEN_BUG, None),
        (Verdict.OPEN_BUG, IssueStatus(linked_prs=[pr(8, "open", closes=True)])),
        (Verdict.OPEN_BUG, IssueStatus(state="closed", state_reason="not_planned", closed_at=DAY)),
        (Verdict.FIXED_ON_MAIN,
         IssueStatus(state="closed", state_reason="completed", closed_at=DAY)),
    ],
)
def test_no_issue_state_line_otherwise(verdict: Verdict, status: IssueStatus | None) -> None:
    assert issue_state_lines(verdict, status) == []


# -- reading a basis back --------------------------------------------------------------------------


def test_every_line_decide_writes_is_a_rule_line() -> None:
    logs = (ASSERT_LOG, TYPE_ERROR_LOG, OPTION_ERROR_LOG, PYPARSING_REPORTED, PYPARSING_FIX, "")
    seen = 0
    for (reported, fix), log_a, log_b in itertools.product(PAIRS, logs, logs):
        for runnable, env_ok in ((True, True), (False, True), (True, False)):
            basis = decide(with_log(*reported, log=log_a), with_log(*fix, log=log_b),
                           runnable=runnable, env_ok=env_ok, thread="")[2]
            for line in basis:
                seen += 1
                assert is_rule_line(line), line
    assert seen
    for line in issue_state_lines(Verdict.OPEN_BUG, IssueStatus(
        state="closed", state_reason="completed", closed_at=DAY, closed_by="abc1234def567",
        linked_prs=[pr(7, "merged", closes=True)],
    )):
        assert is_rule_line(line), line


@pytest.mark.parametrize(
    "line",
    [
        "the issue states no version, so the reported side ran the default branch (main)",
        "could not install structlog, which the issue uses, so the test may not match the report",
        "no git tag matches version 9.9, so the reported side ran the default branch (main)",
    ],
)
def test_caveats_are_not_rule_lines(line: str) -> None:
    assert not is_rule_line(line)
    assert not line.startswith(NOTE_PREFIXES)


def test_a_side_run_keeps_the_failure_lines_of_its_last_run_from_the_whole_log() -> None:
    from fixedyet.envbuild import LOG_EXCERPT_CHARS, build_side_run
    from fixedyet.models import ExecResult

    def run_section(number: int, value: int) -> str:
        noise = "\n".join(f"| captured line {i}" for i in range(400))  # longer than the excerpt
        return (
            f"__RUN_START__={number}\n| F\n| E   assert {value} == 2\n"
            "| ----------------------------- Captured stdout call -----------------------------\n"
            f"{noise}\n| 1 failed in 0.1s\n__RC__=1\n"
        )

    stdout = run_section(1, 1) + run_section(2, 3)
    side = build_side_run(ExecResult(exit_code=0, stdout=stdout), ref="r", image="i", runs=2)
    assert len(side.log_excerpt) <= LOG_EXCERPT_CHARS and "E   assert" not in side.log_excerpt
    assert side.failure_detail == "E   assert 3 == 2"  # the last run's, not the first's
    passed = build_side_run(
        ExecResult(exit_code=0, stdout="__RUN_START__=1\n| 1 passed\n__RC__=0\n"),
        ref="r", image="i", runs=1,
    )
    assert passed.failure_detail is None
