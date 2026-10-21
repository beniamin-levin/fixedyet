"""Evaluation runner tests. `reproduce_instance` is replaced by a fake; nothing is paid for."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fixedyet.eval import run
from fixedyet.eval.run import (
    InstanceResult,
    RunReport,
    compute_metrics,
    failure_cause,
    main,
    parse_ids_file,
    parse_ids_text,
    render_summary,
    run_instances,
)
from fixedyet.models import (
    Assessment,
    Confidence,
    NotReproducedReason,
    Outcome,
    ProgressEvent,
    Reproduction,
    SideRun,
    SweBenchInstance,
    Usage,
    Verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def instance(n: int, repo: str = "psf/requests") -> SweBenchInstance:
    return SweBenchInstance(
        instance_id=f"psf__requests-{n}", repo=repo, base_commit="abc", patch="diff --git a/x b/x",
        test_patch="diff --git a/t b/t", problem_statement=f"issue {n}", version="2.3",
    )


def side(*outcomes: Outcome, ref: str = "x") -> SideRun:
    return SideRun(ref=ref, outcomes=list(outcomes))


def assessment(
    verdict: Verdict,
    confidence: Confidence = Confidence.HIGH,
    *,
    cost: float = 0.0,
    credits: int = 0,
    reported: SideRun | None = None,
    fix: SideRun | None = None,
    basis: list[str] | None = None,
    reason: NotReproducedReason | None = None,
) -> Assessment:
    return Assessment(
        issue_url="https://github.com/psf/requests/issues/1", title="t", verdict=verdict,
        confidence=confidence, confidence_basis=basis or [],
        reproduction=Reproduction(reported_side=reported, fix_side=fix),
        not_reproduced_reason=reason,
        usage=Usage(cost_usd=cost, search_credits=credits), model="nemotron-test",
    )


def result(
    n: int, verdict: Verdict | None, confidence: Confidence | None = Confidence.HIGH,
    *, cost: float = 0.0, wall: float = 0.0, cause: str = "", credits: int = 0,
) -> InstanceResult:
    return InstanceResult(
        instance_id=f"psf__requests-{n}", repo="psf/requests", verdict=verdict,
        confidence=confidence if verdict else None, cost_usd=cost, wall_seconds=wall,
        failure_cause=cause, search_credits=credits,
    )


# --- ids file ------------------------------------------------------------------------------


def test_parse_ids_text_skips_comments_blanks_and_repeats() -> None:
    text = """
    # header comment
    psf__requests-1921        # trailing comment

    pytest-dev__pytest-6202
    psf__requests-1921
    """
    assert parse_ids_text(text) == ["psf__requests-1921", "pytest-dev__pytest-6202"]


def test_parse_ids_text_rejects_a_bad_line_with_its_number() -> None:
    with pytest.raises(ValueError, match="line 3"):
        parse_ids_text("psf__requests-1\n# ok\nnot an id\n")


def test_parse_ids_file_reads_the_shipped_selection() -> None:
    ids = parse_ids_file(REPO_ROOT / "eval" / "instances.txt")
    assert len(ids) == 15
    assert len(set(ids)) == 15
    repos = {i.rsplit("-", 1)[0] for i in ids}
    assert repos == {"psf__requests", "pytest-dev__pytest", "pylint-dev__pylint",
                     "pallets__flask"}


# --- metrics arithmetic ----------------------------------------------------------------------


def sample_results() -> list[InstanceResult]:
    return [
        result(1, Verdict.FIXED_ON_MAIN, Confidence.HIGH, cost=0.10, wall=60),
        result(2, Verdict.FIXED_ON_MAIN, Confidence.LOW, cost=0.20, wall=120),
        result(3, Verdict.OPEN_BUG, cost=0.30, wall=180, cause="test also fails"),
        result(4, Verdict.NOT_REPRODUCED, cost=0.40, wall=240, credits=3),
        result(5, Verdict.UNSUPPORTED, Confidence.LOW, cost=0.0, wall=30, credits=1),
        result(6, None, cost=0.0, wall=90, cause="RuntimeError: boom"),
    ]


def test_compute_metrics_counts_and_rates() -> None:
    m = compute_metrics(sample_results())
    assert m.n == 6
    assert m.counts == {
        "fixed_on_main": 2, "open_bug": 1, "not_reproduced": 1, "inconsistent": 0,
        "unsupported": 1, "needs_info": 0, "error": 1,
    }
    assert sum(m.counts.values()) == m.n
    assert m.rate("fixed_on_main") == pytest.approx(2 / 6)
    assert m.rate("open_bug") == pytest.approx(1 / 6)
    assert m.rate("needs_info") == 0.0
    assert m.reproduced_high_confidence == 1
    assert m.search_credits == 4


def test_compute_metrics_cost_and_time() -> None:
    m = compute_metrics(sample_results())
    assert m.total_cost_usd == pytest.approx(1.00)
    assert m.mean_cost_usd == pytest.approx(1.00 / 6)
    assert m.median_cost_usd == pytest.approx(0.15)  # sorted: 0, 0, .1, .2, .3, .4
    assert m.mean_wall_seconds == pytest.approx(720 / 6)
    assert m.median_wall_seconds == pytest.approx(105)  # sorted: 30, 60, 90, 120, 180, 240


def test_compute_metrics_on_no_results_is_all_zero() -> None:
    m = compute_metrics([])
    assert m.n == 0
    assert m.rate("fixed_on_main") == 0.0
    assert m.total_cost_usd == 0.0
    assert m.mean_cost_usd == 0.0
    assert m.median_wall_seconds == 0.0


# --- failure causes ------------------------------------------------------------------------


def test_failure_cause_by_verdict() -> None:
    fail, ok = side(Outcome.FAIL, Outcome.FAIL), side(Outcome.PASS, Outcome.PASS)
    assert failure_cause(assessment(Verdict.FIXED_ON_MAIN, reported=fail, fix=ok), None) == ""
    both_fail = assessment(Verdict.OPEN_BUG, reported=fail, fix=fail)
    assert "also fails with the gold patch" in failure_cause(both_fail, None)
    passes = assessment(
        Verdict.NOT_REPRODUCED, reported=ok, fix=ok, reason=NotReproducedReason.ENVIRONMENT
    )
    cause = failure_cause(passes, None)
    assert "passes at the base commit" in cause
    assert "reason environment" in cause
    regress = assessment(Verdict.INCONSISTENT, reported=ok, fix=fail)
    assert "fails with the gold patch" in failure_cause(regress, None)
    flaky = assessment(Verdict.INCONSISTENT, reported=side(Outcome.FAIL, Outcome.PASS), fix=ok)
    assert "runs disagree" in failure_cause(flaky, None)
    unsupported = assessment(Verdict.UNSUPPORTED, basis=["pip install failed"])
    assert failure_cause(unsupported, None) == "pip install failed"
    assert "no runnable reproduction" in failure_cause(assessment(Verdict.NEEDS_INFO), None)


def test_failure_cause_prefers_the_error() -> None:
    assert failure_cause(None, "TimeoutError: slow") == "TimeoutError: slow"


# --- summary rendering ---------------------------------------------------------------------


def make_report(results: list[InstanceResult], **kw: object) -> RunReport:
    defaults: dict[str, object] = dict(
        dataset="verified", requested_ids=[r.instance_id for r in results], results=results,
        budget_usd=10.0, concurrency=1, started_at=run.utcnow(), elapsed_seconds=725.0,
        status="complete",
    )
    defaults.update(kw)
    return run.build_report(**defaults)  # type: ignore[arg-type]


def test_render_summary_has_the_metric_table() -> None:
    text = render_summary(make_report(sample_results()))
    assert "# FixedYet evaluation, SWE-bench verified" in text
    assert "| Reproduction rate | 2/6 | 33.3% |" in text
    assert "| of which high confidence | 1/6 | 16.7% |" in text
    assert "| Wrong open-bug rate | 1/6 | 16.7% |" in text
    assert "| Not reproduced rate | 1/6 | 16.7% |" in text
    assert "| Inconsistent rate | 0/6 | 0.0% |" in text
    assert "| Setup failure rate | 1/6 | 16.7% |" in text
    assert "| Needs-info rate | 0/6 | 0.0% |" in text
    assert "| Harness error rate | 1/6 | 16.7% |" in text
    assert "| Total cost | $1.0000 |" in text
    assert "| Mean cost per issue | $0.1667 |" in text
    assert "| Median cost per issue | $0.1500 |" in text
    assert "| Mean wall time per issue | 2m00s |" in text
    assert "| Tavily credits (not in the cost above) | 4 |" in text
    assert "Elapsed: 12m05s" in text
    assert "nemotron-test" not in text  # no assessments attached to these sample results


def test_render_summary_instance_table_escapes_cells() -> None:
    rows = [result(3, Verdict.OPEN_BUG, cost=0.3, wall=65, cause="a | b\nsecond line")]
    text = render_summary(make_report(rows))
    assert "| psf__requests-3 | open_bug | high | $0.3000 | 1m05s | a \\| b second line |" in text


def test_render_summary_lists_models_and_skipped_ids() -> None:
    done = result(1, Verdict.FIXED_ON_MAIN, cost=4.0, wall=10)
    done.assessment = assessment(Verdict.FIXED_ON_MAIN, cost=4.0)
    report = make_report(
        [done], requested_ids=["psf__requests-1", "psf__requests-2"], status="budget_stop"
    )
    assert report.skipped_ids == ["psf__requests-2"]
    text = render_summary(report)
    assert "Model: nemotron-test" in text
    assert "Instances run: 1 of 2 requested" in text
    assert "Stopped early" in text
    assert "## Not run" in text
    assert "- psf__requests-2" in text


# --- running -------------------------------------------------------------------------------


class FakeReproduce:
    """Stands in for pipeline.reproduce_instance; records calls and hands out scripted results."""

    def __init__(self, script: dict[int, Assessment | BaseException] | None = None,
                 default_cost: float = 0.5, delay: float = 0.0) -> None:
        self.script = script or {}
        self.default_cost = default_cost
        self.delay = delay
        self.started: list[str] = []
        self.python_versions: list[str | None] = []
        self.running = 0
        self.max_running = 0

    async def __call__(
        self, inst: SweBenchInstance, *, settings: object = None, progress: object = None,
        python_version: str | None = None,
    ) -> Assessment:
        n = int(inst.instance_id.rsplit("-", 1)[1])
        self.started.append(inst.instance_id)
        self.python_versions.append(python_version)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if callable(progress):
                progress(ProgressEvent(stage="agent", message=f"working on {n}"))
            await asyncio.sleep(self.delay)
            outcome = self.script.get(n)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome or assessment(
                Verdict.FIXED_ON_MAIN, cost=self.default_cost, reported=side(Outcome.FAIL),
                fix=side(Outcome.PASS),
            )
        finally:
            self.running -= 1


async def test_run_instances_returns_results_in_input_order() -> None:
    fake = FakeReproduce(delay=0.01)
    lines: list[str] = []
    results = await run_instances(
        [instance(n) for n in (3, 1, 2)], reproduce=fake, concurrency=2, emit=lines.append
    )
    assert [r.instance_id for r in results] == ["psf__requests-3", "psf__requests-1",
                                               "psf__requests-2"]
    assert fake.max_running == 2
    assert all(r.verdict is Verdict.FIXED_ON_MAIN and r.cost_usd == 0.5 for r in results)
    assert all(r.python_version == "3.9" and r.wall_seconds > 0 for r in results)
    assert fake.python_versions == ["3.9", "3.9", "3.9"]  # SWE-bench's version reaches the build
    assert any("[1/3] start psf__requests-3" in line for line in lines)
    assert any("[agent] working on 1" in line for line in lines)
    assert any("psf__requests-2: fixed_on_main (high) cost $0.5000" in line for line in lines)


async def test_run_instances_concurrency_one_runs_one_at_a_time() -> None:
    fake = FakeReproduce(delay=0.01)
    await run_instances([instance(n) for n in range(1, 5)], reproduce=fake, concurrency=1,
                        emit=lambda s: None)
    assert fake.max_running == 1
    assert fake.started == [f"psf__requests-{n}" for n in range(1, 5)]


async def test_budget_stops_new_instances_but_not_running_ones() -> None:
    fake = FakeReproduce(default_cost=4.0)
    lines: list[str] = []
    results = await run_instances(
        [instance(n) for n in range(1, 6)], reproduce=fake, concurrency=1, budget_usd=10.0,
        emit=lines.append,
    )
    # $4 -> $8 (still under $10, so the third starts) -> $12 (over, so 4 and 5 never start)
    assert [r.instance_id for r in results] == [f"psf__requests-{n}" for n in (1, 2, 3)]
    assert fake.started == [f"psf__requests-{n}" for n in (1, 2, 3)]
    assert sum(r.cost_usd for r in results) == pytest.approx(12.0)
    budget_lines = [line for line in lines if line.startswith("budget reached")]
    assert len(budget_lines) == 1
    assert "$12.0000 of $10.0000" in budget_lines[0]


async def test_budget_boundary_stops_when_spend_equals_the_limit() -> None:
    fake = FakeReproduce(default_cost=5.0)
    results = await run_instances(
        [instance(n) for n in range(1, 5)], reproduce=fake, budget_usd=10.0,
        emit=lambda s: None,
    )
    assert len(results) == 2  # $5 + $5 = $10 reaches the budget exactly


async def test_a_crash_is_recorded_and_the_run_goes_on() -> None:
    fake = FakeReproduce(script={2: RuntimeError("sandbox exploded")})
    results = await run_instances([instance(n) for n in (1, 2, 3)], reproduce=fake,
                                  emit=lambda s: None)
    assert [r.category for r in results] == ["fixed_on_main", "error", "fixed_on_main"]
    broken = results[1]
    assert broken.verdict is None and broken.assessment is None
    assert broken.error == "RuntimeError: sandbox exploded"
    assert broken.failure_cause == broken.error
    assert broken.cost_usd == 0.0


class FakeBudgetExceeded(RuntimeError):
    """Same shape as fixedyet.llm.BudgetExceeded: carries what the run had spent."""

    def __init__(self, spent: float) -> None:
        super().__init__(f"run budget exceeded: spent ${spent:.4f}")
        self.spent = spent


async def test_a_crash_that_reports_its_spend_is_charged_to_the_budget() -> None:
    fake = FakeReproduce(script={1: FakeBudgetExceeded(0.65)})
    lines: list[str] = []
    results = await run_instances(
        [instance(1), instance(2)], reproduce=fake, budget_usd=0.5, emit=lines.append
    )
    assert len(results) == 1  # the second instance never started
    assert results[0].category == "error"
    assert results[0].cost_usd == pytest.approx(0.65)
    assert any(line.startswith("budget reached") for line in lines)


async def test_instance_timeout_is_an_error_result() -> None:
    fake = FakeReproduce(delay=5.0)
    results = await run_instances([instance(1)], reproduce=fake, instance_timeout_s=0.05,
                                  emit=lambda s: None)
    assert results[0].category == "error"
    assert results[0].error == "timed out after 0.05s"


async def test_checkpoint_sees_every_finished_instance() -> None:
    fake = FakeReproduce()
    seen: list[int] = []
    await run_instances([instance(n) for n in (1, 2, 3)], reproduce=fake, emit=lambda s: None,
                        checkpoint=lambda rs: seen.append(len(rs)))
    assert seen == [1, 2, 3]


# --- main() end to end ---------------------------------------------------------------------


@pytest.fixture
def fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> FakeReproduce:
    fake = FakeReproduce(default_cost=1.0)
    monkeypatch.setattr(run, "_resolve_reproduce", lambda: fake)

    def fake_load(dataset: str, ids: list[str] | None = None, **kw: object) -> list:
        assert dataset in ("lite", "verified")
        return [instance(int(i.rsplit("-", 1)[1])) for i in ids or []]

    monkeypatch.setattr(run, "load_instances", fake_load)
    return fake


def test_main_writes_json_and_markdown(
    fake_pipeline: FakeReproduce, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("# two\npsf__requests-1\npsf__requests-2\npsf__requests-3\n")
    out = tmp_path / "results"
    code = main(["--ids-file", str(ids_file), "--limit", "2", "--out", str(out),
                 "--dataset", "lite"])
    assert code == 0
    files = sorted(p.name for p in out.iterdir())
    assert len(files) == 2
    assert files[0].endswith(".json") and files[1].endswith(".md")
    assert files[0][:-5] == files[1][:-3]  # same UTC timestamp stem
    assert len(files[0]) == len("20261021T101500Z.json")

    report = RunReport.model_validate_json((out / files[0]).read_text(encoding="utf-8"))
    assert report.dataset == "lite"
    assert report.status == "complete"
    assert report.requested_ids == ["psf__requests-1", "psf__requests-2"]
    assert report.metrics.counts["fixed_on_main"] == 2
    assert report.metrics.total_cost_usd == pytest.approx(2.0)
    assert report.results[0].assessment is not None
    assert report.models == ["nemotron-test"]
    json.loads((out / files[0]).read_text(encoding="utf-8"))  # plain JSON

    markdown = (out / files[1]).read_text(encoding="utf-8")
    assert "| Reproduction rate | 2/2 | 100.0% |" in markdown
    stdout = capsys.readouterr().out
    assert "[1/2] start psf__requests-1" in stdout
    assert "| Reproduction rate | 2/2 | 100.0% |" in stdout


def test_main_budget_stop_is_recorded(
    fake_pipeline: FakeReproduce, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "results"
    code = main(["--ids", *[f"psf__requests-{n}" for n in range(1, 6)], "--out", str(out),
                 "--budget-usd", "2"])
    assert code == 0
    report = RunReport.model_validate_json(next(out.glob("*.json")).read_text(encoding="utf-8"))
    assert report.status == "budget_stop"
    assert len(report.results) == 2
    assert report.skipped_ids == [f"psf__requests-{n}" for n in (3, 4, 5)]
    assert "Stopped early" in next(out.glob("*.md")).read_text(encoding="utf-8")
    assert "budget reached" in capsys.readouterr().out


def test_main_ids_flag_replaces_the_ids_file(
    fake_pipeline: FakeReproduce, tmp_path: Path
) -> None:
    code = main(["--ids", "psf__requests-9", "--ids-file", str(tmp_path / "missing.txt"),
                 "--out", str(tmp_path / "o")])
    assert code == 0
    assert fake_pipeline.started == ["psf__requests-9"]


def test_main_returns_1_when_nothing_produced_an_assessment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeReproduce(script={1: RuntimeError("no key")})
    monkeypatch.setattr(run, "_resolve_reproduce", lambda: fake)
    monkeypatch.setattr(run, "load_instances", lambda d, ids=None, **kw: [instance(1)])
    assert main(["--ids", "psf__requests-1", "--out", str(tmp_path)]) == 1


def test_main_reports_bad_input_with_exit_code_2(
    fake_pipeline: FakeReproduce, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.txt"
    bad.write_text("nonsense\n")
    assert main(["--ids-file", str(bad), "--out", str(tmp_path)]) == 2
    assert "not a SWE-bench instance id" in capsys.readouterr().err
    assert main(["--ids-file", str(tmp_path / "nope.txt"), "--out", str(tmp_path)]) == 2
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n")
    assert main(["--ids-file", str(empty), "--out", str(tmp_path)]) == 2


def test_main_reports_a_missing_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing() -> object:
        raise ImportError("cannot import name 'reproduce_instance'")

    monkeypatch.setattr(run, "_resolve_reproduce", missing)
    monkeypatch.setattr(run, "load_instances", lambda d, ids=None, **kw: [instance(1)])
    assert main(["--ids", "psf__requests-1", "--out", str(tmp_path)]) == 2
    assert "reproduce_instance" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [["--concurrency", "3"], ["--concurrency", "0"],
                                  ["--limit", "0"], ["--budget-usd", "0"],
                                  ["--dataset", "full"]])
def test_main_rejects_bad_flags(flag: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([*flag, "--ids", "psf__requests-1"])
    assert exc.value.code == 2


def test_main_keeps_partial_results_on_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeReproduce(script={2: KeyboardInterrupt()})
    monkeypatch.setattr(run, "_resolve_reproduce", lambda: fake)
    monkeypatch.setattr(
        run, "load_instances", lambda d, ids=None, **kw: [instance(int(i[-1])) for i in ids]
    )
    code = main(["--ids", "psf__requests-1", "psf__requests-2", "--out", str(tmp_path)])
    assert code == 130
    report = RunReport.model_validate_json(next(tmp_path.glob("*.json")).read_text("utf-8"))
    assert report.status == "interrupted"
    assert [r.instance_id for r in report.results] == ["psf__requests-1"]
    assert report.skipped_ids == ["psf__requests-2"]
    assert report.finished_at is None
