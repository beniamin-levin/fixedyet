"""FastAPI demo app: paste an issue URL, watch the progress, read the report.

Endpoints
    GET  /                      the single page (static/index.html)
    POST /api/runs              start a live run, returns {"id"}
    GET  /api/runs/{id}         status, progress events, assessment and markdown
    GET  /api/runs/{id}/events  progress as server-sent events
    GET  /api/examples          saved example assessments from the examples/ directory
    GET  /api/status            live runs left today, and the model budget left in USD
    GET  /reports               the "All reports" page (static/reports.html)
    GET  /api/reports           summaries of every example and finished run, newest first
    GET  /api/reports/{id}      one report: assessment, markdown, and its saved backport
    GET  /healthz               liveness

Maintainer mode (only when ``settings.admin_token`` is set; otherwise every endpoint below
answers 404, and a wrong token answers 401 with nothing else)
    GET    /api/admin/check             {"ok": true} for a valid ``Authorization: Bearer`` token
    POST   /api/reports/{id}/backport   step 1: verify the report's backport in the sandbox, no
                                        pull request (a job: progress over /api/runs)
    POST   /api/reports/{id}/backport/pr  step 2: for a verified backport, open a DRAFT pull
                                        request in the token owner's fork, or on the upstream
                                        project once the maintainer typed its name
    POST   /api/reports/{id}/hide       keep a report off the public page
    DELETE /api/reports/{id}/hide       show it again

Live runs cost real money, so the app allows one run at a time and a global daily limit
(``settings.demo_daily_limit``, counted in a JSON file under ``settings.runs_dir``). Beyond that,
the spend ledger caps the total model spend of all runs ever (``settings.total_budget_usd``): a
run is refused with 429 when less than ``settings.run_budget_usd`` is left under that cap.
Finished runs are written to ``<runs_dir>/web/<id>.json`` and survive restarts. A backport job
is a run of kind "backport": it takes the same single slot, streams the same events, and does
not count against the daily limit. It only verifies; opening the pull request is a second,
separate request that the maintainer makes after reading the result.

The page and the API work at the root of a host and under a prefix. The page asks for
``static/...`` and ``api/...`` without a leading slash. Behind a reverse proxy that strips a
prefix (nginx ``location /fixedyet/ { proxy_pass http://127.0.0.1:8090/; }``), start the server
with ``fixedyet serve --root-path /fixedyet`` so redirects and absolute URLs in JSON carry it.
Responses never carry secrets or stack traces: failures are logged on the server and the
client gets a short, friendly message.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fixedyet.models import Assessment, BackportResult, ProgressEvent
from fixedyet.web import parse_issue_url
from fixedyet.web.reports import ReportStore, backport_ready, public_record, valid_report_id

logger = logging.getLogger("fixedyet.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
RUN_TIMEOUT_S = 25 * 60
BACKPORT_TIMEOUT_S = 75 * 60  # two project test suites (900 s each) plus the build
PULL_REQUEST_TIMEOUT_S = 60  # fork, push and open: seconds of GitHub and git, no sandbox
KEEPALIVE_S = 15.0
MAX_RUNS_IN_MEMORY = 100
DEFAULT_DAILY_LIMIT = 5
DEFAULT_RUN_BUDGET_USD = 0.60

PAUSED_MESSAGE = (
    "Live runs are paused while the demo has no sandbox backend, so nothing can be reproduced "
    "right now. Read the example reports instead, or run FixedYet yourself."
)

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        b"connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    ),
]


# --- seams to the core modules (imported lazily; tests replace these) -----------------


# A branch the maintainer names for a backport (git's rules, kept simple: no "..", no spaces).
_TARGET_BRANCH_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9_./+-]{0,199}$")


def load_settings() -> Any:
    from fixedyet.config import get_settings

    return get_settings()


async def run_pipeline(issue_url: str, *, settings: Any, progress: Any) -> Assessment:
    from fixedyet.pipeline import reproduce

    return await reproduce(issue_url, settings=settings, progress=progress)


async def run_backport(
    assessment: Assessment, *, settings: Any, progress: Any, target_branch: str | None = None
) -> BackportResult:
    from fixedyet.backport import backport

    return await backport(
        assessment, settings=settings, progress=progress, target_branch=target_branch
    )


def github_token(settings: Any) -> str:
    """The token pull requests are opened with: GITHUB_TOKEN, else ``gh auth token``."""
    from fixedyet.pullrequest import resolve_token

    return resolve_token(getattr(settings, "github_token", None))


async def fetch_target_activity(repo: str, branch: str, *, settings: Any) -> dict[str, Any] | None:
    """When the maintenance branch last moved and what the latest release is, read with the GitHub
    token. Best effort: None when it cannot be read, and never an exception."""
    from fixedyet.github import target_activity

    try:
        token: str | None = await asyncio.to_thread(github_token, settings)
    except Exception:  # noqa: BLE001 - no token is fine here: public data, a lower rate limit
        token = None
    try:
        return await target_activity(repo, branch, token=token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("target activity of %s@%s unreadable (%s)", repo, branch, type(exc).__name__)
        return None


async def open_draft_pr(
    result: BackportResult, *, token: str, say: Callable[[str], None], target: str = "fork"
) -> Any:
    """Open the draft pull request: in the token owner's fork, or on the upstream project. The
    endpoint only passes ``"upstream"`` after the maintainer typed the repository's name."""
    from fixedyet.pullrequest import open_backport_pr

    return await open_backport_pr(
        result, token=token, target="upstream" if target == "upstream" else "fork",
        confirmed_upstream=target == "upstream", say=say,
    )


