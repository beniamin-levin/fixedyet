"""Web demo: a FastAPI app plus a static single page.

This module also holds the one piece of validation shared with the MCP server, so that
neither of them has to import the FastAPI app just to check a URL.
"""

from __future__ import annotations

import re
from typing import NamedTuple

_ISSUE_URL = re.compile(
    r"^https://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/"
    r"issues/(?P<number>[1-9][0-9]{0,8})/?(?:[?#].*)?$"
)

MAX_URL_LENGTH = 300

INVALID_URL_MESSAGE = (
    "That does not look like a public GitHub issue URL. "
    "Use the form https://github.com/owner/repo/issues/123."
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

    Pull request URLs, other hosts, plain http and anything with extra path segments are
    rejected. A trailing ``#issuecomment-...`` or query string is accepted and dropped.
    """
    text = raw.strip()
    if not text or len(text) > MAX_URL_LENGTH:
        raise ValueError(INVALID_URL_MESSAGE)
    match = _ISSUE_URL.match(text)
    if match is None or match["repo"] in {".", ".."}:
        raise ValueError(INVALID_URL_MESSAGE)
    return IssueRef(match["owner"], match["repo"], int(match["number"]))
