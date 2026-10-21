from __future__ import annotations

import httpx
import pytest
import respx

from fixedyet import github
from fixedyet.github import (
    GitHubClient,
    GitHubNotFound,
    GitHubRateLimited,
    parse_issue_url,
)
from fixedyet.models import Issue

API = "https://api.github.com"


@pytest.fixture
def issue() -> Issue:
    return Issue(url="https://github.com/acme/widgets/issues/42", owner="acme", repo="widgets",
                 number=42, title="parse() drops the last item")


# -- URL parsing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/widgets/issues/42",
        "http://github.com/acme/widgets/issues/42/",
        "https://www.github.com/acme/widgets/issues/42",
        "  https://github.com/acme/widgets/issues/42  ",
    ],
)
def test_parse_issue_url_accepts(url: str) -> None:
    assert parse_issue_url(url) == ("acme", "widgets", 42)


def test_parse_issue_url_keeps_dots_and_dashes_in_names() -> None:
    assert parse_issue_url("https://github.com/psf/requests.py-x/issues/7") == (
        "psf", "requests.py-x", 7,
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/acme/widgets/issues/42",
        "https://github.com/acme/widgets/pull/42",
        "https://github.com/acme/widgets/issues/abc",
        "https://github.com/acme/widgets",
        "https://github.com.evil.test/acme/widgets/issues/42",
        "ftp://github.com/acme/widgets/issues/42",
        "not a url",
    ],
)
def test_parse_issue_url_rejects(url: str) -> None:
    with pytest.raises(ValueError):
        parse_issue_url(url)


# -- fetch_issue -----------------------------------------------------------------------------


@respx.mock
async def test_fetch_issue_collects_issue_comments_and_default_branch() -> None:
    respx.get(f"{API}/repos/acme/widgets/issues/42").respond(
        json={
            "title": "parse() drops the last item",
            "body": "It loses 'c'.",
            "state": "open",
            "labels": [{"name": "bug"}, {"name": "help wanted"}],
            "created_at": "2024-05-01T10:00:00Z",
        }
    )
    comments_route = respx.get(f"{API}/repos/acme/widgets/issues/42/comments").respond(
        json=[{"user": {"login": "alice"}, "body": "same here"}, {"user": None, "body": "me too"}]
    )
    respx.get(f"{API}/repos/acme/widgets").respond(json={"default_branch": "develop"})

    async with GitHubClient("tok") as gh:
        issue = await gh.fetch_issue("https://github.com/acme/widgets/issues/42")

    assert issue.title == "parse() drops the last item"
    assert issue.body == "It loses 'c'."
    assert issue.labels == ["bug", "help wanted"]
    assert issue.comments == ["@alice: same here", "@unknown: me too"]
    assert issue.default_branch == "develop"
    assert issue.created_at is not None and issue.created_at.year == 2024
    assert comments_route.calls[0].request.url.params["per_page"] == "30"
    assert comments_route.calls[0].request.headers["authorization"] == "Bearer tok"


@respx.mock
async def test_fetch_issue_without_token_sends_no_authorization_header() -> None:
    route = respx.get(f"{API}/repos/acme/widgets/issues/42").respond(json={"title": "t"})
    respx.get(f"{API}/repos/acme/widgets/issues/42/comments").respond(json=[])
    respx.get(f"{API}/repos/acme/widgets").respond(json={})
    async with GitHubClient() as gh:
        issue = await gh.fetch_issue("https://github.com/acme/widgets/issues/42")
    assert "authorization" not in route.calls[0].request.headers
    assert issue.default_branch == "main"  # falls back when the API does not say


@respx.mock
async def test_fetch_issue_caps_comments_at_thirty() -> None:
    respx.get(f"{API}/repos/acme/widgets/issues/42").respond(json={"title": "t"})
    respx.get(f"{API}/repos/acme/widgets/issues/42/comments").respond(
        json=[{"user": {"login": "u"}, "body": str(i)} for i in range(45)]
    )
    respx.get(f"{API}/repos/acme/widgets").respond(json={})
    async with GitHubClient() as gh:
        issue = await gh.fetch_issue("https://github.com/acme/widgets/issues/42")
    assert len(issue.comments) == 30


