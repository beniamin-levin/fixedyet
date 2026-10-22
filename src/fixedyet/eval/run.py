"""Evaluation runner: reproduce each SWE-bench instance, aggregate the metrics.

    python -m fixedyet.eval.run --dataset verified --ids-file eval/instances.txt

For every instance the runner calls ``fixedyet.pipeline.reproduce_instance`` (the agent
sees the problem statement at the base commit, never the gold patch or SWE-bench's tests),
records the Assessment with the wall time, and writes two files into ``--out``:

    <UTC timestamp>.json   everything, including each full Assessment
    <UTC timestamp>.md     the metrics table and one row per instance

Both files are rewritten after every finished instance, so an interrupted run keeps what
it has. The run stops starting new instances once cumulative cost reaches ``--budget-usd``.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from fixedyet.eval.swebench import DatasetError, load_instances, python_version_for
from fixedyet.models import (
    Assessment,
    Confidence,
    ProgressEvent,
    SideRun,
    SweBenchInstance,
    Verdict,
    utcnow,
)

DEFAULT_IDS_FILE = Path("eval/instances.txt")
DEFAULT_OUT_DIR = Path("eval/results")
DEFAULT_BUDGET_USD = 10.0
DEFAULT_INSTANCE_TIMEOUT_S = 1800.0
MAX_CONCURRENCY = 2

# Result categories: the six verdicts plus "error" for a run that crashed or timed out.
CATEGORIES: tuple[str, ...] = (
    Verdict.FIXED_ON_MAIN.value,
    Verdict.OPEN_BUG.value,
    Verdict.NOT_REPRODUCED.value,
    Verdict.INCONSISTENT.value,
    Verdict.UNSUPPORTED.value,
    Verdict.NEEDS_INFO.value,
    "error",
)

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$")

ReproduceFn = Callable[..., Awaitable[Assessment]]
Status = Literal["running", "complete", "budget_stop", "interrupted"]


# ---------------------------------------------------------------------------
# Result and report models
# ---------------------------------------------------------------------------


class InstanceResult(BaseModel):
    instance_id: str
    repo: str
    version: str = ""
    python_version: str | None = None
    verdict: Verdict | None = None  # None when the run crashed or timed out
    confidence: Confidence | None = None
    cost_usd: float = 0.0
    search_credits: int = 0
    wall_seconds: float = 0.0
    failure_cause: str = ""  # empty when the bug was reproduced
    error: str | None = None
    assessment: Assessment | None = None

    @property
    def category(self) -> str:
        return self.verdict.value if self.verdict else "error"


class Metrics(BaseModel):
    n: int
    counts: dict[str, int]
    reproduced_high_confidence: int
    # Every open_bug is wrong here (the gold patch fixes the bug); these are the ones a reader
    # would have trusted. Default 0 so results files written before the field still load.
    wrong_open_bug_high_confidence: int = 0
    total_cost_usd: float
    mean_cost_usd: float
    median_cost_usd: float
    mean_wall_seconds: float
    median_wall_seconds: float
    search_credits: int

    def rate(self, category: str) -> float:
        return self.counts.get(category, 0) / self.n if self.n else 0.0


class RunReport(BaseModel):
    dataset: str
    status: Status
    started_at: datetime
    finished_at: datetime | None = None
    elapsed_seconds: float = 0.0
    budget_usd: float
    concurrency: int
    requested_ids: list[str]
    skipped_ids: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    metrics: Metrics
    results: list[InstanceResult]


# ---------------------------------------------------------------------------
# Ids file
# ---------------------------------------------------------------------------


def parse_ids_text(text: str) -> list[str]:
    """One instance id per line. ``#`` starts a comment; blank lines and repeats are dropped."""
    ids: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not _ID_RE.match(line):
            raise ValueError(f"line {number}: {line!r} is not a SWE-bench instance id")
        if line not in ids:
            ids.append(line)
    return ids


