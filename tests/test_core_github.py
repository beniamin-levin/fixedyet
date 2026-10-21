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


# -- how active a maintenance branch is ---------------------------------------------------------


def branch_json(date: str = "2025-11-03T10:00:00Z") -> dict:
    return {"name": "8.4.x", "commit": {"sha": "a" * 40, "commit": {"committer": {"date": date}}}}


@respx.mock
async def test_target_activity_reads_the_last_commit_and_the_latest_release() -> None:
    branch = respx.get(f"{API}/repos/acme/widgets/branches/8.4.x").respond(json=branch_json())
    respx.get(f"{API}/repos/acme/widgets/releases/latest").respond(
        json={"tag_name": "9.1.1", "published_at": "2026-09-01T08:00:00Z"}
    )
    got = await github.target_activity("acme/widgets", "8.4.x", token="tok")
    assert got is not None
    assert got["branch"] == "8.4.x" and got["last_commit_sha"] == "a" * 40
    assert got["last_commit_date"] == "2025-11-03T10:00:00Z"
    assert got["latest_release"] == "9.1.1" and got["release_source"] == "release"
    assert got["latest_release_date"] == "2026-09-01T08:00:00Z"
    assert got["checked_at"]
    assert branch.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
async def test_target_activity_keeps_a_slash_in_the_branch_name() -> None:
    route = respx.get(f"{API}/repos/acme/widgets/branches/stable/8.1").respond(json=branch_json())
    respx.get(f"{API}/repos/acme/widgets/releases/latest").respond(json={"tag_name": "8.1.2"})
    got = await github.target_activity("acme/widgets", "stable/8.1")
    assert got is not None and route.called
    assert got["latest_release_date"] is None


@respx.mock
async def test_without_releases_the_latest_release_is_the_highest_plain_version_tag() -> None:
    respx.get(f"{API}/repos/acme/widgets/branches/8.4.x").respond(json=branch_json())
    respx.get(f"{API}/repos/acme/widgets/releases/latest").respond(404, json={})
    respx.get(f"{API}/repos/acme/widgets/tags").respond(json=[
        {"name": "9.0.0rc1", "commit": {"sha": "1" * 40}},
        {"name": "8.4.9", "commit": {"sha": "2" * 40}},
        {"name": "v8.4.10", "commit": {"sha": "3" * 40}},
        {"name": "nightly", "commit": {"sha": "4" * 40}},
        {"name": "8.3.12", "commit": {"sha": "5" * 40}},
    ])
    respx.get(f"{API}/repos/acme/widgets/commits/{'3' * 40}").respond(
        json={"commit": {"committer": {"date": "2026-08-01T00:00:00Z"}}}
    )
    got = await github.target_activity("acme/widgets", "8.4.x")
    assert got is not None
    assert got["latest_release"] == "v8.4.10" and got["release_source"] == "tag"
    assert got["latest_release_date"] == "2026-08-01T00:00:00Z"


@respx.mock
async def test_a_piece_that_cannot_be_read_is_none_and_nothing_is_raised() -> None:
    respx.get(f"{API}/repos/acme/widgets/branches/8.4.x").respond(500, json={})
    respx.get(f"{API}/repos/acme/widgets/releases/latest").respond(json={"tag_name": "9.1.1"})
    got = await github.target_activity("acme/widgets", "8.4.x")
    assert got is not None
    assert got["last_commit_date"] is None and got["latest_release"] == "9.1.1"

    respx.get(f"{API}/repos/acme/widgets/branches/8.4.x").respond(json=branch_json())
    respx.get(f"{API}/repos/acme/widgets/releases/latest").respond(404, json={})
    respx.get(f"{API}/repos/acme/widgets/tags").respond(500, json={})
    got = await github.target_activity("acme/widgets", "8.4.x")
    assert got is not None
    assert got["latest_release"] is None and got["release_source"] is None
    assert got["last_commit_date"] == "2025-11-03T10:00:00Z"


@respx.mock
async def test_target_activity_is_none_when_nothing_can_be_read() -> None:
    respx.get(f"{API}/repos/acme/widgets/branches/8.4.x").mock(
        side_effect=httpx.ConnectError("no network")
    )
    respx.get(f"{API}/repos/acme/widgets/releases/latest").respond(403, json={"message": "no"})
    assert await github.target_activity("acme/widgets", "8.4.x") is None

    respx.get(f"{API}/repos/acme/widgets/branches/8.4.x").respond(200, text="not json")
    respx.get(f"{API}/repos/acme/widgets/releases/latest").respond(200, text="<html>")
    assert await github.target_activity("acme/widgets", "8.4.x") is None


# -- where the issue stands -----------------------------------------------------------------------


def _pr_node(number: int, state: str, *, merged: bool = False, repo: str = "acme/widgets",
             draft: bool = False) -> dict:
    return {"__typename": "PullRequest", "number": number,
            "url": f"https://github.com/{repo}/pull/{number}", "title": f"PR {number}",
            "state": state, "merged": merged,
            "mergedAt": "2026-10-17T16:11:24Z" if merged else None, "isDraft": draft,
            "repository": {"nameWithOwner": repo}}


