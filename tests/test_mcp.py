"""Tests for the MCP server tools, with the pipeline replaced by a fake."""

from __future__ import annotations

import json
import re

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
    assert set(tools) == {"reproduce_issue", "check_fixed"}
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
