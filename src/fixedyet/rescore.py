"""`fixedyet rescore`: run the rule table again over saved results, with no model call.

    fixedyet rescore runs/                       # every <runs dir>/*/assessment.json, read only
    fixedyet rescore runs/ --write               # ... and update the files whose verdict changed
    fixedyet rescore eval/results/20261021T114507Z.json --md rescore.md

Inputs can be a runs directory, an `assessment.json`, an eval results file
(`eval/results/<stamp>.json`) or a saved web report (`{"assessment": {...}}`). Only
`assessment.json` files are ever written; eval results and web reports are read only, because
they are the record of a run.

What a re-score reads:

- the two `SideRun`s stored in the assessment (the sandbox runs are not repeated);
- the issue thread, for the rule about an exception the issue never names: GitHub for a live
  issue, the cached SWE-bench problem statement for an eval instance. Without it (`--offline`,
  or GitHub unreachable) that rule keeps whatever the assessment already says;
- the issue's state and linked pull requests (GitHub, `GitHubClient.issue_status`), or the ones
  stored in the assessment when GitHub cannot be asked.

Lines of the old basis that the rules did not write (the pipeline's caveats: no version stated,
an extra that could not be installed) are kept, and still cap the confidence at low.

`--write` is safe to repeat. A file changes only when the verdict, the confidence, the basis or
the stored issue status changes; the first verdict is kept in `Assessment.rescored`, and the
basis ends with "re-scored by rules <version>". Each file is replaced atomically (temp file and
`os.replace`, owner and mode kept), and `report.md` next to it is rendered again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from fixedyet.models import (
    Assessment,
    Confidence,
    IssueStatus,
    Rescore,
    Verdict,
    utcnow,
)
from fixedyet.verdict import (
    NOTE_PREFIXES,
    RULES_VERSION,
    decide,
    is_rule_line,
    issue_state_lines,
)

RESCORE_NOTE = "re-scored by rules "
_NEVER_NAMES = re.compile(r"^the test fails with \S+ on both sides and the issue never names it")
RESCORABLE = {
    Verdict.FIXED_ON_MAIN,
    Verdict.OPEN_BUG,
    Verdict.NOT_REPRODUCED,
    Verdict.INCONSISTENT,
}
LOWERED_ACTION = (
    "Read the basis before acting on this verdict: the rules lowered its confidence after the run."
)
TRUTHS = ("open", "fixed", "bad_test")
"""Labels for live issues: `open` (the bug is still there on main), `fixed` (it is not), and
`bad_test` (the issue is open, but the saved test cannot show it, so `open_bug` is wrong as
evidence). Eval instances are `fixed` by construction: the gold patch fixes them."""

Kind = Literal["assessment", "eval", "web"]


# -- the pure part ---------------------------------------------------------------------------------


def _both_sides_ran(a: Assessment) -> bool:
    rep = a.reproduction
    return bool(rep.reported_side and rep.reported_side.outcomes and rep.fix_side
                and rep.fix_side.outcomes)


def _status_key(status: IssueStatus | None) -> Any:
    return None if status is None else status.model_dump(exclude={"checked_at"})


def rescore_assessment(
    a: Assessment,
    *,
    thread: str | None = None,
    status: IssueStatus | None = None,
    at: datetime | None = None,
) -> Assessment:
    """The assessment as the current rules score it. Returns ``a`` itself when nothing changes.

    ``thread`` is the issue text (None: the rule about an exception the issue never names keeps
    the line the assessment has); ``status`` is a fresh issue status (None: the stored one).
    Verdicts that come from no run (needs_info, unsupported) are never changed.
    """
    if a.verdict not in RESCORABLE or not _both_sides_ran(a):
        return a
    rep = a.reproduction
    status = status if status is not None else a.issue_status
    old_basis = [line for line in a.confidence_basis if not line.startswith(RESCORE_NOTE)]
    extras = [line for line in old_basis if not is_rule_line(line)]
    caveats = [line for line in extras if not line.startswith(NOTE_PREFIXES)]
    notes = [line for line in extras if line.startswith(NOTE_PREFIXES)]

    verdict, confidence, basis = decide(
        rep.reported_side, rep.fix_side, runnable=True, env_ok=True, thread=thread
    )
    if thread is None and verdict is Verdict.OPEN_BUG:
        # Without the thread this rule cannot be checked again: keep what it found then.
        kept = [line for line in old_basis if _NEVER_NAMES.match(line)]
        if kept and not any(_NEVER_NAMES.match(line) for line in basis):
            basis, confidence = [*basis, *kept], Confidence.LOW
    state_lines = issue_state_lines(verdict, status)
    if (caveats or state_lines) and confidence is Confidence.HIGH:
        confidence = Confidence.LOW
    new_basis = [*basis, *state_lines, *caveats, *notes]

    same_result = (verdict, confidence, new_basis) == (a.verdict, a.confidence, old_basis)
    if same_result and _status_key(status) == _status_key(a.issue_status):
        return a

    first = a.rescored or Rescore(
        rules=RULES_VERSION,
        original_verdict=a.verdict,
        original_confidence=a.confidence,
        original_basis=list(a.confidence_basis),
        original_rules=a.rules,
    )
    first = first.model_copy(update={"rules": RULES_VERSION, "at": at or utcnow()})
    if (verdict, confidence) == (first.original_verdict, first.original_confidence):
        note = f"{RESCORE_NOTE}{RULES_VERSION}: verdict and confidence unchanged"
    else:
        note = (
            f"{RESCORE_NOTE}{RULES_VERSION}: the first verdict was "
            f"{first.original_verdict.value}, {first.original_confidence.value} confidence"
        )
    next_actions = list(a.next_actions)
    if verdict is not a.verdict:
        from fixedyet.pipeline import DEFAULT_NEXT_ACTIONS  # the model's actions fit the old one

        next_actions = list(DEFAULT_NEXT_ACTIONS[verdict])
    elif confidence is Confidence.LOW and LOWERED_ACTION not in next_actions:
        if a.confidence is Confidence.HIGH:
            next_actions = [LOWERED_ACTION, *next_actions]
    return a.model_copy(
        update={
            "verdict": verdict,
            "confidence": confidence,
            "confidence_basis": [*new_basis, note],
            "issue_status": status,
            "next_actions": next_actions,
            "rescored": first,
            "rules": RULES_VERSION,
        }
    )


# -- reading the inputs ---------------------------------------------------------------------------


@dataclass
class Item:
    label: str
    source: Path
    kind: Kind
    assessment: Assessment
    group: str  # the input it came from, for the per-input metrics
    instance_id: str | None = None
    dataset: str | None = None
    truth: str | None = None
    after: Assessment | None = None  # None until scored

    @property
    def changed(self) -> bool:
        """Anything stored changes (the issue status included)."""
        return self.after is not None and self.after is not self.assessment

    @property
    def result_changed(self) -> bool:
        """The verdict, the confidence or the basis changes."""
        if self.after is None:
            return False
        before = [b for b in self.assessment.confidence_basis if not b.startswith(RESCORE_NOTE)]
        after = [b for b in self.after.confidence_basis if not b.startswith(RESCORE_NOTE)]
        return (self.after.verdict, self.after.confidence, after) != (
            self.assessment.verdict, self.assessment.confidence, before
        )


@dataclass
class Loaded:
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_inputs(paths: Iterable[Path]) -> Loaded:
    """Every assessment in ``paths`` (runs directories, assessment files, eval results, web
    reports). Unreadable files become errors, never exceptions."""
    loaded = Loaded()
    for path in paths:
        if path.is_dir():
            # a runs directory (<stamp>-<owner>-<repo>-<n>/assessment.json), else a directory
            # of saved reports (*.json)
            root = path.resolve()
            runs = sorted(path.glob("*/assessment.json"))
            files = runs or sorted(path.glob("*.json"))
            if not files:
                loaded.errors.append(f"{path}: no */assessment.json or *.json in this directory")
            for file in files:
                if not file.resolve().is_relative_to(root) or file.is_symlink():
                    loaded.errors.append(f"{file}: outside {path}, skipped")
                    continue
                label = file.parent.name if runs else file.stem
                _load_file(file, loaded, group=str(path), label=label)
        elif path.is_file():
            # an eval results file is one run; single reports are grouped by their directory
            _load_file(path, loaded, group=str(path), label=path.stem)
        else:
            loaded.errors.append(f"{path}: no such file or directory")
    return loaded


def _load_file(path: Path, loaded: Loaded, *, group: str, label: str) -> None:
    try:
        data = _read_json(path)
    except (OSError, ValueError) as exc:
        loaded.errors.append(f"{path}: unreadable ({exc})")
        return
    try:
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            dataset = str(data.get("dataset") or "verified")
            for row in data["results"]:
                if not row.get("assessment"):
                    continue  # the run crashed: nothing to score
                loaded.items.append(Item(
                    label=str(row.get("instance_id")), source=path, kind="eval", group=group,
                    assessment=Assessment.model_validate(row["assessment"]),
                    instance_id=row.get("instance_id"), dataset=dataset, truth="fixed",
                ))
        elif isinstance(data, dict) and (
            isinstance(data.get("assessment"), dict) or "verdict" in data
        ):
            web = isinstance(data.get("assessment"), dict)
            a = Assessment.model_validate(data["assessment"] if web else data)
            kind: Kind = "assessment" if path.name == "assessment.json" and not web else "web"
            instance_id = _swebench_id(a)
            loaded.items.append(Item(
                label=label, source=path, kind=kind, group=_folder_group(path, group),
                assessment=a, instance_id=instance_id,
                dataset="verified" if instance_id else None,
                truth="fixed" if instance_id else None,
            ))
        else:
            loaded.errors.append(f"{path}: not an assessment, eval results or web report")
    except ValidationError as exc:
        loaded.errors.append(f"{path}: not a valid assessment ({exc.error_count()} errors)")


def _swebench_id(a: Assessment) -> str | None:
    """The instance id of an eval run saved as an assessment (``swe-bench:<id>``)."""
    prefix = "swe-bench:"
    return a.issue_url[len(prefix):] if a.issue_url.startswith(prefix) else None


def _folder_group(path: Path, group: str) -> str:
    """A report given as a file joins the other files of its directory; one found in a runs
    directory keeps that directory's group."""
    return str(path.parent) if group == str(path) else group


