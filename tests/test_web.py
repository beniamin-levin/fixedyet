"""Tests for the web demo: validation, limits, run lifecycle, server-sent events, examples."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from fixedyet import ledger
from fixedyet.models import (
    Assessment,
    BackportResult,
    BackportStatus,
    Confidence,
    Outcome,
    ProgressEvent,
    Reproduction,
    SideRun,
    Verdict,
)
from fixedyet.web import app as web
from fixedyet.web import parse_issue_url

ISSUE = "https://github.com/psf/requests/issues/6000"
SECRET = "super-secret-key-123"


def make_assessment(verdict: Verdict = Verdict.FIXED_ON_MAIN) -> Assessment:
    return Assessment(
        issue_url=ISSUE,
        title="Fake issue",
        verdict=verdict,
        confidence=Confidence.HIGH,
        reproduction=Reproduction(
            repro_test_path="tests/test_issue_6000.py",
            repro_test_code="def test_it():\n    assert 1 == 2\n",
            reported_side=SideRun(ref="v2.31.0", outcomes=[Outcome.FAIL, Outcome.FAIL]),
            fix_side=SideRun(ref="main", outcomes=[Outcome.PASS, Outcome.PASS]),
        ),
    )


class Fake:
    """A stand-in for the pipeline. Counts calls and can be held open with a gate."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None

    async def __call__(
        self, issue_url: str, *, settings: Any, progress: Callable[[ProgressEvent], Awaitable[None]]
    ) -> Assessment:
        self.calls.append(issue_url)
        await progress(ProgressEvent(stage="intake", message="Read the issue"))
        await progress(ProgressEvent(stage="agent", message="Writing the test"))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        await progress(ProgressEvent(stage="verdict", message="fixed_on_main"))
        return make_assessment()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    f = Fake()
    monkeypatch.setattr(web, "run_pipeline", f)
    monkeypatch.setattr(web, "render_report", lambda a: f"# {a.title}\n\n{a.verdict.value}")
    monkeypatch.setattr(
        web, "render_backport", lambda b: f"# rendered backport: {b.status.value}"
    )
    return f


def make_settings(
    tmp_path: Path, limit: int = 5, *, total_budget_usd: float = 20.0, run_budget_usd: float = 0.60
) -> SimpleNamespace:
    return SimpleNamespace(
        demo_daily_limit=limit,
        runs_dir=tmp_path / "runs",
        total_budget_usd=total_budget_usd,
        run_budget_usd=run_budget_usd,
    )


def make_client(settings: SimpleNamespace, examples_dir: Path | None = None) -> httpx.AsyncClient:
    app = web.create_app(settings, examples_dir=examples_dir)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def settings(tmp_path: Path) -> SimpleNamespace:
    return make_settings(tmp_path)


@pytest.fixture
async def client(settings: SimpleNamespace, tmp_path: Path):
    async with make_client(settings, examples_dir=tmp_path / "no-examples") as c:
        yield c


async def wait_finished(client: httpx.AsyncClient, run_id: str) -> dict:
    async with asyncio.timeout(5):
        while True:
            data = (await client.get(f"/api/runs/{run_id}")).json()
            if data["status"] != "running":
                return data
            await asyncio.sleep(0.01)


async def start(client: httpx.AsyncClient, url: str = ISSUE) -> httpx.Response:
    return await client.post("/api/runs", json={"issue_url": url})


# --- basics -----------------------------------------------------------------------------


