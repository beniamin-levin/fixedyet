"""The reports behind the public "All reports" page, and the maintainer's state next to them.

    <runs_dir>/<stamp>-<owner>-<repo>-<number>/assessment.json + report.md   finished runs
    examples/*.json + *.md                                                   curated examples
    <runs_dir>/backports/<report id>.json                                    backport + draft PR
    <runs_dir>/fixes/<report id>.json                                        fix + draft PR
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fixedyet.models import Assessment, BackportResult, Verdict
from fixedyet.web import parse_issue_url

logger = logging.getLogger("fixedyet.web")

REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
BACKPORTS_DIR = "backports"
FIXES_DIR = "fixes"
HIDDEN_FILE = "hidden.json"
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")  # the rule backport.py applies to a fixing commit
STALE_DAYS = 180  # a target branch with no commit for longer than this looks abandoned


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


def fix_ready(assessment: Assessment) -> bool:
    """True when fix mode has something to work with: an open bug with a reproduction test, on a
    GitHub issue (the rule `fixmode.fix_ready` applies)."""
    rep = assessment.reproduction
    if assessment.verdict is not Verdict.OPEN_BUG or not (rep.repro_test_code
                                                         and rep.repro_test_path):
        return False
    try:
        parse_issue_url(assessment.issue_url)
    except ValueError:
        return False
    return True


def _iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def backport_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """``{status, pr_url, pr_target}`` of a saved backport record, or None when there is none.
    ``pr_target`` is "upstream" or "fork": where the pull request in ``pr_url`` is."""
    if not record:
        return None
    return {
        "status": record.get("status"),
        "pr_url": record.get("pr_url") or None,
        "pr_target": record.get("pr_target") if record.get("pr_url") else None,
    }


def pr_target_of(pr_url: str | None, upstream: str | None) -> str | None:
    """"upstream" when a pull request URL is in the ``upstream`` repository, else "fork"."""
    match = re.match(r"^https://github\.com/([^/]+/[^/]+)/pull/\d+", pr_url or "")
    if not match:
        return None
    return "upstream" if upstream and match.group(1).lower() == upstream.lower() else "fork"


def _major_minor(text: str | None) -> tuple[int, int | None] | None:
    """``(8, 4)`` for 8.4.x, v8.4.1, stable/8.4; ``(3, None)`` for 3.x; None for neither."""
    match = re.search(r"(\d+)\.(\d+)", text or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d+)\.[xX]\b", text or "")
    return (int(match.group(1)), None) if match else None


def target_warning(
    activity: object, branch: str | None, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Why a maintainer may not want a backport to this branch, or None when it looks alive.

    The rule: the branch's last commit is more than ``STALE_DAYS`` days old, or the project's
    latest release has a newer major.minor than the branch (a 8.4.x branch, release 9.1.1).
    ``activity`` is what ``github.target_activity`` saved; unknown pieces never raise a warning.
    """
    if not isinstance(activity, dict) or not branch:
        return None
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    date_text = None
    raw = activity.get("last_commit_date")
    if isinstance(raw, str):
        try:
            last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            last = None
        if last is not None:
            last = last if last.tzinfo else last.replace(tzinfo=UTC)
            date_text = last.date().isoformat()
            if now - last > timedelta(days=STALE_DAYS):
                reasons.append("stale")
    release = activity.get("latest_release")
    release_text = release if isinstance(release, str) and release else None
    ours, latest = _major_minor(branch), _major_minor(release_text)
    if ours and latest:
        depth = 2 if ours[1] is not None and latest[1] is not None else 1
        if latest[:depth] > ours[:depth]:
            reasons.append("superseded")
    if not reasons:
        return None
    return {
        "reasons": reasons,
        "text": (
            f"Last commit on {branch}: {date_text or 'unknown'}; latest release: "
            f"{release_text or 'unknown'}. Maintainers may not accept backports to this branch."
        ),
    }


def public_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """A saved backport record as the API shows it: plus ``target_warning``, worked out now."""
    if not record:
        return None
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    warning = target_warning(record.get("target_activity"), result.get("target_branch"))
    return {**record, "target_warning": warning}


def example_fix_record(assessment: dict[str, Any] | None) -> dict[str, Any] | None:
    """The fix a curated example carries in its assessment (``fix``), as the record the page
    shows for a fix saved on this server. ``example`` marks it: it was verified elsewhere, so
    there is no pull request step to offer for it here."""
    fix = assessment.get("fix") if isinstance(assessment, dict) else None
    if not isinstance(fix, dict) or not isinstance(fix.get("status"), str):
        return None
    return {
        "status": fix["status"], "pr_url": None, "pr_target": None, "pr_number": None,
        "prs": {}, "result": fix, "saved_at": fix.get("created_at"), "example": True,
    }


