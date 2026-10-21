"""The maintainer's fix mode on the reports page: step 1 (attempt and verify a fix of an open bug)
and step 2 (the draft pull request), with auth and the typed upstream confirmation. Offline: the
fix run, the GitHub API and the push are fakes."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from fixedyet import pullrequest
from fixedyet.models import (
    Assessment,
    Confidence,
    FixAttempt,
    FixResult,
    FixStatus,
    Outcome,
    ProgressEvent,
    Reproduction,
    SideRun,
    SuiteRun,
    Usage,
    Verdict,
)
from fixedyet.web import app as web
from fixedyet.web import reports

ISSUE = "https://github.com/acme/widgets/issues/43"
BASE = "b" * 40
TOKEN = "ghp_SECRETSECRETSECRET1234"
ADMIN = "admin-token-for-tests-0123456789abcdef"
RUN = "20261020T100000Z-acme-widgets-43"
AUTH = {"Authorization": f"Bearer {ADMIN}"}
DIFF = (
    "diff --git a/widgets/core.py b/widgets/core.py\n--- a/widgets/core.py\n+++ b/widgets/core.py\n"
    "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')[:-1]\n"
    "+    return text.split(',')\n"
)


def make_assessment(verdict: Verdict = Verdict.OPEN_BUG, *, code: str | None = "def test_x():\n"
                    "    assert 1\n") -> Assessment:
    return Assessment(
        issue_url=ISSUE, title="Issue 43", verdict=verdict, confidence=Confidence.HIGH,
        reproduction=Reproduction(
            repro_test_path="tests/test_fixedyet_repro.py", repro_test_code=code,
            reported_side=SideRun(ref="8.1.3", outcomes=[Outcome.FAIL, Outcome.FAIL]),
            fix_side=SideRun(ref="main", outcomes=[Outcome.FAIL, Outcome.FAIL]),
        ),
        usage=Usage(cost_usd=0.02, wall_seconds=30.0),
        created_at=datetime(2026, 10, 20, 10, 0, tzinfo=UTC),
    )


def make_fix(status: FixStatus = FixStatus.VERIFIED) -> FixResult:
    verified = status is FixStatus.VERIFIED
    return FixResult(
        status=status, reason="attempt 1 of 3: verified" if verified else "no verified fix",
        issue_url=ISSUE, title="Issue 43", repo="acme/widgets", base_branch="main", base_sha=BASE,
        summary="Keep the last item in parse" if verified else "", explanation="It sliced.",
        diff=DIFF if verified else "", files_changed=["widgets/core.py"] if verified else [],
        lines_added=1, lines_removed=1, repro_test_path="tests/test_issue_43.py",
        repro_test_code="def test_x():\n    assert 1\n",
        regression_test_path="tests/test_issue_43.py",
        repro_before=SideRun(ref="main (before)", outcomes=[Outcome.FAIL, Outcome.FAIL]),
        repro_after=SideRun(ref="main (after)", outcomes=[Outcome.PASS, Outcome.PASS])
        if verified else None,
        suite_before=SuiteRun(ran=True, passed=10), suite_after=SuiteRun(ran=True, passed=10),
        attempts=[FixAttempt(number=1, outcome="verified" if verified else "repro_still_fails")],
    )


def write_run(runs_dir: Path, name: str, assessment: Assessment) -> None:
    folder = runs_dir / name
    folder.mkdir(parents=True)
    (folder / "assessment.json").write_text(assessment.model_dump_json(), encoding="utf-8")
    (folder / "report.md").write_text("# saved\n", encoding="utf-8")


@pytest.fixture
def settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        demo_daily_limit=5, runs_dir=tmp_path / "runs", total_budget_usd=20.0,
        run_budget_usd=0.60, fix_budget_usd=0.25, admin_token=ADMIN, github_token=TOKEN,
    )


@pytest.fixture
async def client(settings: SimpleNamespace, tmp_path: Path):
    app = web.create_app(settings, examples_dir=tmp_path / "examples")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _renderers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "render_report", lambda a: f"# rendered {a.title}")
    monkeypatch.setattr(web, "render_fix", lambda f: f"# rendered fix {f.status.value}")


class FakeFix:
    """Stands in for ``attempt_fix``: records calls, can be held open, returns a canned result."""

    def __init__(self) -> None:
        self.calls: list[Assessment] = []
        self.gate: asyncio.Event | None = None
        self.result = make_fix()

    async def __call__(self, assessment: Assessment, *, settings: Any, progress: Any) -> FixResult:
        self.calls.append(assessment)
        await progress(ProgressEvent(stage="fix", message="Attempt 1 of 3: asking the model"))
        if self.gate is not None:
            await self.gate.wait()
        return self.result


class FakePullRequests:
    def __init__(self) -> None:
        self.calls: list[tuple[FixResult, str, str]] = []

    async def __call__(self, result: FixResult, *, token: str, say: Any, target: str = "fork"):
        self.calls.append((result, token, target))
        say("Opening a draft pull request")
        base_repo = "alice/widgets" if target == "fork" else "acme/widgets"
        number = 5 if target == "fork" else 99
        return pullrequest.PullRequest(
            url=f"https://github.com/{base_repo}/pull/{number}", number=number, draft=True,
            base_repo=base_repo, base_branch="main", head="alice:fixedyet/fix-43",
            fork="alice/widgets", login="alice", created=True,
        )


@pytest.fixture
def fix_fake(monkeypatch: pytest.MonkeyPatch) -> FakeFix:
    fake = FakeFix()
    monkeypatch.setattr(web, "run_fix", fake)
    return fake


@pytest.fixture
def pr_fake(monkeypatch: pytest.MonkeyPatch) -> FakePullRequests:
    fake = FakePullRequests()
    monkeypatch.setattr(web, "open_fix_draft_pr", fake)
    monkeypatch.setattr(web, "github_token", lambda settings: TOKEN)
    return fake


async def wait_finished(client: httpx.AsyncClient, run_id: str) -> dict:
    async with asyncio.timeout(5):
        while True:
            data = (await client.get(f"/api/runs/{run_id}")).json()
            if data["status"] != "running":
                return data
            await asyncio.sleep(0.01)


def saved(settings: SimpleNamespace) -> dict:
    return json.loads((settings.runs_dir / "fixes" / f"{RUN}.json").read_text())


async def verified(client: httpx.AsyncClient, settings: SimpleNamespace) -> None:
    write_run(settings.runs_dir, RUN, make_assessment())
    res = await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)
    await wait_finished(client, res.json()["id"])


# -- step 1 ------------------------------------------------------------------------------------


async def test_an_open_bug_can_be_fixed_and_the_result_is_saved_without_a_pull_request(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN, make_assessment())
    row = (await client.get("/api/reports")).json()[0]
    assert row["can_fix"] is True and row["fix"] is None and row["can_backport"] is False

    res = await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)
    assert res.status_code == 202
    job = await wait_finished(client, res.json()["id"])
    assert job["status"] == "done" and (job["kind"], job["report_id"]) == ("fix", RUN)
    assert job["result"] == {"status": "verified", "pr_url": None}
    assert job["markdown"] == "# rendered fix verified"
    messages = [e["message"] for e in job["events"]]
    assert messages[0] == "Attempt 1 of 3: asking the model"
    assert messages[-1] == "Verified. Read the diff, then open the draft pull request."
    assert pr_fake.calls == []  # step 1 never opens a pull request

    record = saved(settings)
    assert record["status"] == "verified" and record["prs"] == {} and record["pr_url"] is None
    assert record["result"]["diff"] == DIFF
    row = (await client.get("/api/reports")).json()[0]
    assert row["fix"] == {"status": "verified", "pr_url": None, "pr_target": None}
    detail = (await client.get(f"/api/reports/{RUN}")).json()
    assert detail["can_fix"] is True and detail["fix"]["result"]["summary"]


async def test_the_fix_job_streams_its_progress_and_skips_the_daily_limit(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
) -> None:
    write_run(settings.runs_dir, RUN, make_assessment())
    before = (await client.get("/api/status")).json()
    job_id = (await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)).json()["id"]
    await wait_finished(client, job_id)
    text = (await client.get(f"/api/runs/{job_id}/events")).text
    assert "event: progress" in text and "event: done" in text
    assert (await client.get("/api/status")).json() == before


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": f"Basic {ADMIN}"}])
async def test_a_stranger_cannot_start_a_fix(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    headers: dict[str, str],
) -> None:
    write_run(settings.runs_dir, RUN, make_assessment())
    res = await client.post(f"/api/reports/{RUN}/fix", headers=headers)
    assert res.status_code == 401 and res.json() == {"error": "unauthorized",
                                                       "message": "Not authorized."}
    assert fix_fake.calls == []


async def test_without_maintainer_mode_the_fix_endpoints_do_not_exist(
    settings: SimpleNamespace, tmp_path: Path, fix_fake: FakeFix,
) -> None:
    settings.admin_token = ""
    write_run(settings.runs_dir, RUN, make_assessment())
    app = web.create_app(settings, examples_dir=tmp_path / "examples")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        for path in (f"/api/reports/{RUN}/fix", f"/api/reports/{RUN}/fix/pr"):
            assert (await client.post(path, headers=AUTH, json={"target": "fork"})).status_code \
                == 404
    assert fix_fake.calls == []


@pytest.mark.parametrize(
    "assessment",
    [make_assessment(Verdict.FIXED_ON_MAIN), make_assessment(Verdict.NOT_REPRODUCED),
     make_assessment(code=None)],
)
async def test_only_an_open_bug_with_a_test_can_be_fixed(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    assessment: Assessment,
) -> None:
    write_run(settings.runs_dir, RUN, assessment)
    res = await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)
    assert res.status_code == 422 and res.json()["error"] == "not_fixable"
    assert fix_fake.calls == []
    assert (await client.get("/api/reports")).json()[0]["can_fix"] is False


async def test_an_unknown_report_is_404(client: httpx.AsyncClient, fix_fake: FakeFix) -> None:
    assert (await client.post("/api/reports/20261020T100000Z-nobody-1/fix",
                              headers=AUTH)).status_code == 404


async def test_a_verified_fix_is_not_run_again_and_a_failed_one_may_be(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
) -> None:
    fix_fake.result = make_fix(FixStatus.NOT_FIXED)
    await verified(client, settings)
    assert saved(settings)["status"] == "not_fixed"
    fix_fake.result = make_fix()
    res = await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)
    assert res.status_code == 202
    await wait_finished(client, res.json()["id"])
    again = await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)
    assert again.status_code == 200 and again.json()["fix"]["status"] == "verified"
    assert len(fix_fake.calls) == 2


async def test_a_fix_waits_for_the_one_slot(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
) -> None:
    write_run(settings.runs_dir, RUN, make_assessment())
    fix_fake.gate = asyncio.Event()
    first = await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)
    second = await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)
    assert second.status_code == 409 and second.json()["error"] == "busy"
    fix_fake.gate.set()
    await wait_finished(client, first.json()["id"])


async def test_a_fix_is_refused_when_the_total_budget_cannot_cover_it(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_run(settings.runs_dir, RUN, make_assessment())
    monkeypatch.setattr(web, "budget_remaining", lambda s: 0.10)
    res = await client.post(f"/api/reports/{RUN}/fix", headers=AUTH)
    assert res.status_code == 429 and res.json()["error"] == "budget_exhausted"
    assert fix_fake.calls == []


async def test_a_crashed_fix_job_says_little_and_leaks_nothing(
    client: httpx.AsyncClient, settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> FixResult:
        raise RuntimeError(f"secret {TOKEN}")

    monkeypatch.setattr(web, "run_fix", boom)
    write_run(settings.runs_dir, RUN, make_assessment())
    job = await wait_finished(client, (await client.post(f"/api/reports/{RUN}/fix",
                                                         headers=AUTH)).json()["id"])
    assert job["status"] == "error" and TOKEN not in json.dumps(job)
    assert "server log" in job["error"]


# -- step 2 ------------------------------------------------------------------------------------


async def test_step_2_opens_a_draft_in_the_fork_and_is_idempotent(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    pr_fake: FakePullRequests,
) -> None:
    await verified(client, settings)
    res = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH, json={"target": "fork"})
    assert res.status_code == 200
    body = res.json()
    assert body["pr_url"] == "https://github.com/alice/widgets/pull/5" and body["created"] is True
    assert body["fix"]["prs"]["fork"]["url"] == body["pr_url"]
    ((result, token, target),) = pr_fake.calls
    assert result.status is FixStatus.VERIFIED and token == TOKEN and target == "fork"
    record = saved(settings)
    assert (record["pr_target"], record["pr_number"]) == ("fork", 5)
    assert TOKEN not in json.dumps(record) and TOKEN not in res.text

    again = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH, json={"target": "fork"})
    assert again.status_code == 200 and again.json()["created"] is False
    assert len(pr_fake.calls) == 1
    row = (await client.get("/api/reports")).json()[0]
    assert row["fix"]["pr_url"] == "https://github.com/alice/widgets/pull/5"


@pytest.mark.parametrize(
    "body",
    [{"target": "upstream"}, {"target": "upstream", "confirm": ""},
     {"target": "upstream", "confirm": "acme/other"}, {"target": "upstream", "confirm": 7},
     {"target": "upstream", "confirm": "alice/widgets"}],
)
async def test_an_upstream_fix_pr_needs_the_repository_typed_exactly(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    pr_fake: FakePullRequests, body: Any,
) -> None:
    await verified(client, settings)
    res = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH, json=body)
    assert res.status_code == 400 and res.json()["error"] == "confirmation_mismatch"
    assert "acme/widgets" in res.json()["message"]
    assert pr_fake.calls == []


async def test_a_confirmed_upstream_fix_pr_is_opened_once(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    pr_fake: FakePullRequests,
) -> None:
    await verified(client, settings)
    body = {"target": "upstream", "confirm": "  ACME/Widgets "}
    res = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH, json=body)
    assert res.status_code == 200 and res.json()["pr_url"].endswith("/acme/widgets/pull/99")
    again = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH, json=body)
    assert again.json()["created"] is False
    assert [t for _, _, t in pr_fake.calls] == ["upstream"]


@pytest.mark.parametrize("body", [None, {}, {"target": "origin"}, ["fork"]])
async def test_step_2_takes_only_the_fork_or_the_upstream(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    pr_fake: FakePullRequests, body: Any,
) -> None:
    await verified(client, settings)
    res = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH,
                            **({} if body is None else {"json": body}))
    assert res.status_code == 400 and res.json()["error"] == "invalid_request"
    assert pr_fake.calls == []


async def test_step_2_needs_a_verified_fix_and_the_maintainer(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN, make_assessment())
    res = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH, json={"target": "fork"})
    assert res.status_code == 409 and res.json()["error"] == "not_verified"
    assert "Verify the fix first" in res.json()["message"]
    fix_fake.result = make_fix(FixStatus.NOT_FIXED)
    await wait_finished(client, (await client.post(f"/api/reports/{RUN}/fix",
                                                   headers=AUTH)).json()["id"])
    res = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH, json={"target": "fork"})
    assert res.status_code == 409
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        res = await client.post(f"/api/reports/{RUN}/fix/pr", headers=headers,
                                json={"target": "fork"})
        assert res.status_code == 401
    assert pr_fake.calls == []


async def test_step_2_without_a_github_token_is_a_503(
    client: httpx.AsyncClient, settings: SimpleNamespace, fix_fake: FakeFix,
    pr_fake: FakePullRequests, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await verified(client, settings)

    def none(settings: Any) -> str:
        raise pullrequest.PullRequestError("no GitHub token")

    monkeypatch.setattr(web, "github_token", none)
    res = await client.post(f"/api/reports/{RUN}/fix/pr", headers=AUTH, json={"target": "fork"})
    assert res.status_code == 503 and pr_fake.calls == []


async def test_the_fix_seam_confirms_the_upstream_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def spy(result: FixResult, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "pr"

    monkeypatch.setattr(pullrequest, "open_fix_pr", spy)
    await web.open_fix_draft_pr(make_fix(), token=TOKEN, say=lambda m: None)
    assert (seen["target"], seen["confirmed_upstream"]) == ("fork", False)
    await web.open_fix_draft_pr(make_fix(), token=TOKEN, say=lambda m: None, target="upstream")
    assert (seen["target"], seen["confirmed_upstream"]) == ("upstream", True)


def test_saved_fix_paths_stay_inside_the_fixes_folder(tmp_path: Path) -> None:
    store = reports.ReportStore(tmp_path, lambda: [], lambda a: "")
    assert store.fix_path("../etc/passwd") is None and store.fix_path("a/b") is None
    path = store.fix_path(RUN)
    assert path is not None and path.parent == tmp_path / "fixes"


# -- the page ------------------------------------------------------------------------------------


def test_the_page_has_the_fix_controls_and_a_light_diff() -> None:
    js = (web.STATIC_DIR / "reports.js").read_text(encoding="utf-8")
    common = (web.STATIC_DIR / "common.js").read_text(encoding="utf-8")
    css = (web.STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "Attempt a fix" in js and "/fix`" in js and "'fix' : 'backport'}/pr" in js
    assert "Only the demo\\'s maintainer can run a fix attempt" in js
    assert "Open in my fork only" in js and "Open draft PR on" in js
    assert "highlightDiff" in common and "renderFix" in common
    for name in ("--diff-add-bg", "--diff-del-bg"):
        value = css.split(f"{name}:")[1].split(";")[0].strip()
        assert value.startswith("#") and int(value[1:3], 16) > 0xD0  # a light tint
    assert "color-scheme: light" in css
