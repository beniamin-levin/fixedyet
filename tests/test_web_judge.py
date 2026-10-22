"""Judge access and the scope check of the web demo.

Judges get codes (FIXEDYET_JUDGE_CODES) with their own daily quota, so the public limit cannot
lock them out during the judging period; part of the budget can be kept for them. Before a run is
counted, the repository's language is checked: only pure-Python projects can be run.
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from fixedyet import ledger
from fixedyet.config import Settings
from fixedyet.models import Assessment, Confidence, ProgressEvent, Verdict
from fixedyet.web import app as web

ISSUE = "https://github.com/psf/requests/issues/6000"
CODE = "judge-code-0123456789"
OTHER_CODE = "second-judge-code-abcdef"
ADMIN = "maintainer-key-0123456789abcdef"
REAL_LOOKUP = web.fetch_repo_language  # conftest replaces it in every test; kept for one test
REAL_ISSUE_CHECK = web.check_issue  # likewise


class Pipeline:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(
        self, issue_url: str, *, settings: Any, progress: Callable[[ProgressEvent], Awaitable[None]]
    ) -> Assessment:
        self.calls.append(issue_url)
        return Assessment(
            issue_url=issue_url, title="Fake", verdict=Verdict.OPEN_BUG,
            confidence=Confidence.HIGH,
        )


class Languages:
    """Stands in for the GitHub lookup: a language per repository, or an exception."""

    def __init__(self, answer: str | None | BaseException = "Python") -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def __call__(self, owner: str, repo: str, *, settings: Any) -> str | None:
        self.calls.append(f"{owner}/{repo}")
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class Issues:
    """Stands in for the issue lookup: None for an issue, or the exception GitHub's answer gives."""

    def __init__(self, answer: BaseException | None = None) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def __call__(self, owner: str, repo: str, number: int, *, settings: Any) -> None:
        self.calls.append(f"{owner}/{repo}#{number}")
        if self.answer is not None:
            raise self.answer


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> Pipeline:
    fake = Pipeline()
    monkeypatch.setattr(web, "run_pipeline", fake)
    monkeypatch.setattr(web, "render_report", lambda a: f"# {a.title}")
    return fake


@pytest.fixture
def languages(monkeypatch: pytest.MonkeyPatch) -> Languages:
    fake = Languages()
    monkeypatch.setattr(web, "fetch_repo_language", fake)
    return fake


@pytest.fixture
def issues(monkeypatch: pytest.MonkeyPatch) -> Issues:
    fake = Issues()
    monkeypatch.setattr(web, "check_issue", fake)
    return fake


def make_settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "demo_daily_limit": 1,
        "runs_dir": tmp_path / "runs",
        "total_budget_usd": 20.0,
        "run_budget_usd": 0.60,
        "judge_codes": f"{CODE}, {OTHER_CODE}",
        "judge_daily_limit": 2,
        "judge_reserve_usd": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def client_for(settings: SimpleNamespace) -> httpx.AsyncClient:
    app = web.create_app(settings, examples_dir=settings.runs_dir / "no-examples")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def run(client: httpx.AsyncClient, *, code: str | None = None, url: str = ISSUE,
              admin: bool = False) -> httpx.Response:
    headers = {"X-Judge-Code": code} if code else {}
    if admin:
        headers["Authorization"] = f"Bearer {ADMIN}"
    res = await client.post("/api/runs", json={"issue_url": url}, headers=headers)
    if res.status_code == 202:  # let it finish, so the next one is not refused as busy
        async with asyncio.timeout(5):
            while True:
                data = (await client.get(f"/api/runs/{res.json()['id']}")).json()
                if data["status"] != "running":
                    break
                await asyncio.sleep(0.01)
    return res


# --- judge codes ------------------------------------------------------------------------


async def test_judge_check_is_404_without_codes(tmp_path: Path) -> None:
    async with client_for(make_settings(tmp_path, judge_codes="")) as client:
        res = await client.get("/api/judge/check", headers={"X-Judge-Code": CODE})
        status = (await client.get("/api/status")).json()
    assert res.status_code == 404
    assert status["judge_access"] is False and "judge" not in status