async def test_healthz(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


async def test_head_requests_work_for_monitors(client: httpx.AsyncClient) -> None:
    assert (await client.head("/healthz")).status_code == 200
    assert (await client.head("/")).status_code == 200


async def test_index_and_static_files(client: httpx.AsyncClient) -> None:
    res = await client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "Reproduce before you answer" in res.text
    assert "https://github.com/beniamin-levin/fixedyet" in res.text
    assert "Nebius Sandboxes" in res.text
    assert (await client.get("/static/app.js")).status_code == 200
    assert (await client.get("/static/style.css")).status_code == 200


async def test_security_headers(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in res.headers["content-security-policy"]


PAGE_FILES = ("index.html", "reports.html", "common.js", "app.js", "reports.js", "style.css")
ABSOLUTE_LOCAL_URL = re.compile(r"""(?:["'`(]|=)/(?:api|static|healthz)\b""")
ROOT_RELATIVE_ATTRIBUTE = re.compile(r"""\b(?:href|src|action)\s*=\s*["']/(?!/)""")


def test_the_page_has_no_absolute_api_or_static_references() -> None:
    """The demo is served at /fixedyet/ behind a proxy, so every URL must be relative."""
    for name in PAGE_FILES:
        text = (web.STATIC_DIR / name).read_text(encoding="utf-8")
        assert not ABSOLUTE_LOCAL_URL.search(text), f"{name} has a root-relative /api or /static"
        assert not ROOT_RELATIVE_ATTRIBUTE.search(text), f"{name} has a root-relative URL"
        assert "url(/" not in text
    html = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'href="static/style.css"' in html and 'src="static/app.js"' in html
    assert 'src="static/common.js"' in html
    for call in ("api/status", "api/examples", "api/runs"):
        assert call in js
    assert re.findall(r"""(?:fetch|EventSource)\(\s*['`]/""", js) == []


def test_the_page_shows_the_new_name_and_never_the_old_one() -> None:
    for name in PAGE_FILES:
        text = (web.STATIC_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"issue[-_ ]?repro", text, re.IGNORECASE)
    html = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<title>FixedYet: reproduce before you answer</title>" in html
    assert "FixedYet" in html.split('class="brand"', 1)[1].split("</a>", 1)[0]


def test_the_scripts_never_put_text_into_the_page_as_html() -> None:
    """Report text keeps its brackets (a failure line can quote "<p>"), so the escaping is the
    page's: every string goes in through textContent or a text node."""
    for name in ("common.js", "app.js", "reports.js"):
        script = (web.STATIC_DIR / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in script, f"{name} uses {sink}"


def test_page_has_no_dark_background() -> None:
    css = (web.STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "color-scheme: light" in css
    assert "prefers-color-scheme: dark" not in css


# --- served under a prefix --------------------------------------------------------------


def like_uvicorn(app: Any, prefix: str) -> Any:
    """What `uvicorn --root-path /fixedyet` hands the app behind a proxy that strips the prefix:
    the request path as the app would see it, with the root path put in front (uvicorn 0.26+)."""

    async def wrapper(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, "root_path": prefix, "path": prefix + scope["path"]}
        await app(scope, receive, send)

    return wrapper


@pytest.fixture
async def prefixed(tmp_path: Path):
    app = web.create_app(make_settings(tmp_path), examples_dir=tmp_path / "no-examples")
    transport = httpx.ASGITransport(app=like_uvicorn(app, "/fixedyet"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_the_app_works_under_a_root_path(prefixed: httpx.AsyncClient) -> None:
    index = await prefixed.get("/")
    assert index.status_code == 200 and "FixedYet" in index.text
    assert (await prefixed.get("/healthz")).json() == {"status": "ok"}
    assert (await prefixed.get("/api/status")).json()["remaining"] == 5
    assert (await prefixed.get("/api/examples")).json() == []
    assert (await prefixed.get("/static/app.js")).status_code == 200
    assert (await prefixed.get("/nothing-here")).status_code == 404


async def test_static_files_are_revalidated_under_a_root_path_too(
    prefixed: httpx.AsyncClient,
) -> None:
    res = await prefixed.get("/static/style.css")
    assert res.headers["cache-control"] == "no-cache"
    assert res.headers["x-content-type-options"] == "nosniff"


async def test_redirects_carry_the_root_path(prefixed: httpx.AsyncClient) -> None:
    res = await prefixed.get("/api/examples/")  # the route has no trailing slash
    assert res.status_code in (307, 308)
    assert res.headers["location"] == "http://test/fixedyet/api/examples"


async def test_error_bodies_point_at_the_prefixed_examples_url(
    tmp_path: Path,
) -> None:
    app = web.create_app(make_settings(tmp_path, limit=0), examples_dir=tmp_path / "x")
    transport = httpx.ASGITransport(app=like_uvicorn(app, "/fixedyet"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        res = await start(c)
    assert res.status_code == 429
    assert res.json()["examples"] == "/fixedyet/api/examples"


def test_route_path_strips_only_a_whole_leading_segment() -> None:
    assert web.route_path({"path": "/fixedyet/api/x", "root_path": "/fixedyet"}) == "/api/x"
    assert web.route_path({"path": "/fixedyet", "root_path": "/fixedyet"}) == "/"
    assert web.route_path({"path": "/fixedyetx/api", "root_path": "/fixedyet"}) == "/fixedyetx/api"
    assert web.route_path({"path": "/api/x", "root_path": ""}) == "/api/x"
    assert web.route_path({"path": "/api/x"}) == "/api/x"


# --- validation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "ftp://github.com/psf/requests/issues/1",
        "https://gitlab.com/psf/requests/issues/1",
        "github.com.evil.example/psf/requests/issues/1",
        "https://user@github.com/psf/requests/issues/1",
        "https://github.com.evil.example/psf/requests/issues/1",
        "https://github.com/psf/requests/pull/1",
        "https://github.com/psf/requests/issues/abc",
        "https://github.com/psf/requests/issues/0",
        "https://github.com/psf/requests/issues/",
        "https://github.com/psf/issues/1",
        "https://github.com/psf/requests/issues/1/extra",
        "javascript:alert(1)",
        "https://github.com/../requests/issues/1",
        "https://github.com/psf/../issues/1",
        "https://github.com/" + "a" * 400 + "/b/issues/1",
    ],
)
async def test_invalid_urls_are_rejected(client: httpx.AsyncClient, fake: Fake, url: str) -> None:
    res = await start(client, url)
    assert res.status_code == 400
    body = res.json()
    assert body["error"] == "invalid_issue_url"
    assert "github.com/owner/repo/issues/123" in body["message"]
    assert fake.calls == []


async def test_missing_body_field_is_a_friendly_422(client: httpx.AsyncClient, fake: Fake) -> None:
    res = await client.post("/api/runs", json={"url": ISSUE})
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_request"
    assert fake.calls == []


def test_parse_issue_url_normalises() -> None:
    ref = parse_issue_url(" https://www.github.com/psf/requests/issues/6000/#issuecomment-5 ")
    assert (ref.owner, ref.repo, ref.number) == ("psf", "requests", 6000)
    assert ref.url == ISSUE
    assert parse_issue_url(f"{ISSUE}?notification_referrer_id=x").url == ISSUE
    assert parse_issue_url("https://github.com/a-b/c.d_e/issues/12").number == 12


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/psf/requests/issues/6000",
        "github.com/psf/requests/issues/6000",
        "www.github.com/psf/requests/issues/6000/",
        "HTTPS://WWW.GitHub.Com/psf/requests/issues/6000",
        "Http://GITHUB.COM/psf/requests/issues/6000?a=1#issuecomment-99",
    ],
)
def test_parse_issue_url_takes_what_people_paste(url: str) -> None:
    assert parse_issue_url(url).url == ISSUE


def test_parse_issue_url_keeps_owner_and_repository_as_typed() -> None:
    ref = parse_issue_url("github.com/PyCQA/isort/issues/2723")
    assert ref.url == "https://github.com/PyCQA/isort/issues/2723"


def test_the_page_accepts_the_same_url_prefixes_as_the_server() -> None:
    """The page checks a URL before it sends it, so it must not turn away what the server takes."""
    from fixedyet import web as web_package

    script = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    prefix = web_package._GITHUB_PREFIX
    as_js = prefix.pattern.replace("/", "\\/")  # the same pattern as a JavaScript literal
    assert prefix.flags & re.IGNORECASE
    assert f"/{as_js}/i" in script
    assert "startRun(url)" in script  # the plain https form is what is sent


async def test_a_pasted_url_runs_as_its_plain_https_form(
    client: httpx.AsyncClient, fake: Fake
) -> None:
    res = await start(client, "github.com/psf/requests/issues/6000/#issuecomment-1")
    assert res.status_code == 202
    assert (await wait_finished(client, res.json()["id"]))["issue_url"] == ISSUE
    assert fake.calls == [ISSUE]


async def test_a_pull_request_url_is_named_as_one(client: httpx.AsyncClient, fake: Fake) -> None:
    res = await start(client, "https://github.com/psf/requests/pull/6001/files")
    assert res.status_code == 400 and res.json()["error"] == "invalid_issue_url"
    message = res.json()["message"]
    assert "pull request URL" in message and "github.com/owner/repo/issues/123" in message
    assert fake.calls == []


# --- run lifecycle ----------------------------------------------------------------------


async def test_run_lifecycle_and_persistence(
    client: httpx.AsyncClient, fake: Fake, settings: SimpleNamespace
) -> None:
    res = await start(client, f"{ISSUE}#issuecomment-1")
    assert res.status_code == 202
    run_id = res.json()["id"]
    assert len(run_id) == 12

    data = await wait_finished(client, run_id)
    assert data["status"] == "done"
    assert data["issue_url"] == ISSUE
    assert fake.calls == [ISSUE]
    stages = [e["stage"] for e in data["events"]]
    assert stages == ["intake", "agent", "verdict", "done"]
    assert data["assessment"]["verdict"] == "fixed_on_main"
    assert data["assessment"]["reproduction"]["reported_side"]["outcomes"] == ["fail", "fail"]
    assert data["markdown"].startswith("# Fake issue")
    assert data["error"] is None
    assert data["finished_at"] is not None

    saved = settings.runs_dir / "web" / f"{run_id}.json"
    assert saved.exists()
    assert json.loads(saved.read_text(encoding="utf-8"))["status"] == "done"

    # A second app over the same runs_dir is a restarted server: the run is still there.
    async with make_client(settings) as restarted:
        again = (await restarted.get(f"/api/runs/{run_id}")).json()
    assert again["status"] == "done"
    assert again["assessment"]["title"] == "Fake issue"
    assert [e["stage"] for e in again["events"]] == stages


async def test_unknown_and_malformed_run_ids_are_404(client: httpx.AsyncClient) -> None:
    for bad in ["000000000000", "..%2F..%2Fetc%2Fpasswd", "abc", "A" * 12, "g" * 12]:
        assert (await client.get(f"/api/runs/{bad}")).status_code == 404
        assert (await client.get(f"/api/runs/{bad}/events")).status_code == 404


async def test_interrupted_run_on_disk_is_reported_as_failed(
    client: httpx.AsyncClient, settings: SimpleNamespace
) -> None:
    folder = settings.runs_dir / "web"
    folder.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    (folder / "aaaaaaaaaaaa.json").write_text(
        json.dumps(
            {
                "id": "aaaaaaaaaaaa", "issue_url": ISSUE, "status": "running",
                "created_at": now, "finished_at": None, "events": [],
                "assessment": None, "markdown": None, "error": None,
            }
        ),
        encoding="utf-8",
    )
    data = (await client.get("/api/runs/aaaaaaaaaaaa")).json()
    assert data["status"] == "error"
    assert "restarted" in data["error"]


async def test_failure_is_friendly_and_leaks_nothing(
    client: httpx.AsyncClient, fake: Fake, caplog: pytest.LogCaptureFixture
) -> None:
    fake.error = RuntimeError(f"boom with {SECRET} in the message")
    run_id = (await start(client)).json()["id"]
    data = await wait_finished(client, run_id)

    assert data["status"] == "error"
    assert data["assessment"] is None
    text = json.dumps(data)
    assert SECRET not in text
    assert "Traceback" not in text
    assert "RuntimeError" not in text
    assert "failed before it reached a verdict" in data["error"]
    assert data["events"][-1]["stage"] == "error"
    assert f"run {run_id} failed" in caplog.text  # the detail went to the server log

    # The failed run frees the single slot.
    assert (await start(client)).status_code == 202


async def test_budget_exceeded_gets_its_own_message(client: httpx.AsyncClient, fake: Fake) -> None:
    class BudgetExceeded(Exception):
        pass

    fake.error = BudgetExceeded("spent $0.61")
    data = await wait_finished(client, (await start(client)).json()["id"])
    assert "cost budget" in data["error"]
    assert "$0.61" not in json.dumps(data)


async def test_timeout_message(
    client: httpx.AsyncClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web, "RUN_TIMEOUT_S", 0.05)
    fake.gate = asyncio.Event()  # never opened
    data = await wait_finished(client, (await start(client)).json()["id"])
    assert data["status"] == "error"
    assert "longer than" in data["error"]


async def test_markdown_failure_keeps_the_verdict(
    client: httpx.AsyncClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: Assessment) -> str:
        raise ValueError("renderer bug")

    monkeypatch.setattr(web, "render_report", broken)
    data = await wait_finished(client, (await start(client)).json()["id"])
    assert data["status"] == "done"
    assert data["markdown"] is None
    assert data["assessment"]["verdict"] == "fixed_on_main"


# --- limits -----------------------------------------------------------------------------


async def test_only_one_run_at_a_time(client: httpx.AsyncClient, fake: Fake) -> None:
    fake.gate = asyncio.Event()
    first = await start(client)
    assert first.status_code == 202
    await asyncio.sleep(0.02)

    status = (await client.get("/api/status")).json()
    assert status["busy"] is True

    second = await start(client, "https://github.com/pallets/flask/issues/1")
    assert second.status_code == 429
    body = second.json()
    assert body["error"] == "busy"
    assert body["examples"] == "/api/examples"
    assert "example" in body["message"].lower()
    assert "retry-after" in second.headers
    assert len(fake.calls) == 1

    fake.gate.set()
    await wait_finished(client, first.json()["id"])
    third = await start(client, "https://github.com/pallets/flask/issues/1")
    assert third.status_code == 202


async def test_daily_limit(tmp_path: Path, fake: Fake) -> None:
    settings = make_settings(tmp_path, limit=2)
    async with make_client(settings) as client:
        for _ in range(2):
            res = await start(client)
            assert res.status_code == 202
            await wait_finished(client, res.json()["id"])

        res = await start(client)
        assert res.status_code == 429
        body = res.json()
        assert body["error"] == "daily_limit"
        assert body["examples"] == "/api/examples"
        assert "example" in body["message"].lower()
        assert len(fake.calls) == 2

        status = (await client.get("/api/status")).json()
        assert status == {
            "daily_limit": 2,
            "runs_today": 2,
            "remaining": 0,
            "busy": False,
            "budget_remaining_usd": 20.0,
            "judge_access": False,
        }

    # The count lives in a file, so a restart does not reset it.
    async with make_client(settings) as restarted:
        assert (await start(restarted)).status_code == 429


async def test_daily_limit_resets_on_a_new_day(tmp_path: Path, fake: Fake) -> None:
    settings = make_settings(tmp_path, limit=1)
    folder = settings.runs_dir / "web"
    folder.mkdir(parents=True)
    (folder / "daily.json").write_text(
        json.dumps({"date": "2020-01-01", "count": 99}), encoding="utf-8"
    )
    async with make_client(settings) as client:
        res = await start(client)
        assert res.status_code == 202
        await wait_finished(client, res.json()["id"])
    counter = json.loads((folder / "daily.json").read_text(encoding="utf-8"))
    assert counter["count"] == 1
    assert counter["date"] == datetime.now(UTC).date().isoformat()


async def test_the_maintainer_skips_the_daily_limit_and_its_counter(
    tmp_path: Path, fake: Fake
) -> None:
    settings = make_settings(tmp_path, limit=0)  # paused for everyone else
    settings.admin_token = "maintainer-key-0123456789abcdef"
    async with make_client(settings) as client:
        assert (await start(client)).status_code == 429
        wrong = await client.post(
            "/api/runs", json={"issue_url": ISSUE}, headers={"Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 429
        res = await client.post(
            "/api/runs", json={"issue_url": ISSUE},
            headers={"Authorization": f"Bearer {settings.admin_token}"},
        )
        assert res.status_code == 202
        await wait_finished(client, res.json()["id"])
        assert (await client.get("/api/status")).json()["runs_today"] == 0
    assert len(fake.calls) == 1


async def test_zero_limit_blocks_live_runs(tmp_path: Path, fake: Fake) -> None:
    async with make_client(make_settings(tmp_path, limit=0)) as client:
        assert (await start(client)).status_code == 429
    assert fake.calls == []


async def test_zero_limit_says_runs_are_paused_not_used_up(tmp_path: Path, fake: Fake) -> None:
    async with make_client(make_settings(tmp_path, limit=0)) as client:
        res = await start(client)
    body = res.json()
    assert res.status_code == 429 and body["error"] == "daily_limit"
    message = body["message"].lower()
    assert "paused" in message and "no sandbox backend" in message
    assert "example reports" in message
    assert "used up" not in message and "today" not in message
    assert body["examples"] == "/api/examples"


async def test_a_real_limit_still_says_used_up_today(tmp_path: Path, fake: Fake) -> None:
    async with make_client(make_settings(tmp_path, limit=1)) as client:
        first = await start(client)
        assert first.status_code == 202
        await wait_finished(client, first.json()["id"])
        res = await start(client)
    message = res.json()["message"]
    assert res.status_code == 429 and "used up" in message and "today" in message.lower()
    assert "paused" not in message


def test_the_page_hint_for_a_zero_limit_also_says_paused() -> None:
    script = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "Live runs are paused while the demo has no sandbox backend" in script
    assert "Today\\'s live runs are used up" in script


async def test_rejected_runs_do_not_use_the_quota(tmp_path: Path, fake: Fake) -> None:
    async with make_client(make_settings(tmp_path, limit=1)) as client:
        assert (await start(client, "nonsense")).status_code == 400
        assert (await client.get("/api/status")).json()["remaining"] == 1


# --- total spend cap --------------------------------------------------------------------


async def test_status_reports_the_budget_left_under_the_total_cap(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, total_budget_usd=5.0)
    async with make_client(settings) as client:
        assert (await client.get("/api/status")).json()["budget_remaining_usd"] == 5.0
        ledger.record(settings, 1.25)
        assert (await client.get("/api/status")).json()["budget_remaining_usd"] == 3.75


async def test_run_is_refused_with_429_when_less_than_one_run_budget_is_left(
    tmp_path: Path, fake: Fake
) -> None:
    settings = make_settings(tmp_path, total_budget_usd=10.0, run_budget_usd=0.60)
    ledger.record(settings, 9.50)  # $0.50 left, a run may cost $0.60
    async with make_client(settings) as client:
        res = await start(client)
        assert res.status_code == 429
        body = res.json()
        assert body["error"] == "budget_exhausted"
        assert body["examples"] == "/api/examples"
        assert "budget" in body["message"].lower() and "example" in body["message"].lower()
        assert "$" not in body["message"] and "Traceback" not in res.text
        assert (await client.get("/api/status")).json()["budget_remaining_usd"] == 0.5
    assert fake.calls == []  # the pipeline was never started
    assert not (settings.runs_dir / "web" / "daily.json").exists()  # and no quota was used


async def test_run_starts_when_exactly_one_run_budget_is_left(tmp_path: Path, fake: Fake) -> None:
    settings = make_settings(tmp_path, total_budget_usd=10.0, run_budget_usd=0.60)
    ledger.record(settings, 9.40)
    async with make_client(settings) as client:
        res = await start(client)
        assert res.status_code == 202
        await wait_finished(client, res.json()["id"])
    assert len(fake.calls) == 1


async def test_an_unreadable_ledger_blocks_runs(tmp_path: Path, fake: Fake) -> None:
    settings = make_settings(tmp_path)
    settings.runs_dir.mkdir(parents=True)
    (settings.runs_dir / ledger.LEDGER_NAME).write_text("{not json", encoding="utf-8")
    async with make_client(settings) as client:
        res = await start(client)
        assert res.status_code == 429 and res.json()["error"] == "budget_exhausted"
        assert (await client.get("/api/status")).json()["budget_remaining_usd"] == 0.0
    assert fake.calls == []


async def test_total_budget_exceeded_gets_its_own_message(
    client: httpx.AsyncClient, fake: Fake
) -> None:
    class TotalBudgetExceeded(Exception):
        pass

    fake.error = TotalBudgetExceeded("total budget reached: $19.99 of $20.00")
    data = await wait_finished(client, (await start(client)).json()["id"])
    assert "total spending budget" in data["error"]
    assert "$19.99" not in json.dumps(data)


# --- server-sent events -----------------------------------------------------------------


def parse_sse(text: str) -> list[tuple[str, dict, str | None]]:
    frames = []
    for block in text.strip().split("\n\n"):
        event, data, event_id = "message", None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif line.startswith("id: "):
                event_id = line[4:]
        if data is not None:
            frames.append((event, data, event_id))
    return frames


async def test_sse_streams_progress_then_done(client: httpx.AsyncClient, fake: Fake) -> None:
    run_id = (await start(client)).json()["id"]
    await wait_finished(client, run_id)

    res = await client.get(f"/api/runs/{run_id}/events")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["cache-control"].startswith("no-cache")
    assert res.headers["x-accel-buffering"] == "no"

    frames = parse_sse(res.text)
    kinds = [kind for kind, _, _ in frames]
    assert kinds == ["progress", "progress", "progress", "progress", "done"]
    assert [d["stage"] for _, d, _ in frames[:-1]] == ["intake", "agent", "verdict", "done"]
    assert [i for _, _, i in frames[:-1]] == ["0", "1", "2", "3"]
    assert frames[-1][1] == {"status": "done", "error": None}


async def test_sse_resumes_after_last_event_id(client: httpx.AsyncClient, fake: Fake) -> None:
    run_id = (await start(client)).json()["id"]
    await wait_finished(client, run_id)
    res = await client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "1"})
    stages = [d["stage"] for kind, d, _ in parse_sse(res.text) if kind == "progress"]
    assert stages == ["verdict", "done"]


async def test_sse_waits_for_a_running_run(client: httpx.AsyncClient, fake: Fake) -> None:
    fake.gate = asyncio.Event()
    run_id = (await start(client)).json()["id"]
    await asyncio.sleep(0.02)

    async def read() -> str:
        return (await client.get(f"/api/runs/{run_id}/events")).text

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.05)
    assert not reader.done()  # still streaming: the run is not finished
    fake.gate.set()
    text = await asyncio.wait_for(reader, 5)
    frames = parse_sse(text)
    assert frames[-1][0] == "done"
    assert [d["stage"] for k, d, _ in frames if k == "progress"][-1] == "done"


async def test_sse_for_a_failed_run_reports_the_error(
    client: httpx.AsyncClient, fake: Fake
) -> None:
    fake.error = RuntimeError(SECRET)
    run_id = (await start(client)).json()["id"]
    await wait_finished(client, run_id)
    text = (await client.get(f"/api/runs/{run_id}/events")).text
    assert SECRET not in text
    kind, data, _ = parse_sse(text)[-1]
    assert kind == "done"
    assert data["status"] == "error"
    assert data["error"]


# --- examples ---------------------------------------------------------------------------


async def test_examples_endpoint_lists_valid_examples_only(
    tmp_path: Path, settings: SimpleNamespace
) -> None:
    folder = tmp_path / "examples"
    folder.mkdir()
    good = make_assessment(Verdict.OPEN_BUG).model_dump(mode="json")
    good["_example"] = {"label": "Open bug", "illustrative": True, "note": "made up"}
    (folder / "a-good.json").write_text(json.dumps(good), encoding="utf-8")
    (folder / "a-good.md").write_text("# Saved markdown\n", encoding="utf-8")
    plain = make_assessment(Verdict.NOT_REPRODUCED).model_dump(mode="json")
    (folder / "b-plain.json").write_text(json.dumps(plain), encoding="utf-8")
    (folder / "c-broken.json").write_text("{not json", encoding="utf-8")
    (folder / "d-wrong-schema.json").write_text(json.dumps({"verdict": "nope"}), encoding="utf-8")
    (folder / "readme.txt").write_text("ignored", encoding="utf-8")

    async with make_client(settings, examples_dir=folder) as client:
        res = await client.get("/api/examples")
    assert res.status_code == 200
    items = res.json()
    assert [i["id"] for i in items] == ["a-good", "b-plain"]
    first = items[0]
    assert first["verdict"] == "open_bug"
    assert first["label"] == "Open bug"
    assert first["illustrative"] is True
    assert first["note"] == "made up"
    assert first["markdown"] == "# Saved markdown\n"
    assert first["assessment"]["title"] == "Fake issue"
    assert "_example" not in first["assessment"]
    assert items[1]["illustrative"] is False


async def test_examples_without_markdown_fall_back_to_the_renderer(
    tmp_path: Path, settings: SimpleNamespace, fake: Fake
) -> None:
    folder = tmp_path / "examples"
    folder.mkdir()
    (folder / "x.json").write_text(make_assessment().model_dump_json(), encoding="utf-8")
    async with make_client(settings, examples_dir=folder) as client:
        items = (await client.get("/api/examples")).json()
    assert items[0]["markdown"] == "# Fake issue\n\nfixed_on_main"


async def test_missing_examples_directory_is_an_empty_list(
    client: httpx.AsyncClient,
) -> None:
    res = await client.get("/api/examples")
    assert res.status_code == 200
    assert res.json() == []


def test_bundled_examples_are_real_runs_on_real_issues() -> None:
    items = web.load_examples(web.default_examples_dir())
    assert items, "the demo page needs at least one saved example"
    for item in items:
        match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)", item["issue_url"])
        assert match, item["issue_url"]  # a real GitHub issue, not an example.com stand-in
        if item["kind"] == "backport":
            assert item["id"] == "real-backport-{}-{}-{}".format(*match.groups()).lower()
            assert item["verdict"].removeprefix("backport_") in {s.value for s in BackportStatus}
            assert item["backport"]["status"] and item["assessment"] is None
        else:
            assert item["id"] == "real-{}-{}-{}".format(*match.groups()).lower()
            assert item["verdict"] in {v.value for v in Verdict}
        assert item["markdown"].startswith("# ")


def make_backport(status: BackportStatus = BackportStatus.VERIFIED) -> BackportResult:
    return BackportResult(
        status=status, reason="the test fails before and passes after",
        issue_url="https://github.com/pallets/flask/issues/5000", title="A backported fix",
        repo="pallets/flask", reported_version="3.0.3", target_branch="3.0.x",
        target_branch_head="b" * 40, fixing_commit="d" * 40,
        repro_before=SideRun(ref="3.0.x (before)", outcomes=[Outcome.FAIL, Outcome.FAIL]),
        repro_after=SideRun(ref="3.0.x (after)", outcomes=[Outcome.PASS, Outcome.PASS]),
    )


async def test_a_backport_result_in_the_examples_folder_is_listed(
    tmp_path: Path, settings: SimpleNamespace, fake: Fake
) -> None:
    folder = tmp_path / "examples"
    folder.mkdir()
    (folder / "a-fix.json").write_text(make_assessment().model_dump_json(), encoding="utf-8")
    data = make_backport().model_dump(mode="json")
    data["_example"] = {"label": "Verified backport (real run)", "note": "real"}
    (folder / "real-backport-pallets-flask-5000.json").write_text(json.dumps(data))
    (folder / "real-backport-pallets-flask-5000.md").write_text("# Verified backport: saved\n")
    conflict = make_backport(BackportStatus.CONFLICTS).model_dump_json()
    (folder / "z-conflict.json").write_text(conflict, encoding="utf-8")

    async with make_client(settings, examples_dir=folder) as client:
        items = (await client.get("/api/examples")).json()
    by_id = {i["id"]: i for i in items}
    assert list(by_id) == ["a-fix", "real-backport-pallets-flask-5000", "z-conflict"]

    entry = by_id["real-backport-pallets-flask-5000"]
    assert entry["kind"] == "backport" and entry["verdict"] == "backport_verified"
    assert entry["label"] == "Verified backport (real run)" and entry["illustrative"] is False
    assert entry["assessment"] is None and entry["confidence"] == ""
    assert entry["backport"]["target_branch"] == "3.0.x" and "_example" not in entry["backport"]
    assert entry["markdown"] == "# Verified backport: saved\n"
    assert entry["title"] == "A backported fix" and entry["issue_url"].endswith("/issues/5000")
    # a backport without a saved .md falls back to the renderer
    assert by_id["z-conflict"]["verdict"] == "backport_conflicts"
    assert by_id["z-conflict"]["markdown"] == "# rendered backport: conflicts"
    # assessments keep their shape
    assert by_id["a-fix"]["kind"] == "assessment" and by_id["a-fix"]["backport"] is None
    assert by_id["a-fix"]["assessment"]["title"] == "Fake issue"


def test_the_page_script_can_draw_a_backport_example() -> None:
    """The demo page lists the examples and opens each on the reports page, which draws it."""
    shared = (web.STATIC_DIR / "common.js").read_text(encoding="utf-8")
    script = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    reports = (web.STATIC_DIR / "reports.js").read_text(encoding="utf-8")
    assert "function renderBackport" in shared and "renderBackport(" in reports
    assert "reports#${encodeURIComponent(ex.id)}" in script
    for status in BackportStatus:
        assert f"backport_{status.value}:" in shared