@respx.mock
async def test_issue_status_through_graphql_reads_the_sidebar_and_the_closer(issue: Issue) -> None:
    route = respx.post(f"{API}/graphql").respond(json={"data": {"repository": {"issue": {
        "state": "CLOSED", "stateReason": "COMPLETED", "closedAt": "2026-10-17T16:11:26Z",
        "closedByPullRequestsReferences": {"nodes": [
            _pr_node(643, "MERGED", merged=True), _pr_node(650, "CLOSED"),
        ]},
        "timelineItems": {"nodes": [
            {"__typename": "CrossReferencedEvent", "willCloseTarget": False,
             "source": _pr_node(642, "MERGED", merged=True)},
            {"__typename": "CrossReferencedEvent", "willCloseTarget": False,
             "source": _pr_node(12, "OPEN", repo="someone/else")},
            {"__typename": "CrossReferencedEvent", "willCloseTarget": False,
             "source": {"__typename": "Issue"}},
            {"__typename": "ConnectedEvent", "subject": _pr_node(660, "OPEN", draft=True)},
            {"__typename": "ClosedEvent", "closer": {"__typename": "PullRequest",
                                                     "number": 643, "url": "https://x/643"}},
        ]},
    }}}})
    async with GitHubClient("tok") as gh:
        status = await gh.issue_status(issue)
    assert status is not None and status.source == "graphql"
    assert (status.state, status.state_reason) == ("closed", "completed")
    assert status.closed_at is not None and status.closed_at.day == 17
    assert (status.closed_by, status.closed_by_url) == ("#643", "https://x/643")
    assert [(p.number, p.state, p.closes, p.draft) for p in status.linked_prs] == [
        (643, "merged", True, False), (660, "open", True, True), (642, "merged", False, False),
    ]  # closing ones first; closed-unmerged and other projects' pull requests left out
    assert [p.number for p in status.open_prs] == [660]
    body = route.calls[0].request.read().decode()
    assert '"number": 42' in body or '"number":42' in body


@respx.mock
async def test_issue_status_falls_back_to_rest_when_graphql_fails(issue: Issue) -> None:
    respx.post(f"{API}/graphql").respond(json={"errors": [{"message": "nope"}]})
    respx.get(f"{API}/repos/acme/widgets/issues/42").respond(json={
        "state": "open", "state_reason": "reopened", "closed_at": None})
    respx.get(f"{API}/repos/acme/widgets/issues/42/timeline").respond(json=[
        {"event": "cross-referenced", "source": {"issue": {
            "number": 7, "html_url": "https://github.com/acme/widgets/pull/7", "title": "Fixes #42",
            "body": "", "state": "closed", "pull_request": {"merged_at": "2026-10-01T00:00:00Z"},
            "repository": {"full_name": "acme/widgets"}}}},
        {"event": "cross-referenced", "source": {"issue": {
            "number": 8, "html_url": "https://github.com/acme/widgets/pull/8", "title": "wip",
            "state": "open", "draft": True, "pull_request": {"merged_at": None},
            "repository": {"full_name": "acme/widgets"}}}},
        {"event": "cross-referenced", "source": {"issue": {
            "number": 9, "html_url": "https://github.com/acme/widgets/issues/9", "title": "dup",
            "state": "open", "repository": {"full_name": "acme/widgets"}}}},
        {"event": "closed", "commit_id": "abc123"},
    ])
    async with GitHubClient("tok") as gh:
        status = await gh.issue_status(issue)
    assert status is not None and status.source == "rest"
    assert (status.state, status.state_reason, status.closed_by) == ("open", "reopened", None)
    assert [(p.number, p.state, p.closes, p.draft) for p in status.linked_prs] == [
        (7, "merged", True, False), (8, "open", False, True),
    ]


@respx.mock
async def test_issue_status_without_a_token_uses_rest_only(issue: Issue) -> None:
    graphql = respx.post(f"{API}/graphql").respond(json={})
    respx.get(f"{API}/repos/acme/widgets/issues/42").respond(json={
        "state": "closed", "state_reason": "completed", "closed_at": "2026-10-02T00:00:00Z"})
    respx.get(f"{API}/repos/acme/widgets/issues/42/timeline").respond(json=[
        {"event": "closed", "commit_id": "abc123def4567890"}])
    async with GitHubClient() as gh:
        status = await gh.issue_status(issue)
    assert not graphql.called
    assert status is not None and status.closed_by == "abc123def4567890"
    assert status.closed_by_url == "https://github.com/acme/widgets/commit/abc123def4567890"


@respx.mock
async def test_issue_status_is_none_when_nothing_can_be_read(issue: Issue) -> None:
    respx.post(f"{API}/graphql").respond(502)
    respx.get(f"{API}/repos/acme/widgets/issues/42").respond(500)
    respx.get(f"{API}/repos/acme/widgets/issues/42/timeline").respond(500)
    async with GitHubClient("tok") as gh:
        assert await gh.issue_status(issue) is None


@respx.mock
async def test_fetch_issue_reads_why_and_when_it_was_closed() -> None:
    respx.get(f"{API}/repos/acme/widgets/issues/42").respond(json={
        "title": "t", "state": "closed", "state_reason": "not_planned",
        "closed_at": "2026-10-02T00:00:00Z"})
    respx.get(f"{API}/repos/acme/widgets/issues/42/comments").respond(json=[])
    respx.get(f"{API}/repos/acme/widgets").respond(json={})
    async with GitHubClient() as gh:
        got = await gh.fetch_issue("https://github.com/acme/widgets/issues/42")
    assert (got.state, got.state_reason) == ("closed", "not_planned")
    assert got.closed_at is not None and got.closed_at.month == 10
