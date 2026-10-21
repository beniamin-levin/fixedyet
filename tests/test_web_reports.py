"""Tests for the "All reports" page API and maintainer mode: listing, ids, admin auth, the backport
job, hiding. Offline: the backport, the GitHub API and the push are all fakes."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from fixedyet import pullrequest
from fixedyet.config import PROJECT_ROOT, Settings
from fixedyet.models import (
    Assessment,
    BackportResult,
    BackportStatus,
    Confidence,
    Outcome,
    ProgressEvent,
    Reproduction,
    SideRun,
    Usage,
    Verdict,
)
from fixedyet.web import app as web
from fixedyet.web import reports

ISSUE = "https://github.com/acme/widgets/issues/42"
FIX = "d959898db264aaf07e70ad4eafa254286f9a5185"
BASE = "b" * 40
TOKEN = "ghp_SECRETSECRETSECRET1234"
ADMIN = "admin-token-for-tests-0123456789abcdef"
RUN_OLD = "20261010T100000Z-acme-widgets-42"
RUN_NEW = "20261020T100000Z-acme-widgets-43"


def make_assessment(
    verdict: Verdict = Verdict.FIXED_ON_MAIN,
    *,
    number: int = 42,
    commit: str | None = FIX,
    created: datetime | None = None,
    cost: float = 0.0123,
) -> Assessment:
    return Assessment(
        issue_url=f"https://github.com/acme/widgets/issues/{number}",
        title=f"Issue {number}",
        verdict=verdict,
        confidence=Confidence.HIGH,
        reproduction=Reproduction(
            repro_test_path="tests/test_repro.py",
            repro_test_code="def test_it():\n    assert 1 == 2\n",
            reported_side=SideRun(ref="8.1.3", outcomes=[Outcome.FAIL, Outcome.FAIL]),
            fix_side=SideRun(ref="main", outcomes=[Outcome.PASS, Outcome.PASS]),
            fixing_commit=commit,
            fixed_in_release="8.2.0" if commit else None,
        ),
        usage=Usage(cost_usd=cost, wall_seconds=33.5),
        created_at=created or datetime(2026, 10, 10, 10, 0, tzinfo=UTC),
    )


def make_result(status: BackportStatus = BackportStatus.VERIFIED) -> BackportResult:
    return BackportResult(
        status=status, reason="the test fails before and passes after",
        issue_url=ISSUE, title="Issue 42", repo="acme/widgets", reported_version="8.1.3",
        target_branch="8.1.x", target_branch_head=BASE, fixing_commit=FIX,
        patch="From x\n", repro_test_path="tests/test_repro.py", repro_test_code="x",
        repro_before=SideRun(ref="8.1.x (before)", outcomes=[Outcome.FAIL, Outcome.FAIL]),
        repro_after=SideRun(ref="8.1.x (after)", outcomes=[Outcome.PASS, Outcome.PASS]),
    )


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def write_run(
    runs_dir: Path, name: str, assessment: Assessment, markdown: str | None = "# saved report\n"
) -> Path:
    folder = runs_dir / name
    folder.mkdir(parents=True)
    (folder / "assessment.json").write_text(assessment.model_dump_json(), encoding="utf-8")
    if markdown is not None:
        (folder / "report.md").write_text(markdown, encoding="utf-8")
    return folder


def make_settings(tmp_path: Path, *, admin_token: str = ADMIN, github_token: str = TOKEN):
    return SimpleNamespace(
        demo_daily_limit=5, runs_dir=tmp_path / "runs", total_budget_usd=20.0,
        run_budget_usd=0.60, admin_token=admin_token, github_token=github_token,
    )


@pytest.fixture
def settings(tmp_path: Path) -> SimpleNamespace:
    return make_settings(tmp_path)


@pytest.fixture
def examples_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "examples"
    folder.mkdir()
    return folder


def make_client(settings: SimpleNamespace, examples_dir: Path) -> httpx.AsyncClient:
    app = web.create_app(settings, examples_dir=examples_dir)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client(settings: SimpleNamespace, examples_dir: Path):
    async with make_client(settings, examples_dir) as c:
        yield c


AUTH = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture(autouse=True)
def _renderers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "render_report", lambda a: f"# rendered {a.title}")
    monkeypatch.setattr(web, "render_backport", lambda b: f"# rendered backport {b.status.value}")


# --- the list -----------------------------------------------------------------------------


async def test_the_list_merges_examples_and_runs_newest_first(
    client: httpx.AsyncClient, settings: SimpleNamespace, examples_dir: Path
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment(number=42))
    write_run(
        settings.runs_dir, RUN_NEW,
        make_assessment(Verdict.OPEN_BUG, number=43, commit=None,
                        created=datetime(2026, 10, 20, 10, 0, tzinfo=UTC)),
    )
    sample = make_assessment(number=44, created=datetime(2026, 10, 15, 8, 30, tzinfo=UTC))
    (examples_dir / "real-acme-widgets-44.json").write_text(sample.model_dump_json())

    res = await client.get("/api/reports")
    assert res.status_code == 200
    rows = res.json()
    assert [r["id"] for r in rows] == [RUN_NEW, "real-acme-widgets-44", RUN_OLD]
    assert [r["example"] for r in rows] == [False, True, False]

    newest = rows[0]
    assert newest["issue_url"] == "https://github.com/acme/widgets/issues/43"
    assert (newest["repo"], newest["number"], newest["title"]) == ("acme/widgets", 43, "Issue 43")
    assert (newest["verdict"], newest["confidence"]) == ("open_bug", "high")
    assert newest["reported_version"] == "8.1.3" and newest["fix_ref"] == "main"
    assert newest["fixing_commit"] is None and newest["can_backport"] is False
    assert newest["created_at"].startswith("2026-10-20T10:00:00")
    assert newest["duration_s"] == 33.5 and newest["cost_usd"] == 0.0123
    assert newest["backport"] is None

    oldest = rows[2]
    assert oldest["fixing_commit"] == FIX and oldest["fixed_in_release"] == "8.2.0"
    assert oldest["can_backport"] is True


async def test_the_list_is_public_and_needs_no_runs_directory(
    settings: SimpleNamespace, examples_dir: Path
) -> None:
    async with make_client(settings, examples_dir) as client:
        res = await client.get("/api/reports")
    assert res.status_code == 200 and res.json() == []
    assert not settings.runs_dir.exists()  # a read never creates anything


async def test_unreadable_and_invalid_folders_are_skipped_silently(
    client: httpx.AsyncClient, settings: SimpleNamespace
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    (settings.runs_dir / "20261011T000000Z-broken-json-1").mkdir()
    (settings.runs_dir / "20261011T000000Z-broken-json-1" / "assessment.json").write_text("{nope")
    (settings.runs_dir / "20261012T000000Z-wrong-schema-1").mkdir()
    (settings.runs_dir / "20261012T000000Z-wrong-schema-1" / "assessment.json").write_text(
        json.dumps({"verdict": "nope"})
    )
    (settings.runs_dir / "20261013T000000Z-empty-1").mkdir()
    (settings.runs_dir / "web").mkdir()  # not a report either
    (settings.runs_dir / "stray.txt").write_text("not a folder")
    (settings.runs_dir / "has space-1").mkdir()
    (settings.runs_dir / "has space-1" / "assessment.json").write_text(
        make_assessment().model_dump_json()
    )

    rows = (await client.get("/api/reports")).json()
    assert [r["id"] for r in rows] == [RUN_OLD]
    assert (await client.get("/api/reports/20261011T000000Z-broken-json-1")).status_code == 404
    assert (await client.get("/api/reports/20261012T000000Z-wrong-schema-1")).status_code == 404


async def test_a_run_folder_that_is_a_symlink_out_of_the_runs_dir_is_ignored(
    client: httpx.AsyncClient, settings: SimpleNamespace, tmp_path: Path
) -> None:
    outside = write_run(tmp_path / "elsewhere", "secret", make_assessment(number=99))
    settings.runs_dir.mkdir(parents=True)
    (settings.runs_dir / "20261014T000000Z-link-1").symlink_to(outside, target_is_directory=True)
    assert (await client.get("/api/reports")).json() == []
    assert (await client.get("/api/reports/20261014T000000Z-link-1")).status_code == 404


async def test_a_backport_example_is_listed_with_its_pull_request(
    client: httpx.AsyncClient, examples_dir: Path
) -> None:
    data = make_result().model_dump(mode="json")
    data["_example"] = {"label": "Verified", "pr_url": "https://github.com/me/widgets/pull/1"}
    (examples_dir / "real-backport-acme-widgets-42.json").write_text(json.dumps(data))
    (examples_dir / "x-bad-pr.json").write_text(
        json.dumps({**make_result().model_dump(mode="json"),
                    "_example": {"pr_url": "javascript:alert(1)"}})
    )

    rows = {r["id"]: r for r in (await client.get("/api/reports")).json()}
    row = rows["real-backport-acme-widgets-42"]
    assert (row["kind"], row["verdict"], row["confidence"]) == ("backport", "backport_verified", "")
    assert row["backport"] == {"status": "verified", "pr_url": "https://github.com/me/widgets/pull/1"}
    assert row["example"] is True and row["can_backport"] is False
    assert rows["x-bad-pr"]["backport"]["pr_url"] is None  # only a github.com link is kept


# --- one report ---------------------------------------------------------------------------


async def test_a_report_comes_with_its_assessment_and_markdown(
    client: httpx.AsyncClient, settings: SimpleNamespace
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    write_run(settings.runs_dir, RUN_NEW, make_assessment(number=43), markdown=None)

    saved = (await client.get(f"/api/reports/{RUN_OLD}")).json()
    assert saved["id"] == RUN_OLD and saved["example"] is False and saved["kind"] == "assessment"
    assert saved["assessment"]["title"] == "Issue 42"
    assert saved["assessment"]["reproduction"]["fixing_commit"] == FIX
    assert saved["markdown"] == "# saved report\n"
    assert saved["backport"] is None and saved["can_backport"] is True
    assert saved["summary"]["number"] == 42

    rendered = (await client.get(f"/api/reports/{RUN_NEW}")).json()
    assert rendered["markdown"] == "# rendered Issue 43"  # no report.md: rendered on demand


async def test_an_example_report_carries_its_label_and_note(
    client: httpx.AsyncClient, examples_dir: Path
) -> None:
    data = make_assessment().model_dump(mode="json")
    data["_example"] = {"label": "Fixed", "note": "- **real** run", "illustrative": False}
    (examples_dir / "real-acme-widgets-42.json").write_text(json.dumps(data))
    (examples_dir / "real-acme-widgets-42.md").write_text("# saved\n")
    got = (await client.get("/api/reports/real-acme-widgets-42")).json()
    assert got["example"] is True and got["label"] == "Fixed" and got["note"] == "- **real** run"
    assert got["markdown"] == "# saved\n" and got["assessment"]["title"] == "Issue 42"


async def test_a_backport_example_report_has_its_result_as_the_backport(
    client: httpx.AsyncClient, examples_dir: Path
) -> None:
    data = make_result().model_dump(mode="json")
    data["_example"] = {"pr_url": "https://github.com/me/widgets/pull/1"}
    (examples_dir / "real-backport-acme-widgets-42.json").write_text(json.dumps(data))
    got = (await client.get("/api/reports/real-backport-acme-widgets-42")).json()
    assert got["kind"] == "backport" and got["assessment"] is None
    assert got["backport"]["status"] == "verified"
    assert got["backport"]["pr_url"] == "https://github.com/me/widgets/pull/1"
    assert got["backport"]["result"]["target_branch"] == "8.1.x"
    assert got["can_backport"] is False


async def test_unknown_report_ids_are_404(client: httpx.AsyncClient) -> None:
    res = await client.get("/api/reports/20261010T100000Z-nobody-1")
    assert res.status_code == 404 and res.json()["error"] == "not_found"


# --- ids and paths ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", ".", "..", "...", ".hidden", "-x", "_x", "a b", "a/b", "../x", "a..b", "/etc/passwd",
     "a\\b", "x" * 161, "é", "a%2Fb", "a:b", "a\nb", "a;b"],
)
def test_report_ids_are_checked_strictly(bad: str) -> None:
    assert reports.valid_report_id(bad) is False


@pytest.mark.parametrize(
    "good", ["20261010T100000Z-acme-widgets-42", "real-pallets-click-3384", "a", "A1.b_c-d", "9"]
)
def test_ordinary_report_ids_are_accepted(good: str) -> None:
    assert reports.valid_report_id(good) is True


@pytest.mark.parametrize(
    "path",
    [
        "..%2F..%2Fetc%2Fpasswd", "%2e%2e", "%2e%2e%2Fsecret", "%2Fetc%2Fpasswd",
        "..%5C..%5Csecret", "%252e%252e", "a%00b", "x" * 400,
    ],
)
async def test_traversal_attempts_find_nothing(
    client: httpx.AsyncClient, settings: SimpleNamespace, tmp_path: Path, path: str
) -> None:
    write_run(tmp_path, "secret", make_assessment(number=99))  # a valid report next to runs/
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    for method in ("GET", "POST", "DELETE"):
        suffix = "" if method == "GET" else "/hide"
        res = await client.request(method, f"/api/reports/{path}{suffix}", headers=AUTH)
        assert res.status_code in (404, 405), (method, path, res.status_code)
        assert "Issue 99" not in res.text and "root:" not in res.text
    res = await client.post(f"/api/reports/{path}/backport", headers=AUTH)
    assert res.status_code == 404


def test_saved_backport_paths_stay_inside_the_backports_folder(tmp_path: Path) -> None:
    store = reports.ReportStore(tmp_path / "runs", lambda: [], lambda a: "")
    assert store.backport_path("../escape") is None
    assert store.backport_path("/abs") is None
    path = store.backport_path("ok-1")
    assert path == tmp_path / "runs" / "backports" / "ok-1.json"
    with pytest.raises(ValueError):
        store.save_backport("../escape", {"status": "x"})
    assert not (tmp_path / "runs").exists()


# --- maintainer mode: who may call ---------------------------------------------------------


ADMIN_CALLS = [
    ("GET", "/api/admin/check"),
    ("POST", "/api/reports/real-acme-widgets-42/backport"),
    ("POST", "/api/reports/real-acme-widgets-42/hide"),
    ("DELETE", "/api/reports/real-acme-widgets-42/hide"),
    ("POST", "/api/reports/no-such-report/backport"),
]


@pytest.mark.parametrize("token", ["", "   "])
async def test_without_an_admin_token_every_admin_endpoint_is_404(
    tmp_path: Path, examples_dir: Path, token: str
) -> None:
    async with make_client(make_settings(tmp_path, admin_token=token), examples_dir) as client:
        for method, path in ADMIN_CALLS:
            for headers in ({}, AUTH, {"Authorization": "Bearer "}):
                res = await client.request(method, path, headers=headers)
                assert res.status_code == 404, (method, path)
                assert res.json() == {"error": "not_found", "message": "Not found."}


async def test_a_settings_object_without_the_field_is_maintainer_mode_off(
    tmp_path: Path, examples_dir: Path
) -> None:
    plain = make_settings(tmp_path)
    del plain.admin_token
    async with make_client(plain, examples_dir) as client:
        assert (await client.get("/api/admin/check", headers=AUTH)).status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Bearer {ADMIN}x"},
        {"Authorization": f"Bearer {ADMIN[:-1]}"},
        {"Authorization": "Bearer"},
        {"Authorization": f"Basic {ADMIN}"},
        {"Authorization": ADMIN},
        {"X-Admin-Token": ADMIN},
    ],
)
async def test_a_wrong_or_missing_token_gets_a_bare_401(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    for method, path in ADMIN_CALLS:
        res = await client.request(method, path, headers=headers)
        assert res.status_code == 401, (method, path)
        assert res.json() == {"error": "unauthorized", "message": "Not authorized."}
        assert res.headers["www-authenticate"] == "Bearer"
        assert ADMIN not in res.text


async def test_the_right_token_works_and_is_never_echoed_or_logged(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    res = await client.get("/api/admin/check", headers={"Authorization": f"bearer  {ADMIN} "})
    assert res.status_code == 200 and res.json() == {"ok": True}
    await client.get("/api/admin/check", headers={"Authorization": "Bearer nope"})
    assert ADMIN not in res.text and ADMIN not in caplog.text and "nope" not in caplog.text


def test_the_token_is_compared_in_constant_time() -> None:
    source = inspect.getsource(web.create_app)
    assert "hmac.compare_digest" in source
    assert not re.search(r"(?:token|given|expected)\s*(?:==|!=)", source)


def test_the_admin_token_is_a_secret_setting() -> None:
    s = Settings.load(env={"FIXEDYET_ADMIN_TOKEN": f" {ADMIN} "})
    assert s.admin_token == ADMIN
    assert ADMIN not in repr(s) and "admin_token='<set>'" in repr(s)
    assert Settings.load(env={}).admin_token == ""
    assert "FIXEDYET_ADMIN_TOKEN=" in (PROJECT_ROOT / ".env.example").read_text()


# --- hiding -------------------------------------------------------------------------------


async def test_hide_and_unhide_a_report(
    client: httpx.AsyncClient, settings: SimpleNamespace, examples_dir: Path
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    write_run(settings.runs_dir, RUN_NEW, make_assessment(number=43))
    (examples_dir / "real-acme-widgets-44.json").write_text(make_assessment().model_dump_json())

    for report_id in (RUN_OLD, "real-acme-widgets-44"):
        res = await client.post(f"/api/reports/{report_id}/hide", headers=AUTH)
        assert res.status_code == 200 and res.json() == {"id": report_id, "hidden": True}
    hidden_file = settings.runs_dir / "hidden.json"
    both = [RUN_OLD, "real-acme-widgets-44"]
    assert sorted(json.loads(hidden_file.read_text())["hidden"]) == both

    # visitors: gone from the list and a 404 on the detail page
    assert [r["id"] for r in (await client.get("/api/reports")).json()] == [RUN_NEW]
    assert (await client.get(f"/api/reports/{RUN_OLD}")).status_code == 404
    assert (await client.get("/api/reports/real-acme-widgets-44")).status_code == 404
    # the maintainer still sees them, tagged, so a report can be shown again
    rows = (await client.get("/api/reports", headers=AUTH)).json()
    assert {r["id"]: r.get("hidden", False) for r in rows} == {
        RUN_NEW: False, RUN_OLD: True, "real-acme-widgets-44": True,
    }
    detail = (await client.get(f"/api/reports/{RUN_OLD}", headers=AUTH)).json()
    assert detail["hidden"] is True
    assert (await client.get(f"/api/reports/{RUN_NEW}", headers=AUTH)).json()["hidden"] is False
    # a wrong token is just a visitor on the public endpoints
    assert [r["id"] for r in (await client.get(
        "/api/reports", headers={"Authorization": "Bearer wrong"})).json()] == [RUN_NEW]

    res = await client.delete(f"/api/reports/{RUN_OLD}/hide", headers=AUTH)
    assert res.status_code == 200 and res.json() == {"id": RUN_OLD, "hidden": False}
    assert (await client.get(f"/api/reports/{RUN_OLD}")).status_code == 200
    assert json.loads(hidden_file.read_text())["hidden"] == ["real-acme-widgets-44"]
    # hiding twice and unhiding twice change nothing
    await client.post(f"/api/reports/{RUN_NEW}/hide", headers=AUTH)
    await client.post(f"/api/reports/{RUN_NEW}/hide", headers=AUTH)
    await client.delete(f"/api/reports/{RUN_NEW}/hide", headers=AUTH)
    await client.delete(f"/api/reports/{RUN_NEW}/hide", headers=AUTH)
    assert json.loads(hidden_file.read_text())["hidden"] == ["real-acme-widgets-44"]


async def test_hiding_an_unknown_report_is_404_and_writes_nothing(
    client: httpx.AsyncClient, settings: SimpleNamespace
) -> None:
    res = await client.post("/api/reports/20261010T100000Z-nobody-1/hide", headers=AUTH)
    assert res.status_code == 404
    assert not (settings.runs_dir / "hidden.json").exists()


async def test_a_stale_hidden_id_can_still_be_unhidden(
    client: httpx.AsyncClient, settings: SimpleNamespace
) -> None:
    settings.runs_dir.mkdir(parents=True)
    (settings.runs_dir / "hidden.json").write_text(json.dumps({"hidden": ["gone-1"]}))
    res = await client.delete("/api/reports/gone-1/hide", headers=AUTH)
    assert res.status_code == 200
    assert json.loads((settings.runs_dir / "hidden.json").read_text()) == {"hidden": []}


async def test_hidden_json_is_written_atomically(
    client: httpx.AsyncClient, settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    calls: list[tuple[Path, Path]] = []
    real_replace = reports.os.replace

    def spy(src: Any, dst: Any) -> None:
        calls.append((Path(src), Path(dst)))
        assert Path(src).read_text()  # the temporary file is complete before the rename
        real_replace(src, dst)

    monkeypatch.setattr(reports.os, "replace", spy)
    await client.post(f"/api/reports/{RUN_OLD}/hide", headers=AUTH)
    ((src, dst),) = calls
    assert dst == settings.runs_dir / "hidden.json" and src.parent == dst.parent
    assert src.name.startswith(".hidden.json.") and src.name.endswith(".tmp")
    assert names(settings.runs_dir) == [RUN_OLD, "hidden.json"]


async def test_a_corrupt_hidden_file_hides_nothing(
    client: httpx.AsyncClient, settings: SimpleNamespace
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    (settings.runs_dir / "hidden.json").write_text("{not json")
    assert [r["id"] for r in (await client.get("/api/reports")).json()] == [RUN_OLD]


# --- the backport job ---------------------------------------------------------------------


class FakeBackport:
    """Stands in for ``backport()``: records calls, can be held open, returns a canned result."""

    def __init__(self) -> None:
        self.calls: list[Assessment] = []
        self.gate: asyncio.Event | None = None
        self.result = make_result()

    async def __call__(self, assessment: Assessment, *, settings: Any, progress: Any):
        self.calls.append(assessment)
        await progress(ProgressEvent(stage="backport", message="Listing the branches"))
        if self.gate is not None:
            await self.gate.wait()
        await progress(ProgressEvent(stage="backport", message="verified"))
        return self.result


class FakePullRequests:
    """Stands in for ``open_draft_pr``."""

    def __init__(self) -> None:
        self.calls: list[tuple[BackportResult, str]] = []
        self.error: Exception | None = None

    async def __call__(self, result: BackportResult, *, token: str, say: Any):
        self.calls.append((result, token))
        say("Opening a draft pull request")
        if self.error is not None:
            raise self.error
        return pullrequest.PullRequest(
            url="https://github.com/alice/widgets/pull/5", number=5, draft=True,
            base_repo="alice/widgets", base_branch="8.1.x", head="alice:fixedyet/backport-42-8.1.x",
            fork="alice/widgets", login="alice", created=True,
        )


@pytest.fixture
def backport_fake(monkeypatch: pytest.MonkeyPatch) -> FakeBackport:
    fake = FakeBackport()
    monkeypatch.setattr(web, "run_backport", fake)
    return fake


@pytest.fixture
def pr_fake(monkeypatch: pytest.MonkeyPatch) -> FakePullRequests:
    fake = FakePullRequests()
    monkeypatch.setattr(web, "open_draft_pr", fake)
    monkeypatch.setattr(web, "github_token", lambda settings: TOKEN)
    return fake


async def wait_finished(client: httpx.AsyncClient, run_id: str) -> dict:
    async with asyncio.timeout(5):
        while True:
            data = (await client.get(f"/api/runs/{run_id}")).json()
            if data["status"] != "running":
                return data
            await asyncio.sleep(0.01)


async def backport_job(client: httpx.AsyncClient, report_id: str = RUN_OLD) -> httpx.Response:
    return await client.post(f"/api/reports/{report_id}/backport", headers=AUTH)


async def test_a_backport_job_opens_a_draft_pr_and_saves_the_result(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())

    res = await backport_job(client)
    assert res.status_code == 202
    job = await wait_finished(client, res.json()["id"])

    assert job["status"] == "done" and job["error"] is None
    assert (job["kind"], job["report_id"]) == ("backport", RUN_OLD)
    assert job["issue_url"] == ISSUE and job["assessment"] is None
    assert job["result"] == {"status": "verified", "pr_url": "https://github.com/alice/widgets/pull/5"}
    assert job["markdown"] == "# rendered backport verified"
    messages = [e["message"] for e in job["events"]]
    assert messages[:2] == ["Listing the branches", "verified"]
    assert "Opening a draft pull request" in messages
    assert messages[-1] == "Draft pull request: https://github.com/alice/widgets/pull/5"
    assert job["events"][-1]["stage"] == "done"

    (assessment,) = backport_fake.calls
    assert assessment.issue_url == ISSUE and assessment.reproduction.fixing_commit == FIX
    ((result, token),) = pr_fake.calls
    assert result.status is BackportStatus.VERIFIED and token == TOKEN

    # saved where the examples dir does not matter: <runs_dir>/backports/<id>.json
    record = json.loads((settings.runs_dir / "backports" / f"{RUN_OLD}.json").read_text())
    assert record["status"] == "verified" and record["report_id"] == RUN_OLD
    assert record["pr_url"] == "https://github.com/alice/widgets/pull/5"
    assert record["pr"]["number"] == 5 and record["pr"]["draft"] is True
    assert record["pr"]["base_repo"] == "alice/widgets"  # the fork, not acme/widgets
    assert record["result"]["target_branch"] == "8.1.x" and record["pr_error"] is None
    assert TOKEN not in json.dumps(record) and TOKEN not in json.dumps(job)

    # ... and then visible to everyone, in the list and on the detail page
    (row,) = [r for r in (await client.get("/api/reports")).json() if r["id"] == RUN_OLD]
    assert row["backport"] == {"status": "verified", "pr_url": "https://github.com/alice/widgets/pull/5"}
    detail = (await client.get(f"/api/reports/{RUN_OLD}")).json()
    assert detail["backport"]["pr_url"] == "https://github.com/alice/widgets/pull/5"
    assert detail["backport"]["result"]["status"] == "verified"


async def test_a_backport_job_streams_its_progress_like_a_run(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    job_id = (await backport_job(client)).json()["id"]
    await wait_finished(client, job_id)
    text = (await client.get(f"/api/runs/{job_id}/events")).text
    assert text.count("event: progress") >= 4 and "event: done" in text
    assert "Draft pull request: https://github.com/alice/widgets/pull/5" in text


async def test_a_backport_does_not_use_the_daily_limit(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    before = (await client.get("/api/status")).json()
    await wait_finished(client, (await backport_job(client)).json()["id"])
    after = (await client.get("/api/status")).json()
    assert before == after and after["remaining"] == 5
    assert not (settings.runs_dir / "web" / "daily.json").exists()


@pytest.mark.parametrize(
    ("verdict", "commit"),
    [(Verdict.OPEN_BUG, FIX), (Verdict.NOT_REPRODUCED, None), (Verdict.FIXED_ON_MAIN, None),
     (Verdict.FIXED_ON_MAIN, "not-a-sha")],
)
async def test_a_backport_starts_only_for_fixed_on_main_with_a_known_fix(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, verdict: Verdict, commit: str | None,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment(verdict, commit=commit))
    res = await backport_job(client)
    assert res.status_code == 422 and res.json()["error"] == "not_backportable"
    assert backport_fake.calls == [] and pr_fake.calls == []
    assert (await client.get("/api/status")).json()["busy"] is False
    summary = (await client.get("/api/reports")).json()[0]
    assert summary["can_backport"] is False


async def test_a_backport_example_cannot_be_backported_again(
    client: httpx.AsyncClient, examples_dir: Path, backport_fake: FakeBackport
) -> None:
    example = examples_dir / "real-backport-acme-widgets-42.json"
    example.write_text(make_result().model_dump_json())
    res = await backport_job(client, "real-backport-acme-widgets-42")
    assert res.status_code == 422 and backport_fake.calls == []


async def test_an_example_assessment_can_be_backported_and_saves_under_runs(
    client: httpx.AsyncClient, settings: SimpleNamespace, examples_dir: Path,
    backport_fake: FakeBackport, pr_fake: FakePullRequests,
) -> None:
    """The examples directory is read-only on the server, so results go to the runs dir."""
    (examples_dir / "real-acme-widgets-42.json").write_text(make_assessment().model_dump_json())
    res = await backport_job(client, "real-acme-widgets-42")
    assert res.status_code == 202
    await wait_finished(client, res.json()["id"])
    assert (settings.runs_dir / "backports" / "real-acme-widgets-42.json").exists()
    assert names(examples_dir) == ["real-acme-widgets-42.json"]
    (row,) = (await client.get("/api/reports")).json()
    assert row["backport"]["pr_url"] == "https://github.com/alice/widgets/pull/5"


async def test_a_backport_is_refused_with_409_while_anything_else_is_running(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    write_run(settings.runs_dir, RUN_NEW, make_assessment(number=43))
    backport_fake.gate = asyncio.Event()
    first = await backport_job(client)
    assert first.status_code == 202
    await asyncio.sleep(0.02)

    second = await backport_job(client, RUN_NEW)
    assert second.status_code == 409 and second.json()["error"] == "busy"
    assert "in progress" in second.json()["message"]
    assert (await client.get("/api/status")).json()["busy"] is True
    # a live run is refused while the job holds the slot, too
    async def pipeline(*a: Any, **k: Any) -> Assessment:
        raise AssertionError("must not start")

    monkeypatch.setattr(web, "run_pipeline", pipeline)
    live = await client.post("/api/runs", json={"issue_url": ISSUE})
    assert live.status_code == 429 and live.json()["error"] == "busy"
    assert len(backport_fake.calls) == 1

    backport_fake.gate.set()
    await wait_finished(client, first.json()["id"])
    assert (await backport_job(client, RUN_NEW)).status_code == 202


async def test_a_backport_is_refused_with_409_while_a_live_run_is_going(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    gate = asyncio.Event()

    async def pipeline(issue_url: str, *, settings: Any, progress: Any) -> Assessment:
        await gate.wait()
        return make_assessment()

    monkeypatch.setattr(web, "run_pipeline", pipeline)
    live = await client.post("/api/runs", json={"issue_url": ISSUE})
    assert live.status_code == 202
    res = await backport_job(client)
    assert res.status_code == 409 and backport_fake.calls == []
    gate.set()
    await wait_finished(client, live.json()["id"])


async def test_asking_again_returns_the_saved_pull_request_without_a_new_job(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    await wait_finished(client, (await backport_job(client)).json()["id"])

    again = await backport_job(client)
    assert again.status_code == 200
    body = again.json()
    assert body["report_id"] == RUN_OLD
    assert body["backport"]["pr_url"] == "https://github.com/alice/widgets/pull/5"
    assert len(backport_fake.calls) == 1 and len(pr_fake.calls) == 1
    assert (await client.get("/api/status")).json()["busy"] is False

    # and even while something else is running
    backport_fake.gate = asyncio.Event()
    write_run(settings.runs_dir, RUN_NEW, make_assessment(number=43))
    assert (await backport_job(client, RUN_NEW)).status_code == 202
    await asyncio.sleep(0.02)
    assert (await backport_job(client)).status_code == 200
    backport_fake.gate.set()


async def test_a_backport_that_is_not_verified_gets_no_pull_request_and_may_be_retried(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    backport_fake.result = make_result(BackportStatus.CONFLICTS)
    job = await wait_finished(client, (await backport_job(client)).json()["id"])
    assert job["status"] == "done" and job["result"] == {"status": "conflicts", "pr_url": None}
    assert "No pull request was opened" in job["events"][-1]["message"]
    assert pr_fake.calls == []

    (row,) = (await client.get("/api/reports")).json()
    assert row["backport"] == {"status": "conflicts", "pr_url": None}
    saved = (await client.get(f"/api/reports/{RUN_OLD}")).json()["backport"]
    assert saved["status"] == "conflicts" and saved["pr_url"] is None

    backport_fake.result = make_result()  # the second try may run again and succeed
    retry = await backport_job(client)
    assert retry.status_code == 202
    await wait_finished(client, retry.json()["id"])
    assert len(backport_fake.calls) == 2 and len(pr_fake.calls) == 1


async def test_a_failed_pull_request_keeps_the_verified_result_and_the_retry_reuses_it(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    pr_fake.error = pullrequest.PullRequestError(
        f"GitHub returned 403 for POST /repos/alice/widgets/forks: nope {TOKEN}"
    )
    job = await wait_finished(client, (await backport_job(client)).json()["id"])
    assert job["status"] == "error" and "403" in job["error"]
    assert TOKEN not in json.dumps(job)  # a second scrub, on top of pullrequest.py's own
    saved = json.loads((settings.runs_dir / "backports" / f"{RUN_OLD}.json").read_text())
    assert saved["status"] == "verified" and saved["pr_url"] is None and "403" in saved["pr_error"]
    assert TOKEN not in json.dumps(saved)

    pr_fake.error = None
    retry = await wait_finished(client, (await backport_job(client)).json()["id"])
    assert retry["status"] == "done"
    assert len(backport_fake.calls) == 1  # the sandbox work was not repeated
    assert len(pr_fake.calls) == 2
    assert "Reusing the verified backport" in retry["events"][0]["message"]
    saved = json.loads((settings.runs_dir / "backports" / f"{RUN_OLD}.json").read_text())
    assert saved["pr_url"] == "https://github.com/alice/widgets/pull/5"
    assert saved["pr_error"] is None


async def test_a_server_without_a_github_token_fails_the_job_before_any_sandbox_work(
    tmp_path: Path, examples_dir: Path, backport_fake: FakeBackport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path, github_token="")
    write_run(settings.runs_dir, RUN_OLD, make_assessment())

    def no_gh() -> str:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(pullrequest, "_gh_auth_token", no_gh)
    async with make_client(settings, examples_dir) as client:
        res = await backport_job(client)
        assert res.status_code == 202
        job = await wait_finished(client, res.json()["id"])
    assert job["status"] == "error"
    assert "GITHUB_TOKEN is unset" in job["error"] and "gh is not installed" in job["error"]
    assert job["events"][-1]["stage"] == "error"
    assert backport_fake.calls == []
    assert not (settings.runs_dir / "backports").exists()


async def test_an_unexpected_failure_is_friendly_and_leaks_nothing(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())

    async def boom(*args: Any, **kwargs: Any) -> BackportResult:
        raise RuntimeError(f"sandbox exploded with {TOKEN} in the message")

    monkeypatch.setattr(web, "run_backport", boom)
    job = await wait_finished(client, (await backport_job(client)).json()["id"])
    assert job["status"] == "error" and "details are in the server log" in job["error"]
    assert TOKEN not in json.dumps(job) and "RuntimeError" not in json.dumps(job)
    assert f"backport job {job['id']} failed" in caplog.text
    assert (await client.get("/api/status")).json()["busy"] is False  # the slot is free again


async def test_a_job_that_times_out_says_so(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    monkeypatch.setattr(web, "BACKPORT_TIMEOUT_S", 0.05)
    backport_fake.gate = asyncio.Event()  # never opened
    job = await wait_finished(client, (await backport_job(client)).json()["id"])
    assert job["status"] == "error" and "took longer than" in job["error"]


async def test_a_finished_job_survives_a_restart(
    settings: SimpleNamespace, examples_dir: Path, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    async with make_client(settings, examples_dir) as client:
        job_id = (await backport_job(client)).json()["id"]
        await wait_finished(client, job_id)
    async with make_client(settings, examples_dir) as restarted:
        again = (await restarted.get(f"/api/runs/{job_id}")).json()
    assert again["kind"] == "backport" and again["report_id"] == RUN_OLD
    assert again["result"]["pr_url"] == "https://github.com/alice/widgets/pull/5"


# --- the pull request always goes to the fork ----------------------------------------------


async def test_the_web_app_opens_pull_requests_on_the_fork_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def spy(result: BackportResult, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "pr"

    monkeypatch.setattr(pullrequest, "open_backport_pr", spy)
    got = await web.open_draft_pr(make_result(), token=TOKEN, say=lambda m: None)
    assert got == "pr"
    assert seen["target"] == "fork" and "confirmed_upstream" not in seen
    assert set(seen) == {"token", "target", "say"}
    assert list(inspect.signature(web.open_draft_pr).parameters) == ["result", "token", "say"]


def test_no_code_path_in_the_web_app_names_the_upstream_target() -> None:
    for name in ("app.py", "reports.py"):
        source = (Path(web.__file__).parent / name).read_text(encoding="utf-8")
        code = re.sub(r"#.*", "", re.sub(r'""".*?"""', "", source, flags=re.S))
        assert "confirmed_upstream" not in code and "confirm_upstream" not in code
        assert not re.search(r"""target\s*=\s*["']upstream""", code)
        assert "yes_upstream" not in code


