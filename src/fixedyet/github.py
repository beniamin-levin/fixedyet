"""GitHub API access: fetch an issue, find duplicates, find the pull request that fixed it, and
read where the issue stands (closed as completed? linked pull requests?).

Only public data is read, with an optional token for the higher rate limit. Everything that is
"nice to have" (duplicate search, fix lookup, the issue's status) swallows API errors and returns
nothing, because a rate limit must never fail a reproduction run. Fetching the issue itself raises.

The issue's status uses GraphQL when there is a token (GitHub's GraphQL API needs one): it is the
only API that lists the pull requests linked in the issue's Development sidebar
(`closedByPullRequestsReferences`) and the pull request or commit that closed it. Without a token
the REST timeline gives the cross-referenced pull requests and the closing commit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from fixedyet.models import Evidence, Issue, IssueStatus, LinkedPullRequest
from fixedyet.untrusted import strip_hidden, strip_hidden_comment

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
MAX_COMMENTS = 30
MAX_RATE_LIMIT_WAIT_S = 15  # sleep and retry once if GitHub says "try again in <= 15 s"

_ISSUE_PATH = re.compile(r"^/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/(?P<number>\d+)/?$")
_SCHEMELESS = re.compile(r"^(?:www\.)?github\.com/", re.IGNORECASE)


class GitHubError(RuntimeError):
    """Any failure talking to the GitHub API."""


class GitHubNotFound(GitHubError):
    """404 or 410: the issue does not exist (or was deleted), or it is in a private repository."""


class GitHubNotAnIssue(GitHubError):
    """The number belongs to a pull request. GitHub's issues API answers for pull requests too,
    so without this check a pull request would be run as if it were a bug report."""


class GitHubRateLimited(GitHubError):
    """The rate limit is exhausted and the reset is too far away to wait for."""


def _comment(data: dict[str, Any]) -> str:
    """``@login: body`` with the hidden parts of the body removed. A bot account's login is
    written ``name[bot]`` (GitHub's own form), so the bot's folded blocks go too."""
    user = data.get("user") or {}
    login = str(user.get("login") or "unknown")
    if user.get("type") == "Bot" and not login.endswith("[bot]"):
        login += "[bot]"
    return strip_hidden_comment(f"@{login}: {data.get('body') or ''}")


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """'https://github.com/psf/requests/issues/6710' -> ('psf', 'requests', 6710).

    Takes the URL as people paste it: http or no scheme at all, ``www.``, any case in the host,
    a trailing slash, a query or an ``#issuecomment-...`` anchor. Owner and repository keep the
    case they were typed in.
    """
    text = url.strip()
    if _SCHEMELESS.match(text):
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in (
        "github.com",
        "www.github.com",
    ):
        raise ValueError(f"not a github.com issue URL: {url!r}")
    match = _ISSUE_PATH.match(parsed.path)
    if match is None:
        raise ValueError(f"expected https://github.com/<owner>/<repo>/issues/<number>, got {url!r}")
    return match["owner"], match["repo"], int(match["number"])


_STOPWORDS = frozenset(
    "a an the and or not of in on at to for with without when while is are was were be been it "
    "its this that these those does do doesn don can cannot could should would will from by as "
    "into than then how why what which who has have had but if so no nor".split()
)


def _search_terms(text: str, limit: int = 200, max_terms: int | None = None) -> str:
    """Reduce free text to plain words so it cannot inject GitHub search qualifiers.

    GitHub search ANDs every word, so a whole title matches nothing but the issue itself. With
    ``max_terms`` only the first words that carry meaning are kept: a title puts its topic first.
    """
    words = [w.lstrip("-") for w in re.sub(r"[^\w.\-\s]", " ", text).split()]
    words = [w for w in words if w and w.upper() not in {"AND", "OR", "NOT"}]
    if max_terms is not None:
        seen: set[str] = set()
        words = [
            w for w in words
            if w.lower() not in _STOPWORDS and not (w.lower() in seen or seen.add(w.lower()))
        ][:max_terms]
    return " ".join(words)[:limit]


class GitHubClient:
    """Async wrapper over the endpoints we need: `async with GitHubClient(token) as gh`."""

    def __init__(
        self,
        token: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = API_ROOT,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fixedyet",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._has_token = bool(token)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=20.0, follow_redirects=True
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- low level -------------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with one polite retry on a short rate-limit window. Returns decoded JSON."""
        for attempt in (1, 2):
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                raise GitHubError(f"GitHub request failed: {exc}") from exc
            if response.status_code in (404, 410):  # 410: a deleted issue, or issues turned off
                raise GitHubNotFound(f"GitHub returned {response.status_code} for {path}")
            wait = self._rate_limit_wait(response)
            if wait is not None:
                if attempt == 1 and wait <= MAX_RATE_LIMIT_WAIT_S:
                    log.info("GitHub rate limit; waiting %.0f s", wait)
                    await asyncio.sleep(wait)
                    continue
                raise GitHubRateLimited(
                    f"GitHub API rate limit reached (resets in about {wait:.0f} s). "
                    "Set GITHUB_TOKEN for a higher limit."
                )
            if response.status_code >= 400:
                raise GitHubError(f"GitHub returned {response.status_code} for {path}")
            return response.json()
        raise GitHubError(f"GitHub request kept failing for {path}")  # pragma: no cover

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a GraphQL query (needs a token). Returns ``data``; GraphQL errors raise."""
        try:
            response = await self._client.post(
                "/graphql", json={"query": query, "variables": variables}
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub GraphQL request failed: {exc}") from exc
        if self._rate_limit_wait(response) is not None:
            raise GitHubRateLimited("GitHub GraphQL rate limit reached")
        if response.status_code >= 400:
            raise GitHubError(f"GitHub GraphQL returned {response.status_code}")
        payload = response.json()
        if payload.get("errors") or not isinstance(payload.get("data"), dict):
            raise GitHubError(f"GitHub GraphQL errors: {str(payload.get('errors'))[:300]}")
        return payload["data"]

    @staticmethod
    def _rate_limit_wait(response: httpx.Response) -> float | None:
        """Seconds to wait if this response is a rate-limit refusal, else None."""
        if response.status_code not in (403, 429):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset", "")
            return max(0.0, float(reset) - time.time()) if reset.isdigit() else 60.0
        if response.status_code == 429 or "rate limit" in response.text.lower():
            return 60.0  # secondary rate limit without headers
        return None

    # -- how alive a branch is -------------------------------------------------------------

    async def target_activity(self, repo: str, branch: str) -> dict[str, Any] | None:
        """When ``branch`` last got a commit, and the project's latest release (tag and date).

        Best effort, for a maintainer deciding whether a backport is welcome: every piece that
        cannot be read is None, nothing is raised, and None comes back when nothing could be read.
        The latest release is ``/releases/latest``; a project without releases falls back to its
        highest version tag.
        """
        base = f"/repos/{repo}"
        commit_date = commit_sha = None
        try:
            data = await self._get(f"{base}/branches/{quote(branch, safe='/')}")
            commit_sha = data["commit"]["sha"]
            detail = data["commit"].get("commit") or {}
            commit_date = (detail.get("committer") or detail.get("author") or {}).get("date")
        except Exception as exc:  # noqa: BLE001 - a nice-to-have must never fail its caller
            log.debug("branch activity of %s@%s unreadable: %s", repo, branch, type(exc).__name__)
        release = release_date = source = None
        try:
            data = await self._get(f"{base}/releases/latest")
            release = data["tag_name"]
            release_date = data.get("published_at") or data.get("created_at")
            source = "release"
        except GitHubNotFound:
            release, release_date = await self._latest_tag(base)
            source = "tag" if release else None
        except Exception as exc:  # noqa: BLE001
            log.debug("latest release of %s unreadable: %s", repo, type(exc).__name__)
        if not (commit_date or release):
            return None
        return {
            "branch": branch,
            "last_commit_sha": commit_sha,
            "last_commit_date": commit_date,
            "latest_release": release,
            "latest_release_date": release_date,
            "release_source": source,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    async def _latest_tag(self, base: str) -> tuple[str | None, str | None]:
        """The highest plain version tag (no a/b/rc/dev suffix) and its commit date, or Nones."""
        try:
            tags = await self._get(f"{base}/tags", {"per_page": 100})
            best: tuple[tuple[int, ...], str, str] | None = None
            for tag in tags:
                name = str(tag.get("name", ""))
                # 8.4.1, v8.4.1, pytest-8.4.1: not 8.4.0rc1 or 1.0b2, which are pre-releases
                match = re.fullmatch(r"(?:[A-Za-z][\w]*[-_])?v?(\d+)\.(\d+)(?:\.(\d+))?", name)
                if match:
                    key = tuple(int(g or 0) for g in match.groups())
                    if best is None or key > best[0]:
                        best = (key, name, str((tag.get("commit") or {}).get("sha", "")))
            if best is None:
                return None, None
            date = None
            if best[2]:
                try:
                    detail = (await self._get(f"{base}/commits/{best[2]}")).get("commit") or {}
                    date = (detail.get("committer") or detail.get("author") or {}).get("date")
                except Exception:  # noqa: BLE001
                    date = None
            return best[1], date
        except Exception as exc:  # noqa: BLE001
            log.debug("tags of %s unreadable: %s", base, type(exc).__name__)
            return None, None

    # -- issue ---------------------------------------------------------------------------

    async def issue_data(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """The issue as the REST API returns it, in one request. Raises GitHubNotFound when there
        is no such issue and GitHubNotAnIssue when the number is a pull request's."""
        data = await self._get(f"/repos/{owner}/{repo}/issues/{number}")
        if not isinstance(data, dict):
            raise GitHubError(f"GitHub returned no issue for {owner}/{repo}#{number}")
        if data.get("pull_request"):
            raise GitHubNotAnIssue(
                f"{owner}/{repo}#{number} is a pull request, not an issue. FixedYet reproduces "
                "bug reports: give it the URL of the issue the pull request is about."
            )
        return data

    async def fetch_issue(self, url: str) -> Issue:
        """Issue, up to 30 comments, and the repository's default branch. A pull request's number
        raises GitHubNotAnIssue."""
        owner, repo, number = parse_issue_url(url)
        base = f"/repos/{owner}/{repo}"
        data, comments, repo_info = await asyncio.gather(
            self.issue_data(owner, repo, number),
            self._get(f"{base}/issues/{number}/comments", {"per_page": MAX_COMMENTS}),
            self._get(base),
        )
        # What a reader of the thread cannot see (HTML comments, invisible characters, a bot's
        # folded blocks) is where instructions for AI agents hide: it never reaches the model.
        return Issue(
            url=f"https://github.com/{owner}/{repo}/issues/{number}",
            owner=owner,
            repo=repo,
            number=number,
            title=strip_hidden(data.get("title") or ""),
            body=strip_hidden(data.get("body") or ""),
            comments=[_comment(c) for c in comments[:MAX_COMMENTS]],
            labels=[label["name"] for label in data.get("labels", []) if isinstance(label, dict)],
            state=data.get("state") or "open",
            state_reason=data.get("state_reason"),
            closed_at=data.get("closed_at"),
            created_at=data.get("created_at"),
            default_branch=repo_info.get("default_branch") or "main",
        )

    # -- where the issue stands -----------------------------------------------------------

    async def compare_status(self, owner: str, repo: str, base: str, head: str) -> str | None:
        """How `head` relates to `base` in git history: "ahead" (base is an ancestor of head),
        "behind", "diverged" or "identical". None when GitHub cannot say."""
        try:
            data = await self._get(f"/repos/{owner}/{repo}/compare/{base}...{head}")
        except GitHubError as exc:
            log.info("compare %s...%s skipped: %s", base[:10], head[:10], exc)
            return None
        status = data.get("status") if isinstance(data, dict) else None
        return status if isinstance(status, str) else None

    async def issue_status(self, issue: Issue) -> IssueStatus | None:
        """The issue's state, why it was closed, and the open or merged pull requests of its own
        repository that are linked to it. Best effort: None when nothing could be read.

        GraphQL with a token (the Development sidebar and the closing pull request), else the
        REST timeline, which sees cross-references and closing commits but not the sidebar.
        """
        if self._has_token:
            try:
                return await self._issue_status_graphql(issue)
            except (GitHubError, KeyError, TypeError, ValueError) as exc:
                log.info("issue status through GraphQL failed, trying REST: %s", exc)
        try:
            return await self._issue_status_rest(issue)
        except (GitHubError, KeyError, TypeError, ValueError) as exc:
            log.warning("issue status skipped: %s", exc)
            return None

    async def _issue_status_graphql(self, issue: Issue) -> IssueStatus:
        data = await self._graphql(
            _STATUS_QUERY, {"owner": issue.owner, "repo": issue.repo, "number": issue.number}
        )
        node = ((data.get("repository") or {}).get("issue")) or {}
        if not node:
            raise GitHubError("GraphQL returned no issue")
        full_name = f"{issue.owner}/{issue.repo}".lower()
        prs: dict[int, LinkedPullRequest] = {}

        def add(pr: dict[str, Any] | None, closes: bool) -> None:
            if not pr or pr.get("__typename", "PullRequest") != "PullRequest":
                return
            if ((pr.get("repository") or {}).get("nameWithOwner") or "").lower() != full_name:
                return  # another project's pull request that mentions this issue
            linked = _linked_pr(
                number=pr.get("number"), url=pr.get("url"), title=pr.get("title"),
                state="merged" if pr.get("merged") else str(pr.get("state") or "").lower(),
                draft=bool(pr.get("isDraft")), merged_at=pr.get("mergedAt"), closes=closes,
            )
            if linked is None:
                return
            if (known := prs.get(linked.number)) is not None:
                linked.closes = linked.closes or known.closes
            prs[linked.number] = linked

        for pr in (node.get("closedByPullRequestsReferences") or {}).get("nodes") or []:
            add(pr, True)
        closed_by = closed_by_url = None
        for item in (node.get("timelineItems") or {}).get("nodes") or []:
            kind = (item or {}).get("__typename")
            if kind == "CrossReferencedEvent":
                add(item.get("source"), bool(item.get("willCloseTarget")))
            elif kind == "ConnectedEvent":
                add(item.get("subject"), True)
            elif kind == "ClosedEvent":
                closer = item.get("closer") or {}
                if closer.get("__typename") == "PullRequest" and closer.get("number"):
                    closed_by, closed_by_url = f"#{closer['number']}", closer.get("url")
                elif closer.get("__typename") == "Commit" and closer.get("oid"):
                    closed_by, closed_by_url = str(closer["oid"]), closer.get("url")
                else:
                    closed_by = closed_by_url = None
        state = str(node.get("state") or "open").lower()
        return IssueStatus(
            state=state,
            state_reason=(str(node["stateReason"]).lower() if node.get("stateReason") else None),
            closed_at=node.get("closedAt"),
            closed_by=closed_by if state == "closed" else None,
            closed_by_url=closed_by_url if state == "closed" else None,
            linked_prs=_kept(prs.values()),
            source="graphql",
        )

    async def _issue_status_rest(self, issue: Issue) -> IssueStatus:
        base = f"/repos/{issue.owner}/{issue.repo}"
        data, timeline = await asyncio.gather(
            self._get(f"{base}/issues/{issue.number}"),
            self._get(f"{base}/issues/{issue.number}/timeline", {"per_page": 100}),
        )
        full_name = f"{issue.owner}/{issue.repo}".lower()
        prs: dict[int, LinkedPullRequest] = {}
        closed_by = closed_by_url = None
        for event in timeline if isinstance(timeline, list) else []:
            if event.get("event") == "closed":
                sha = event.get("commit_id")
                closed_by = str(sha) if sha else None
                closed_by_url = (
                    f"https://github.com/{issue.owner}/{issue.repo}/commit/{sha}" if sha else None
                )
            source = (event.get("source") or {}).get("issue") or {}
            if event.get("event") != "cross-referenced" or not source.get("pull_request"):
                continue
            if ((source.get("repository") or {}).get("full_name") or "").lower() != full_name:
                continue
            merged_at = (source.get("pull_request") or {}).get("merged_at")
            linked = _linked_pr(
                number=source.get("number"), url=source.get("html_url"),
                title=source.get("title"),
                state="merged" if merged_at else str(source.get("state") or "").lower(),
                draft=bool(source.get("draft")), merged_at=merged_at,
                closes=_closes(source, issue.number),
            )
            if linked is not None:
                prs[linked.number] = linked
        state = str(data.get("state") or "open").lower()
        return IssueStatus(
            state=state,
            state_reason=data.get("state_reason"),
            closed_at=data.get("closed_at"),
            closed_by=closed_by if state == "closed" else None,
            closed_by_url=closed_by_url if state == "closed" else None,
            linked_prs=_kept(prs.values()),
            source="rest",
        )

    # -- duplicates ------------------------------------------------------------------------

    async def search_related_issues(
        self, issue: Issue, query: str, limit: int = 5
    ) -> list[Evidence]:
        """Other issues in the same repository that look like this one. Best effort, free."""
        # Most specific query first, then a looser one: every extra word narrows a GitHub search.
        queries: list[str] = []
        for max_terms in (5, 3):
            terms = _search_terms(query, max_terms=max_terms)
            if terms and terms not in queries:
                queries.append(terms)
        data: dict[str, Any] = {}
        for terms in queries:
            q = f"{terms} repo:{issue.owner}/{issue.repo} is:issue"
            try:
                data = await self._get("/search/issues", {"q": q, "per_page": limit + 1})
            except GitHubError as exc:
                log.warning("duplicate search skipped: %s", exc)
                return []
            if any(item.get("number") != issue.number for item in data.get("items", [])):
                break
        found: list[Evidence] = []
        for item in data.get("items", []):
            if item.get("number") == issue.number:
                continue
            state = item.get("state", "open")
            found.append(
                Evidence(
                    kind="duplicate",
                    title=f"#{item['number']} {item.get('title', '')}".strip(),
                    url=item.get("html_url"),
                    why=f"Similar issue in the same repository ({state}, "
                    f"{item.get('comments', 0)} comments).",
                )
            )
        return found[:limit]

    # -- fix ------------------------------------------------------------------------------

    async def find_fix_pr(self, issue: Issue) -> tuple[str, str] | None:
        """(merge commit sha, pull request URL) of a merged PR that references the issue."""
        try:
            return await self._find_fix_pr(issue)
        except GitHubError as exc:
            log.warning("fix lookup skipped: %s", exc)
            return None

    async def _find_fix_pr(self, issue: Issue) -> tuple[str, str] | None:
        repo_path = f"/repos/{issue.owner}/{issue.repo}"
        full_name = f"{issue.owner}/{issue.repo}".lower()
        candidates: list[dict[str, Any]] = []

        # 1. The issue timeline lists every pull request that mentioned it.
        timeline = await self._get(f"{repo_path}/issues/{issue.number}/timeline", {"per_page": 100})
        for event in timeline:
            source = (event.get("source") or {}).get("issue") or {}
            repo_of_source = (source.get("repository") or {}).get("full_name", "").lower()
            if (
                event.get("event") == "cross-referenced"
                and source.get("pull_request")
                and source["pull_request"].get("merged_at")
                and repo_of_source == full_name
            ):
                candidates.append(source)

        # 2. Fallback: search merged PRs, then keep only those that really name the issue.
        if not candidates:
            data = await self._get(
                "/search/issues",
                {"q": f"repo:{full_name} is:pr is:merged {issue.number}", "per_page": 10},
            )
            mention = re.compile(rf"(?<![\w/]){re.escape(f'#{issue.number}')}(?!\d)")
            for item in data.get("items", []):
                text = f"{item.get('title', '')}\n{item.get('body') or ''}"
                if item.get("pull_request", {}).get("merged_at") and mention.search(text):
                    candidates.append(item)

        if not candidates:
            return None
        best = max(candidates, key=lambda pr: (_closes(pr, issue.number), _merged_at(pr)))
        pr = await self._get(f"{repo_path}/pulls/{best['number']}")
        sha = pr.get("merge_commit_sha")
        if not pr.get("merged") or not sha:
            return None
        return sha, pr.get("html_url") or best.get("html_url", "")


_STATUS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      state stateReason closedAt
      closedByPullRequestsReferences(first: 20, includeClosedPrs: true) {
        nodes { number url title state merged mergedAt isDraft repository { nameWithOwner } }
      }
      timelineItems(last: 50, itemTypes: [CLOSED_EVENT, CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
        nodes {
          __typename
          ... on ClosedEvent {
            closer {
              __typename
              ... on PullRequest { number url }
              ... on Commit { oid url }
            }
          }
          ... on CrossReferencedEvent {
            willCloseTarget
            source {
              __typename
              ... on PullRequest {
                number url title state merged mergedAt isDraft repository { nameWithOwner }
              }
            }
          }
          ... on ConnectedEvent {
            subject {
              __typename
              ... on PullRequest {
                number url title state merged mergedAt isDraft repository { nameWithOwner }
              }
            }
          }
        }
      }
    }
  }
}
"""
MAX_LINKED_PRS = 20


def _linked_pr(
    *, number: Any, url: Any, title: Any, state: str, draft: bool, merged_at: Any, closes: bool
) -> LinkedPullRequest | None:
    """A linked pull request, or None for one that is closed without a merge (or unreadable)."""
    if state not in ("open", "merged") or not isinstance(number, int):
        return None
    url = str(url or "")
    if not url.startswith("https://github.com/"):
        return None
    return LinkedPullRequest(
        number=number, url=url, title=" ".join(strip_hidden(str(title or "")).split())[:200],
        state=state, draft=draft, merged_at=merged_at or None, closes=closes,
    )


def _kept(prs: Any) -> list[LinkedPullRequest]:
    """Closing ones first, then by number; at most MAX_LINKED_PRS."""
    return sorted(prs, key=lambda pr: (not pr.closes, pr.number))[:MAX_LINKED_PRS]


def _closes(pr: dict[str, Any], number: int) -> bool:
    """Does the PR text use a closing keyword for this issue ('fixes #12')?"""
    text = f"{pr.get('title', '')}\n{pr.get('body') or ''}"
    pattern = rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#{number}(?!\d)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _merged_at(pr: dict[str, Any]) -> str:
    return (pr.get("pull_request") or {}).get("merged_at") or ""


# -- one-shot helpers for callers that make a single request ----------------------------------


async def fetch_issue(url: str, *, token: str | None = None) -> Issue:
    async with GitHubClient(token) as gh:
        return await gh.fetch_issue(url)


async def search_related_issues(
    issue: Issue, query: str, *, token: str | None = None
) -> list[Evidence]:
    async with GitHubClient(token) as gh:
        return await gh.search_related_issues(issue, query)


async def find_fix_pr(issue: Issue, *, token: str | None = None) -> tuple[str, str] | None:
    async with GitHubClient(token) as gh:
        return await gh.find_fix_pr(issue)


async def issue_status(issue: Issue, *, token: str | None = None) -> IssueStatus | None:
    async with GitHubClient(token) as gh:
        return await gh.issue_status(issue)


async def target_activity(
    repo: str, branch: str, *, token: str | None = None
) -> dict[str, Any] | None:
    async with GitHubClient(token) as gh:
        return await gh.target_activity(repo, branch)