@respx.mock
async def test_fetch_issue_404_raises_not_found() -> None:
    respx.get(f"{API}/repos/acme/widgets/issues/42").respond(404)
    respx.get(f"{API}/repos/acme/widgets/issues/42/comments").respond(404)
    respx.get(f"{API}/repos/acme/widgets").respond(404)
    async with GitHubClient() as gh:
        with pytest.raises(GitHubNotFound):
            await gh.fetch_issue("https://github.com/acme/widgets/issues/42")


@respx.mock
async def test_rate_limit_far_in_the_future_raises_with_a_hint() -> None:
    reset = str(int(github.time.time()) + 3_000)
    limited = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
        json={"message": "API rate limit exceeded"},
    )
    respx.get(f"{API}/repos/acme/widgets/issues/42").mock(return_value=limited)
    respx.get(f"{API}/repos/acme/widgets/issues/42/comments").mock(return_value=limited)
    respx.get(f"{API}/repos/acme/widgets").mock(return_value=limited)
    async with GitHubClient() as gh:
        with pytest.raises(GitHubRateLimited, match="GITHUB_TOKEN"):
            await gh.fetch_issue("https://github.com/acme/widgets/issues/42")


@respx.mock
async def test_short_rate_limit_waits_once_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(github.asyncio, "sleep", fake_sleep)
    route = respx.get(f"{API}/repos/acme/widgets").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json={"default_branch": "main"}),
        ]
    )
    async with GitHubClient() as gh:
        data = await gh._get("/repos/acme/widgets")
    assert data == {"default_branch": "main"}
    assert slept == [2.0]
    assert route.call_count == 2


# -- duplicate search ------------------------------------------------------------------------


@respx.mock
async def test_search_related_issues_excludes_the_issue_itself_and_builds_evidence(
    issue: Issue,
) -> None:
    route = respx.get(f"{API}/search/issues").respond(
        json={
            "items": [
                {"number": 42, "title": "parse() drops the last item", "state": "open",
                 "html_url": "https://github.com/acme/widgets/issues/42", "comments": 1},
                {"number": 17, "title": "Last item missing in parse", "state": "closed",
                 "html_url": "https://github.com/acme/widgets/issues/17", "comments": 4},
            ]
        }
    )
    async with GitHubClient() as gh:
        found = await gh.search_related_issues(issue, issue.title)
    assert [e.url for e in found] == ["https://github.com/acme/widgets/issues/17"]
    assert found[0].kind == "duplicate"
    assert "closed" in found[0].why and found[0].title.startswith("#17")
    q = route.calls[0].request.url.params["q"]
    assert "repo:acme/widgets" in q and "is:issue" in q


@respx.mock
async def test_search_query_cannot_inject_qualifiers(issue: Issue) -> None:
    route = respx.get(f"{API}/search/issues").respond(json={"items": []})
    async with GitHubClient() as gh:
        await gh.search_related_issues(issue, 'crash repo:other/secret OR user:bob "x" -label:bug')
    q = route.calls[0].request.url.params["q"]
    assert "repo:other" not in q and "user:bob" not in q
    assert q.count("repo:") == 1  # only ours


@respx.mock
async def test_search_rate_limited_returns_nothing_instead_of_raising(issue: Issue) -> None:
    respx.get(f"{API}/search/issues").respond(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}
    )
    async with GitHubClient() as gh:
        assert await gh.search_related_issues(issue, "anything") == []


async def test_search_with_empty_query_makes_no_request(issue: Issue) -> None:
    async with GitHubClient() as gh:
        assert await gh.search_related_issues(issue, "  !!! ") == []


# -- fix PR ----------------------------------------------------------------------------------


def _xref(number: int, merged: str | None, *, repo: str = "acme/widgets", title: str = "t",
          body: str = "") -> dict:
    return {
        "event": "cross-referenced",
        "source": {"issue": {
            "number": number, "title": title, "body": body,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "repository": {"full_name": repo},
            "pull_request": {"merged_at": merged},
        }},
    }