def render_report(assessment: Assessment) -> str:
    from fixedyet.report import render_markdown

    return render_markdown(assessment)


def render_backport(result: BackportResult) -> str:
    from fixedyet.report import render_backport_markdown

    return render_backport_markdown(result)


def budget_remaining(settings: Any) -> float:
    """USD left under the total spend cap. An unreadable ledger counts as nothing left."""
    from fixedyet import ledger

    try:
        return ledger.remaining(settings)
    except ledger.LedgerError:
        logger.exception("the spend ledger cannot be read; treating the budget as used up")
        return 0.0


# --- small helpers --------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _error(
    status: int, code: str, message: str, *, headers: dict[str, str] | None = None, **extra: Any
) -> JSONResponse:
    body = {"error": code, "message": message, **extra}
    return JSONResponse(body, status_code=status, headers=headers)


def _public_error(exc: BaseException) -> str:
    """A message safe to show to anyone. The real exception goes to the server log only."""
    if isinstance(exc, TimeoutError):
        return (
            f"The run took longer than {RUN_TIMEOUT_S // 60} minutes and was stopped. "
            "That says nothing about the issue itself."
        )
    if type(exc).__name__ == "TotalBudgetExceeded":
        return (
            "The demo's total spending budget is used up, so the run was stopped. "
            "Nothing was concluded about the issue."
        )
    if type(exc).__name__ == "BudgetExceeded":
        return (
            "The run reached its cost budget before it could reach a verdict. "
            "Nothing was concluded about the issue."
        )
    return (
        "The run failed before it reached a verdict. That says nothing about the issue itself. "
        "The details are in the server log."
    )


# --- run records ----------------------------------------------------------------------


@dataclass
class Run:
    id: str
    issue_url: str
    created_at: datetime = field(default_factory=_now)
    status: str = "running"  # running | done | error
    finished_at: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    assessment: dict[str, Any] | None = None
    markdown: str | None = None
    error: str | None = None
    kind: str = "run"  # run | backport
    report_id: str | None = None  # a backport job: the report whose fix it backports
    result: dict[str, Any] | None = None  # a backport job: {"status", "pr_url"} once finished
    _changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def add_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.notify()

    def notify(self) -> None:
        """Wake everyone waiting for news. Waiters grab ``_changed`` before they look."""
        old, self._changed = self._changed, asyncio.Event()
        old.set()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "issue_url": self.issue_url,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "events": self.events,
            "assessment": self.assessment,
            "markdown": self.markdown,
            "error": self.error,
            "kind": self.kind,
            "report_id": self.report_id,
            "result": self.result,
        }

    @classmethod
    def from_public(cls, data: dict[str, Any]) -> Run:
        run = cls(
            id=data["id"],
            issue_url=data["issue_url"],
            created_at=datetime.fromisoformat(data["created_at"]),
            status=data.get("status", "error"),
            finished_at=(
                datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None
            ),
            events=list(data.get("events") or []),
            assessment=data.get("assessment"),
            markdown=data.get("markdown"),
            error=data.get("error"),
            kind=str(data.get("kind") or "run"),
            report_id=data.get("report_id"),
            result=data.get("result"),
        )
        if run.status == "running":  # the process died mid-run
            run.status = "error"
            run.error = "The server restarted while this run was in progress."
        return run


