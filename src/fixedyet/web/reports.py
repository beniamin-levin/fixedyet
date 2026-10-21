"""The reports behind the public "All reports" page, and the maintainer's state next to them.

    <runs_dir>/<stamp>-<owner>-<repo>-<number>/assessment.json + report.md   finished runs
    examples/*.json + *.md                                                   curated examples
    <runs_dir>/backports/<report id>.json                                    backport + draft PR
    <runs_dir>/hidden.json                                                   ids kept off the page

Report ids come from the URL, so every one is checked against a strict pattern and every path
built from one is resolved and checked to stay inside its directory. A folder that cannot be
read or does not validate is skipped (and logged at debug level): one bad file must not take the
page down. Nothing here talks to the network or a model.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fixedyet.models import Assessment, BackportResult, Verdict
from fixedyet.web import parse_issue_url

logger = logging.getLogger("fixedyet.web")

REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
BACKPORTS_DIR = "backports"
HIDDEN_FILE = "hidden.json"
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")  # the rule backport.py applies to a fixing commit


def valid_report_id(value: object) -> bool:
    """Letters, digits, ``.``, ``_`` and ``-``; starts with a letter or digit; no ``..``."""
    return isinstance(value, str) and bool(REPORT_ID.match(value)) and ".." not in value


def inside(path: Path, root: Path) -> bool:
    """True when ``path`` resolves to somewhere under ``root`` (symlinks and ``..`` followed)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write a temporary file next to ``path`` and rename it over, so a reader sees all or none."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def backport_ready(assessment: Assessment) -> bool:
    """True when ``backport()`` has something to work with: fixed on main, a fixing commit it can
    cherry-pick, and the reproduction test it verifies with."""
    rep = assessment.reproduction
    return (
        assessment.verdict is Verdict.FIXED_ON_MAIN
        and bool(_SHA.match((rep.fixing_commit or "").strip()))
        and bool(rep.repro_test_code and rep.repro_test_path)
    )


def _iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def backport_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """``{status, pr_url}`` of a saved backport record, or None when there is none."""
    if not record:
        return None
    return {"status": record.get("status"), "pr_url": record.get("pr_url") or None}


def summarise(
    report_id: str, assessment: Assessment, *, example: bool, record: dict[str, Any] | None
) -> dict[str, Any]:
    rep = assessment.reproduction
    try:
        owner, repo, number = parse_issue_url(assessment.issue_url)
        full_name: str | None = f"{owner}/{repo}"
    except ValueError:
        full_name, number = None, None
    return {
        "id": report_id,
        "kind": "assessment",
        "issue_url": assessment.issue_url,
        "repo": full_name,
        "number": number,
        "title": assessment.title,
        "verdict": assessment.verdict.value,
        "confidence": assessment.confidence.value,
        "reported_version": rep.reported_side.ref if rep.reported_side else None,
        "fix_ref": rep.fix_side.ref if rep.fix_side else None,
        "fixing_commit": rep.fixing_commit,
        "fixed_in_release": rep.fixed_in_release,
        "created_at": _iso(assessment.created_at),
        "duration_s": assessment.usage.wall_seconds,
        "cost_usd": assessment.usage.cost_usd,
        "example": example,
        "backport": backport_summary(record),
        "can_backport": backport_ready(assessment),
    }


def summarise_backport(
    report_id: str, result: BackportResult, *, pr_url: str | None
) -> dict[str, Any]:
    """The summary of a curated example that is itself a backport result."""
    try:
        _, _, number = parse_issue_url(result.issue_url)
    except ValueError:
        number = None
    return {
        "id": report_id,
        "kind": "backport",
        "issue_url": result.issue_url,
        "repo": result.repo or None,
        "number": number,
        "title": result.title,
        "verdict": f"backport_{result.status.value}",
        "confidence": "",
        "reported_version": result.reported_version,
        "fix_ref": result.target_branch,
        "fixing_commit": result.fixing_commit,
        "fixed_in_release": None,
        "created_at": _iso(result.created_at),
        "duration_s": result.usage.wall_seconds,
        "cost_usd": result.usage.cost_usd,
        "example": True,
        "backport": {"status": result.status.value, "pr_url": pr_url},
        "can_backport": False,
    }