@respx.mock
async def test_find_fix_pr_prefers_a_closing_keyword_over_a_later_mention(issue: Issue) -> None:
    respx.get(f"{API}/repos/acme/widgets/issues/42/timeline").respond(
        json=[
            _xref(50, "2024-06-01T00:00:00Z", title="Refactor parse", body="see #42"),
            _xref(48, "2024-05-20T00:00:00Z", title="Fix parse", body="Fixes #42"),
            _xref(51, None, title="WIP", body="Fixes #42"),  # not merged
            _xref(52, "2024-07-01T00:00:00Z", repo="other/fork", body="Fixes #42"),  # other repo
        ]
    )
    pulls = respx.get(f"{API}/repos/acme/widgets/pulls/48").respond(
        json={"merged": True, "merge_commit_sha": "abc123def456",
              "html_url": "https://github.com/acme/widgets/pull/48"}
    )
    async with GitHubClient() as gh:
        assert await gh.find_fix_pr(issue) == (
            "abc123def456", "https://github.com/acme/widgets/pull/48",
        )
    assert pulls.called


@respx.mock
async def test_find_fix_pr_falls_back_to_search_and_checks_the_mention(issue: Issue) -> None:
    respx.get(f"{API}/repos/acme/widgets/issues/42/timeline").respond(json=[])
    respx.get(f"{API}/search/issues").respond(
        json={"items": [
            {"number": 60, "title": "Unrelated 142", "body": "mentions #142 only",
             "html_url": "https://github.com/acme/widgets/pull/60",
             "pull_request": {"merged_at": "2024-01-01T00:00:00Z"}},
            {"number": 61, "title": "Fix last item (#42)", "body": "",
             "html_url": "https://github.com/acme/widgets/pull/61",
             "pull_request": {"merged_at": "2024-01-02T00:00:00Z"}},
        ]}
    )
    respx.get(f"{API}/repos/acme/widgets/pulls/61").respond(
        json={"merged": True, "merge_commit_sha": "feed", "html_url": "https://x/pull/61"}
    )
    async with GitHubClient() as gh:
        assert await gh.find_fix_pr(issue) == ("feed", "https://x/pull/61")


@respx.mock
async def test_find_fix_pr_none_when_nothing_references_the_issue(issue: Issue) -> None:
    respx.get(f"{API}/repos/acme/widgets/issues/42/timeline").respond(json=[])
    respx.get(f"{API}/search/issues").respond(json={"items": []})
    async with GitHubClient() as gh:
        assert await gh.find_fix_pr(issue) is None


@respx.mock
async def test_find_fix_pr_swallows_api_errors(issue: Issue) -> None:
    respx.get(f"{API}/repos/acme/widgets/issues/42/timeline").respond(500)
    async with GitHubClient() as gh:
        assert await gh.find_fix_pr(issue) is None


@respx.mock
async def test_module_level_helpers_open_and_close_their_own_client(issue: Issue) -> None:
    respx.get(f"{API}/search/issues").respond(json={"items": []})
    assert await github.search_related_issues(issue, "parse") == []


def test_search_terms_keep_the_first_meaningful_words_only() -> None:
    from fixedyet.github import _search_terms

    title = "No stdout/stderr is captured with `invoke` during tests in combination with structlog"
    assert _search_terms(title, max_terms=4) == "stdout stderr captured invoke"
    assert _search_terms(title, max_terms=2) == "stdout stderr"
    assert _search_terms("Parse parse PARSE the last item", max_terms=3) == "Parse last item"
    assert _search_terms("-label bug OR user:bob", max_terms=9) == "label bug user bob"
    assert _search_terms("  !!! ", max_terms=3) == ""
    assert _search_terms("a b c") == "a b c"  # without max_terms nothing is dropped


@respx.mock
async def test_search_related_issues_loosens_the_query_when_nothing_else_matches(
    issue: Issue,
) -> None:
    other = {"number": 17, "title": "same", "state": "open",
             "html_url": "https://github.com/acme/widgets/issues/17", "comments": 0}
    itself = {"number": 42, "title": "this", "state": "open",
              "html_url": "https://github.com/acme/widgets/issues/42", "comments": 0}

    def answer(request: httpx.Request) -> httpx.Response:
        words = request.url.params["q"].split(" repo:")[0].split()
        return httpx.Response(200, json={"items": [itself] if len(words) > 3 else [itself, other]})

    route = respx.get(f"{API}/search/issues").mock(side_effect=answer)
    issue.title = "parse() drops the last item silently when the input ends in a comma"
    async with GitHubClient() as gh:
        found = await gh.search_related_issues(issue, issue.title)
    assert route.call_count == 2  # five words found only the issue itself, three found more
    assert [e.url for e in found] == ["https://github.com/acme/widgets/issues/17"]
