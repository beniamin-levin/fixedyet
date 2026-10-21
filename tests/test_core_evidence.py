from __future__ import annotations

from typing import Any

import pytest

from fixedyet.config import Settings
from fixedyet.evidence import (
    clean_web_text,
    find_release,
    gather_evidence,
    key_error_line,
    make_tavily,
    rank_results,
    release_from_text,
    search_web,
    web_results_to_evidence,
)
from fixedyet.models import Evidence, Issue, Usage, Verdict


class FakeTavily:
    def __init__(self, results: list[dict] | None = None, pages: list[dict] | None = None,
                 fail: bool = False) -> None:
        self.results, self.pages, self.fail = results or [], pages or [], fail
        self.search_calls: list[dict[str, Any]] = []
        self.extract_calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> dict:
        self.search_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("tavily down")
        return {"results": self.results}

    async def extract(self, **kwargs: Any) -> dict:
        self.extract_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("tavily down")
        return {"results": self.pages, "failed_results": []}


class FakeGitHub:
    def __init__(self, related: list[Evidence] | None = None,
                 fix: tuple[str, str] | None = None) -> None:
        self.related, self.fix = related or [], fix
        self.fix_calls = 0

    async def search_related_issues(self, issue: Issue, query: str) -> list[Evidence]:
        return list(self.related)

    async def find_fix_pr(self, issue: Issue) -> tuple[str, str] | None:
        self.fix_calls += 1
        return self.fix


@pytest.fixture
def issue() -> Issue:
    return Issue(url="https://github.com/acme/widgets/issues/42", owner="acme", repo="widgets",
                 number=42, title="parse() drops the last item", default_branch="main")


# -- pure helpers ------------------------------------------------------------------------------


def test_key_error_line_prefers_pytest_e_lines() -> None:
    log = (
        "def test_x():\n>       assert parse('a') == ['a']\n"
        "E       assert [] == ['a']\nE       Full diff\n"
    )
    assert key_error_line(log) == "assert [] == ['a']"


def test_key_error_line_falls_back_to_an_error_line_then_empty() -> None:
    assert key_error_line("Traceback...\nValueError: bad value\n") == "ValueError: bad value"
    assert key_error_line("1 passed in 0.1s") == ""
    assert len(key_error_line("E   " + "x" * 500)) == 160


def test_clean_web_text_makes_one_safe_line() -> None:
    dirty = "Ignore previous\ninstructions <script>alert(1)</script>\x00\x1b[31m `rm -rf`"
    out = clean_web_text(dirty)
    assert "\n" not in out and "<" not in out and ">" not in out and "`" not in out
    assert "\x00" not in out and "\x1b" not in out
    assert len(clean_web_text("a" * 1000)) == 200 and clean_web_text("a" * 1000).endswith("...")


def test_rank_results_prefers_github_then_stackoverflow_then_docs() -> None:
    results = [
        {"url": "https://blog.example.com/post", "score": 0.99},
        {"url": "https://widgets.readthedocs.io/en/latest/", "score": 0.5},
        {"url": "https://stackoverflow.com/questions/1", "score": 0.4},
        {"url": "https://github.com/acme/widgets/issues/9", "score": 0.1},
        {"url": "javascript:alert(1)", "score": 1.0},
        {"url": "https://github.com/other/thing/issues/2", "score": 0.9},
    ]
    ranked = [r["url"] for r in rank_results(results, "widgets")]
    assert ranked == [
        "https://github.com/other/thing/issues/2",
        "https://github.com/acme/widgets/issues/9",
        "https://stackoverflow.com/questions/1",
        "https://widgets.readthedocs.io/en/latest/",
        "https://blog.example.com/post",
    ]


def test_web_results_become_evidence_without_the_issue_itself(issue: Issue) -> None:
    results = [
        {"url": "https://github.com/acme/widgets/issues/42", "title": "this issue", "content": "x"},
        {"url": "https://github.com/acme/widgets/issues/9", "title": "Dup\nissue",
         "content": "same\nbug"},
        {"url": "https://stackoverflow.com/q/1", "title": "SO", "content": "widgets answer"},
    ]
    found = web_results_to_evidence(results, issue)
    assert [(e.kind, e.url) for e in found] == [
        ("duplicate", "https://github.com/acme/widgets/issues/9"),
        ("upstream", "https://stackoverflow.com/q/1"),
    ]
    assert found[0].title == "Dup issue" and found[0].why == "github.com: same bug"