async def test_judge_check_accepts_each_code_and_nothing_else(tmp_path: Path) -> None:
    async with client_for(make_settings(tmp_path)) as client:
        for code in (CODE, OTHER_CODE, f"  {CODE}  "):
            res = await client.get("/api/judge/check", headers={"X-Judge-Code": code})
            assert res.status_code == 200
            assert res.json() == {"ok": True, "daily_limit": 2, "runs_today": 0, "remaining": 2}
        for wrong in ("", "nope", CODE[:-1], CODE + "x", CODE.upper()):
            res = await client.get("/api/judge/check", headers={"X-Judge-Code": wrong})
            assert res.status_code == 401
            assert CODE not in res.text and OTHER_CODE not in res.text
        status = (await client.get("/api/status")).json()
        assert status["judge_access"] is True and "judge" not in status  # no code, no quota


async def test_short_codes_are_ignored(tmp_path: Path) -> None:
    async with client_for(make_settings(tmp_path, judge_codes="short, also-short")) as client:
        res = await client.get("/api/judge/check", headers={"X-Judge-Code": "short"})
        assert res.status_code == 404  # no valid code configured at all


async def test_every_code_is_compared_in_constant_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compared: list[bytes] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        compared.append(b)
        return real(a, b)

    monkeypatch.setattr(web.hmac, "compare_digest", spy)
    async with client_for(make_settings(tmp_path)) as client:
        await client.get("/api/judge/check", headers={"X-Judge-Code": CODE})
    # the first code matched, and the second was still compared
    assert compared == [CODE.encode(), OTHER_CODE.encode()]