@pytest.fixture
def github():
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as router:
        info = {"fork": True, "parent": {"full_name": "acme/widgets"}, "default_branch": "main"}
        router.get("/user").respond(json={"login": "alice", "id": 7, "name": "Alice"})
        router.get("/repos/alice/widgets").respond(json=info)
        router.get("/repos/alice/widgets/git/ref/heads/main").respond(json={})
        router.post("/repos/alice/widgets/merge-upstream").respond(200, json={})
        router.get("/repos/alice/widgets/branches/8.1.x").respond(json={"commit": {"sha": BASE}})
        router.get("/repos/alice/widgets/pulls").respond(json=[])
        router.get("/repos/acme/widgets/pulls").respond(json=[])
        router.post("/repos/alice/widgets/pulls").respond(
            201, json={"html_url": "https://github.com/alice/widgets/pull/5", "number": 5,
                       "draft": True},
        )
        router.post("/repos/acme/widgets/pulls").respond(
            201, json={"html_url": "https://github.com/acme/widgets/pull/99", "number": 99,
                       "draft": True},
        )
        yield router


async def test_a_job_sent_every_upstream_hint_still_opens_the_draft_in_the_fork(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    github: respx.MockRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``open_backport_pr`` against a mocked GitHub: the only pull request created is
    in alice's fork, whatever the request asks for. Nothing is mocked in between but the push."""
    pushed: list[pullrequest.PushRequest] = []

    async def push(request: pullrequest.PushRequest) -> str:
        pushed.append(request)
        return "e" * 40

    monkeypatch.setattr(pullrequest, "push_with_git", push)
    monkeypatch.setattr(web, "github_token", lambda settings: TOKEN)
    write_run(settings.runs_dir, RUN_OLD, make_assessment())

    res = await client.post(
        f"/api/reports/{RUN_OLD}/backport?target=upstream&pr_target=upstream&yes_upstream=1",
        headers=AUTH,
        json={"target": "upstream", "pr_target": "upstream", "confirmed_upstream": True},
    )
    assert res.status_code == 202
    job = await wait_finished(client, res.json()["id"])

    assert job["status"] == "done"
    assert job["result"]["pr_url"] == "https://github.com/alice/widgets/pull/5"
    posts = [c.request.url.path for c in github.calls if c.request.method == "POST"]
    assert "/repos/alice/widgets/pulls" in posts
    assert "/repos/acme/widgets/pulls" not in posts  # the upstream project saw nothing
    assert not any(p.startswith("/repos/acme/") and "/pulls" in p for p in posts)
    body = json.loads(
        next(c for c in github.calls if c.request.url.path == "/repos/alice/widgets/pulls"
             and c.request.method == "POST").request.content
    )
    assert body["draft"] is True and body["base"] == "8.1.x"
    assert body["body"].rstrip().endswith(
        "Prepared with FixedYet (AI-assisted: NVIDIA Nemotron via Nebius Token Factory); "
        "reviewed and submitted by @alice."
    )
    (push_request,) = pushed
    assert push_request.fork_url == "https://github.com/alice/widgets.git"
    saved = json.loads((settings.runs_dir / "backports" / f"{RUN_OLD}.json").read_text())
    assert saved["pr"]["base_repo"] == "alice/widgets" and saved["pr"]["fork"] == "alice/widgets"


# --- the pages ----------------------------------------------------------------------------


async def test_the_reports_page_is_served_and_uses_relative_urls(
    client: httpx.AsyncClient,
) -> None:
    res = await client.get("/reports")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert (await client.head("/reports")).status_code == 200
    html = res.text
    assert 'href="static/style.css"' in html and 'src="static/common.js"' in html
    assert 'src="static/reports.js"' in html and 'href="./"' in html
    for chip in ("All", "Fixed on main", "Open bug", "Not reproduced", "Other"):
        assert chip in html
    assert "Maintainer sign-in" in html
    assert "<script>" not in html and not re.search(r"""(?:href|src|action)=["']http://""", html)
    index = (await client.get("/")).text
    assert 'href="reports"' in index  # a clear link in the header


async def test_the_reports_page_works_behind_a_prefix(tmp_path: Path) -> None:
    app = web.create_app(make_settings(tmp_path), examples_dir=tmp_path / "x")

    async def prefixed(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, "root_path": "/fixedyet", "path": "/fixedyet" + scope["path"]}
        await app(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=prefixed), base_url="http://test"
    ) as client:
        assert (await client.get("/reports")).status_code == 200
        assert (await client.get("/api/reports")).json() == []
        moved = await client.get("/reports/")
        assert moved.status_code in (307, 308)
        assert moved.headers["location"] == "http://test/fixedyet/reports"


