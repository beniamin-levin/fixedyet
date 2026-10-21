"""The showcase: the bundled examples, the fix an example carries, and how the reports page
orders a report and folds repeated runs of one issue."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fixedyet.models import Assessment, FixResult, FixStatus
from fixedyet.web import app as web
from fixedyet.web import reports

JINJA = "real-pallets-jinja-2244"


def bundled() -> dict[str, dict]:
    return {item["id"]: item for item in web.load_examples(web.default_examples_dir())}


def test_the_showcase_has_no_issue_that_upstream_closed_as_fixed() -> None:
    """pallets/click#2827 was shown as an open bug, but upstream closed it as completed on
    2026-05-16 with an opt-in fix. It must not come back as a showcase."""
    assert not any("click/issues/2827" in item["issue_url"] for item in bundled().values())


def test_the_examples_come_in_the_curated_order() -> None:
    assert list(bundled()) == [
        JINJA,
        "real-backport-pydantic-pydantic-13991",
        "real-pallets-click-3384",
        "real-backport-pytest-dev-pytest-13784",
    ]


def test_the_jinja_example_is_an_open_bug_with_its_verified_fix() -> None:
    item = bundled()[JINJA]
    assessment = Assessment.model_validate(item["assessment"])
    assert assessment.verdict.value == "open_bug" and assessment.confidence.value == "high"
    fix = assessment.fix
    assert fix is not None and fix.status is FixStatus.VERIFIED
    assert fix.files_changed == ["src/jinja2/tests.py"]
    assert (fix.lines_added, fix.lines_removed) == (1, 1)
    assert "not isinstance(value, Undefined)" in fix.diff
    assert fix.suite_before.passed == fix.suite_after.passed == 911 and not fix.newly_failing
    assert "[pallets/jinja#2259](https://github.com/pallets/jinja/pull/2259)" in item["fix_note"]
    assert "## Fix" in item["markdown"] or "Fix" in item["markdown"]


def test_the_pydantic_example_is_the_upstream_backport() -> None:
    item = bundled()["real-backport-pydantic-pydantic-13991"]
    assert item["verdict"] == "backport_verified"
    assert item["backport"]["target_branch"] == "v2.14-fixes"
    assert item["pr_url"] == "https://github.com/pydantic/pydantic/pull/14043"


def test_an_example_without_an_order_follows_the_ranked_ones(tmp_path: Path) -> None:
    folder = tmp_path / "examples"
    folder.mkdir()
    base = Assessment(
        issue_url="https://github.com/a/b/issues/1", title="t", verdict="open_bug",
        confidence="high",
    ).model_dump(mode="json")
    for name, order in (("a-first-by-name", None), ("b-ranked", 2), ("c-ranked", 1)):
        data = dict(base)
        if order is not None:
            data["_example"] = {"order": order}
        (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    assert [i["id"] for i in web.load_examples(folder)] == ["c-ranked", "b-ranked",
                                                            "a-first-by-name"]


def test_an_example_fix_becomes_a_record_marked_as_an_example() -> None:
    fix = FixResult(status=FixStatus.VERIFIED, issue_url="https://github.com/a/b/issues/1")
    record = reports.example_fix_record({"fix": fix.model_dump(mode="json")})
    assert record is not None
    assert record["status"] == "verified" and record["example"] is True
    assert record["prs"] == {} and record["pr_url"] is None
    assert reports.example_fix_record({"fix": None}) is None
    assert reports.example_fix_record(None) is None


@pytest.fixture
async def client(tmp_path: Path):
    settings = SimpleNamespace(
        demo_daily_limit=5, runs_dir=tmp_path / "runs", total_budget_usd=20.0,
        run_budget_usd=0.60,
    )
    app = web.create_app(settings, examples_dir=web.default_examples_dir())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_the_jinja_report_offers_its_verified_fix_to_every_visitor(
    client: httpx.AsyncClient,
) -> None:
    rows = {r["id"]: r for r in (await client.get("/api/reports")).json()}
    assert rows[JINJA]["fix"] == {"status": "verified", "pr_url": None, "pr_target": None}
    detail = (await client.get(f"/api/reports/{JINJA}")).json()
    assert detail["fix"]["status"] == "verified" and detail["fix"]["example"] is True
    assert "not isinstance(value, Undefined)" in detail["fix"]["result"]["diff"]
    assert "pallets/jinja#2259" in detail["fix_note"]


async def test_a_fix_saved_on_the_server_wins_over_the_example_one(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    folder = tmp_path / "runs" / "fixes"
    folder.mkdir(parents=True)
    saved = {"status": "not_fixed", "prs": {}, "result": {"status": "not_fixed"}}
    (folder / f"{JINJA}.json").write_text(json.dumps(saved), encoding="utf-8")
    detail = (await client.get(f"/api/reports/{JINJA}")).json()
    assert detail["fix"]["status"] == "not_fixed" and "example" not in detail["fix"]


# --- the page scripts ---------------------------------------------------------------------


def script(name: str) -> str:
    return (web.STATIC_DIR / name).read_text(encoding="utf-8")


def test_the_report_shows_the_verdict_before_the_action_cards() -> None:
    js = script("reports.js")
    body = js.split("function drawDetail(d) {", 1)[1].split("\n  }\n", 1)[0]
    assert "verdict.after(...actions)" in body
    assert body.index("reportNode(d)") < body.index("backportCard(d)")


def test_a_visitor_sees_the_verified_diff_unfolded() -> None:
    js = script("reports.js")
    view = js.split("function verifiedFixView(", 1)[1].split("\n  }\n", 1)[0]
    assert "codeBlock(result.diff, 'diff')" in view
    assert "details" not in view  # not folded away
    assert "verified && (!state.admin || record.example)" in js


def test_repeated_runs_of_one_issue_fold_under_the_newest() -> None:
    js = script("reports.js")
    assert "function groupByIssue(" in js
    assert "earlier run${earlier.count === 1 ? '' : 's'}" in js
    assert ".report-row.earlier" in script("style.css")