def parse_labels(text: str) -> dict[str, str]:
    """``<issue URL or instance id> <open|fixed|bad_test>`` per line; ``#`` starts a comment."""
    labels: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or parts[1] not in TRUTHS:
            raise ValueError(f"line {number}: expected '<url or id> <{'|'.join(TRUTHS)}>'")
        labels[parts[0].rstrip("/")] = parts[1]
    return labels


# -- what the rules need from outside --------------------------------------------------------------


def _eval_threads(items: list[Item], *, offline: bool, cache_dir: Path) -> dict[str, str]:
    """Problem statements of the eval instances, from the dataset cache (downloaded if missing
    and not offline). Empty when it cannot be read: the thread rule then keeps its lines."""
    from fixedyet.eval import swebench

    threads: dict[str, str] = {}
    for dataset in sorted({i.dataset for i in items if i.instance_id and i.dataset}):
        ids = sorted({i.instance_id for i in items if i.dataset == dataset and i.instance_id})
        try:
            cached = swebench._read_cache(swebench.cache_path(dataset, cache_dir))
            if cached is None and not offline:
                cached = swebench.load_instances(dataset, cache_dir=cache_dir)
        except (swebench.DatasetError, OSError) as exc:
            print(f"warning: SWE-bench {dataset} not readable ({exc})", file=sys.stderr)
            cached = None
        for instance in cached or []:
            if instance.instance_id in ids:
                threads[instance.instance_id] = instance.problem_statement
    return threads


