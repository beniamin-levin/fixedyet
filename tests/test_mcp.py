"""Tests for the MCP server tools, with the pipeline replaced by a fake."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fixedyet import mcp_server
from fixedyet.models import (
    Assessment,
    Confidence,
    NotReproducedReason,
    Outcome,
    Reproduction,
    SideRun,
    Verdict,
)

ISSUE = "https://github.com/psf/requests/issues/6000"


def make_assessment(
    verdict: Verdict = Verdict.FIXED_ON_MAIN,
    confidence: Confidence = Confidence.HIGH,
    reproduction: Reproduction | None = None,
    not_reproduced_reason: NotReproducedReason | None = None,
) -> Assessment:
    return Assessment(
        issue_url=ISSUE,
        title="Fake issue",
        verdict=verdict,
        confidence=confidence,
        not_reproduced_reason=not_reproduced_reason,
        reproduction=reproduction
        or Reproduction(
            repro_test_code="def test_it():\n    assert 1 == 2\n",
            reported_side=SideRun(ref="v2.31.0", outcomes=[Outcome.FAIL, Outcome.FAIL]),
            fix_side=SideRun(ref="main", outcomes=[Outcome.PASS, Outcome.PASS]),
        ),
    )


class FakePipeline:
    def __init__(self, assessment: Assessment) -> None:
        self.assessment = assessment
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, issue_url: str, fix_ref: str | None) -> Assessment:
        self.calls.append((issue_url, fix_ref))
        return self.assessment


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> FakePipeline:
    fake = FakePipeline(make_assessment())
    monkeypatch.setattr(mcp_server, "run_pipeline", fake)
    monkeypatch.setattr(mcp_server, "render_report", lambda a: f"# {a.title}\n\n{a.verdict.value}")
    return fake


def json_block(text: str) -> dict:
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    assert match, "no JSON block in the output"
    return json.loads(match.group(1))


async def test_reproduce_issue_returns_markdown_then_json(pipeline: FakePipeline) -> None:
    out = await mcp_server.reproduce_issue(ISSUE)

    assert out.startswith("# Fake issue\n\nfixed_on_main")
    assert out.index("```json") > out.index("fixed_on_main")
    data = json_block(out)
    assessment = Assessment.model_validate(data)
    assert assessment.verdict is Verdict.FIXED_ON_MAIN
    assert data["reproduction"]["reported_side"]["outcomes"] == ["fail", "fail"]
    assert pipeline.calls == [(ISSUE, None)]


async def test_reproduce_issue_passes_fix_ref_and_cleans_the_url(pipeline: FakePipeline) -> None:
    await mcp_server.reproduce_issue(f"{ISSUE}#issuecomment-9", fix_ref=" v2.32.0 ")
    assert pipeline.calls == [(ISSUE, "v2.32.0")]


async def test_blank_fix_ref_means_default_branch(pipeline: FakePipeline) -> None:
    await mcp_server.reproduce_issue(ISSUE, fix_ref="  ")
    assert pipeline.calls == [(ISSUE, None)]


async def test_a_broken_renderer_does_not_hide_the_verdict(
    pipeline: FakePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: Assessment) -> str:
        raise ValueError("renderer bug")

    monkeypatch.setattr(mcp_server, "render_report", broken)
    out = await mcp_server.reproduce_issue(ISSUE)
    assert "Verdict: fixed_on_main" in out
    assert json_block(out)["verdict"] == "fixed_on_main"


@pytest.mark.parametrize(
    ("verdict", "fixed"),
    [
        (Verdict.FIXED_ON_MAIN, True),
        (Verdict.OPEN_BUG, False),
        (Verdict.NOT_REPRODUCED, None),
        (Verdict.INCONSISTENT, None),
        (Verdict.NEEDS_INFO, None),
        (Verdict.UNSUPPORTED, None),
    ],
)
async def test_check_fixed_maps_every_verdict(
    pipeline: FakePipeline, verdict: Verdict, fixed: bool | None
) -> None:
    pipeline.assessment = make_assessment(verdict)

    result = await mcp_server.check_fixed(ISSUE, "v2.32.0")

    assert result["fixed"] is fixed
    assert result["verdict"] == verdict.value
    assert result["confidence"] == "high"
    assert isinstance(result["summary"], str)
    assert result["summary"]
    assert set(result) == {"fixed", "verdict", "confidence", "summary"}
    assert pipeline.calls == [(ISSUE, "v2.32.0")]


async def test_check_fixed_summary_names_the_ref_and_the_fix(pipeline: FakePipeline) -> None:
    pipeline.assessment = make_assessment(
        reproduction=Reproduction(
            reported_side=SideRun(ref="v2.31.0", outcomes=[Outcome.FAIL]),
            fix_side=SideRun(ref="v2.32.0", outcomes=[Outcome.PASS]),
            fixing_commit="abc1234",
            fixed_in_release="2.32.0",
        )
    )
    result = await mcp_server.check_fixed(ISSUE, "v2.32.0")
    assert "v2.32.0" in result["summary"]
    assert "abc1234" in result["summary"]
    assert "2.32.0" in result["summary"]


async def test_check_fixed_summary_flags_low_confidence(pipeline: FakePipeline) -> None:
    pipeline.assessment = make_assessment(Verdict.OPEN_BUG, Confidence.LOW)
    result = await mcp_server.check_fixed(ISSUE, "main")
    assert result["confidence"] == "low"
    assert "Confidence is low" in result["summary"]


async def test_not_reproduced_summary_gives_the_reason(pipeline: FakePipeline) -> None:
    pipeline.assessment = make_assessment(
        Verdict.NOT_REPRODUCED, not_reproduced_reason=NotReproducedReason.CONFIGURATION
    )
    result = await mcp_server.check_fixed(ISSUE, "main")
    assert result["fixed"] is None
    assert "configuration" in result["summary"]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/psf/requests/issues/1",
        "https://github.com/psf/requests/pull/1",
        "file:///etc/passwd",
    ],
)
async def test_bad_urls_are_rejected_before_any_run(pipeline: FakePipeline, url: str) -> None:
    with pytest.raises(ValueError, match="GitHub issue URL"):
        await mcp_server.reproduce_issue(url)
    with pytest.raises(ValueError, match="GitHub issue URL"):
        await mcp_server.check_fixed(url, "main")
    assert pipeline.calls == []


@pytest.mark.parametrize(
    "ref", ["", "  ", "--upload-pack=x", "main; rm -rf /", "a b", "$(whoami)", "x..y", "`id`"]
)
async def test_bad_refs_are_rejected_before_any_run(pipeline: FakePipeline, ref: str) -> None:
    with pytest.raises(ValueError, match="ref"):
        await mcp_server.check_fixed(ISSUE, ref)
    if ref.strip():
        with pytest.raises(ValueError, match="git ref"):
            await mcp_server.reproduce_issue(ISSUE, fix_ref=ref)
    assert pipeline.calls == []


@pytest.mark.parametrize(
    "ref", ["main", "v2.32.0", "release/2.x", "3f9c2ab", "feature_x", "HEAD~1"]
)
async def test_ordinary_refs_are_accepted(pipeline: FakePipeline, ref: str) -> None:
    await mcp_server.check_fixed(ISSUE, ref)
    assert pipeline.calls == [(ISSUE, ref)]


async def test_both_tools_are_registered_with_descriptions() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    assert set(tools) == {"reproduce_issue", "check_fixed", "propose_backport", "propose_fix"}
    assert "sandbox" in tools["reproduce_issue"].description.lower()
    assert "fixed" in tools["check_fixed"].description.lower()


async def test_tools_can_be_called_through_the_server(pipeline: FakePipeline) -> None:
    result = await mcp_server.mcp.call_tool("check_fixed", {"issue_url": ISSUE, "ref": "main"})
    text = repr(result)
    assert "fixed_on_main" in text
    assert pipeline.calls == [(ISSUE, "main")]


def test_main_runs_the_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_run(transport: str = "stdio", **_: object) -> None:
        seen.append(transport)

    monkeypatch.setattr(mcp_server.mcp, "run", fake_run)
    mcp_server.main()
    assert seen == ["stdio"]


# --- propose_backport ---------------------------------------------------------------------


def make_backport(status: str = "verified"):
    from fixedyet.models import BackportResult, BackportStatus

    return BackportResult(
        status=BackportStatus(status), reason=f"it is {status}", issue_url=ISSUE,
        title="Fake issue", repo="psf/requests", reported_version="2.31.0",
        target_branch="2.31.x", fixing_commit="d959898db264aaf07e70ad4eafa254286f9a5185",
    )


class FakeBackport:
    def __init__(self, status: str = "verified") -> None:
        self.status = status
        self.calls: list[tuple[Assessment, str | None]] = []

    async def __call__(self, assessment: Assessment, target_branch: str | None):
        self.calls.append((assessment, target_branch))
        return make_backport(self.status)


@pytest.fixture
def backports(monkeypatch: pytest.MonkeyPatch, pipeline: FakePipeline) -> FakeBackport:
    fake = FakeBackport()
    monkeypatch.setattr(mcp_server, "run_backport", fake)
    return fake


async def test_propose_backport_reproduces_then_returns_markdown_and_json(
    pipeline: FakePipeline, backports: FakeBackport
) -> None:
    out = await mcp_server.propose_backport(ISSUE, "2.31.x")

    assert out.startswith("# Verified backport: the fix applies to 2.31.x")
    data = json_block(out)
    assert data["status"] == "verified" and data["target_branch"] == "2.31.x"
    assert pipeline.calls == [(ISSUE, None)]  # the fix side is the default branch
    (assessment, branch) = backports.calls[0]
    assert assessment.verdict is Verdict.FIXED_ON_MAIN and branch == "2.31.x"


async def test_propose_backport_without_a_branch_lets_the_detection_choose(
    pipeline: FakePipeline, backports: FakeBackport
) -> None:
    await mcp_server.propose_backport(ISSUE)
    await mcp_server.propose_backport(ISSUE, "  ")
    assert [branch for _, branch in backports.calls] == [None, None]


@pytest.mark.parametrize("status", ["conflicts", "regression", "not_applicable", "error"])
async def test_propose_backport_reports_every_status(
    pipeline: FakePipeline, backports: FakeBackport, status: str
) -> None:
    backports.status = status
    out = await mcp_server.propose_backport(ISSUE)
    assert json_block(out)["status"] == status and f"`{status}`" in out


async def test_propose_backport_validates_before_any_run(
    pipeline: FakePipeline, backports: FakeBackport
) -> None:
    with pytest.raises(ValueError):
        await mcp_server.propose_backport("https://example.com/x")
    with pytest.raises(ValueError):
        await mcp_server.propose_backport(ISSUE, "../etc")
    with pytest.raises(ValueError):
        await mcp_server.propose_backport(ISSUE, "a b")
    assert pipeline.calls == [] and backports.calls == []


async def test_propose_backport_is_a_dry_run_that_never_touches_github(
    pipeline: FakePipeline, backports: FakeBackport, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the MCP tool must never open a pull request")

    monkeypatch.setattr("fixedyet.pullrequest.open_backport_pr", forbidden)
    monkeypatch.setattr("fixedyet.pullrequest.PullRequestClient", forbidden)
    await mcp_server.propose_backport(ISSUE)


def test_the_mcp_server_has_no_way_to_reach_the_pull_request_code() -> None:
    from pathlib import Path

    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert "fixedyet.pullrequest" not in source and "open_backport_pr" not in source


async def test_propose_backport_is_described_as_a_dry_run() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    text = tools["propose_backport"].description.lower()
    assert "dry run" in text and "nothing is pushed" in text


async def test_a_broken_backport_renderer_does_not_hide_the_status(
    pipeline: FakePipeline, backports: FakeBackport, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_server, "render_backport", broken)
    out = await mcp_server.propose_backport(ISSUE)
    assert out.startswith("# Backport: verified") and json_block(out)["status"] == "verified"


# --- propose_fix --------------------------------------------------------------------------


def make_fix(status: str = "verified"):
    from fixedyet.models import FixResult, FixStatus

    return FixResult(
        status=FixStatus(status), reason=f"it is {status}", issue_url=ISSUE, title="Fake issue",
        repo="psf/requests", base_branch="main", base_sha="5ef70112a1ff19c05324ff889dd30405b100204",
        diff="diff --git a/src/x.py b/src/x.py\n-old\n+new\n" if status == "verified" else "",
        files_changed=["src/x.py"] if status == "verified" else [],
    )


class FakeFix:
    def __init__(self, status: str = "verified") -> None:
        self.status = status
        self.calls: list[Assessment] = []

    async def __call__(self, assessment: Assessment):
        self.calls.append(assessment)
        return make_fix(self.status)


@pytest.fixture
def fixes(monkeypatch: pytest.MonkeyPatch, pipeline: FakePipeline) -> FakeFix:
    fake = FakeFix()
    monkeypatch.setattr(mcp_server, "run_fix", fake)
    monkeypatch.setattr(
        mcp_server, "render_fix", lambda r: f"# Fix: {r.status.value}\n\n{r.reason}"
    )
    return fake


async def test_propose_fix_reproduces_then_returns_the_verified_diff(
    pipeline: FakePipeline, fixes: FakeFix
) -> None:
    pipeline.assessment = make_assessment(Verdict.OPEN_BUG)
    out = await mcp_server.propose_fix(f"{ISSUE}#issuecomment-1")

    assert out.startswith("Reproduction verdict: open_bug (high confidence).")
    assert "no pull request was opened" in out
    assert out.index("# Fix: verified") < out.index("```json")
    data = json_block(out)
    assert data["status"] == "verified" and "+new" in data["diff"]
    assert pipeline.calls == [(ISSUE, None)]  # main is the fix side, as fix mode needs
    assert [a.verdict for a in fixes.calls] == [Verdict.OPEN_BUG]


@pytest.mark.parametrize(
    "status", ["not_fixed", "already_fixed", "not_applicable", "error"]
)
async def test_propose_fix_reports_every_status(
    pipeline: FakePipeline, fixes: FakeFix, status: str
) -> None:
    fixes.status = status
    out = await mcp_server.propose_fix(ISSUE)
    assert json_block(out)["status"] == status
    assert f"# Fix: {status}" in out


async def test_propose_fix_on_a_bug_that_is_not_open_says_so(
    pipeline: FakePipeline, fixes: FakeFix
) -> None:
    """Fix mode itself answers not_applicable for any verdict but open_bug; the tool passes the
    reproduction on and shows its verdict."""
    fixes.status = "not_applicable"
    out = await mcp_server.propose_fix(ISSUE)  # the fake pipeline says fixed_on_main
    assert out.startswith("Reproduction verdict: fixed_on_main")
    assert json_block(out)["status"] == "not_applicable"


async def test_propose_fix_validates_before_any_run(
    pipeline: FakePipeline, fixes: FakeFix
) -> None:
    for url in ("", "https://example.com/x/y/issues/1", "https://github.com/x/y/pull/1"):
        with pytest.raises(ValueError, match="GitHub issue URL"):
            await mcp_server.propose_fix(url)
    assert pipeline.calls == [] and fixes.calls == []


async def test_propose_fix_is_a_dry_run_that_never_touches_github(
    pipeline: FakePipeline, fixes: FakeFix, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the MCP tool must never open a pull request")

    monkeypatch.setattr("fixedyet.pullrequest.open_fix_pr", forbidden)
    monkeypatch.setattr("fixedyet.pullrequest.PullRequestClient", forbidden)
    pipeline.assessment = make_assessment(Verdict.OPEN_BUG)
    await mcp_server.propose_fix(ISSUE)
    assert "open_fix_pr" not in SOURCE


SOURCE = Path(mcp_server.__file__).read_text(encoding="utf-8")


async def test_the_real_fix_seam_calls_fix_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Assessment] = []

    async def attempt_fix(assessment: Assessment, **kwargs: object):
        seen.append(assessment)
        assert kwargs == {}  # settings, sandbox and budget come from the environment
        return make_fix()

    monkeypatch.setattr("fixedyet.fixmode.attempt_fix", attempt_fix)
    assessment = make_assessment(Verdict.OPEN_BUG)
    result = await mcp_server.run_fix(assessment)
    assert result.status.value == "verified" and seen == [assessment]


async def test_propose_fix_is_described_as_a_dry_run() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    text = tools["propose_fix"].description.lower()
    assert "dry run" in text and "nothing is pushed" in text and "open bug" in text
    tool = tools["propose_fix"]
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)  # mcp 2 / 1
    assert schema["required"] == ["issue_url"]


async def test_a_broken_fix_renderer_does_not_hide_the_status(
    pipeline: FakePipeline, fixes: FakeFix, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_server, "render_fix", broken)
    out = await mcp_server.propose_fix(ISSUE)
    assert "# Fix: verified" in out and json_block(out)["status"] == "verified"
