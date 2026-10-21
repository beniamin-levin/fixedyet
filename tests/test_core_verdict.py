"""The decision matrix, checked row by row and then exhaustively against an independent oracle."""

from __future__ import annotations

import itertools

import pytest

from fixedyet.models import Confidence, Outcome, SideRun, Verdict
from fixedyet.verdict import decide, error_reason, failure_kind, is_setup_error

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
        with_log(F, F, log=ASSERT_LOG), with_log(F, F, log=ASSERT_LOG.replace("fnord", "x"))
    )
    assert (verdict, confidence) == (Verdict.OPEN_BUG, HIGH)
    assert any("the same kind of failure on both sides (AssertionError)" in b for b in basis)
    assert basis[-1] == "no run errored"


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