class RunStore:
    """In-memory runs backed by JSON files, plus the daily counter."""

    def __init__(self, runs_dir: Path) -> None:
        self.dir = runs_dir / "web"
        self.runs: dict[str, Run] = {}
        self.active: Run | None = None
        self.tasks: set[asyncio.Task[None]] = set()

    # persistence
    def _path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def save(self, run: Run) -> None:
        try:
            self._write_json(self._path(run.id), run.public())
        except OSError:
            logger.exception("could not persist run %s", run.id)

    def get(self, run_id: str) -> Run | None:
        if not RUN_ID.match(run_id):
            return None
        run = self.runs.get(run_id)
        if run is not None:
            return run
        try:
            data = json.loads(self._path(run_id).read_text(encoding="utf-8"))
            run = Run.from_public(data)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        self.runs[run_id] = run
        return run

    def create(self, issue_url: str, *, kind: str = "run", report_id: str | None = None) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], issue_url=issue_url, kind=kind, report_id=report_id)
        self.runs[run.id] = run
        self.active = run
        return run

    def finish(self, run: Run) -> None:
        run.finished_at = _now()
        self.save(run)
        run.notify()
        if self.active is run:
            self.active = None
        finished = [r for r in self.runs.values() if r.finished]
        for old in sorted(finished, key=lambda r: r.created_at)[:-MAX_RUNS_IN_MEMORY]:
            self.runs.pop(old.id, None)  # still on disk

    # daily counter
    @property
    def _counter_path(self) -> Path:
        return self.dir / "daily.json"

    @staticmethod
    def _today() -> str:
        return _now().date().isoformat()

    def runs_today(self) -> int:
        try:
            data = json.loads(self._counter_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict) or data.get("date") != self._today():
            return 0
        count = data.get("count", 0)
        return count if isinstance(count, int) and count > 0 else 0

    def count_run(self) -> None:
        try:
            self._write_json(
                self._counter_path, {"date": self._today(), "count": self.runs_today() + 1}
            )
        except OSError:
            logger.exception("could not update the daily run counter")


# --- examples -------------------------------------------------------------------------