@dataclass
class Found:
    """One report as the detail view needs it."""

    id: str
    example: bool
    summary: dict[str, Any]
    assessment: dict[str, Any] | None
    markdown: str
    record: dict[str, Any] | None  # the saved backport (an example backport's own result too)
    extra: dict[str, Any]  # label, note, illustrative, pr_url of an example


class ReportStore:
    """Finds, lists and annotates reports. ``examples`` returns what ``/api/examples`` serves;
    ``render`` writes the markdown of a run folder that has no ``report.md``."""

    def __init__(
        self,
        runs_dir: Path,
        examples: Callable[[], list[dict[str, Any]]],
        render: Callable[[Assessment], str],
    ) -> None:
        self.runs_dir = Path(runs_dir)
        self._examples = examples
        self._render = render
        # folder name -> ((mtime_ns, size) of assessment.json, summary); listing re-parses a
        # folder only when its file changed
        self._summaries: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

    # -- hidden ids ---------------------------------------------------------------------

    @property
    def _hidden_path(self) -> Path:
        return self.runs_dir / HIDDEN_FILE

    def hidden_ids(self) -> set[str]:
        try:
            data = json.loads(self._hidden_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError):
            logger.exception("hidden.json cannot be read; showing every report")
            return set()
        ids = data.get("hidden") if isinstance(data, dict) else data
        return {i for i in ids if valid_report_id(i)} if isinstance(ids, list) else set()

    def set_hidden(self, report_id: str, hidden: bool) -> None:
        ids = self.hidden_ids()
        if hidden:
            ids.add(report_id)
        else:
            ids.discard(report_id)
        write_json_atomic(self._hidden_path, {"hidden": sorted(ids)})

    # -- saved backports ----------------------------------------------------------------

    def backport_path(self, report_id: str) -> Path | None:
        if not valid_report_id(report_id):
            return None
        folder = self.runs_dir / BACKPORTS_DIR
        path = folder / f"{report_id}.json"
        return path if inside(path, folder) else None

    def load_backport(self, report_id: str) -> dict[str, Any] | None:
        path = self.backport_path(report_id)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.debug("skipping unreadable saved backport %s", path.name)
            return None
        return data if isinstance(data, dict) and isinstance(data.get("status"), str) else None

    def save_backport(self, report_id: str, record: dict[str, Any]) -> None:
        path = self.backport_path(report_id)
        if path is None:
            raise ValueError("not a report id")
        write_json_atomic(path, record)

    # -- reading reports ----------------------------------------------------------------

    def _run_folder(self, report_id: str) -> Path | None:
        if not valid_report_id(report_id):
            return None
        folder = self.runs_dir / report_id
        if not (inside(folder, self.runs_dir) and folder.is_dir()):
            return None
        return folder

    def _read_run(self, folder: Path) -> Assessment | None:
        try:
            return Assessment.model_validate_json(
                (folder / "assessment.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            logger.debug("skipping unreadable run folder %s", folder.name)
            return None

    def _example(self, report_id: str) -> dict[str, Any] | None:
        return next((e for e in self._examples() if e["id"] == report_id), None)

    def find(self, report_id: str, *, include_hidden: bool = False) -> Found | None:
        """The report with this id, or None when it is unknown, unreadable or hidden."""
        if not valid_report_id(report_id):
            return None
        if not include_hidden and report_id in self.hidden_ids():
            return None
        record = self.load_backport(report_id)
        example = self._example(report_id)
        if example is not None:
            return self._found_example(example, record)
        folder = self._run_folder(report_id)
        assessment = self._read_run(folder) if folder else None
        if folder is None or assessment is None:
            return None
        markdown = ""
        try:
            markdown = (folder / "report.md").read_text(encoding="utf-8")
        except OSError:
            with contextlib.suppress(Exception):
                markdown = self._render(assessment)
        return Found(
            id=report_id, example=False,
            summary=summarise(report_id, assessment, example=False, record=record),
            assessment=assessment.model_dump(mode="json"), markdown=markdown, record=record,
            extra={},
        )

    def _found_example(self, item: dict[str, Any], record: dict[str, Any] | None) -> Found:
        report_id = str(item["id"])
        extra = {
            "label": item.get("label", ""), "note": item.get("note", ""),
            "illustrative": bool(item.get("illustrative", False)),
            "pr_url": item.get("pr_url"),
        }
        if item.get("backport"):
            result = BackportResult.model_validate(item["backport"])
            # the example's own result is its saved backport
            record = {
                "status": result.status.value, "pr_url": item.get("pr_url"),
                "result": item["backport"], "saved_at": _iso(result.created_at),
            }
            return Found(
                id=report_id, example=True,
                summary=summarise_backport(report_id, result, pr_url=item.get("pr_url")),
                assessment=None, markdown=item.get("markdown", ""), record=record, extra=extra,
            )
        assessment = Assessment.model_validate(item["assessment"])
        return Found(
            id=report_id, example=True,
            summary=summarise(report_id, assessment, example=True, record=record),
            assessment=item["assessment"], markdown=item.get("markdown", ""), record=record,
            extra=extra,
        )

    def detail(self, found: Found, *, admin: bool) -> dict[str, Any]:
        return {
            "id": found.id,
            "example": found.example,
            "kind": found.summary["kind"],
            **found.extra,
            "summary": found.summary,
            "assessment": found.assessment,
            "markdown": found.markdown,
            "backport": found.record,
            "can_backport": found.summary["can_backport"],
            "hidden": found.id in self.hidden_ids() if admin else False,
        }

    # -- listing ------------------------------------------------------------------------

    def _run_summaries(self) -> list[dict[str, Any]]:
        try:
            folders = [p for p in self.runs_dir.iterdir() if p.is_dir()]
        except OSError:
            return []
        found: list[dict[str, Any]] = []
        for folder in folders:
            if not valid_report_id(folder.name) or not inside(folder, self.runs_dir):
                continue
            try:
                stat = (folder / "assessment.json").stat()
            except OSError:
                continue  # web/, backports/ and anything else that is not a run
            stamp = (stat.st_mtime_ns, stat.st_size)
            cached = self._summaries.get(folder.name)
            if cached and cached[0] == stamp:
                summary = cached[1]
            else:
                assessment = self._read_run(folder)
                if assessment is None:
                    continue
                summary = summarise(folder.name, assessment, example=False, record=None)
                self._summaries[folder.name] = (stamp, summary)
            found.append(summary)
        return found

    def summaries(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        """Summaries of every example and every readable run, newest first."""
        summaries: dict[str, dict[str, Any]] = {}
        for summary in self._run_summaries():
            summary = {**summary, "backport": backport_summary(self.load_backport(summary["id"]))}
            summaries[summary["id"]] = summary
        for item in self._examples():  # examples win over a run folder with the same name
            try:
                found = self._found_example(item, self.load_backport(str(item["id"])))
            except ValueError:
                logger.debug("skipping invalid example %s", item.get("id"))
                continue
            summaries[found.id] = found.summary
        hidden = self.hidden_ids()
        rows = [
            {**s, "hidden": True} if s["id"] in hidden else s
            for s in summaries.values()
            if include_hidden or s["id"] not in hidden
        ]
        return sorted(
            rows, key=lambda s: (datetime.fromisoformat(s["created_at"]), s["id"]), reverse=True
        )
