"""`fixedyet rescore`: the rules run again over saved results, safely and idempotently."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fixedyet import rescore
from fixedyet.cli import main as cli_main
from fixedyet.models import (
    Assessment,
    Confidence,
    IssueStatus,
    LinkedPullRequest,
    Outcome,
    Reproduction,
    SideRun,
    Verdict,
)
from fixedyet.rescore import (
    LOWERED_ACTION,
    load_inputs,
    parse_labels,
    rescore_assessment,
)
from fixedyet.verdict import RULES_VERSION

F, P = Outcome.FAIL, Outcome.PASS
AT = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
URL = "https://github.com/acme/widgets/issues/42"

SAME = "E   AssertionError: assert 'a' == 'b'"
OLD_BASIS = [
    "both runs agree: fail on the reported side",
    "both runs agree: fail on the fix side",
    "the same kind of failure on both sides (AssertionError)",
    "no run errored",
]


def side(ref: str, *outcomes: Outcome, detail: str | None = SAME) -> SideRun:
    return SideRun(ref=ref, outcomes=list(outcomes), failure_detail=detail)


def open_bug(*, fix_detail: str = SAME, basis: list[str] | None = None,
             url: str = URL) -> Assessment:
    return Assessment(
        issue_url=url, title="parse() drops the last item", verdict=Verdict.OPEN_BUG,
        confidence=Confidence.HIGH, confidence_basis=list(basis or OLD_BASIS),
        reproduction=Reproduction(
            repro_test_code="def test_x():\n    assert parse() == 'b'\n",
            reported_side=side("1.2.0", F, F), fix_side=side("main", F, F, detail=fix_detail),
        ),
        next_actions=["Use the reproduction test as the failing test for a fix."],
    )


# -- the pure function -----------------------------------------------------------------------------


def test_an_unchanged_result_is_returned_as_it_is() -> None:
    a = open_bug()
    assert rescore_assessment(a, thread="no exception named", at=AT) is a


def test_a_changed_failure_turns_an_open_bug_inconsistent_and_keeps_the_first_verdict() -> None:
    a = open_bug(fix_detail="E   AssertionError: assert 'c' == 'b'")
    b = rescore_assessment(a, thread="", at=AT)
    assert (b.verdict, b.confidence) == (Verdict.INCONSISTENT, Confidence.LOW)
    assert b.rescored is not None
    assert (b.rescored.original_verdict, b.rescored.original_confidence) == (
        Verdict.OPEN_BUG, Confidence.HIGH)
    assert b.rescored.original_basis == OLD_BASIS and b.rescored.rules == RULES_VERSION
    assert b.rules == RULES_VERSION
    assert b.confidence_basis[-1] == (
        f"re-scored by rules {RULES_VERSION}: the first verdict was open_bug, high confidence"
    )
    assert b.next_actions[0].startswith("Review the reproduction test by hand")  # new defaults
    assert a.verdict is Verdict.OPEN_BUG  # the input is not modified


def test_rescoring_twice_changes_nothing_the_second_time() -> None:
    a = open_bug(fix_detail="E   AssertionError: assert 'c' == 'b'")
    once = rescore_assessment(a, thread="", at=AT)
    again = rescore_assessment(once, thread="", at=datetime(2027, 1, 1, tzinfo=UTC))
    assert again is once
    # read back from JSON, as a second `--write` run would
    reloaded = Assessment.model_validate_json(once.model_dump_json())
    assert rescore_assessment(reloaded, thread="", at=AT) is reloaded


def test_caveats_of_the_pipeline_survive_and_keep_the_confidence_low() -> None:
    caveat = "the issue states no version, so the reported side ran the default branch (main)"
    note = "the root-cause write-up was skipped: budget"
    a = open_bug(basis=[*OLD_BASIS, caveat, note]).model_copy(
        update={"confidence": Confidence.LOW})
    assert rescore_assessment(a, thread="", at=AT) is a  # nothing to change
    status = IssueStatus(state="closed", state_reason="completed",
                         closed_at=datetime(2026, 5, 16, tzinfo=UTC), closed_by="#3391")
    b = rescore_assessment(a, thread="", status=status, at=AT)
    assert b.confidence is Confidence.LOW
    basis = b.confidence_basis
    assert basis.index(caveat) > basis.index(next(x for x in basis if "closed as completed" in x))
    assert note in basis and basis[-1].endswith("verdict and confidence unchanged")


def test_an_issue_closed_as_completed_lowers_an_open_bug_and_is_stored() -> None:
    status = IssueStatus(
        state="closed", state_reason="completed", closed_at=datetime(2026, 5, 16, tzinfo=UTC),
        closed_by="#3391", source="graphql",
        linked_prs=[LinkedPullRequest(number=3391, url="https://github.com/acme/widgets/pull/3391",
                                      state="merged", closes=True)],
    )
    b = rescore_assessment(open_bug(), thread="", status=status, at=AT)
    assert (b.verdict, b.confidence) == (Verdict.OPEN_BUG, Confidence.LOW)
    assert any(line.startswith("the issue was closed as completed on 2026-05-16 by pull request "
                               "#3391") for line in b.confidence_basis)
    assert b.issue_status == status
    assert b.next_actions[0] == LOWERED_ACTION
    # a later run without GitHub reads the stored status and agrees
    assert rescore_assessment(b, thread=None, status=None, at=AT) is b


def test_a_newer_status_without_a_new_result_is_still_stored() -> None:
    status = IssueStatus(linked_prs=[LinkedPullRequest(
        number=7, url="https://github.com/acme/widgets/pull/7", state="open", closes=True)])
    b = rescore_assessment(open_bug(), thread="", status=status, at=AT)
    assert b.issue_status == status and (b.verdict, b.confidence) == (
        Verdict.OPEN_BUG, Confidence.HIGH)
    assert b.confidence_basis[-1].endswith("verdict and confidence unchanged")
    same_later = status.model_copy(update={"checked_at": datetime(2027, 1, 1, tzinfo=UTC)})
    assert rescore_assessment(b, thread="", status=same_later, at=AT) is b


def test_without_the_thread_the_exception_rule_keeps_what_it_found() -> None:
    detail = "E   TypeError: 'NoneType' object is not callable"
    a = open_bug(fix_detail=detail).model_copy()
    a.reproduction.reported_side.failure_detail = detail
    scored = rescore_assessment(a, thread="the reporter saw wrong output", at=AT)
    assert scored.confidence is Confidence.LOW
    line = next(x for x in scored.confidence_basis if "never names it" in x)
    assert rescore_assessment(scored, thread=None, at=AT) is scored  # kept, not dropped
    assert line in rescore_assessment(scored, thread=None, at=AT).confidence_basis
    named = rescore_assessment(scored, thread="it raises TypeError", at=AT)
    assert named.confidence is Confidence.HIGH  # with the thread back, the rule can clear it


@pytest.mark.parametrize("verdict", [Verdict.NEEDS_INFO, Verdict.UNSUPPORTED])
def test_verdicts_without_runs_are_never_touched(verdict: Verdict) -> None:
    a = open_bug().model_copy(update={"verdict": verdict, "confidence": Confidence.LOW})
    assert rescore_assessment(a, thread="", at=AT) is a


def test_a_side_that_never_ran_is_never_touched() -> None:
    a = open_bug()
    a.reproduction.fix_side = None
    assert rescore_assessment(a, thread="", at=AT) is a


# -- inputs, labels, the command -------------------------------------------------------------------


def write_run(runs: Path, name: str, a: Assessment) -> Path:
    folder = runs / name
    folder.mkdir(parents=True)
    path = folder / "assessment.json"
    path.write_text(a.model_dump_json(indent=2), encoding="utf-8")
    (folder / "report.md").write_text("old report\n", encoding="utf-8")
    return path


def test_load_inputs_reads_runs_dirs_eval_results_and_web_reports(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    write_run(runs, "20261021T150603Z-acme-widgets-42", open_bug())
    (runs / "backports").mkdir()
    eval_file = tmp_path / "results.json"
    eval_file.write_text(json.dumps({"dataset": "verified", "results": [
        {"instance_id": "acme__widgets-42", "assessment": json.loads(open_bug().model_dump_json())},
        {"instance_id": "acme__widgets-43", "assessment": None},
    ]}), encoding="utf-8")
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"id": "x", "assessment": json.loads(open_bug().model_dump_json())}),
                   encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    loaded = load_inputs([runs, eval_file, web, broken, tmp_path / "missing"])
    kinds = sorted((i.kind, i.label) for i in loaded.items)
    assert kinds == [("assessment", "20261021T150603Z-acme-widgets-42"),
                     ("eval", "acme__widgets-42"), ("web", "web")]
    eval_item = next(i for i in loaded.items if i.kind == "eval")
    assert (eval_item.truth, eval_item.dataset) == ("fixed", "verified")
    assert len(loaded.errors) == 2


def test_parse_labels() -> None:
    labels = parse_labels(f"# comment\n{URL}/ open  # note\nacme__widgets-1 fixed\n\n")
    assert labels == {URL: "open", "acme__widgets-1": "fixed"}
    with pytest.raises(ValueError, match="line 1"):
        parse_labels(f"{URL} maybe\n")


def test_rescore_write_is_atomic_keeps_the_mode_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "runs"
    changed = write_run(runs, "20261021T150603Z-acme-widgets-42",
                        open_bug(fix_detail="E   AssertionError: assert 'c' == 'b'"))
    same = write_run(runs, "20261021T150822Z-acme-widgets-43", open_bug(url=URL[:-2] + "43"))
    os.chmod(changed, 0o640)
    same_bytes = same.read_bytes()

    assert rescore.main([str(runs), "--offline"]) == 0  # read only first
    assert "1 result(s) would change" in capsys.readouterr().err
    assert Assessment.model_validate_json(changed.read_text()).verdict is Verdict.OPEN_BUG

    assert cli_main(["rescore", str(runs), "--offline", "--write"]) == 0
    out = capsys.readouterr()
    assert "wrote 1 assessment file(s)" in out.err
    assert "open_bug, high | inconsistent, low" in out.out
    after = Assessment.model_validate_json(changed.read_text())
    assert after.verdict is Verdict.INCONSISTENT and after.rescored is not None
    assert after.rescored.original_verdict is Verdict.OPEN_BUG
    assert (changed.stat().st_mode & 0o777) == 0o640
    report = (changed.parent / "report.md").read_text()
    assert report.startswith("# Inconsistent") and "re-scored by rules" in report
    assert same.read_bytes() == same_bytes  # nothing to change: not rewritten
    assert not list(runs.rglob("*.rescore-tmp"))

    written = changed.read_bytes()
    assert rescore.main([str(runs), "--offline", "--write"]) == 0
    assert "wrote 0 assessment file(s)" in capsys.readouterr().err
    assert changed.read_bytes() == written


def test_eval_results_are_never_written(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    eval_file = tmp_path / "results.json"
    a = open_bug(fix_detail="E   AssertionError: assert 'c' == 'b'", url="swe-bench:acme__w-1")
    text = json.dumps({"dataset": "verified", "results": [
        {"instance_id": "acme__w-1", "assessment": json.loads(a.model_dump_json())}]})
    eval_file.write_text(text, encoding="utf-8")
    md = tmp_path / "summary.md"
    assert rescore.main([str(eval_file), "--offline", "--write", "--md", str(md),
                         "--dataset-cache", str(tmp_path / "cache")]) == 0
    err = capsys.readouterr().err
    assert "1 changed result(s) in read-only inputs were not written" in err
    assert eval_file.read_text() == text
    summary = md.read_text()
    assert "| 1 | 1 | 0 | 1 → 0 | 1 → 0 | 1 → 0 | - |" in summary


def test_labels_count_real_open_bugs_kept(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    write_run(runs, "a-1", open_bug(url=URL))
    write_run(runs, "a-2", open_bug(url=URL[:-2] + "43",
                                    fix_detail="E   AssertionError: assert 'c' == 'b'"))
    labels = tmp_path / "labels.txt"
    labels.write_text(f"{URL} open\n{URL[:-2]}43 bad_test\n", encoding="utf-8")
    md = tmp_path / "s.md"
    assert rescore.main([str(runs), "--offline", "--labels", str(labels), "--md", str(md)]) == 0
    assert "| 2 | 1 | 0 | 2 → 1 | 1 → 0 | 1 → 0 | 1/1 (1 high) |" in md.read_text()


def test_no_readable_input_is_an_error(tmp_path: Path) -> None:
    assert rescore.main([str(tmp_path / "nothing"), "--offline"]) == 1