def default_examples_dir() -> Path:
    override = os.environ.get("FIXEDYET_EXAMPLES_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "examples"


def load_examples(directory: Path) -> list[dict[str, Any]]:
    """Read ``*.json`` Assessments (optional ``*.md`` next to each). Bad files are skipped.

    A file may carry an ``_example`` object with ``label``, ``note`` and ``illustrative``
    (true for hand-written samples that are not the output of a real run), and for a backport a
    ``pr_url`` (the draft pull request that was opened from it). A file holding a BackportResult
    (`fixedyet backport --json`, recognised by its ``status`` and no ``verdict``) becomes an
    example of ``kind`` "backport" with ``verdict`` "backport_<status>".
    """
    examples: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            meta = data.pop("_example", None) if isinstance(data, dict) else None
            is_backport = isinstance(data, dict) and "status" in data and "verdict" not in data
            backport = BackportResult.model_validate(data) if is_backport else None
            assessment = None if is_backport else Assessment.model_validate(data)
        except (OSError, ValueError):
            logger.warning("skipping unreadable example %s", path.name)
            continue
        meta = meta if isinstance(meta, dict) else {}
        pr_url = meta.get("pr_url")
        if not (isinstance(pr_url, str) and pr_url.startswith("https://github.com/")):
            pr_url = None
        markdown = ""
        try:
            markdown = path.with_suffix(".md").read_text(encoding="utf-8")
        except OSError:
            with contextlib.suppress(Exception):
                if backport is not None:
                    markdown = render_backport(backport)
                elif assessment is not None:
                    markdown = render_report(assessment)
        if backport is not None:
            examples.append(
                {
                    "id": path.stem,
                    "kind": "backport",
                    "label": str(meta.get("label") or backport.title),
                    "note": str(meta.get("note") or ""),
                    "illustrative": bool(meta.get("illustrative", False)),
                    "issue_url": backport.issue_url,
                    "title": backport.title,
                    "verdict": f"backport_{backport.status.value}",
                    "confidence": "",
                    "assessment": None,
                    "backport": backport.model_dump(mode="json"),
                    "pr_url": pr_url,
                    "markdown": markdown,
                }
            )
            continue
        assert assessment is not None
        examples.append(
            {
                "id": path.stem,
                "kind": "assessment",
                "label": str(meta.get("label") or assessment.title),
                "note": str(meta.get("note") or ""),
                "illustrative": bool(meta.get("illustrative", False)),
                "issue_url": assessment.issue_url,
                "title": assessment.title,
                "verdict": assessment.verdict.value,
                "confidence": assessment.confidence.value,
                "assessment": assessment.model_dump(mode="json"),
                "backport": None,
                "pr_url": None,
                "markdown": markdown,
            }
        )
    return examples


# --- security headers (pure ASGI so server-sent events are never buffered) ------------


def route_path(scope: MutableMapping[str, Any]) -> str:
    """The request path without the ``root_path`` prefix.

    uvicorn puts the prefix in front of ``scope["path"]`` when it is started with a root path.
    """
    path: str = scope["path"]
    root = scope.get("root_path", "")
    if root and (path == root or path.startswith(root + "/")):
        return path[len(root) :] or "/"
    return path


class SecurityHeaders:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        revalidate = route_path(scope).startswith("/static/")

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in SECURITY_HEADERS if k not in names]
                if revalidate and b"cache-control" not in names:
                    extra.append((b"cache-control", b"no-cache"))  # a deploy must show at once
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --- the app --------------------------------------------------------------------------


class RunRequest(BaseModel):
    issue_url: str