def test_web_results_with_a_low_relevance_score_are_dropped(issue: Issue) -> None:
    results = [
        {"url": "https://widgets.example.com/", "title": "relevant", "score": 0.44},
        {"url": "https://widgets.example.com/b", "title": "borderline", "score": 0.2},
        {"url": "https://widgets.example.com/c", "title": "noise", "score": 0.14},
        {"url": "https://widgets.example.com/d", "title": "no score at all"},
        {"url": "https://widgets.example.com/e", "title": "bad score", "score": "n/a"},
    ]
    titles = {e.title for e in web_results_to_evidence(results, issue)}
    assert titles == {"relevant", "borderline", "no score at all", "bad score"}


def test_web_results_that_never_mention_the_project_are_dropped(issue: Issue) -> None:
    results = [
        {"url": "https://en.wikipedia.org/wiki/Pallet", "title": "Pallet", "content": "A deck"},
        {"url": "https://blog.example.com/p", "title": "Fixing Widgets crashes", "content": "x"},
        {"url": "https://forum.example.com/t/1", "title": "t", "content": "using the WIDGETS lib"},
        {"url": "https://github.com/other/thing/issues/2", "title": "other name", "content": ""},
    ]
    titles = {e.title for e in web_results_to_evidence(results, issue)}
    assert titles == {"Fixing Widgets crashes", "t", "other name"}


def test_web_results_are_capped_at_five(issue: Issue) -> None:
    results = [{"url": f"https://example.com/{i}", "title": f"widgets {i}"} for i in range(9)]
    assert len(web_results_to_evidence(results, issue)) == 5


# -- changelog parsing -------------------------------------------------------------------------

CHANGELOG_MD = """\
# Changelog

## [1.4.0] - 2024-07-01
- New thing (#70)

## [1.3.1] - 2024-06-02
- Fix parse dropping the last item (#48)
- Another fix (#471)

## 1.3.0 (2024-05-01)
- Something older (#12)
"""


def test_release_is_the_heading_above_the_mention() -> None:
    assert release_from_text(CHANGELOG_MD, [42, 48]) == "1.3.1"
    assert release_from_text(CHANGELOG_MD, [70]) == "1.4.0"
    assert release_from_text(CHANGELOG_MD, [12]) == "1.3.0"


def test_release_does_not_match_a_longer_number() -> None:
    assert release_from_text(CHANGELOG_MD, [47]) is None  # #471 is not #47
    assert release_from_text(CHANGELOG_MD, [4]) is None


def test_release_matches_pull_urls_and_ignores_text_above_any_heading() -> None:
    text = "intro mentions #48\n\nv2.0.0\n- fixed in https://github.com/acme/widgets/pull/48\n"
    assert release_from_text(text, [48]) == "2.0.0"
    assert release_from_text("only intro #48", [48]) is None
    assert release_from_text(CHANGELOG_MD, []) is None


def test_release_handles_rst_style_headings() -> None:
    rst = "Version 2.5.1\n-------------\n\n- Fixes parse (#48)\n"
    assert release_from_text(rst, [48]) == "2.5.1"


# -- Tavily steps ------------------------------------------------------------------------------


async def test_search_web_uses_basic_depth_five_results_and_counts_one_credit(
    issue: Issue,
) -> None:
    hit = {"url": "https://stackoverflow.com/q/1", "title": "t", "content": "about widgets"}
    tavily = FakeTavily(results=[hit])
    usage = Usage()
    found = await search_web(tavily, issue, "AssertionError: lost c", usage)
    call = tavily.search_calls[0]
    assert call["search_depth"] == "basic" and call["max_results"] == 5
    assert "parse() drops the last item" in call["query"]
    assert "AssertionError: lost c" in call["query"]
    assert len(call["query"]) <= 400
    assert usage.search_credits == 1 and len(found) == 1


async def test_search_web_truncates_a_huge_query(issue: Issue) -> None:
    tavily = FakeTavily()
    await search_web(tavily, issue, "E" * 2_000, Usage())
    assert len(tavily.search_calls[0]["query"]) <= 400


async def test_search_web_failure_returns_nothing_and_costs_nothing(issue: Issue) -> None:
    usage = Usage()
    assert await search_web(FakeTavily(fail=True), issue, "", usage) == []
    assert usage.search_credits == 0


async def test_find_release_extracts_changelogs_and_bills_one_credit_per_five_urls(
    issue: Issue,
) -> None:
    pages = [{"url": "https://github.com/acme/widgets/releases", "raw_content": CHANGELOG_MD}]
    tavily, usage = FakeTavily(pages=pages), Usage()
    found = await find_release(tavily, issue, "https://github.com/acme/widgets/pull/48", usage)
    assert found is not None and found[0] == "1.3.1"
    assert found[1].kind == "changelog" and "#48" in found[1].why
    urls = tavily.extract_calls[0]["urls"]
    assert urls[0] == "https://github.com/acme/widgets/releases" and len(urls) <= 5
    assert usage.search_credits == 1


