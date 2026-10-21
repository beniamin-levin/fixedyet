"""Evidence gathered after the verdict: duplicates, upstream reports, the fix, the release.

Nothing here can change the verdict. Web content is untrusted: it is shortened, stripped of control
characters, shown to the user as plain text, and never given to a model as instructions.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from fixedyet.config import Settings
from fixedyet.github import GitHubClient
from fixedyet.models import Evidence, Issue, Usage, Verdict

log = logging.getLogger(__name__)

TAVILY_MAX_RESULTS = 5
EXTRACT_URLS_PER_CREDIT = 5  # Tavily bills 1 credit per 5 extracted URLs (basic depth)
MIN_WEB_SCORE = 0.2  # Tavily's relevance score; below this a hit is noise (a "Hello World" blog)
SNIPPET_CHARS = 200
QUERY_CHARS = 380  # Tavily rejects queries over 400 characters


class EvidenceResult(BaseModel):
    evidence: list[Evidence] = Field(default_factory=list)
    fixing_commit: str | None = None
    fixing_pr_url: str | None = None
    fixed_in_release: str | None = None


# -- small pure helpers ----------------------------------------------------------------------------


def key_error_line(log_text: str) -> str:
    """The most telling line of a pytest log: first `E   ...` line, else an `...Error: ...` line."""
    lines = [line.rstrip() for line in log_text.splitlines()]
    for line in lines:
        if line.startswith("E ") and len(line.strip()) > 3:
            return line[1:].strip()[:160]
    for line in reversed(lines):
        if re.search(r"\b\w*(?:Error|Exception)\b", line):
            return line.strip()[:160]
    return ""


def clean_web_text(text: str, limit: int = SNIPPET_CHARS) -> str:
    """One line of plain text: control characters and markup-ish brackets removed, then cut."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"[<>`]", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _domain_rank(url: str, repo: str) -> int:
    """Lower is better: GitHub, Stack Overflow, the project's docs, then everything else."""
    host = _host(url)
    if host == "github.com":
        return 0
    if host.endswith("stackoverflow.com"):
        return 1
    if host.endswith(("readthedocs.io", "readthedocs.org")) or host.startswith("docs.") or (
        repo.lower() in host
    ):
        return 2
    return 3


def _score(result: dict[str, Any]) -> float:
    """Tavily's relevance score; a result that carries none is kept, not guessed at."""
    try:
        return float(result["score"])
    except (KeyError, TypeError, ValueError):
        return 1.0


def _names_project(result: dict[str, Any], issue: Issue) -> bool:
    """Does the hit mention the project at all? A search for "pallets/click" can return a page
    about wooden pallets; GitHub pages are always about some project, so they pass."""
    url = str(result.get("url", ""))
    if _host(url) == "github.com":
        return True
    text = f"{url} {result.get('title') or ''} {result.get('content') or ''}".lower()
    return issue.repo.lower() in text


def rank_results(results: list[dict[str, Any]], repo: str) -> list[dict[str, Any]]:
    """Stable sort: preferred domains first, then Tavily's own score."""
    valid = [r for r in results if str(r.get("url", "")).startswith(("http://", "https://"))]
    return sorted(valid, key=lambda r: (_domain_rank(r["url"], repo), -_score(r)))


def web_results_to_evidence(results: list[dict[str, Any]], issue: Issue) -> list[Evidence]:
    own = f"github.com/{issue.owner}/{issue.repo}/issues/{issue.number}".lower()
    found: list[Evidence] = []
    relevant = [r for r in results if _score(r) >= MIN_WEB_SCORE and _names_project(r, issue)]
    for result in rank_results(relevant, issue.repo)[:TAVILY_MAX_RESULTS]:
        url = str(result["url"])
        if own in url.lower():
            continue  # the search found the issue itself
        host = _host(url)
        same_repo_issue = (
            host == "github.com"
            and f"/{issue.owner}/{issue.repo}/issues/".lower() in url.lower()
        )
        found.append(
            Evidence(
                kind="duplicate" if same_repo_issue else "upstream",
                title=clean_web_text(str(result.get("title") or url), 120),
                url=url,
                why=f"{host}: {clean_web_text(str(result.get('content') or ''))}".strip(": "),
            )
        )
    return found


_HEADING = re.compile(
    r"^(?:#{1,6}\s*|=+\s*)?(?:(?:version|release)\s+)?\[?v?(\d+(?:\.\d+){1,3}[\w.+-]*)",
    re.IGNORECASE,
)


def release_from_text(text: str, numbers: list[int]) -> str | None:
    """The version heading above the first changelog line that mentions one of `numbers`.

    Changelogs are written newest first, with the version as a heading above its entries, so the
    last heading seen before a mention is the release that shipped the change.
    """
    if not numbers:
        return None
    alternatives = "|".join(str(n) for n in numbers)
    mention = re.compile(
        rf"(?<![\w/.])#(?:{alternatives})(?!\d)|/(?:pull|issues)/(?:{alternatives})(?!\d)"
    )
    version: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        heading = _HEADING.match(stripped) if len(stripped) < 80 else None
        if heading:
            version = heading.group(1).rstrip(".:)]")
        elif version and mention.search(line):
            return version
    return None


# -- Tavily ----------------------------------------------------------------------------------------


def make_tavily(settings: Settings) -> Any | None:
    """An async Tavily client, or None when no key is configured (evidence is then skipped)."""
    if not settings.tavily_api_key:
        return None
    from tavily import AsyncTavilyClient

    return AsyncTavilyClient(api_key=settings.tavily_api_key)


async def search_web(tavily: Any, issue: Issue, error_line: str, usage: Usage) -> list[Evidence]:
    """One basic Tavily search (1 credit): issue title plus the key error line."""
    query = clean_web_text(f"{issue.owner}/{issue.repo} {issue.title} {error_line}", QUERY_CHARS)
    try:
        response = await tavily.search(
            query=query, search_depth="basic", max_results=TAVILY_MAX_RESULTS, topic="general"
        )
    except Exception as exc:  # Tavily errors must never fail a run
        log.warning("Tavily search failed: %s", exc)
        return []
    usage.search_credits += 1
    return web_results_to_evidence(response.get("results") or [], issue)


async def find_release(
    tavily: Any, issue: Issue, pr_url: str | None, usage: Usage
) -> tuple[str, Evidence] | None:
    """Extract changelog pages; return (version, evidence) for the release that mentions the fix."""
    base = f"https://raw.githubusercontent.com/{issue.owner}/{issue.repo}/{issue.default_branch}"
    urls = [f"https://github.com/{issue.owner}/{issue.repo}/releases"] + [
        f"{base}/{name}" for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "CHANGES.rst")
    ]
    numbers = [issue.number]
    if pr_url and (match := re.search(r"/pull/(\d+)", pr_url)):
        numbers.append(int(match.group(1)))
    try:
        response = await tavily.extract(urls=urls, extract_depth="basic", format="markdown")
    except Exception as exc:
        log.warning("Tavily extract failed: %s", exc)
        return None
    usage.search_credits += math.ceil(len(urls) / EXTRACT_URLS_PER_CREDIT)
    for page in response.get("results") or []:
        version = release_from_text(str(page.get("raw_content") or ""), numbers)
        if version:
            mentioned = " / ".join(f"#{n}" for n in numbers)
            return version, Evidence(
                kind="changelog",
                title=f"Released in {version}",
                url=page.get("url"),
                why=f"The changelog entry for {version} mentions {mentioned}.",
            )
    return None


# -- the whole step --------------------------------------------------------------------------------


async def gather_evidence(
    *,
    issue: Issue,
    verdict: Verdict,
    reported_log: str,
    usage: Usage,
    settings: Settings,
    github: GitHubClient | None = None,
    tavily: Any | None = None,
    look_for_fix: bool = True,
) -> EvidenceResult:
    """Collect evidence for a decided verdict. Every source is optional and failure-tolerant.

    `github=None` skips the GitHub lookups (eval mode has no real issue); a missing Tavily key
    skips the web search and the changelog extraction.
    """
    result = EvidenceResult()
    tavily = tavily if tavily is not None else make_tavily(settings)
    error_line = key_error_line(reported_log)
    if error_line:
        result.evidence.append(
            Evidence(
                kind="log",
                title="Key error at the reported version",
                why=clean_web_text(error_line),
            )
        )

    async def duplicates() -> list[Evidence]:
        return await github.search_related_issues(issue, issue.title) if github else []

    async def web() -> list[Evidence]:
        return await search_web(tavily, issue, error_line, usage) if tavily else []

    async def fix() -> tuple[str, str] | None:
        if github is None or not look_for_fix or verdict is not Verdict.FIXED_ON_MAIN:
            return None
        return await github.find_fix_pr(issue)

    related, web_hits, fix_pr = await asyncio.gather(duplicates(), web(), fix())
    seen: set[str] = set()
    for item in [*related, *web_hits]:
        if item.url and item.url in seen:
            continue
        seen.add(item.url or "")
        result.evidence.append(item)

    if fix_pr:
        sha, pr_url = fix_pr
        result.fixing_commit, result.fixing_pr_url = sha, pr_url
        result.evidence.append(
            Evidence(
                kind="upstream",
                title="Merged pull request that references this issue",
                url=pr_url,
                why=f"Merge commit {sha[:10]}.",
            )
        )
        if tavily:
            found = await find_release(tavily, issue, pr_url, usage)
            if found:
                result.fixed_in_release, release_evidence = found
                result.evidence.append(release_evidence)
    return result
