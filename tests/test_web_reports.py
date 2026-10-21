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

from fixedyet import github, pullrequest
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
    assert row["backport"] == {
        "status": "verified", "pr_url": "https://github.com/me/widgets/pull/1",
        "pr_target": "fork",  # a pull request in some other repository than acme/widgets
    }
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
    ("POST", "/api/reports/real-acme-widgets-42/backport/pr"),
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


# --- step 1: verifying a backport --------------------------------------------------------


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


class FakeActivity:
    """Stands in for ``fetch_target_activity``: what GitHub says about the target branch."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.result: dict[str, Any] | None = {
            "branch": "8.1.x", "last_commit_sha": "c" * 40,
            "last_commit_date": "2026-10-01T09:30:00Z",
            "latest_release": "8.1.9", "latest_release_date": "2026-09-20T08:00:00Z",
            "release_source": "release", "checked_at": "2026-10-21T10:00:00+00:00",
        }

    async def __call__(self, repo: str, branch: str, *, settings: Any):
        self.calls.append((repo, branch))
        if self.error is not None:
            raise self.error
        return self.result


class FakePullRequests:
    """Stands in for ``open_draft_pr``."""

    def __init__(self) -> None:
        self.calls: list[tuple[BackportResult, str]] = []
        self.targets: list[str] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self, result: BackportResult, *, token: str, say: Any, target: str = "fork"):
        self.calls.append((result, token))
        self.targets.append(target)
        say("Opening a draft pull request")
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return pullrequest.PullRequest(
            url="https://github.com/alice/widgets/pull/5", number=5, draft=True,
            base_repo="alice/widgets", base_branch="8.1.x", head="alice:fixedyet/backport-42-8.1.x",
            fork="alice/widgets", login="alice", created=True,
        )


@pytest.fixture
def activity_fake(monkeypatch: pytest.MonkeyPatch) -> FakeActivity:
    fake = FakeActivity()
    monkeypatch.setattr(web, "fetch_target_activity", fake)
    return fake


@pytest.fixture
def backport_fake(monkeypatch: pytest.MonkeyPatch, activity_fake: FakeActivity) -> FakeBackport:
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


def saved_record(settings: SimpleNamespace, report_id: str = RUN_OLD) -> dict:
    return json.loads((settings.runs_dir / "backports" / f"{report_id}.json").read_text())


async def test_verifying_a_backport_saves_the_result_and_opens_no_pull_request(
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
    assert job["result"] == {"status": "verified", "pr_url": None}
    assert job["markdown"] == "# rendered backport verified"
    messages = [e["message"] for e in job["events"]]
    assert messages[:2] == ["Listing the branches", "verified"]
    assert "Checking how active 8.1.x is on acme/widgets" in messages
    assert messages[-1] == "Verified. Read the patch, then open the draft pull request."
    assert job["events"][-1]["stage"] == "done"

    (assessment,) = backport_fake.calls
    assert assessment.issue_url == ISSUE and assessment.reproduction.fixing_commit == FIX
    assert pr_fake.calls == []  # step 1 never opens a pull request

    # saved where the examples dir does not matter: <runs_dir>/backports/<id>.json
    record = saved_record(settings)
    assert record["status"] == "verified" and record["report_id"] == RUN_OLD
    assert (record["pr_url"], record["pr_target"], record["pr_number"]) == (None, None, None)
    assert record["prs"] == {}
    assert record["result"]["target_branch"] == "8.1.x" and record["result"]["patch"]
    assert record["target_activity"]["latest_release"] == "8.1.9"
    assert TOKEN not in json.dumps(record) and TOKEN not in json.dumps(job)

    # visible to everyone: in the list (no pull request yet) and on the detail page
    (row,) = [r for r in (await client.get("/api/reports")).json() if r["id"] == RUN_OLD]
    assert row["backport"] == {"status": "verified", "pr_url": None, "pr_target": None}
    detail = (await client.get(f"/api/reports/{RUN_OLD}")).json()
    assert detail["backport"]["status"] == "verified" and detail["backport"]["pr_url"] is None
    assert detail["backport"]["result"]["status"] == "verified"


async def test_a_backport_job_streams_its_progress_like_a_run(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    job_id = (await backport_job(client)).json()["id"]
    await wait_finished(client, job_id)
    text = (await client.get(f"/api/runs/{job_id}/events")).text
    assert text.count("event: progress") >= 4 and "event: done" in text
    assert "Verified. Read the patch" in text


async def test_a_backport_does_not_use_the_daily_limit(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
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
    verdict: Verdict, commit: str | None,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment(verdict, commit=commit))
    res = await backport_job(client)
    assert res.status_code == 422 and res.json()["error"] == "not_backportable"
    assert backport_fake.calls == []
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


async def test_an_example_assessment_can_be_verified_and_saves_under_runs(
    client: httpx.AsyncClient, settings: SimpleNamespace, examples_dir: Path,
    backport_fake: FakeBackport,
) -> None:
    """The examples directory is read-only on the server, so results go to the runs dir."""
    (examples_dir / "real-acme-widgets-42.json").write_text(make_assessment().model_dump_json())
    res = await backport_job(client, "real-acme-widgets-42")
    assert res.status_code == 202
    await wait_finished(client, res.json()["id"])
    assert (settings.runs_dir / "backports" / "real-acme-widgets-42.json").exists()
    assert names(examples_dir) == ["real-acme-widgets-42.json"]
    (row,) = (await client.get("/api/reports")).json()
    assert row["backport"] == {"status": "verified", "pr_url": None, "pr_target": None}


async def test_a_backport_is_refused_with_409_while_anything_else_is_running(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    monkeypatch: pytest.MonkeyPatch,
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

    async def pipeline(*a: Any, **k: Any) -> Assessment:
        raise AssertionError("must not start")

    monkeypatch.setattr(web, "run_pipeline", pipeline)  # a live run is refused too
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


async def test_a_verified_backport_is_not_run_again(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    await wait_finished(client, (await backport_job(client)).json()["id"])

    again = await backport_job(client)
    assert again.status_code == 200
    body = again.json()
    assert body["report_id"] == RUN_OLD and body["backport"]["status"] == "verified"
    assert body["backport"]["result"]["target_branch"] == "8.1.x"
    assert len(backport_fake.calls) == 1
    assert (await client.get("/api/status")).json()["busy"] is False

    # and even while something else is running
    backport_fake.gate = asyncio.Event()
    write_run(settings.runs_dir, RUN_NEW, make_assessment(number=43))
    assert (await backport_job(client, RUN_NEW)).status_code == 202
    await asyncio.sleep(0.02)
    assert (await backport_job(client)).status_code == 200
    backport_fake.gate.set()


async def test_a_backport_that_is_not_verified_may_be_run_again(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    backport_fake.result = make_result(BackportStatus.CONFLICTS)
    job = await wait_finished(client, (await backport_job(client)).json()["id"])
    assert job["status"] == "done" and job["result"] == {"status": "conflicts", "pr_url": None}
    assert "Backport conflicts" in job["events"][-1]["message"]

    (row,) = (await client.get("/api/reports")).json()
    assert row["backport"] == {"status": "conflicts", "pr_url": None, "pr_target": None}
    saved = (await client.get(f"/api/reports/{RUN_OLD}")).json()["backport"]
    assert saved["status"] == "conflicts" and saved["pr_url"] is None

    backport_fake.result = make_result()  # the second try runs again, and may succeed
    retry = await backport_job(client)
    assert retry.status_code == 202
    await wait_finished(client, retry.json()["id"])
    assert len(backport_fake.calls) == 2 and saved_record(settings)["status"] == "verified"


async def test_the_activity_of_the_target_branch_is_recorded_with_the_result(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    activity_fake: FakeActivity,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    await wait_finished(client, (await backport_job(client)).json()["id"])
    assert activity_fake.calls == [("acme/widgets", "8.1.x")]
    assert saved_record(settings)["target_activity"] == activity_fake.result
    detail = (await client.get(f"/api/reports/{RUN_OLD}")).json()["backport"]
    assert detail["target_activity"]["last_commit_date"] == "2026-10-01T09:30:00Z"
    assert detail["target_warning"] is None  # a recent commit, and 8.1.9 is the branch's own line

    activity_fake.result = {**activity_fake.result, "last_commit_date": "2024-11-03T09:30:00Z"}
    other = write_run(settings.runs_dir, RUN_NEW, make_assessment(number=43))
    assert other.exists()
    await wait_finished(client, (await backport_job(client, RUN_NEW)).json()["id"])
    stale = (await client.get(f"/api/reports/{RUN_NEW}")).json()["backport"]["target_warning"]
    assert stale["reasons"] == ["stale"]
    assert stale["text"] == (
        "Last commit on 8.1.x: 2024-11-03; latest release: 8.1.9. "
        "Maintainers may not accept backports to this branch."
    )


@pytest.mark.parametrize("failure", ["raises", "none"])
async def test_a_failure_reading_the_branch_activity_never_fails_the_job(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    activity_fake: FakeActivity, failure: str,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    if failure == "raises":
        activity_fake.error = RuntimeError(f"GitHub exploded with {TOKEN}")
    else:
        activity_fake.result = None
    job = await wait_finished(client, (await backport_job(client)).json()["id"])
    assert job["status"] == "done" and job["error"] is None
    record = saved_record(settings)
    assert record["status"] == "verified" and record["target_activity"] is None
    assert TOKEN not in json.dumps(record) and TOKEN not in json.dumps(job)
    detail = (await client.get(f"/api/reports/{RUN_OLD}")).json()["backport"]
    assert detail["target_activity"] is None and detail["target_warning"] is None


async def test_the_real_activity_seam_reads_github_with_the_token_and_swallows_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, str | None]] = []

    async def fake_read(repo: str, branch: str, *, token: str | None = None):
        seen.append((repo, branch, token))
        return {"branch": branch}

    monkeypatch.setattr(github, "target_activity", fake_read)
    monkeypatch.setattr(web, "github_token", lambda settings: TOKEN)
    got = await web.fetch_target_activity("acme/widgets", "8.1.x", settings=object())
    assert got == {"branch": "8.1.x"} and seen == [("acme/widgets", "8.1.x", TOKEN)]

    def no_token(settings: Any) -> str:
        raise pullrequest.PullRequestError("no GitHub token")

    async def explode(*args: Any, **kwargs: Any):
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "github_token", no_token)
    assert (await web.fetch_target_activity("a/b", "1.x", settings=object())) == {"branch": "1.x"}
    assert seen[-1] == ("a/b", "1.x", None)  # no token: an anonymous read of public data
    monkeypatch.setattr(github, "target_activity", explode)
    assert await web.fetch_target_activity("a/b", "1.x", settings=object()) is None


async def test_a_server_without_a_github_token_still_verifies(
    tmp_path: Path, examples_dir: Path, backport_fake: FakeBackport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only step 2 needs the token."""
    settings = make_settings(tmp_path, github_token="")
    write_run(settings.runs_dir, RUN_OLD, make_assessment())

    def no_gh() -> str:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(pullrequest, "_gh_auth_token", no_gh)
    async with make_client(settings, examples_dir) as client:
        job = await wait_finished(client, (await backport_job(client)).json()["id"])
        assert job["status"] == "done" and job["result"]["status"] == "verified"
        res = await client.post(
            f"/api/reports/{RUN_OLD}/backport/pr", headers=AUTH, json={"target": "fork"}
        )
    assert res.status_code == 503 and res.json()["error"] == "no_github_token"
    assert "GITHUB_TOKEN is unset" in res.json()["message"]
    assert saved_record(settings)["pr_url"] is None