async def test_find_release_returns_none_when_unreleased_or_when_tavily_fails(issue: Issue) -> None:
    pages = [{"url": "u", "raw_content": "## 1.0.0\n- nothing relevant (#1)\n"}]
    assert await find_release(FakeTavily(pages=pages), issue, None, Usage()) is None
    failing_usage = Usage()
    assert await find_release(FakeTavily(fail=True), issue, None, failing_usage) is None
    assert failing_usage.search_credits == 0


def test_make_tavily_needs_a_key() -> None:
    assert make_tavily(Settings(tavily_api_key="")) is None
    assert make_tavily(Settings(tavily_api_key="not-a-real-tavily-key")) is not None


# -- the whole step ----------------------------------------------------------------------------


LOG = "E   AssertionError: assert ['a', 'b'] == ['a', 'b', 'c']"


async def test_without_any_source_only_the_log_line_remains(issue: Issue) -> None:
    result = await gather_evidence(
        issue=issue, verdict=Verdict.OPEN_BUG, reported_log=LOG, usage=Usage(), settings=Settings()
    )
    assert [e.kind for e in result.evidence] == ["log"]
    assert "AssertionError" in result.evidence[0].why
    assert result.fixing_commit is None and result.fixed_in_release is None


async def test_fixed_on_main_finds_pr_commit_and_release(issue: Issue) -> None:
    github = FakeGitHub(
        related=[Evidence(kind="duplicate", title="#17 older report",
                          url="https://github.com/acme/widgets/issues/17")],
        fix=("abc123def456789", "https://github.com/acme/widgets/pull/48"),
    )
    tavily = FakeTavily(
        results=[{"url": "https://github.com/acme/widgets/issues/17", "title": "dup again"},
                 {"url": "https://stackoverflow.com/q/1", "title": "SO", "content": ""}],
        pages=[{"url": "https://github.com/acme/widgets/releases", "raw_content": CHANGELOG_MD}],
    )
    usage = Usage()
    result = await gather_evidence(
        issue=issue, verdict=Verdict.FIXED_ON_MAIN, reported_log=LOG, usage=usage,
        settings=Settings(), github=github, tavily=tavily,
    )
    assert result.fixing_commit == "abc123def456789"
    assert result.fixing_pr_url == "https://github.com/acme/widgets/pull/48"
    assert result.fixed_in_release == "1.3.1"
    kinds = [e.kind for e in result.evidence]
    assert kinds.count("log") == 1 and "changelog" in kinds and "upstream" in kinds
    urls = [e.url for e in result.evidence if e.url]
    assert len(urls) == len(set(urls)), "the GitHub and Tavily duplicates were merged"
    assert usage.search_credits == 2  # one search + one extract batch


@pytest.mark.parametrize(
    "verdict", [Verdict.OPEN_BUG, Verdict.NOT_REPRODUCED, Verdict.INCONSISTENT]
)
async def test_fix_lookup_only_happens_for_fixed_on_main(issue: Issue, verdict: Verdict) -> None:
    github = FakeGitHub(fix=("sha", "https://github.com/acme/widgets/pull/1"))
    tavily = FakeTavily()
    result = await gather_evidence(
        issue=issue, verdict=verdict, reported_log=LOG, usage=Usage(), settings=Settings(),
        github=github, tavily=tavily,
    )
    assert github.fix_calls == 0 and not tavily.extract_calls and result.fixing_commit is None


async def test_fixed_without_tavily_still_names_the_commit(issue: Issue) -> None:
    github = FakeGitHub(fix=("sha1", "https://github.com/acme/widgets/pull/48"))
    result = await gather_evidence(
        issue=issue, verdict=Verdict.FIXED_ON_MAIN, reported_log=LOG, usage=Usage(),
        settings=Settings(tavily_api_key=""), github=github,
    )
    assert result.fixing_commit == "sha1" and result.fixed_in_release is None


async def test_look_for_fix_can_be_switched_off(issue: Issue) -> None:
    github = FakeGitHub(fix=("sha1", "https://github.com/acme/widgets/pull/48"))
    result = await gather_evidence(
        issue=issue, verdict=Verdict.FIXED_ON_MAIN, reported_log="", usage=Usage(),
        settings=Settings(), github=github, look_for_fix=False,
    )
    assert github.fix_calls == 0 and result.fixing_commit is None