def parse_ids_file(path: Path) -> list[str]:
    return parse_ids_text(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _outcomes(side: SideRun | None) -> str:
    if side is None:
        return "not run"
    return ",".join(o.value for o in side.outcomes) or "no runs"


def failure_cause(assessment: Assessment | None, error: str | None) -> str:
    """One line saying why an instance was not reproduced. Empty for fixed_on_main."""
    if error:
        return error
    if assessment is None:
        return "no assessment"
    verdict = assessment.verdict
    repro = assessment.reproduction
    sides = f"base: {_outcomes(repro.reported_side)}; gold patch: {_outcomes(repro.fix_side)}"
    basis = "; ".join(assessment.confidence_basis)
    if verdict is Verdict.FIXED_ON_MAIN:
        return ""
    if verdict is Verdict.OPEN_BUG:
        return f"test also fails with the gold patch ({sides})"
    if verdict is Verdict.NOT_REPRODUCED:
        reason = assessment.not_reproduced_reason
        suffix = f", reason {reason.value}" if reason else ""
        return f"test passes at the base commit{suffix} ({sides})"
    if verdict is Verdict.INCONSISTENT:
        reported, fix = repro.reported_side, repro.fix_side
        if (reported and reported.stable_outcome is None) or (fix and fix.stable_outcome is None):
            return f"runs disagree on a side ({sides})"
        if reported and fix and reported.stable_outcome is fix.stable_outcome:
            # rules v2: both sides fail, with different failure text
            return f"fails on both sides, with different failure text ({sides})"
        return f"passes at base but fails with the gold patch ({sides})"
    if verdict is Verdict.UNSUPPORTED:
        return basis or f"environment or test errored ({sides})"
    if verdict is Verdict.NEEDS_INFO:
        return basis or "no runnable reproduction in the issue"
    return basis


def compute_metrics(results: list[InstanceResult]) -> Metrics:
    n = len(results)
    counts = {c: 0 for c in CATEGORIES}
    for r in results:
        counts[r.category] += 1
    costs = [r.cost_usd for r in results]
    walls = [r.wall_seconds for r in results]
    high = sum(
        1 for r in results if r.verdict is Verdict.FIXED_ON_MAIN and r.confidence is Confidence.HIGH
    )
    wrong_high = sum(
        1 for r in results if r.verdict is Verdict.OPEN_BUG and r.confidence is Confidence.HIGH
    )
    return Metrics(
        n=n,
        counts=counts,
        reproduced_high_confidence=high,
        wrong_open_bug_high_confidence=wrong_high,
        total_cost_usd=sum(costs),
        mean_cost_usd=statistics.fmean(costs) if costs else 0.0,
        median_cost_usd=statistics.median(costs) if costs else 0.0,
        mean_wall_seconds=statistics.fmean(walls) if walls else 0.0,
        median_wall_seconds=statistics.median(walls) if walls else 0.0,
        search_credits=sum(r.search_credits for r in results),
    )


def build_report(
    *,
    dataset: str,
    requested_ids: list[str],
    results: list[InstanceResult],
    budget_usd: float,
    concurrency: int,
    started_at: datetime,
    elapsed_seconds: float,
    status: Status,
) -> RunReport:
    done = {r.instance_id for r in results}
    models = sorted({r.assessment.model for r in results if r.assessment and r.assessment.model})
    return RunReport(
        dataset=dataset,
        status=status,
        started_at=started_at,
        finished_at=utcnow() if status in ("complete", "budget_stop") else None,
        elapsed_seconds=elapsed_seconds,
        budget_usd=budget_usd,
        concurrency=concurrency,
        requested_ids=requested_ids,
        skipped_ids=[i for i in requested_ids if i not in done],
        models=models,
        metrics=compute_metrics(results),
        results=results,
    )


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------

_METRIC_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Reproduction rate", "fixed_on_main",
     "the agent's test fails at the base commit and passes with the gold patch"),
    ("Wrong open-bug rate", "open_bug",
     "the test fails on both sides, so the verdict says the bug is still open"),
    ("Not reproduced rate", "not_reproduced", "the test passes at the base commit"),
    ("Inconsistent rate", "inconsistent",
     "passes at base but fails with the patch, or runs disagree"),
    ("Setup failure rate", "unsupported", "the environment could not be built or the test errored"),
    ("Needs-info rate", "needs_info", "no runnable reproduction found in the issue"),
    ("Harness error rate", "error", "the run crashed or timed out before an Assessment"),
)


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def _usd(value: float) -> str:
    return f"${value:.4f}"


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _cell(text: str, limit: int = 140) -> str:
    flat = " ".join(text.split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def render_summary(report: RunReport) -> str:
    m = report.metrics
    lines: list[str] = [f"# FixedYet evaluation, SWE-bench {report.dataset}", ""]
    lines.append(f"- Started: {report.started_at:%Y-%m-%d %H:%M:%S} UTC")
    lines.append(f"- Instances run: {m.n} of {len(report.requested_ids)} requested")
    if report.models:
        lines.append(f"- Model: {', '.join(report.models)}")
    lines.append(f"- Concurrency: {report.concurrency}; budget: {_usd(report.budget_usd)}")
    lines.append(f"- Elapsed: {_duration(report.elapsed_seconds)}")
    if report.status == "budget_stop":
        lines.append(
            f"- **Stopped early:** cumulative cost reached the budget; "
            f"{len(report.skipped_ids)} instances were not run. Rates cover the {m.n} that ran."
        )
    elif report.status == "interrupted":
        lines.append(
            f"- **Interrupted:** {len(report.skipped_ids)} instances were not run. "
            f"Rates cover the {m.n} that ran."
        )
    elif report.status == "running":
        lines.append(
            f"- **In progress:** {len(report.skipped_ids)} instances have not finished yet. "
            f"Rates cover the {m.n} done so far."
        )
    lines += ["", "## Metrics", "", "| Metric | Count | Rate | Definition |", "|---|---|---|---|"]
    for label, key, definition in _METRIC_ROWS:
        count = m.counts.get(key, 0)
        lines.append(f"| {label} | {count}/{m.n} | {_pct(m.rate(key))} | {definition} |")
        if key == "fixed_on_main":
            high = m.reproduced_high_confidence
            share = high / m.n if m.n else 0.0
            lines.append(
                f"| of which high confidence | {high}/{m.n} | {_pct(share)} "
                f"| both runs agree on both sides |"
            )
        if key == "open_bug":
            wrong = m.wrong_open_bug_high_confidence
            share = wrong / m.n if m.n else 0.0
            lines.append(
                f"| Wrong open bug at high confidence | {wrong}/{m.n} | {_pct(share)} "
                "| the open_bug verdicts no rule flagged: the ones a maintainer would trust |"
            )
    lines += [
        "",
        "## Cost and time",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Total cost | {_usd(m.total_cost_usd)} |",
        f"| Mean cost per issue | {_usd(m.mean_cost_usd)} |",
        f"| Median cost per issue | {_usd(m.median_cost_usd)} |",
        f"| Mean wall time per issue | {_duration(m.mean_wall_seconds)} |",
        f"| Median wall time per issue | {_duration(m.median_wall_seconds)} |",
        f"| Tavily credits (not in the cost above) | {m.search_credits} |",
        "",
        "## Instances",
        "",
        "| Instance | Verdict | Confidence | Cost | Time | Failure cause |",
        "|---|---|---|---|---|---|",
    ]
    for r in report.results:
        lines.append(
            f"| {r.instance_id} | {r.category} | {r.confidence.value if r.confidence else '-'} "
            f"| {_usd(r.cost_usd)} | {_duration(r.wall_seconds)} "
            f"| {_cell(r.failure_cause) or '-'} |"
        )
    if report.skipped_ids:
        lines += ["", "## Not run", ""] + [f"- {i}" for i in report.skipped_ids]
    return "\n".join(lines) + "\n"


def write_outputs(report: RunReport, out_dir: Path, stamp: str) -> tuple[Path, Path]:
    """Write ``<stamp>.json`` and ``<stamp>.md`` into ``out_dir``; return both paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_summary(report), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _emit(line: str) -> None:
    print(line, flush=True)


def _resolve_reproduce() -> ReproduceFn:
    """Import the pipeline on first use, so loading this module needs no settings or keys."""
    from fixedyet.pipeline import reproduce_instance

    return reproduce_instance


async def _run_one(
    instance: SweBenchInstance,
    reproduce: ReproduceFn,
    timeout_s: float | None,
    emit: Callable[[str], None],
) -> InstanceResult:
    def progress(event: ProgressEvent) -> None:
        emit(f"    {instance.instance_id} [{event.stage}] {_cell(event.message, 160)}")

    started = time.monotonic()
    assessment: Assessment | None = None
    error: str | None = None
    lost_cost = 0.0  # money spent by a run that died before returning an Assessment
    deadline = asyncio.timeout(timeout_s)
    python_version = python_version_for(instance)  # SWE-bench's own version for this release
    try:
        async with deadline:
            assessment = await reproduce(
                instance, progress=progress, python_version=python_version
            )
    except TimeoutError as exc:
        if deadline.expired():
            error = f"timed out after {timeout_s:g}s"
        else:
            error = _cell(f"TimeoutError: {exc}", 300)
    except Exception as exc:  # one broken instance must not end the run
        error = _cell(f"{type(exc).__name__}: {exc}", 300)
        spent = getattr(exc, "spent", None)  # llm.BudgetExceeded carries what the run cost
        if isinstance(spent, int | float):
            lost_cost = float(spent)
    wall = time.monotonic() - started
    return InstanceResult(
        instance_id=instance.instance_id,
        repo=instance.repo,
        version=instance.version,
        python_version=python_version,
        verdict=assessment.verdict if assessment else None,
        confidence=assessment.confidence if assessment else None,
        cost_usd=assessment.usage.cost_usd if assessment else lost_cost,
        search_credits=assessment.usage.search_credits if assessment else 0,
        wall_seconds=wall,
        failure_cause=failure_cause(assessment, error),
        error=error,
        assessment=assessment,
    )


async def run_instances(
    instances: list[SweBenchInstance],
    *,
    reproduce: ReproduceFn,
    concurrency: int = 1,
    budget_usd: float = DEFAULT_BUDGET_USD,
    instance_timeout_s: float | None = DEFAULT_INSTANCE_TIMEOUT_S,
    emit: Callable[[str], None] = _emit,
    checkpoint: Callable[[list[InstanceResult]], None] | None = None,
) -> list[InstanceResult]:
    """Run the instances, at most ``concurrency`` at a time; return results in input order.

    Before starting each instance the cumulative cost is checked, and once it has reached
    ``budget_usd`` no further instance starts. Instances already running are allowed to
    finish, so the total can pass the budget by up to ``concurrency`` instances' cost.
    Instances that never started are simply absent from the returned list.
    """
    finished: dict[str, InstanceResult] = {}
    gate = asyncio.Semaphore(concurrency)
    total = len(instances)
    spent = 0.0
    budget_said = False

    def ordered() -> list[InstanceResult]:
        return [finished[i.instance_id] for i in instances if i.instance_id in finished]

    async def worker(position: int, instance: SweBenchInstance) -> None:
        nonlocal spent, budget_said
        async with gate:
            if spent >= budget_usd:
                if not budget_said:
                    budget_said = True
                    emit(
                        f"budget reached: spent {_usd(spent)} of {_usd(budget_usd)}; "
                        f"not starting the remaining instances"
                    )
                return
            emit(f"[{position}/{total}] start {instance.instance_id}")
            result = await _run_one(instance, reproduce, instance_timeout_s, emit)
            spent += result.cost_usd
            finished[instance.instance_id] = result
            emit(
                f"[{position}/{total}] {instance.instance_id}: {result.category}"
                f" ({result.confidence.value if result.confidence else '-'})"
                f" cost {_usd(result.cost_usd)} time {_duration(result.wall_seconds)}"
                f" | spent {_usd(spent)} of {_usd(budget_usd)}"
            )
            if checkpoint:
                checkpoint(ordered())

    await asyncio.gather(*(worker(n, inst) for n, inst in enumerate(instances, start=1)))
    return ordered()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _concurrency(value: str) -> int:
    n = int(value)
    if not 1 <= n <= MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_CONCURRENCY}")
    return n


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def _positive_float(value: str) -> float:
    x = float(value)
    if x <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return x


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fixedyet eval",
        description="Run FixedYet over SWE-bench instances and report the metrics.",
    )
    p.add_argument("--dataset", choices=["lite", "verified"], default="verified")
    p.add_argument("--ids-file", type=Path, default=DEFAULT_IDS_FILE,
                   help="one instance id per line, # comments allowed")
    p.add_argument("--ids", nargs="+", metavar="ID",
                   help="instance ids on the command line; replaces --ids-file")
    p.add_argument("--limit", type=_positive_int, help="run only the first N ids")
    p.add_argument("--concurrency", type=_concurrency, default=1,
                   help=f"instances at a time, 1 to {MAX_CONCURRENCY} (default 1)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="results directory")
    p.add_argument("--budget-usd", type=_positive_float, default=DEFAULT_BUDGET_USD,
                   help="stop starting instances once cumulative cost reaches this (default 10)")
    p.add_argument("--instance-timeout-s", type=_positive_float,
                   default=DEFAULT_INSTANCE_TIMEOUT_S,
                   help="give up on one instance after this many seconds (default 1800)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ids = list(dict.fromkeys(args.ids)) if args.ids else parse_ids_file(args.ids_file)
        if args.limit:
            ids = ids[: args.limit]
        if not ids:
            raise ValueError("no instance ids to run")
        instances = load_instances(args.dataset, ids)
        reproduce = _resolve_reproduce()
    except (OSError, ValueError, DatasetError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    started_at = utcnow()
    clock = time.monotonic()
    latest: list[InstanceResult] = []

    def make_report(results: list[InstanceResult], status: Status) -> RunReport:
        return build_report(
            dataset=args.dataset, requested_ids=ids, results=results,
            budget_usd=args.budget_usd, concurrency=args.concurrency, started_at=started_at,
            elapsed_seconds=time.monotonic() - clock, status=status,
        )

    def checkpoint(results: list[InstanceResult]) -> None:
        latest[:] = results
        write_outputs(make_report(results, "running"), args.out, stamp)

    _emit(f"running {len(instances)} instances from SWE-bench {args.dataset}, "
          f"concurrency {args.concurrency}, budget {_usd(args.budget_usd)}")
    try:
        results = asyncio.run(
            run_instances(
                instances, reproduce=reproduce, concurrency=args.concurrency,
                budget_usd=args.budget_usd, instance_timeout_s=args.instance_timeout_s,
                checkpoint=checkpoint,
            )
        )
    except KeyboardInterrupt:
        json_path, md_path = write_outputs(make_report(latest, "interrupted"), args.out, stamp)
        _emit(f"interrupted; partial results in {json_path} and {md_path}")
        return 130

    status: Status = "complete" if len(results) == len(ids) else "budget_stop"
    report = make_report(results, status)
    json_path, md_path = write_outputs(report, args.out, stamp)
    _emit("")
    _emit(render_summary(report))
    _emit(f"wrote {json_path} and {md_path}")
    return 0 if any(r.assessment for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