async def test_judges_have_their_own_daily_quota(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    settings = make_settings(tmp_path)  # 1 public run a day, 2 judge runs
    async with client_for(settings) as client:
        assert (await run(client)).status_code == 202
        public = await run(client)
        assert public.status_code == 429 and public.json()["error"] == "daily_limit"

        assert (await run(client, code=CODE)).status_code == 202  # the public limit is no bar
        status = (await client.get("/api/status", headers={"X-Judge-Code": CODE})).json()
        assert status["judge"] == {"daily_limit": 2, "runs_today": 1, "remaining": 1}
        assert status["runs_today"] == 1  # the judge's run did not touch the public counter

        assert (await run(client, code=OTHER_CODE)).status_code == 202  # one quota for all codes
        refused = await run(client, code=CODE)
        assert refused.status_code == 429
        body = refused.json()
        assert body["error"] == "judge_limit" and "2 a day" in body["message"]
        assert CODE not in refused.text

        wrong = await run(client, code="not-a-judge-code-at-all")
        assert wrong.status_code == 429 and wrong.json()["error"] == "daily_limit"
    assert len(pipeline.calls) == 3
    assert (settings.runs_dir / "web" / "judge-daily.json").exists()


async def test_judges_run_while_public_runs_are_paused(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    async with client_for(make_settings(tmp_path, demo_daily_limit=0)) as client:
        assert (await run(client)).status_code == 429
        assert (await run(client, code=CODE)).status_code == 202


async def test_the_maintainer_counts_against_neither_quota(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    settings = make_settings(tmp_path, admin_token=ADMIN)
    async with client_for(settings) as client:
        assert (await run(client, code=CODE, admin=True)).status_code == 202
        status = (await client.get("/api/status", headers={"X-Judge-Code": CODE})).json()
    assert status["runs_today"] == 0 and status["judge"]["runs_today"] == 0


async def test_the_reserve_keeps_budget_for_judges(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    settings = make_settings(
        tmp_path, demo_daily_limit=5, total_budget_usd=10.0, judge_reserve_usd=3.0
    )
    ledger.record(settings, 6.6)  # $3.40 left: $0.40 above the reserve, less than one run
    async with client_for(settings) as client:
        public = await run(client)
        assert public.status_code == 429 and public.json()["error"] == "budget_exhausted"
        assert "judges" in public.json()["message"]
        assert (await run(client, code=CODE)).status_code == 202
    assert len(pipeline.calls) == 1
    assert not (settings.runs_dir / "web" / "daily.json").exists()


async def test_without_a_reserve_the_whole_budget_is_public(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    settings = make_settings(tmp_path, total_budget_usd=10.0)
    ledger.record(settings, 9.0)
    async with client_for(settings) as client:
        assert (await run(client)).status_code == 202


async def test_judges_still_stop_at_the_total_cap(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    settings = make_settings(tmp_path, total_budget_usd=10.0, judge_reserve_usd=3.0)
    ledger.record(settings, 9.5)
    async with client_for(settings) as client:
        res = await run(client, code=CODE)
    assert res.status_code == 429 and res.json()["error"] == "budget_exhausted"
    assert "judges" not in res.json()["message"]
    assert pipeline.calls == []


def test_settings_read_the_judge_variables_and_mask_the_codes() -> None:
    settings = Settings.load({
        "FIXEDYET_JUDGE_CODES": CODE, "FIXEDYET_JUDGE_DAILY_LIMIT": "7",
        "FIXEDYET_JUDGE_RESERVE_USD": "4.5",
    })
    assert settings.judge_codes == CODE
    assert settings.judge_daily_limit == 7 and settings.judge_reserve_usd == 4.5
    assert CODE not in repr(settings)
    defaults = Settings.load({})
    assert defaults.judge_codes == "" and defaults.judge_daily_limit == 20
    assert defaults.judge_reserve_usd == 0.0


# --- the scope check: pure-Python projects only -------------------------------------------


async def test_a_javascript_issue_is_refused_and_uses_up_nothing(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    languages.answer = "JavaScript"
    settings = make_settings(tmp_path)
    url = "https://github.com/expressjs/express/issues/5000"
    async with client_for(settings) as client:
        for code in (None, CODE):
            res = await run(client, code=code, url=url)
            assert res.status_code == 422
            body = res.json()
            assert body["error"] == "unsupported_language" and body["language"] == "JavaScript"
            assert "pure-Python" in body["message"] and "expressjs/express" in body["message"]
            assert "nothing was counted" in body["message"]
        status = (await client.get("/api/status", headers={"X-Judge-Code": CODE})).json()
    assert status["runs_today"] == 0 and status["judge"]["runs_today"] == 0
    assert pipeline.calls == []
    assert languages.calls == ["expressjs/express"]  # the second answer came from the cache


async def test_python_and_unknown_languages_run(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    async with client_for(make_settings(tmp_path, demo_daily_limit=5)) as client:
        assert (await run(client)).status_code == 202
        languages.answer = None  # GitHub lists no language
        assert (await run(client, url="https://github.com/a/b/issues/1")).status_code == 202
    assert len(pipeline.calls) == 2


async def test_a_failed_lookup_lets_the_run_go_ahead(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    languages.answer = RuntimeError("GitHub is down")
    async with client_for(make_settings(tmp_path)) as client:
        assert (await run(client)).status_code == 202
    assert len(pipeline.calls) == 1


async def test_a_missing_repository_is_refused_and_uses_up_nothing(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    languages.answer = web.RepositoryNotFound("nobody/nothing")
    async with client_for(make_settings(tmp_path)) as client:
        res = await run(client, url="https://github.com/nobody/nothing/issues/1")
        assert res.status_code == 400 and res.json()["error"] == "repository_not_found"
        assert (await client.get("/api/status")).json()["runs_today"] == 0
    assert pipeline.calls == []


async def test_the_maintainer_skips_the_language_check(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    languages.answer = "Jupyter Notebook"
    async with client_for(make_settings(tmp_path, admin_token=ADMIN)) as client:
        assert (await run(client, admin=True)).status_code == 202
    assert languages.calls == []


async def test_a_used_up_limit_answers_without_asking_github(
    tmp_path: Path, pipeline: Pipeline, languages: Languages
) -> None:
    async with client_for(make_settings(tmp_path, demo_daily_limit=0)) as client:
        assert (await run(client)).status_code == 429
    assert languages.calls == []


async def test_the_lookup_reads_the_repository_language_from_github() -> None:
    """The real seam, against a mocked GitHub API: no network."""
    import respx

    from fixedyet.github import API_ROOT

    lookup = REAL_LOOKUP
    with respx.mock(base_url=API_ROOT) as mock:
        mock.get("/repos/pallets/jinja").respond(200, json={"language": "Python"})
        mock.get("/repos/expressjs/express").respond(200, json={"language": "JavaScript"})
        mock.get("/repos/empty/repo").respond(200, json={"language": None})
        mock.get("/repos/nobody/nothing").respond(404, json={"message": "Not Found"})
        settings = SimpleNamespace(github_token="")
        assert await lookup("pallets", "jinja", settings=settings) == "Python"
        assert await lookup("expressjs", "express", settings=settings) == "JavaScript"
        assert await lookup("empty", "repo", settings=settings) is None
        with pytest.raises(web.RepositoryNotFound):
            await lookup("nobody", "nothing", settings=settings)


# --- the scope check: the URL must name an issue ------------------------------------------


@pytest.mark.parametrize(
    ("answer", "error", "words"),
    [
        (web.NotAnIssue("psf/requests#6000"), "not_an_issue", "is a pull request, not an issue"),
        (web.IssueNotFound("psf/requests#6000"), "issue_not_found", "has no issue #6000"),
    ],
)
async def test_a_pull_request_or_a_missing_issue_is_refused_for_everyone_and_counts_nowhere(
    tmp_path: Path, pipeline: Pipeline, languages: Languages, issues: Issues,
    answer: BaseException, error: str, words: str,
) -> None:
    issues.answer = answer
    settings = make_settings(tmp_path, admin_token=ADMIN)
    async with client_for(settings) as client:
        for who in ({}, {"code": CODE}, {"admin": True}):  # the public, a judge, the maintainer
            res = await run(client, **who)
            assert res.status_code == 422, who
            body = res.json()
            assert body["error"] == error and words in body["message"]
            assert "psf/requests" in body["message"] and "nothing was counted" in body["message"]
        status = (await client.get("/api/status", headers={"X-Judge-Code": CODE})).json()
    assert status["runs_today"] == 0 and status["judge"]["runs_today"] == 0
    assert pipeline.calls == []
    assert issues.calls == ["psf/requests#6000"] * 3


async def test_a_failed_issue_lookup_lets_the_run_go_ahead(
    tmp_path: Path, pipeline: Pipeline, languages: Languages, issues: Issues
) -> None:
    issues.answer = RuntimeError("GitHub is down")
    async with client_for(make_settings(tmp_path)) as client:
        assert (await run(client)).status_code == 202
    assert len(pipeline.calls) == 1


async def test_a_missing_repository_is_named_before_the_missing_issue(
    tmp_path: Path, pipeline: Pipeline, languages: Languages, issues: Issues
) -> None:
    languages.answer = web.RepositoryNotFound("nobody/nothing")
    issues.answer = web.IssueNotFound("nobody/nothing#1")
    async with client_for(make_settings(tmp_path)) as client:
        res = await run(client, url="https://github.com/nobody/nothing/issues/1")
    assert res.json()["error"] == "repository_not_found"


async def test_a_used_up_limit_reads_no_issue_either(
    tmp_path: Path, pipeline: Pipeline, issues: Issues
) -> None:
    async with client_for(make_settings(tmp_path, demo_daily_limit=0)) as client:
        assert (await run(client)).status_code == 429
    assert issues.calls == []


async def test_the_issue_check_reads_the_issue_from_github_in_one_request() -> None:
    """The real seam, against a mocked GitHub API: no network."""
    import respx

    from fixedyet.github import API_ROOT

    check = REAL_ISSUE_CHECK
    settings = SimpleNamespace(github_token="")
    with respx.mock(base_url=API_ROOT) as mock:
        issue = mock.get("/repos/pallets/jinja/issues/2244").respond(
            200, json={"number": 2244, "title": "a bug"}
        )
        mock.get("/repos/pallets/jinja/issues/2259").respond(200, json={
            "number": 2259, "title": "a fix", "pull_request": {"url": "x", "merged_at": None},
        })
        mock.get("/repos/pallets/jinja/issues/99999").respond(404, json={"message": "Not Found"})
        assert await check("pallets", "jinja", 2244, settings=settings) is None
        assert issue.call_count == 1 and len(mock.calls) == 1
        with pytest.raises(web.NotAnIssue):
            await check("pallets", "jinja", 2259, settings=settings)
        with pytest.raises(web.IssueNotFound):
            await check("pallets", "jinja", 99999, settings=settings)


# --- the pages ----------------------------------------------------------------------------


def test_the_page_states_the_scope_and_offers_judge_access() -> None:
    html = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "pure-Python projects tested with pytest" in html
    assert 'id="judge-form"' in html and 'type="password"' in html
    assert "X-Judge-Code" in js and "api/judge/check" in js


def test_no_page_claims_a_microvm_sandbox() -> None:
    for name in ("index.html", "reports.html", "common.js", "app.js", "reports.js"):
        text = (web.STATIC_DIR / name).read_text(encoding="utf-8")
        assert "microvm" not in text.lower(), name
    for name in ("index.html", "reports.html"):
        text = " ".join((web.STATIC_DIR / name).read_text(encoding="utf-8").split())
        assert "Docker sandbox" in text, name
        assert "waiting for beta access" in text, name