async def _github_facts(
    urls: Sequence[str], token: str | None
) -> dict[str, tuple[str | None, IssueStatus | None]]:
    """(thread, status) per issue URL, best effort: (None, None) when GitHub fails."""
    from fixedyet.github import GitHubClient, GitHubError
    from fixedyet.pipeline import thread_text

    facts: dict[str, tuple[str | None, IssueStatus | None]] = {}
    async with GitHubClient(token) as gh:
        for url in urls:
            try:
                issue = await gh.fetch_issue(url)
            except (GitHubError, ValueError) as exc:
                print(f"warning: {url}: {exc}", file=sys.stderr)
                facts[url] = (None, None)
                continue
            facts[url] = (thread_text(issue), await gh.issue_status(issue))
    return facts


def score(
    items: list[Item],
    *,
    offline: bool = False,
    token: str | None = None,
    cache_dir: Path = Path("eval/cache"),
    at: datetime | None = None,
) -> None:
    """Fill in ``Item.after`` for every item."""
    eval_threads = _eval_threads(items, offline=offline, cache_dir=cache_dir)
    urls = sorted({
        i.assessment.issue_url for i in items
        if i.assessment.issue_url.startswith("https://github.com/")
        and i.assessment.verdict in RESCORABLE
    })
    facts = {} if offline or not urls else asyncio.run(_github_facts(urls, token))
    at = at or utcnow()
    for item in items:
        a = item.assessment
        if item.instance_id:  # an eval instance, in a results file or a runs directory
            thread, status = eval_threads.get(item.instance_id), None
        else:
            thread, status = facts.get(a.issue_url, (None, None))
        item.after = rescore_assessment(a, thread=thread, status=status, at=at)