def normalise_record(record: dict[str, Any]) -> dict[str, Any]:
    """Give a record saved before pull requests had targets the ``prs`` map it now has: the
    first deployment only ever opened pull requests in the fork."""
    if isinstance(record.get("prs"), dict):
        return record
    prs: dict[str, Any] = {}
    url = record.get("pr_url")
    if url:
        old = record.get("pr") if isinstance(record.get("pr"), dict) else {}
        prs["fork"] = {**old, "url": url}
        record = {**record, "pr_target": "fork", "pr_number": old.get("number")}
    return {**record, "prs": prs}


def summarise(
    report_id: str, assessment: Assessment, *, example: bool, record: dict[str, Any] | None,
    fix_record: dict[str, Any] | None = None,
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
        "fix": backport_summary(fix_record),
        "can_fix": fix_ready(assessment),
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
        "backport": {
            "status": result.status.value, "pr_url": pr_url,
            "pr_target": pr_target_of(pr_url, result.repo),
        },
        "can_backport": False,
        "fix": None,
        "can_fix": False,
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
    fix_record: dict[str, Any] | None = None  # the saved fix of an open bug


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

    # -- saved backports and fixes ------------------------------------------------------

    def _record_path(self, folder_name: str, report_id: str) -> Path | None:
        if not valid_report_id(report_id):
            return None
        folder = self.runs_dir / folder_name
        path = folder / f"{report_id}.json"
        return path if inside(path, folder) else None

    def _load_record(self, folder_name: str, report_id: str) -> dict[str, Any] | None:
        path = self._record_path(folder_name, report_id)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.debug("skipping unreadable saved %s record %s", folder_name, path.name)
            return None
        if not (isinstance(data, dict) and isinstance(data.get("status"), str)):
            return None
        return normalise_record(data)

    def _save_record(self, folder_name: str, report_id: str, record: dict[str, Any]) -> None:
        path = self._record_path(folder_name, report_id)
        if path is None:
            raise ValueError("not a report id")
        write_json_atomic(path, record)

    def backport_path(self, report_id: str) -> Path | None:
        return self._record_path(BACKPORTS_DIR, report_id)

    def load_backport(self, report_id: str) -> dict[str, Any] | None:
        return self._load_record(BACKPORTS_DIR, report_id)

    def save_backport(self, report_id: str, record: dict[str, Any]) -> None:
        self._save_record(BACKPORTS_DIR, report_id, record)

    def fix_path(self, report_id: str) -> Path | None:
        return self._record_path(FIXES_DIR, report_id)

    def load_fix(self, report_id: str) -> dict[str, Any] | None:
        return self._load_record(FIXES_DIR, report_id)

    def save_fix(self, report_id: str, record: dict[str, Any]) -> None:
        self._save_record(FIXES_DIR, report_id, record)

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
        fix_record = self.load_fix(report_id)
        example = self._example(report_id)
        if example is not None:
            return self._found_example(example, record, fix_record)
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
            summary=summarise(report_id, assessment, example=False, record=record,
                              fix_record=fix_record),
            assessment=assessment.model_dump(mode="json"), markdown=markdown, record=record,
            extra={}, fix_record=fix_record,
        )

    def _found_example(
        self, item: dict[str, Any], record: dict[str, Any] | None,
        fix_record: dict[str, Any] | None = None,
    ) -> Found:
        report_id = str(item["id"])
        extra = {
            "label": item.get("label", ""), "note": item.get("note", ""),
            "fix_note": item.get("fix_note", ""),
            "illustrative": bool(item.get("illustrative", False)),
            "pr_url": item.get("pr_url"),
        }
        if item.get("backport"):
            result = BackportResult.model_validate(item["backport"])
            # the example's own result is its saved backport
            record = {
                "status": result.status.value, "pr_url": item.get("pr_url"),
                "pr_target": pr_target_of(item.get("pr_url"), result.repo),
                "result": item["backport"], "saved_at": _iso(result.created_at),
            }
            return Found(
                id=report_id, example=True,
                summary=summarise_backport(report_id, result, pr_url=item.get("pr_url")),
                assessment=None, markdown=item.get("markdown", ""), record=record, extra=extra,
            )
        assessment = Assessment.model_validate(item["assessment"])
        if fix_record is None:  # a fix saved on this server wins over the example's own
            fix_record = example_fix_record(item["assessment"])
        return Found(
            id=report_id, example=True,
            summary=summarise(report_id, assessment, example=True, record=record,
                              fix_record=fix_record),
            assessment=item["assessment"], markdown=item.get("markdown", ""), record=record,
            extra=extra, fix_record=fix_record,
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
            "backport": public_record(found.record),
            "can_backport": found.summary["can_backport"],
            "fix": found.fix_record,
            "can_fix": found.summary["can_fix"],
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
            summary = {
                **summary, "backport": backport_summary(self.load_backport(summary["id"])),
                "fix": backport_summary(self.load_fix(summary["id"])),
            }
            summaries[summary["id"]] = summary
        for item in self._examples():  # examples win over a run folder with the same name
            try:
                found = self._found_example(
                    item, self.load_backport(str(item["id"])), self.load_fix(str(item["id"]))
                )
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