def test_the_new_pages_have_no_absolute_urls_and_no_dark_theme() -> None:
    absolute = re.compile(r"""(?:["'`(]|=)/(?:api|static|healthz|reports)\b""")
    root_relative = re.compile(r"""\b(?:href|src|action)\s*=\s*["']/(?!/)""")
    for name in ("reports.html", "common.js", "reports.js"):
        text = (web.STATIC_DIR / name).read_text(encoding="utf-8")
        assert not absolute.search(text), name
        assert not root_relative.search(text), name
        assert re.findall(r"""(?:fetch|EventSource)\(\s*['`]/""", text) == []
        assert "style=" not in text, name  # the page's content-security-policy has no inline style
        assert not re.search(r"""<script[^>]+src=["']https?:""", text)
        assert not re.search(r"issue[-_ ]?repro", text, re.IGNORECASE)


# --- the URL builders -----------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_github_url_builders_only_make_links_from_valid_parts() -> None:
    """Runs common.js in node's vm, with no DOM, and prints what the builders return."""
    script = """
    const vm = require('vm'), fs = require('fs');
    const ctx = vm.createContext({});
    vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
    const gh = vm.runInContext('FY.gh', ctx);
    const cases = JSON.parse(process.argv[2]);
    console.log(JSON.stringify(cases.map(([fn, ...args]) => gh[fn](...args))));
    """
    sha = "46ea2a3cfc50ae42310c278387be49d165d71581"
    cases = [
        ["commit", "pytest-dev/pytest", sha],
        ["commit", "pytest-dev/pytest", "46EA2A3"],
        ["commit", "pytest-dev/pytest", "xyz"],
        ["commit", "pytest-dev/../pytest", sha],
        ["commit", "javascript:alert(1)//x", sha],
        ["branch", "pytest-dev/pytest", "8.4.x"],
        ["branch", "pytest-dev/pytest", "stable/8.1"],
        ["branch", "pytest-dev/pytest", "a b"],
        ["branch", "pytest-dev/pytest", "../x"],
        ["branch", "pytest-dev/pytest", "x?y=1"],
        ["tag", "pytest-dev/pytest", "8.4.1"],
        ["tag", "pallets/click", "v8.1.7+build"],
        ["ref", "pytest-dev/pytest", "8.4.1"],
        ["ref", "pytest-dev/pytest", "8.4.x"],
        ["ref", "pytest-dev/pytest", "main"],
        ["ref", "pytest-dev/pytest", sha],
        ["ref", "pytest-dev/pytest", "8.4.x (before)"],
        ["ref", "pytest-dev/pytest", "gold_patch"],
        ["issue", "pytest-dev/pytest", 13784],
        ["issue", "pytest-dev/pytest", "13784; drop"],
        ["issue", "pytest-dev/pytest", 0],
        ["repoOf", "https://github.com/pytest-dev/pytest/issues/13784"],
        ["repoOf", "https://evil.example/pytest-dev/pytest/issues/1"],
        ["repoOf", "pytest-dev/pytest"],
        ["repoOf", "pytest-dev/pytest/extra"],
    ]
    done = subprocess.run(
        ["node", "-e", script, str(web.STATIC_DIR / "common.js"), json.dumps(cases)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    got = json.loads(done.stdout)
    base = "https://github.com/pytest-dev/pytest"
    assert got == [
        f"{base}/commit/{sha}",
        f"{base}/commit/46EA2A3",
        None,
        None,
        None,
        f"{base}/tree/8.4.x",
        f"{base}/tree/stable/8.1",
        None,
        None,
        None,
        f"{base}/releases/tag/8.4.1",
        "https://github.com/pallets/click/releases/tag/v8.1.7%2Bbuild",
        f"{base}/releases/tag/8.4.1",
        f"{base}/tree/8.4.x",
        f"{base}/tree/main",
        f"{base}/commit/{sha}",
        f"{base}/tree/8.4.x",
        None,
        f"{base}/issues/13784",
        None,
        None,
        "pytest-dev/pytest",
        None,
        "pytest-dev/pytest",
        None,
    ]