async def test_an_unexpected_failure_is_friendly_and_leaks_nothing(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    monkeypatch.setattr(web, "BACKPORT_TIMEOUT_S", 0.05)
    backport_fake.gate = asyncio.Event()  # never opened
    job = await wait_finished(client, (await backport_job(client)).json()["id"])
    assert job["status"] == "error" and "took longer than" in job["error"]


async def test_a_finished_job_survives_a_restart(
    settings: SimpleNamespace, examples_dir: Path, backport_fake: FakeBackport,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    async with make_client(settings, examples_dir) as client:
        job_id = (await backport_job(client)).json()["id"]
        await wait_finished(client, job_id)
    async with make_client(settings, examples_dir) as restarted:
        again = (await restarted.get(f"/api/runs/{job_id}")).json()
    assert again["kind"] == "backport" and again["report_id"] == RUN_OLD
    assert again["result"] == {"status": "verified", "pr_url": None}


# --- the rule for an inactive target branch ------------------------------------------------

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def activity(commit: str | None = "2026-10-01T09:30:00Z", release: str | None = "8.4.9") -> dict:
    return {"last_commit_date": commit, "latest_release": release}


@pytest.mark.parametrize(
    ("data", "branch", "reasons"),
    [
        (activity(), "8.4.x", None),  # recent, and the release is the branch's own line
        (activity("2026-04-24T12:00:00Z"), "8.4.x", None),  # exactly 180 days: not more
        (activity("2026-04-24T11:59:00Z"), "8.4.x", ["stale"]),  # a minute past 180 days
        (activity(release="9.1.1"), "8.4.x", ["superseded"]),
        (activity(release="8.5.0"), "8.4.x", ["superseded"]),  # a newer minor is enough
        (activity(release="8.4.10"), "8.4.x", None),  # a newer patch is not
        (activity(release="8.3.2"), "8.4.x", None),  # an older line is not
        (activity(release="v9.0.0"), "stable/8.1", ["superseded"]),
        (activity(release="4.0.0"), "3.x", ["superseded"]),  # a branch with only a major
        (activity(release="3.9.0"), "3.x", None),
        (activity("2024-11-03T00:00:00Z", "9.1.1"), "8.4.x", ["stale", "superseded"]),
        (activity("2024-11-03T00:00:00+02:00"), "main", ["stale"]),  # no version in the name
        (activity(release="9.0.0"), "main", None),
        (activity(None, "9.1.1"), "8.4.x", ["superseded"]),
        (activity(None, None), "8.4.x", None),  # nothing known, nothing to warn about
        (activity("not a date"), "8.4.x", None),
        ({}, "8.4.x", None),
        (None, "8.4.x", None),
        ("junk", "8.4.x", None),
        (activity(release="9.1.1"), None, None),
    ],
)
def test_the_inactive_branch_rule(data: Any, branch: str | None, reasons: list | None) -> None:
    warning = reports.target_warning(data, branch, now=NOW)
    assert (warning["reasons"] if warning else None) == reasons


def test_the_warning_names_the_date_and_the_latest_release() -> None:
    warning = reports.target_warning(
        activity("2025-11-03T10:00:00Z", "9.1.1"), "8.4.x", now=NOW
    )
    assert warning is not None and warning["text"] == (
        "Last commit on 8.4.x: 2025-11-03; latest release: 9.1.1. "
        "Maintainers may not accept backports to this branch."
    )
    unknown = reports.target_warning(activity(None, "9.1.1"), "8.4.x", now=NOW)
    assert unknown is not None
    assert unknown["text"].startswith("Last commit on 8.4.x: unknown; latest release: 9.1.1.")


# --- step 2: the draft pull request --------------------------------------------------------


async def verified(client: httpx.AsyncClient, settings: SimpleNamespace) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    await wait_finished(client, (await backport_job(client)).json()["id"])


async def open_pr(
    client: httpx.AsyncClient, body: Any = None, *, report_id: str = RUN_OLD
) -> httpx.Response:
    return await client.post(
        f"/api/reports/{report_id}/backport/pr", headers=AUTH,
        json={"target": "fork"} if body is None else body,
    )


async def test_step_2_opens_a_draft_pr_in_the_fork_and_saves_it(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    await verified(client, settings)
    res = await open_pr(client)
    assert res.status_code == 200
    body = res.json()
    assert body["target"] == "fork" and body["created"] is True
    assert body["pr_url"] == "https://github.com/alice/widgets/pull/5"
    assert body["backport"]["pr_url"] == body["pr_url"]
    ((result, token),) = pr_fake.calls
    assert result.status is BackportStatus.VERIFIED and token == TOKEN

    record = saved_record(settings)
    assert record["pr_url"] == "https://github.com/alice/widgets/pull/5"
    assert (record["pr_target"], record["pr_number"]) == ("fork", 5)
    fork = record["prs"]["fork"]
    assert fork["url"] == record["pr_url"] and fork["draft"] is True
    assert fork["base_repo"] == "alice/widgets"  # the fork, not acme/widgets
    assert fork["number"] == 5
    assert record["status"] == "verified" and record["target_activity"]  # step 1's data is kept
    assert TOKEN not in json.dumps(record) and TOKEN not in res.text

    (row,) = [r for r in (await client.get("/api/reports")).json() if r["id"] == RUN_OLD]
    assert row["backport"] == {
        "status": "verified", "pr_url": "https://github.com/alice/widgets/pull/5",
        "pr_target": "fork",
    }
    detail = (await client.get(f"/api/reports/{RUN_OLD}")).json()["backport"]
    assert detail["pr_url"] == row["backport"]["pr_url"] and detail["pr_target"] == "fork"


async def test_step_2_is_idempotent_per_target(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    await verified(client, settings)
    await open_pr(client)
    again = await open_pr(client)
    assert again.status_code == 200
    assert again.json()["pr_url"] == "https://github.com/alice/widgets/pull/5"
    assert again.json()["created"] is False
    assert len(pr_fake.calls) == 1 and len(backport_fake.calls) == 1


async def test_step_2_refuses_a_backport_that_is_not_verified(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    res = await open_pr(client)  # nothing was verified yet
    assert res.status_code == 409 and res.json()["error"] == "not_verified"

    backport_fake.result = make_result(BackportStatus.CONFLICTS)
    await wait_finished(client, (await backport_job(client)).json()["id"])
    res = await open_pr(client)
    assert res.status_code == 409 and res.json()["error"] == "not_verified"
    assert pr_fake.calls == []
    assert saved_record(settings)["pr_url"] is None


async def test_step_2_refuses_an_unknown_report_and_a_stranger(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    assert (await open_pr(client, report_id="20261010T100000Z-nobody-1")).status_code == 404
    await verified(client, settings)
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        res = await client.post(
            f"/api/reports/{RUN_OLD}/backport/pr", headers=headers, json={"target": "fork"}
        )
        assert res.status_code == 401
    assert pr_fake.calls == []


@pytest.mark.parametrize(
    "body",
    [None, {}, {"target": None}, {"target": "origin"}, {"target": "UPSTREAM"}, ["fork"], "fork"],
)
async def test_step_2_takes_only_the_fork_or_the_upstream_as_a_target(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, body: Any,
) -> None:
    await verified(client, settings)
    res = await client.post(
        f"/api/reports/{RUN_OLD}/backport/pr", headers=AUTH,
        **({} if body is None else {"json": body}),
    )
    assert res.status_code == 400 and res.json()["error"] == "invalid_request"
    assert pr_fake.calls == []


async def test_step_2_without_a_token_is_a_503_and_changes_nothing(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await verified(client, settings)

    def none(settings: Any) -> str:
        raise pullrequest.PullRequestError("no GitHub token")

    monkeypatch.setattr(web, "github_token", none)
    res = await open_pr(client)
    assert res.status_code == 503 and res.json()["error"] == "no_github_token"
    assert pr_fake.calls == [] and saved_record(settings)["pr_url"] is None


async def test_a_failed_pull_request_is_reported_scrubbed_and_may_be_retried(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, caplog: pytest.LogCaptureFixture,
) -> None:
    await verified(client, settings)
    pr_fake.error = pullrequest.PullRequestError(
        f"GitHub returned 403 for POST /repos/alice/widgets/forks: nope {TOKEN}"
    )
    res = await open_pr(client)
    assert res.status_code == 502 and res.json()["error"] == "pull_request_failed"
    assert "403" in res.json()["message"] and TOKEN not in res.text
    assert saved_record(settings)["pr_url"] is None  # still verified, still without a PR
    assert saved_record(settings)["status"] == "verified"

    pr_fake.error = RuntimeError(f"boom with {TOKEN}")
    res = await open_pr(client)
    assert res.status_code == 502 and "details are in the server log" in res.json()["message"]
    assert TOKEN not in res.text and "RuntimeError" not in res.text

    pr_fake.error = None
    ok = await open_pr(client)
    assert ok.status_code == 200 and ok.json()["created"] is True
    assert len(backport_fake.calls) == 1  # the sandbox work was never repeated


async def test_a_slow_github_is_a_504_and_may_be_retried(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await verified(client, settings)
    monkeypatch.setattr(web, "PULL_REQUEST_TIMEOUT_S", 0.05)
    pr_fake.gate = asyncio.Event()  # never opened
    res = await open_pr(client)
    assert res.status_code == 504 and res.json()["error"] == "timeout"
    assert saved_record(settings)["pr_url"] is None
    pr_fake.gate.set()
    assert (await open_pr(client)).status_code == 200


async def test_a_record_from_the_first_deployment_counts_as_a_fork_pull_request(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    """Before targets existed the job opened a pull request in the fork and saved ``pr``."""
    write_run(settings.runs_dir, RUN_OLD, make_assessment())
    (settings.runs_dir / "backports").mkdir(parents=True)
    old = {
        "report_id": RUN_OLD, "issue_url": ISSUE, "status": "verified",
        "pr_url": "https://github.com/alice/widgets/pull/3",
        "pr": {"number": 3, "base_repo": "alice/widgets", "draft": True},
        "pr_error": None, "result": make_result().model_dump(mode="json"),
    }
    (settings.runs_dir / "backports" / f"{RUN_OLD}.json").write_text(json.dumps(old))

    (row,) = (await client.get("/api/reports")).json()
    assert row["backport"] == {
        "status": "verified", "pr_url": "https://github.com/alice/widgets/pull/3",
        "pr_target": "fork",
    }
    detail = (await client.get(f"/api/reports/{RUN_OLD}")).json()["backport"]
    assert detail["prs"]["fork"]["url"] == "https://github.com/alice/widgets/pull/3"
    assert detail["pr_number"] == 3 and detail["target_warning"] is None
    # verified already: step 1 is not redone, and the fork's pull request is returned
    assert (await backport_job(client)).status_code == 200 and backport_fake.calls == []
    again = await open_pr(client)
    assert again.json()["created"] is False and pr_fake.calls == []


# --- the pull request: the fork by default, the upstream project only when confirmed --------


@pytest.mark.parametrize(
    ("target", "expected", "confirmed"),
    [(None, "fork", False), ("fork", "fork", False), ("upstream", "upstream", True),
     ("anything-else", "fork", False)],
)
async def test_open_draft_pr_confirms_the_upstream_only_when_asked_for_it(
    monkeypatch: pytest.MonkeyPatch, target: str | None, expected: str, confirmed: bool,
) -> None:
    seen: dict[str, Any] = {}

    async def spy(result: BackportResult, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "pr"

    monkeypatch.setattr(pullrequest, "open_backport_pr", spy)
    kwargs = {} if target is None else {"target": target}
    got = await web.open_draft_pr(make_result(), token=TOKEN, say=lambda m: None, **kwargs)
    assert got == "pr"
    assert seen["target"] == expected and seen["confirmed_upstream"] is confirmed


@pytest.mark.parametrize(
    "body",
    [{"target": "upstream"}, {"target": "upstream", "confirm": ""},
     {"target": "upstream", "confirm": "acme/other"}, {"target": "upstream", "confirm": 7},
     {"target": "upstream", "confirm": "alice/widgets"}],
)
async def test_an_upstream_pr_needs_the_repository_typed_exactly(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests, body: Any,
) -> None:
    await verified(client, settings)
    res = await client.post(f"/api/reports/{RUN_OLD}/backport/pr", headers=AUTH, json=body)
    assert res.status_code == 400 and res.json()["error"] == "confirmation_mismatch"
    assert "acme/widgets" in res.json()["message"]
    assert pr_fake.calls == []


async def test_a_confirmed_upstream_pr_is_opened_and_saved_beside_the_fork_one(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    pr_fake: FakePullRequests,
) -> None:
    await verified(client, settings)
    res = await client.post(
        f"/api/reports/{RUN_OLD}/backport/pr", headers=AUTH,
        json={"target": "upstream", "confirm": "  ACME/Widgets "},
    )
    assert res.status_code == 200 and res.json()["target"] == "upstream"
    assert pr_fake.targets == ["upstream"]
    again = await client.post(
        f"/api/reports/{RUN_OLD}/backport/pr", headers=AUTH,
        json={"target": "upstream", "confirm": "acme/widgets"},
    )
    assert again.status_code == 200 and again.json()["created"] is False
    assert pr_fake.targets == ["upstream"]  # idempotent per target
    assert set(saved_record(settings)["prs"]) == {"upstream"}


@pytest.fixture
def github_api():
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


async def test_step_2_against_a_mocked_github_opens_a_draft_in_the_fork(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    github_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``open_backport_pr`` against a mocked GitHub. Nothing is faked in between but
    the git push. An unconfirmed upstream request is refused before any GitHub call; the fork
    request makes the only pull request there is, a draft with the disclosure line."""
    pushed: list[pullrequest.PushRequest] = []

    async def push(request: pullrequest.PushRequest) -> str:
        pushed.append(request)
        return "e" * 40

    monkeypatch.setattr(pullrequest, "push_with_git", push)
    monkeypatch.setattr(web, "github_token", lambda settings: TOKEN)
    await verified(client, settings)

    refused = await client.post(
        f"/api/reports/{RUN_OLD}/backport/pr?target=upstream&pr_target=upstream&yes_upstream=1",
        headers=AUTH, json={"target": "upstream", "confirm": "alice/widgets"},
    )
    assert refused.status_code == 400 and len(github_api.calls) == 0

    res = await open_pr(client)
    assert res.status_code == 200
    assert res.json()["pr_url"] == "https://github.com/alice/widgets/pull/5"
    posts = [c.request.url.path for c in github_api.calls if c.request.method == "POST"]
    assert "/repos/alice/widgets/pulls" in posts
    assert not any(p.startswith("/repos/acme/") and "/pulls" in p for p in posts)
    body = json.loads(
        next(c for c in github_api.calls if c.request.url.path == "/repos/alice/widgets/pulls"
             and c.request.method == "POST").request.content
    )
    assert body["draft"] is True and body["base"] == "8.1.x"
    assert body["head"] == "alice:fixedyet/backport-42-8.1.x"
    assert body["body"].rstrip().endswith(
        "Prepared with FixedYet (AI-assisted: NVIDIA Nemotron via Nebius Token Factory); "
        "reviewed and submitted by @alice."
    )
    (push_request,) = pushed
    assert push_request.fork_url == "https://github.com/alice/widgets.git"
    assert saved_record(settings)["prs"]["fork"]["base_repo"] == "alice/widgets"


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


async def test_a_confirmed_upstream_pr_against_a_mocked_github_is_a_draft_on_the_project(
    client: httpx.AsyncClient, settings: SimpleNamespace, backport_fake: FakeBackport,
    github_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``open_backport_pr``: the branch goes to the fork, the draft opens on upstream
    with ``head`` naming the fork's owner, and the body ends with the disclosure line."""

    async def push(request: pullrequest.PushRequest) -> str:
        return "e" * 40

    monkeypatch.setattr(pullrequest, "push_with_git", push)
    monkeypatch.setattr(web, "github_token", lambda settings: TOKEN)
    await verified(client, settings)
    res = await client.post(
        f"/api/reports/{RUN_OLD}/backport/pr", headers=AUTH,
        json={"target": "upstream", "confirm": "acme/widgets"},
    )
    assert res.status_code == 200
    assert res.json()["pr_url"] == "https://github.com/acme/widgets/pull/99"
    post = next(c for c in github_api.calls
                if c.request.method == "POST" and c.request.url.path == "/repos/acme/widgets/pulls")
    body = json.loads(post.request.content)
    assert body["draft"] is True and body["base"] == "8.1.x"
    assert body["head"] == "alice:fixedyet/backport-42-8.1.x"
    assert "reviewed and submitted by @alice." in body["body"]
    assert saved_record(settings)["prs"]["upstream"]["base_repo"] == "acme/widgets"