def create_app(settings: Any | None = None, *, examples_dir: Path | None = None) -> FastAPI:
    """Build the app. ``settings`` is resolved lazily (first request) when not given."""
    holder: dict[str, Any] = {"settings": settings, "store": None, "reports": None}

    def get_settings() -> Any:
        if holder["settings"] is None:
            holder["settings"] = load_settings()
        return holder["settings"]

    def get_store() -> RunStore:
        if holder["store"] is None:
            runs_dir = Path(str(getattr(get_settings(), "runs_dir", "runs")))
            holder["store"] = RunStore(runs_dir)
        return holder["store"]

    def get_reports() -> ReportStore:
        if holder["reports"] is None:
            runs_dir = Path(str(getattr(get_settings(), "runs_dir", "runs")))
            holder["reports"] = ReportStore(
                runs_dir,
                lambda: load_examples(examples_path),
                lambda assessment: render_report(assessment),
            )
        return holder["reports"]

    def admin_token() -> str:
        return str(getattr(get_settings(), "admin_token", "") or "").strip()

    def is_admin(request: Request) -> bool:
        """True for ``Authorization: Bearer <the admin token>``. False whenever maintainer mode
        is off, so nobody is an admin by accident."""
        expected = admin_token()
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        if not expected or scheme.lower() != "bearer" or not given.strip():
            return False
        return hmac.compare_digest(given.strip().encode("utf-8"), expected.encode("utf-8"))

    def admin_gate(request: Request) -> Response | None:
        """None for the maintainer; otherwise the answer to send. Off: 404, as if the endpoint did
        not exist. Wrong or missing token: 401 that says nothing else. The token is never logged
        or echoed."""
        if not admin_token():
            return _error(404, "not_found", "Not found.")
        if not is_admin(request):
            return _error(
                401, "unauthorized", "Not authorized.", headers={"WWW-Authenticate": "Bearer"}
            )
        return None

    def daily_limit() -> int:
        return int(getattr(get_settings(), "demo_daily_limit", DEFAULT_DAILY_LIMIT))

    def run_budget() -> float:
        return float(getattr(get_settings(), "run_budget_usd", DEFAULT_RUN_BUDGET_USD))

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        store: RunStore | None = holder["store"]
        if store and store.tasks:
            for task in list(store.tasks):
                task.cancel()
            await asyncio.gather(*store.tasks, return_exceptions=True)

    app = FastAPI(
        title="FixedYet",
        summary="Reproduce before you answer",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(SecurityHeaders)
    examples_path = examples_dir or default_examples_dir()

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error(
            422,
            "invalid_request",
            'Send JSON like {"issue_url": "https://github.com/owner/repo/issues/123"}.',
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error: %s", type(exc).__name__, exc_info=exc)
        return _error(500, "internal", "Something went wrong on the server.")

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        store = get_store()
        limit = daily_limit()
        used = store.runs_today()
        busy = store.active is not None and not store.active.finished
        return {
            "daily_limit": limit,
            "runs_today": used,
            "remaining": max(limit - used, 0),
            "busy": busy,
            "budget_remaining_usd": round(budget_remaining(get_settings()), 4),
        }

    @app.post("/api/runs", status_code=202)
    async def start_run(body: RunRequest, request: Request) -> Response:
        try:
            ref = parse_issue_url(body.issue_url)
        except ValueError as exc:
            return _error(400, "invalid_issue_url", str(exc))
        store = get_store()
        examples_url = f"{request.scope.get('root_path', '')}/api/examples"
        admin = is_admin(request)  # the maintainer's runs skip the daily limit and its counter
        if not admin and store.runs_today() >= daily_limit():
            if daily_limit() <= 0:  # a limit of 0 is "paused", not "used up today"
                message = PAUSED_MESSAGE
            else:
                message = (
                    "Today's live runs are used up. Each run costs real compute, so the demo "
                    "has a daily limit. Read the example reports instead, or run FixedYet "
                    "yourself."
                )
            return _error(429, "daily_limit", message, examples=examples_url)
        if budget_remaining(get_settings()) + 1e-9 < run_budget():  # tolerance: float sums
            return _error(
                429,
                "budget_exhausted",
                "The demo's total spending budget is used up, so it cannot start another live "
                "run. Read the example reports instead, or run FixedYet yourself.",
                examples=examples_url,
            )
        if store.active is not None and not store.active.finished:
            return _error(
                429,
                "busy",
                "Another run is in progress and the demo runs one issue at a time. Try again in "
                "a few minutes, or read the example reports meanwhile.",
                headers={"Retry-After": "120"},
                examples=examples_url,
            )
        run = store.create(ref.url)
        if not admin:
            store.count_run()
        task = asyncio.create_task(_execute(store, run, get_settings()))
        store.tasks.add(task)
        task.add_done_callback(store.tasks.discard)
        return JSONResponse({"id": run.id}, status_code=202)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> Response:
        run = get_store().get(run_id)
        if run is None:
            return _error(404, "not_found", "No such run.")
        return JSONResponse(run.public())

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> Response:
        run = get_store().get(run_id)
        if run is None:
            return _error(404, "not_found", "No such run.")
        try:
            start = int(request.headers.get("last-event-id", "-1")) + 1
        except ValueError:
            start = 0
        return StreamingResponse(
            _event_stream(run, max(start, 0)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/examples")
    async def examples() -> list[dict[str, Any]]:
        return load_examples(examples_path)

    @app.api_route("/reports", methods=["GET", "HEAD"], include_in_schema=False)
    async def reports_page() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "reports.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/reports")
    async def list_reports(request: Request) -> list[dict[str, Any]]:
        """Newest first. The maintainer also sees hidden reports, tagged ``hidden``."""
        reports = get_reports()
        return await asyncio.to_thread(reports.summaries, include_hidden=is_admin(request))

    @app.get("/api/reports/{report_id}")
    async def get_report(report_id: str, request: Request) -> Response:
        admin = is_admin(request)
        reports = get_reports()
        found = await asyncio.to_thread(reports.find, report_id, include_hidden=admin)
        if found is None:
            return _error(404, "not_found", "No such report.")
        return JSONResponse(reports.detail(found, admin=admin))

    @app.get("/api/admin/check")
    async def admin_check(request: Request) -> Response:
        return admin_gate(request) or JSONResponse({"ok": True})

    @app.post("/api/reports/{report_id}/hide")
    async def hide_report(report_id: str, request: Request) -> Response:
        return await _set_hidden(report_id, request, True)

    @app.delete("/api/reports/{report_id}/hide")
    async def unhide_report(report_id: str, request: Request) -> Response:
        return await _set_hidden(report_id, request, False)

    async def _set_hidden(report_id: str, request: Request, hidden: bool) -> Response:
        denied = admin_gate(request)
        if denied is not None:
            return denied
        reports = get_reports()
        found = await asyncio.to_thread(reports.find, report_id, include_hidden=True)
        if found is None and not (valid_report_id(report_id) and report_id in reports.hidden_ids()):
            return _error(404, "not_found", "No such report.")  # a stale hidden id can be unhidden
        reports.set_hidden(report_id, hidden)
        return JSONResponse({"id": report_id, "hidden": hidden})

    @app.post("/api/reports/{report_id}/backport")
    async def start_backport(report_id: str, request: Request) -> Response:
        denied = admin_gate(request)
        if denied is not None:
            return denied
        reports = get_reports()
        found = await asyncio.to_thread(reports.find, report_id, include_hidden=True)
        if found is None:
            return _error(404, "not_found", "No such report.")
        saved = reports.load_backport(report_id)
        if saved and saved.get("status") == "verified":  # already verified: nothing to redo
            return JSONResponse({"report_id": report_id, "backport": public_record(saved)})
        assessment = Assessment.model_validate(found.assessment) if found.assessment else None
        if assessment is None or not backport_ready(assessment):
            return _error(
                422,
                "not_backportable",
                "Only a report that is fixed on main, with a known fixing commit and a "
                "reproduction test, can be backported.",
            )
        try:  # optional {"target_branch": "stable"}: a branch the name rules do not find
            body = await request.json()
        except ValueError:
            body = None
        target_branch = body.get("target_branch") if isinstance(body, dict) else None
        if target_branch is not None:
            target_branch = str(target_branch).strip() or None
        if target_branch is not None and not _TARGET_BRANCH_RE.match(target_branch):
            return _error(400, "invalid_branch", "That is not a branch name.")
        store = get_store()
        if store.active is not None and not store.active.finished:
            return _error(
                409,
                "busy",
                "A run or a backport is in progress and the server does one thing at a time. "
                "Try again when it has finished.",
                headers={"Retry-After": "120"},
            )
        run = store.create(assessment.issue_url, kind="backport", report_id=report_id)
        task = asyncio.create_task(
            _execute_backport(
                store, reports, run, assessment, get_settings(), target_branch=target_branch
            )
        )
        store.tasks.add(task)
        task.add_done_callback(store.tasks.discard)
        return JSONResponse({"id": run.id}, status_code=202)

    @app.post("/api/reports/{report_id}/backport/pr")
    async def open_backport_pull_request(report_id: str, request: Request) -> Response:
        """Step 2: a DRAFT pull request for a verified backport. Body ``{"target": "fork"}``, or
        ``{"target": "upstream", "confirm": "<owner/repo>"}``: the upstream project only when
        ``confirm`` names it, so the maintainer types the repository before anything is posted."""
        denied = admin_gate(request)
        if denied is not None:
            return denied
        reports = get_reports()
        found = await asyncio.to_thread(reports.find, report_id, include_hidden=True)
        if found is None:
            return _error(404, "not_found", "No such report.")
        record = reports.load_backport(report_id)
        result = _verified_result(record)
        if record is None or result is None:
            return _error(
                409, "not_verified",
                "Verify the backport first: only a verified backport gets a pull request.",
            )
        try:
            body = await request.json()
        except ValueError:
            body = None
        target = body.get("target") if isinstance(body, dict) else None
        if target not in ("fork", "upstream"):
            return _error(
                400, "invalid_request",
                'Send JSON like {"target": "fork"} or {"target": "upstream", "confirm": '
                '"owner/repo"}.',
            )
        if target == "upstream":
            confirm = body.get("confirm") if isinstance(body, dict) else None
            if not isinstance(confirm, str) or confirm.strip().lower() != result.repo.lower():
                return _error(
                    400, "confirmation_mismatch",
                    f"Type {result.repo} to confirm a pull request on the upstream project.",
                )
        existing = (record.get("prs") or {}).get(target)
        if isinstance(existing, dict) and existing.get("url"):  # this target has its PR already
            return JSONResponse({
                "report_id": report_id, "target": target, "pr_url": existing["url"],
                "created": False, "backport": public_record(record),
            })
        settings = get_settings()
        try:
            token = await asyncio.to_thread(github_token, settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("backport pr %s: no GitHub token (%s)", report_id, type(exc).__name__)
            return _error(
                503, "no_github_token",
                "The server has no GitHub token: GITHUB_TOKEN is unset and gh is not installed "
                "(or not logged in), so a pull request cannot be opened.",
            )

        def say(message: str) -> None:
            logger.info("backport pr %s: %s", report_id, message)

        try:
            async with asyncio.timeout(PULL_REQUEST_TIMEOUT_S):
                pr = await open_draft_pr(result, token=token, say=say, target=target)
            if not str(getattr(pr, "url", "") or ""):
                raise ValueError("GitHub returned no pull request URL")
        except TimeoutError:
            return _error(
                504, "timeout",
                f"GitHub took longer than {PULL_REQUEST_TIMEOUT_S} seconds. Try again: the fork "
                "and the branch are reused, and nothing is opened twice.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("backport pr %s failed", report_id)
            return _error(502, "pull_request_failed", _public_job_error(exc, token))
        record = _with_pr(record, target, pr)
        await asyncio.to_thread(reports.save_backport, report_id, record)
        return JSONResponse({
            "report_id": report_id, "target": target, "pr_url": record["prs"][target]["url"],
            "created": bool(getattr(pr, "created", True)), "backport": public_record(record),
        })

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def _execute(store: RunStore, run: Run, settings: Any) -> None:
    """Run the pipeline in the background and record everything on the run."""

    async def progress(event: ProgressEvent) -> None:
        run.add_event(event.model_dump(mode="json"))

    try:
        assessment = await asyncio.wait_for(
            run_pipeline(run.issue_url, settings=settings, progress=progress),
            timeout=RUN_TIMEOUT_S,
        )
        run.assessment = assessment.model_dump(mode="json")
        try:
            run.markdown = render_report(assessment)
        except Exception:
            logger.exception("could not render the markdown report for run %s", run.id)
        run.status = "done"
        if not run.events or run.events[-1].get("stage") != "done":
            event = ProgressEvent(stage="done", message=f"Verdict: {assessment.verdict.value}")
            run.add_event(event.model_dump(mode="json"))
    except asyncio.CancelledError:
        run.status = "error"
        run.error = "The server was shut down while this run was in progress."
        raise
    except Exception as exc:
        logger.exception("run %s failed", run.id)
        run.status = "error"
        run.error = _public_error(exc)
        event = ProgressEvent(stage="error", message=run.error)
        run.add_event(event.model_dump(mode="json"))
    finally:
        store.finish(run)


def _public_job_error(exc: BaseException, token: str = "") -> str:
    """A message safe to show the maintainer. ``PullRequestError`` texts are written to be shown
    and scrubbed of the token; this scrubs once more."""
    if isinstance(exc, TimeoutError):
        return f"The backport took longer than {BACKPORT_TIMEOUT_S // 60} minutes and was stopped."
    if type(exc).__name__ == "PullRequestError":
        text = str(exc)[:400]
        return text.replace(token, "***") if token else text
    return "The operation failed. The details are in the server log."


def _verified_result(record: dict[str, Any] | None) -> BackportResult | None:
    """The BackportResult of a saved record whose status is verified, else None."""
    if not record or record.get("status") != "verified":
        return None
    if not isinstance(record.get("result"), dict):
        return None
    try:
        result = BackportResult.model_validate(record["result"])
    except ValueError:
        return None
    return result if result.status.value == "verified" else None


def _with_pr(record: dict[str, Any], target: str, pr: Any) -> dict[str, Any]:
    """``record`` with the pull request of ``target`` added to ``prs``. ``pr_url``, ``pr_target``
    and ``pr_number`` show the most recent one's."""
    prs = dict(record.get("prs") or {})
    prs[target] = {
        **{
            key: getattr(pr, key, None)
            for key in ("number", "base_repo", "base_branch", "head", "fork", "login", "draft",
                        "created")
        },
        "url": str(pr.url),
        "opened_at": _iso(_now()),
    }
    return {
        **record, "prs": prs, "pr_url": prs[target]["url"], "pr_target": target,
        "pr_number": prs[target]["number"],
    }


async def _execute_backport(
    store: RunStore, reports: ReportStore, run: Run, assessment: Assessment, settings: Any,
    *, target_branch: str | None = None,
) -> None:
    """Step 1: verify the backport of the fix of ``assessment`` in the sandbox. No pull request.

    The result is saved with ``pr_url`` null, together with how active the target branch is, so
    that the maintainer can read the patch and the warning before deciding on step 2.
    """

    async def progress(event: ProgressEvent) -> None:
        run.add_event(event.model_dump(mode="json"))

    try:
        async with asyncio.timeout(BACKPORT_TIMEOUT_S):
            kwargs = {"target_branch": target_branch} if target_branch else {}
            result = await run_backport(
                assessment, settings=settings, progress=progress, **kwargs
            )
            activity: dict[str, Any] | None = None
            if result.repo and result.target_branch:
                run.add_event(ProgressEvent(
                    stage="backport",
                    message=f"Checking how active {result.target_branch} is on {result.repo}",
                ).model_dump(mode="json"))
                try:
                    activity = await fetch_target_activity(
                        result.repo, result.target_branch, settings=settings
                    )
                except Exception:  # noqa: BLE001 - never fail the job over a nice-to-have
                    logger.warning("backport job %s: target activity failed", run.id)
            record = {
                "report_id": run.report_id,
                "issue_url": assessment.issue_url,
                "status": result.status.value,
                "pr_url": None,
                "pr_target": None,
                "pr_number": None,
                "prs": {},
                "target_activity": activity,
                "result": result.model_dump(mode="json"),
                "saved_at": _iso(_now()),
            }
            await asyncio.to_thread(reports.save_backport, run.report_id or "", record)
        run.result = {"status": record["status"], "pr_url": None}
        with contextlib.suppress(Exception):
            run.markdown = render_backport(result)
        run.status = "done"
        message = (
            "Verified. Read the patch, then open the draft pull request."
            if result.status.value == "verified"
            else f"Backport {result.status.value}: {result.reason}"
        )
        run.add_event(ProgressEvent(stage="done", message=message).model_dump(mode="json"))
    except asyncio.CancelledError:
        run.status = "error"
        run.error = "The server was shut down while this backport was in progress."
        raise
    except Exception as exc:
        logger.exception("backport job %s failed", run.id)
        run.status = "error"
        run.error = _public_job_error(exc)
        run.add_event(ProgressEvent(stage="error", message=run.error).model_dump(mode="json"))
    finally:
        store.finish(run)


def _frame(event: str, data: Any, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(run: Run, start: int) -> AsyncIterator[str]:
    index = start
    while True:
        changed = run._changed  # grab before looking, so nothing can slip between look and wait
        while index < len(run.events):
            yield _frame("progress", run.events[index], index)
            index += 1
        if run.finished and index >= len(run.events):
            yield _frame("done", {"status": run.status, "error": run.error})
            return
        try:
            await asyncio.wait_for(changed.wait(), timeout=KEEPALIVE_S)
        except TimeoutError:
            yield ": keepalive\n\n"


app = create_app()
