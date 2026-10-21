"""FastAPI demo app: paste an issue URL, watch the progress, read the report.

Endpoints
    GET  /                      the single page (static/index.html)
    POST /api/runs              start a live run, returns {"id"}
    GET  /api/runs/{id}         status, progress events, assessment and markdown
    GET  /api/runs/{id}/events  progress as server-sent events
    GET  /api/examples          saved example assessments from the examples/ directory
    GET  /api/status            live runs left today, and the model budget left in USD
    GET  /healthz               liveness

Live runs cost real money, so the app allows one run at a time and a global daily limit
(``settings.demo_daily_limit``, counted in a JSON file under ``settings.runs_dir``). Beyond that,
the spend ledger caps the total model spend of all runs ever (``settings.total_budget_usd``): a
run is refused with 429 when less than ``settings.run_budget_usd`` is left under that cap.
Finished runs are written to ``<runs_dir>/web/<id>.json`` and survive restarts.

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
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, MutableMapping
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

logger = logging.getLogger("fixedyet.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
RUN_TIMEOUT_S = 25 * 60
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


def load_settings() -> Any:
    from fixedyet.config import get_settings

    return get_settings()


async def run_pipeline(issue_url: str, *, settings: Any, progress: Any) -> Assessment:
    from fixedyet.pipeline import reproduce

    return await reproduce(issue_url, settings=settings, progress=progress)


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

    def create(self, issue_url: str) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], issue_url=issue_url)
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
    (true for hand-written samples that are not the output of a real run). A file holding a
    BackportResult (`fixedyet backport --json`, recognised by its ``status`` and no ``verdict``)
    becomes an example of ``kind`` "backport" with ``verdict`` "backport_<status>".
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
    holder: dict[str, Any] = {"settings": settings, "store": None}

    def get_settings() -> Any:
        if holder["settings"] is None:
            holder["settings"] = load_settings()
        return holder["settings"]

    def get_store() -> RunStore:
        if holder["store"] is None:
            runs_dir = Path(str(getattr(get_settings(), "runs_dir", "runs")))
            holder["store"] = RunStore(runs_dir)
        return holder["store"]

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
        if store.runs_today() >= daily_limit():
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