# -- writing ---------------------------------------------------------------------------------------


def _replace(path: Path, text: str) -> None:
    """Write ``text`` over ``path`` atomically, keeping its mode (and owner, when root)."""
    tmp = path.with_name(f".{path.name}.rescore-tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        if path.exists():
            st = path.stat()
            os.chmod(tmp, st.st_mode & 0o7777)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_changes(items: list[Item]) -> list[Path]:
    """Update every changed `assessment.json` and the `report.md` next to it."""
    from fixedyet.report import render_markdown

    written: list[Path] = []
    for item in items:
        if not item.changed or item.kind != "assessment" or item.after is None:
            continue
        _replace(item.source, item.after.model_dump_json(indent=2))
        _replace(item.source.with_name("report.md"), render_markdown(item.after))
        written.append(item.source)
    return written


# -- the report ------------------------------------------------------------------------------------


@dataclass
class Counts:
    n: int = 0
    reproduced: int = 0
    open_bug: int = 0
    wrong_open_bug: int = 0
    wrong_open_bug_high: int = 0
    real_open: int = 0  # labelled open, and the first verdict said open_bug
    real_open_kept: int = 0  # ... still open_bug
    real_open_kept_high: int = 0


def counts(items: Iterable[Item], *, after: bool) -> Counts:
    c = Counts()
    for item in items:
        before_a = item.assessment
        a = (item.after or item.assessment) if after else item.assessment
        c.n += 1
        c.reproduced += a.verdict is Verdict.FIXED_ON_MAIN
        if a.verdict is Verdict.OPEN_BUG:
            c.open_bug += 1
            if item.truth in ("fixed", "bad_test"):
                c.wrong_open_bug += 1
                c.wrong_open_bug_high += a.confidence is Confidence.HIGH
        if item.truth == "open" and before_a.verdict is Verdict.OPEN_BUG:
            c.real_open += 1
            if a.verdict is Verdict.OPEN_BUG:
                c.real_open_kept += 1
                c.real_open_kept_high += a.confidence is Confidence.HIGH
    return c


def _arrow(before: int, after: int) -> str:
    return f"{before}" if before == after else f"{before} → {after}"


def _cell(text: str, limit: int = 300) -> str:
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _why(after: Assessment, before: Assessment) -> str:
    new = [line for line in after.confidence_basis
           if line not in before.confidence_basis and not line.startswith(RESCORE_NOTE)]
    gone = [line for line in before.confidence_basis
            if line not in after.confidence_basis and not line.startswith(RESCORE_NOTE)]
    return "; ".join(new) or ("no longer: " + "; ".join(gone) if gone else "basis unchanged")


def render(items: list[Item], *, errors: Sequence[str] = (), labelled: bool = False) -> str:
    groups: dict[str, list[Item]] = {}
    for item in items:
        groups.setdefault(item.group, []).append(item)
    lines = [f"# FixedYet re-score, rules {RULES_VERSION}", ""]
    lines += [
        "No model call: the stored sandbox runs, the issue thread and the issue's GitHub status "
        "go through the rule table again. Eval instances count as fixed (the gold patch fixes "
        "them), so every `open_bug` there is wrong."
        + (" Live issues use the labels file." if labelled else ""),
        "",
        "| Input | Items | Re-scored | fixed_on_main | open_bug | wrong open bug | wrong open bug "
        "at high confidence | real open bugs kept (high) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for group, members in groups.items():
        b, a = counts(members, after=False), counts(members, after=True)
        known = any(m.truth for m in members)
        kept = (
            f"{a.real_open_kept}/{b.real_open} ({a.real_open_kept_high} high)"
            if b.real_open else "-"
        )
        lines.append(
            f"| {_cell(group)} | {b.n} | {sum(m.result_changed for m in members)} "
            f"| {_arrow(b.reproduced, a.reproduced)} | {_arrow(b.open_bug, a.open_bug)} "
            f"| {_arrow(b.wrong_open_bug, a.wrong_open_bug) if known else '-'} "
            f"| {_arrow(b.wrong_open_bug_high, a.wrong_open_bug_high) if known else '-'} "
            f"| {kept} |"
        )
    changed = [i for i in items if i.result_changed]
    status_only = sum(1 for i in items if i.changed and not i.result_changed)
    lines += ["", "## Changes", ""]
    if not changed:
        lines.append("None: every result reads the same under the current rules.")
    else:
        lines += ["| Item | Input | Truth | Before | After | Why |", "|---|---|---|---|---|---|"]
        for item in changed:
            assert item.after is not None
            b, a = item.assessment, item.after
            lines.append(
                f"| {_cell(item.label, 80)} | {_cell(Path(item.group).name, 60)} "
                f"| {item.truth or '-'} | {b.verdict.value}, {b.confidence.value} "
                f"| {a.verdict.value}, {a.confidence.value} | {_cell(_why(a, b), 600)} |"
            )
    if status_only:
        lines += ["", f"{status_only} more result(s) keep their verdict and only gain the issue's "
                  "GitHub status (state, linked pull requests)."]
    if errors:
        lines += ["", "## Not read", ""] + [f"- {_cell(e)}" for e in errors]
    return "\n".join(lines) + "\n"


# -- command line ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fixedyet rescore",
        description="Run the verdict rules again over saved assessments and eval results, with "
        "no model call. Prints a markdown summary; changes nothing without --write.",
    )
    p.add_argument("paths", nargs="+", type=Path, metavar="PATH",
                   help="a runs directory, an assessment.json, an eval results file or a saved "
                   "web report")
    p.add_argument("--write", action="store_true",
                   help="update each changed assessment.json (and its report.md) in place; the "
                   "first verdict is kept in the file. Eval results and web reports are never "
                   "written")
    p.add_argument("--offline", action="store_true",
                   help="no GitHub and no dataset download: use what the files and the dataset "
                   "cache hold")
    p.add_argument("--labels", type=Path, metavar="FILE",
                   help="'<issue URL or instance id> <open|fixed|bad_test>' per line, for the "
                   "wrong-open-bug and real-open-bugs-kept counts of live issues")
    p.add_argument("--md", type=Path, metavar="FILE", help="also write the summary to FILE")
    p.add_argument("--dataset-cache", type=Path, default=Path("eval/cache"), metavar="DIR",
                   help="the SWE-bench cache directory (default eval/cache)")
    return p


def _token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        from fixedyet.config import get_settings

        return get_settings().github_token or None
    except Exception:  # noqa: BLE001 - a broken .env must not stop a read-only command
        return None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    labels: dict[str, str] = {}
    if args.labels:
        try:
            labels = parse_labels(args.labels.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: {args.labels}: {exc}", file=sys.stderr)
            return 2
    loaded = load_inputs(args.paths)
    for item in loaded.items:
        key = item.instance_id if item.kind == "eval" else item.assessment.issue_url.rstrip("/")
        item.truth = labels.get(key or "", item.truth)
    if not loaded.items:
        for error in loaded.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    score(loaded.items, offline=args.offline, token=None if args.offline else _token(),
          cache_dir=args.dataset_cache)
    summary = render(loaded.items, errors=loaded.errors, labelled=bool(labels))
    print(summary)
    if args.md:
        args.md.write_text(summary, encoding="utf-8")
    if args.write:
        written = write_changes(loaded.items)
        skipped = sum(1 for i in loaded.items if i.changed and i.kind != "assessment")
        print(f"wrote {len(written)} assessment file(s)"
              + (f"; {skipped} changed result(s) in read-only inputs were not written"
                 if skipped else ""), file=sys.stderr)
    else:
        changed = sum(1 for i in loaded.items if i.changed)
        print(f"{changed} result(s) would change; nothing written (add --write)", file=sys.stderr)
    return 1 if loaded.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
