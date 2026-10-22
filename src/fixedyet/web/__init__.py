"""Web demo: a FastAPI app plus a static single page.

This module also holds the one piece of validation shared with the MCP server, so that
neither of them has to import the FastAPI app just to check a URL.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# What people paste: https, http or no scheme, "www.", and the host in any case. Only this part
# is case-insensitive; owner and repository keep the case they were typed in.
_GITHUB_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)
_ISSUE_PATH = re.compile(
    r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/"
    r"issues/(?P<number>[1-9][0-9]{0,8})/?(?:[?#].*)?$"
)
_PULL_PATH = re.compile(r"^[^/]+/[^/]+/pull/\d+(?:[/?#].*)?$")

MAX_URL_LENGTH = 300

INVALID_URL_MESSAGE = (
    "That does not look like a public GitHub issue URL. "
    "Use the form https://github.com/owner/repo/issues/123."
)
PULL_REQUEST_URL_MESSAGE = (
    "That is a pull request URL, not a GitHub issue URL. FixedYet reproduces bug reports: use "
    "the URL of the issue the pull request is about, like https://github.com/owner/repo/issues/123."
)


class IssueRef(NamedTuple):
    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.number}"


def parse_issue_url(raw: str) -> IssueRef:
    """Parse a GitHub issue URL into its parts. Raises ValueError with a friendly message.

    http:// or no scheme, ``www.``, the host in any case, a trailing slash, a query string and an
    ``#issuecomment-...`` anchor are accepted; ``IssueRef.url`` is the plain https form. Pull
    request URLs, other hosts and anything with extra path segments are rejected.
    """
    text = raw.strip()
    if not text or len(text) > MAX_URL_LENGTH:
        raise ValueError(INVALID_URL_MESSAGE)
    prefix = _GITHUB_PREFIX.match(text)
    path = text[prefix.end() :] if prefix else ""
    match = _ISSUE_PATH.match(path)
    if match is None or match["repo"] in {".", ".."}:
        message = PULL_REQUEST_URL_MESSAGE if _PULL_PATH.match(path) else INVALID_URL_MESSAGE
        raise ValueError(message)
    return IssueRef(match["owner"], match["repo"], int(match["number"]))
