"""Evidence gathered after the verdict: similar reports, the fix, the release that ships it.

Nothing here can change the verdict. Web content is untrusted: it is shortened, stripped of control
characters, shown to the user as plain text, and never given to a model as instructions.

A web hit is kept only when it is about this bug, not merely about this project:

- it names the issue's distinctive identifiers (the ones fix mode localizes with:
  `fixmode.distinctive_terms`), weighed by shape: ``host_is_trusted``, ``CliRunner``,
  ``validate.Email`` and the classes the title quotes are strong; plain words are weak;
- or it cites the issue or the fixing pull request by number.

How much it must name depends on where it lives. Index pages (an issue list, a commit list, a
source file) and directories of companies and people never count. A same-repository issue is a
"similar" report unless the thread says it is a duplicate. The release that ships a fix comes from
the project's own changelog first (fetched from the default branch), then from its release notes.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from fixedyet.config import Settings
from fixedyet.github import GitHubClient
from fixedyet.models import Evidence, Issue, Usage, Verdict

log = logging.getLogger(__name__)

TAVILY_MAX_RESULTS = 10  # asked for: one basic search costs one credit whatever the count
MAX_WEB_EVIDENCE = 5  # kept after filtering
EXTRACT_URLS_PER_CREDIT = 5  # Tavily bills 1 credit per 5 extracted URLs (basic depth)
MAX_EXTRACT_URLS = 5  # one credit
MIN_WEB_SCORE = 0.1  # Tavily's relevance score; the name rules below do the real filtering
SNIPPET_CHARS = 200
QUERY_CHARS = 380  # Tavily rejects queries over 400 characters
CHANGELOG_BYTES = 3_000_000
CHANGELOG_TIMEOUT_S = 15.0

# Directories of companies, people and places: they match a project's name ("Marshmallow
# Products Private Limited", "Pydantic IT Department") and never a bug. Dropped.
NOISE_DOMAINS = (
    "tracxn.com", "rocketreach.co", "cbinsights.com", "crunchbase.com", "zoominfo.com",
    "linkedin.com", "mapquest.com", "glassdoor.com", "indeed.com", "yelp.com", "facebook.com",
    "instagram.com", "pitchbook.com", "dnb.com", "opencorporates.com", "apollo.io",
    "signalhire.com", "leadiq.com", "owler.com", "craft.co",
)
# Q&A sites and trackers where another user's report of the same bug can live.
FORUM_DOMAINS = (
    "stackoverflow.com", "stackexchange.com", "superuser.com", "serverfault.com",
    "bugs.python.org", "discuss.python.org", "mail.python.org", "gitlab.com", "bitbucket.org",
    "bugzilla.redhat.com", "bugs.debian.org", "bugs.launchpad.net", "bugs.gentoo.org",
    "bugzilla.suse.com", "gitlab.gnome.org", "codeberg.org",
)
# The project's own changelog, in the order a project most likely keeps it. Fetched from the
# default branch as plain text; the first that cites the issue or the pull request decides.
CHANGELOG_FILES = (
    "CHANGES.rst", "CHANGELOG.md", "CHANGES.md", "HISTORY.md", "CHANGELOG.rst", "HISTORY.rst",
    "NEWS.md", "NEWS.rst", "CHANGES", "CHANGELOG", "NEWS", "docs/changelog.rst",
    "docs/changes.rst", "docs/changelog.md", "docs/history.rst", "docs/news.rst",
)
_CHANGELOG_NAME = re.compile(r"(?i)(?:^|/)(?:changes|changelog|history|news|release[-_ ]?notes"
                             r"|whatsnew|whats[-_]new)(?:\.\w+)?/?$")

TextFetcher = Callable[[str], Awaitable[str | None]]
"""URL to its text, or None when it does not exist or cannot be read. Tests replace it."""


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


def _on(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


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


def rank_results(results: list[dict[str, Any]], repo: str) -> list[dict[str, Any]]:
    """Stable sort: preferred domains first, then Tavily's own score."""
    valid = [r for r in results if str(r.get("url", "")).startswith(("http://", "https://"))]
    return sorted(valid, key=lambda r: (_domain_rank(r["url"], repo), -_score(r)))


def _web_text(result: dict[str, Any]) -> str:
    """Title and content of a hit, with the markdown escapes of `trusted\\_hosts` undone."""
    text = f"{result.get('title') or ''}\n{result.get('content') or ''}"
    return re.sub(r"\\([_*\[\]()#.`-])", r"\1", text)


# -- what the issue is about -----------------------------------------------------------------------

_DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
_TITLE_CALL = re.compile(r"(?:(?<=\.)|\b)([A-Za-z_][A-Za-z0-9_]{2,})(?=\()")
_FILE_NAME = re.compile(r"\.(?:py|pyi|txt|cfg|toml|ini|md|rst|json|ya?ml|lock|html?|js)$")
_NESTED_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\(\s*([A-Za-z_][A-Za-z0-9_]*)\(")


def _strong_shape(term: str) -> bool:
    """A name nobody writes by accident: snake_case or CONSTANT_CASE (an inner underscore), a
    dotted path or a nested call (`str(Marker`), CamelCase with a second hump (`CliRunner`,
    `URLValidator`), letters and digits (`dup2`)."""
    core = term.strip("_")
    return (
        "_" in core
        or "." in core
        or "(" in core
        or re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]", core) is not None
        or re.fullmatch(r"[A-Za-z]+\d+[A-Za-z\d]*", core) is not None and len(core) >= 4
    )


@dataclass
class Terms:
    """The issue's distinctive names, the numbers that cite it, and the project's names."""

    strong: list[str] = field(default_factory=list)  # code-shaped names
    anchors: list[str] = field(default_factory=list)  # classes the title quotes: `Marker`
    weak: list[str] = field(default_factory=list)
    title: set[str] = field(default_factory=set)  # every name the title itself uses
    numbers: list[int] = field(default_factory=list)  # the issue, then the fixing pull request
    owner: str = ""
    repo: str = ""
    names: set[str] = field(default_factory=set)  # the project's names, lower case
    spoken: set[str] = field(default_factory=set)  # strong names from the title, prose or spans

    def hits(self, text: str, *, own: bool = False) -> tuple[list[str], list[str]]:
        """``(strong, weak)`` names that ``text`` mentions as whole words, case-sensitive. A
        quoted class of the title (`Marker`, `Email`) is strong on the project's own pages and
        weak anywhere else, where the same word means something else."""
        anchors = [t for t in self.anchors if _mentions_word(text, t)]
        strong = [t for t in self.strong if _mentions_word(text, t)]
        weak = [t for t in self.weak if _mentions_word(text, t)]
        return (strong + anchors, weak) if own else (strong, anchors + weak)

    def citation(self, text: str, *, own: bool) -> re.Match[str] | None:
        """Where ``text`` cites the issue or the pull request. On the project's own pages a bare
        ``#3384`` (or the ``:issue:`3384``` of a Sphinx changelog) is enough; anywhere else it
        takes ``owner/repo#3384`` or the issue's URL."""
        if not self.numbers:
            return None
        alternatives = "|".join(str(n) for n in self.numbers)
        repo = re.escape(f"{self.owner}/{self.repo}")
        anywhere = re.compile(
            rf"(?i)(?:{repo}#|github\.com/{repo}/(?:issues|pull)/)(?:{alternatives})(?!\d)"
        )
        found = anywhere.search(text)
        if found or not own:
            return found
        return _NUMBER_MENTION(alternatives).search(text)

    def cites(self, text: str, *, own: bool) -> bool:
        return self.citation(text, own=own) is not None

    def names_project(self, text: str) -> bool:
        low = text.lower()
        return f"{self.owner}/{self.repo}".lower() in low or any(n in low for n in self.names)


def _mentions_word(text: str, term: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text) is not None


def _NUMBER_MENTION(alternatives: str) -> re.Pattern[str]:  # noqa: N802 - a pattern factory
    return re.compile(
        rf"(?<![\w/.&])#(?:{alternatives})(?!\d)"
        rf"|/(?:pull|issues)/(?:{alternatives})(?!\d)"
        rf"|[:{{](?:issue|pr|pull|gh)[:}}]`(?:[^`<]*<)?(?:{alternatives})>?`"
        rf"|\b(?:gh|GH)-(?:{alternatives})(?!\d)"
    )


def issue_terms(
    issue: Issue, test_code: str = "", *, package: str | None = None,
    pr_url: str | None = None,
) -> Terms:
    """The names that make a page about *this* bug. Strong: the shapes of `_strong_shape`, and
    the calls and attribute paths of the issue's code spans (`validate.Email`, `str(Marker`).
    Anchors: the classes the title quotes (`Marker`). Weak: other names in the issue's code spans
    and prose. The project's own names are never terms."""
    # fixmode imports backport, which imports the pipeline, which imports this module
    from fixedyet.fixmode import distinctive_terms

    names = {issue.repo.lower()}
    if package:
        names.add(package.lower())
    names |= {n.removeprefix("python-").removeprefix("py-") for n in list(names)}
    names |= {n.replace("_", "-") for n in list(names)}
    names.discard("")
    text = without_tracebacks(f"{issue.title}\n{issue.body}")
    text = re.sub(r"`[\w./-]+`", lambda m: " " if _FILE_NAME.search(m.group(0)[1:-1]) else
                  m.group(0), text)  # `setup.cfg` names a file, not code
    weights = distinctive_terms(text, test_code)
    found = list(weights)
    title_spans = " ".join(re.findall(r"`([^`\n]+)`", issue.title))
    spans = " ".join(re.findall(r"`([^`\n]+)`", text))
    dotted = [d for d in _DOTTED.findall(spans)
              if not re.fullmatch(r"[\d.]+", d) and not _FILE_NAME.search(d)]
    dotted += [f"{outer}({inner}" for outer, inner in _NESTED_CALL.findall(spans)]
    title_names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", title_spans))
    title_names |= set(_TITLE_CALL.findall(issue.title))
    title_names |= {t for t in found if _mentions_word(issue.title, t)}
    strong: list[str] = []
    anchors: list[str] = []
    weak: list[str] = []
    candidates = [*dotted, *found, *sorted(title_names - set(found))]
    for term in candidates:
        if (term.lower() in names or term in strong or term in anchors or term in weak
                or len(term) < 3 or term in _TITLE_STOP):
            continue
        if _strong_shape(term):
            strong.append(term)
        elif term in title_names and term[:1].isupper() and len(term) >= 4:
            anchors.append(term)
        else:
            weak.append(term)
    numbers = [issue.number]
    if pr_url and (match := re.search(r"/pull/(\d+)", pr_url)):
        numbers.append(int(match.group(1)))
    # what the issue says in words or code spans, not what its code blocks or the test merely use
    # (`BaseModel` in a pydantic snippet is on every pydantic page)
    spoken = {t for t in strong + anchors if weights.get(t, 2.0) >= 2.0 or t in title_names}
    return Terms(strong=strong, anchors=anchors, weak=weak, title=title_names, numbers=numbers,
                 owner=issue.owner, repo=issue.repo, names=names, spoken=spoken)


_TITLE_STOP = frozenset({"str", "repr", "len", "int", "list", "dict", "set", "the", "and"})
_FRAME = re.compile(r'^\s*File "[^"]*", line \d+, in \S+\s*$')


def without_tracebacks(text: str) -> str:
    """The text without traceback frames (`File "...", line N, in f` and the source line under
    it): names like `_inner_hookexec` are the test runner's, not the bug's."""
    out: list[str] = []
    skip_next = False
    for line in text.splitlines():
        if _FRAME.match(line):
            skip_next = True
            continue
        if skip_next and line.startswith((" ", "\t")) and line.strip():
            skip_next = False
            continue
        skip_next = False
        out.append(line)
    return "\n".join(out)


def thread_duplicates(issue: Issue) -> set[int]:
    """Issue numbers the thread itself calls duplicates ("Duplicate of #12", "same bug as #12")."""
    repo = re.escape(f"github.com/{issue.owner}/{issue.repo}/issues/")
    pattern = re.compile(
        r"(?i)\b(?:duplicate[sd]?(?:\s+(?:of|by))?|dup(?:e)?\s+of|same\s+(?:bug|issue|problem)"
        r"\s+as|closing\s+in\s+favou?r\s+of)\s*:?\s+(?:issue\s+)?(?:#|https?://" + repo + r")"
        r"(\d+)"
    )
    text = "\n".join([issue.body, *issue.comments])
    return {int(n) for n in pattern.findall(text) if int(n) != issue.number}


# -- judging a web hit -----------------------------------------------------------------------------

_GITHUB_PATH = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)(?:/(?P<section>[^/]+)(?:/(?P<rest>[^?#]*))?)?/?$"
)


@dataclass
class Page:
    """Where a hit lives. ``place`` is one of: own_issue, own_change (a pull request, commit or
    discussion of the project), own_notes (its release notes or changelog), docs (the project's
    own documentation), forum (another tracker, a Q&A site, another repository), web, index
    (a listing: dropped), noise (a directory of companies: dropped)."""

    place: str
    number: int | None = None


def classify(url: str, terms: Terms) -> Page:
    host = _host(url)
    path = urlparse(url).path or "/"
    if host == "github.com":
        match = _GITHUB_PATH.match(path)
        if not match:
            return Page("index")
        own = (match["owner"].lower(), match["repo"].lower()) == (
            terms.owner.lower(), terms.repo.lower()
        )
        section, rest = match["section"] or "", match["rest"] or ""
        number = re.match(r"(\d+)(?:/|$)", rest)
        if section in ("issues", "pull", "discussions") and number:
            kind = "own_issue" if section == "issues" else "own_change"
            return Page(kind if own else "forum", int(number.group(1)))
        if section == "commit" and re.match(r"[0-9a-f]{7,40}(?:/|$)", rest):
            return Page("own_change" if own else "forum")
        if own and section == "releases":
            return Page("own_notes")
        if own and section == "blob" and _CHANGELOG_NAME.search(rest):
            return Page("own_notes")
        return Page("index")  # a repository root, a list of issues or commits, a source file
    if _on(host, NOISE_DOMAINS):
        return Page("noise")
    if _on(host, FORUM_DOMAINS):
        return Page("forum")
    labels = set(host.split(".")) | set(re.split(r"[.-]", host))  # pytest-mock.readthedocs.io
    first = path.strip("/").split("/", 1)[0].lower()
    own_site = bool(labels & terms.names) or first in terms.names or (
        host == "pypi.org" and path.lower().rstrip("/").split("/")[-1] in terms.names
    )
    if own_site:
        if re.search(r"/_(?:modules|sources)/", path):
            return Page("index")  # Sphinx's copy of the source code
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        tail = re.sub(r"\.html?$", "", tail)
        return Page("own_notes" if _CHANGELOG_NAME.search(tail) else "docs")
    return Page("web")


@dataclass
class Hit:
    evidence: Evidence
    rank: tuple[int, int, int, int, float]
    url_key: str


_KIND_ORDER = {"duplicate": 0, "changelog": 1, "similar": 2, "related": 3}
_PLACE_ORDER = {"own_issue": 0, "own_change": 1, "own_notes": 2, "forum": 3, "docs": 4, "web": 5}


def judge(
    result: dict[str, Any], issue: Issue, terms: Terms, duplicates: AbstractSet[int] = frozenset()
) -> Hit | None:
    """The evidence a Tavily hit is worth, or None when it is not about this bug."""
    url = str(result.get("url") or "")
    if not url.startswith(("http://", "https://")) or _score(result) < MIN_WEB_SCORE:
        return None
    page = classify(url, terms)
    if page.place in ("index", "noise"):
        return None
    if page.place == "own_issue" and page.number == issue.number:
        return None  # the search found the issue itself
    text = _web_text(result)
    own = page.place in ("own_issue", "own_change", "own_notes", "docs")
    citation = terms.citation(text, own=own)
    cited = citation is not None
    strong, weak = terms.hits(text, own=own)
    s, w = len(strong), len(weak)
    if page.place == "own_issue":
        keep = cited or s >= 1 or w >= 2
    elif page.place == "own_change":
        keep = cited or (s >= 1 and s + w >= 2)
    elif page.place == "own_notes":  # release notes list everything: a code name must be in them
        keep = cited or any(t in terms.strong and t in terms.spoken for t in strong)
    elif page.place == "docs":  # every page of the docs names the project's main classes
        keep = (cited or any(t in terms.spoken for t in strong) or s >= 2
                or (w >= 2 and bool(set(weak) & terms.title)))
    elif page.place == "forum":  # another tracker or a Q&A site: it must name what the title does
        keep = cited or (s + w >= 2 and any(t in terms.title for t in strong))
    else:  # a blog post or a tutorial is evidence only when it cites the issue
        keep = cited
    if not keep:
        return None

    host = _host(url)
    if page.place == "own_issue":
        dup = page.number in duplicates or re.search(
            rf"(?i)duplicate\s+of\s+#{issue.number}(?!\d)", text
        )
        kind = "duplicate" if dup else "similar"
    elif page.place == "own_notes" and cited:
        kind = "changelog"
    else:
        kind = "related"
    cite = [f"#{re.findall(r'\d+', citation.group(0))[-1]}"] if citation else []
    named = cite + strong + weak
    why = f"{host}: names {', '.join(named[:4])}" if named else host
    snippet = _snippet(text, ([citation.group(0)] if citation else []) + strong + weak)
    evidence = Evidence(
        kind=kind,  # type: ignore[arg-type]
        title=clean_web_text(str(result.get("title") or url), 120),
        url=url,
        why=clean_web_text(f"{why}. {snippet}" if snippet else why),
    )
    rank = (_KIND_ORDER[kind], -int(cited), _PLACE_ORDER[page.place], -(2 * s + w),
            -_score(result))
    return Hit(evidence, rank, _url_key(url))


def _snippet(text: str, needles: list[str], width: int = 150) -> str:
    """The text around the first needle that occurs, else the start of the text. A citation
    (`#3384`) ends the entry it belongs to, so the text before it is shown."""
    flat = " ".join(text.split())
    for needle in needles:
        at = flat.find(needle)
        if at >= 0:
            before = width - len(needle) if re.search(r"\d$", needle) else width // 3
            start = max(0, at - before)
            cut = flat[start:start + width]
            return ("..." if start else "") + cut
    return flat[:width]


def _url_key(url: str) -> str:
    """A URL without its fragment, query and trailing `/files` or `#issuecomment-...`."""
    parsed = urlparse(url)
    path = re.sub(r"^(/[^/]+/[^/]+/(?:issues|pull)/\d+).*$", r"\1", parsed.path)
    return f"{(parsed.hostname or '').lower().removeprefix('www.')}{path.rstrip('/')}".lower()


def web_results_to_evidence(
    results: list[dict[str, Any]], issue: Issue, terms: Terms | None = None,
    duplicates: set[int] | None = None,
) -> list[Evidence]:
    """The Tavily hits that are about this bug, best first, at most `MAX_WEB_EVIDENCE`."""
    terms = terms or issue_terms(issue)
    dups = duplicates if duplicates is not None else thread_duplicates(issue)
    hits: dict[str, Hit] = {}
    for result in results:
        hit = judge(result, issue, terms, dups)
        if hit and (hit.url_key not in hits or hit.rank < hits[hit.url_key].rank):
            hits[hit.url_key] = hit
    best = sorted(hits.values(), key=lambda h: h.rank)
    return [h.evidence for h in best[:MAX_WEB_EVIDENCE]]


def related_issues_to_evidence(
    related: list[Evidence], issue: Issue, terms: Terms, duplicates: set[int]
) -> list[Evidence]:
    """GitHub's search hits for issues in the same repository, judged by their titles alone (the
    search sees only the title's words): kept when the title names the bug's identifiers, and a
    "similar" issue unless the thread calls it a duplicate."""
    kept: list[Evidence] = []
    for item in related:
        number = re.search(r"/issues/(\d+)", item.url or "")
        if number and int(number.group(1)) == issue.number:
            continue
        strong, weak = terms.hits(item.title)  # a title is short: a quoted class alone is weak
        if not (strong or len(weak) >= 2):
            continue
        dup = bool(number) and int(number.group(1)) in duplicates  # type: ignore[union-attr]
        named = ", ".join((strong + weak)[:4])
        why = item.why.replace("Similar issue in the same repository", "Same repository")
        kept.append(item.model_copy(update={
            "kind": "duplicate" if dup else "similar",
            "why": f"{why} Its title names {named}." if named else why,
        }))
    return kept


# -- changelogs ------------------------------------------------------------------------------------

_HEADING = re.compile(
    r"^(?:#{1,6}\s*|=+\s*)?(?:(?:version|release)\s+)?\[?v?(\d+(?:\.\d+){1,3}[\w.+-]*)",
    re.IGNORECASE,
)
_UNRELEASED = re.compile(
    r"^(?:#{1,6}\s*)?[\[(]?(?:unreleased|not yet released|in development)[\])]?(?:\s*[-:(].*)?$",
    re.IGNORECASE,
)


@dataclass
class ReleaseEntry:
    version: str | None  # None under an "Unreleased" heading
    unreleased: bool


def release_entry(text: str, numbers: list[int], after: str | None = None) -> ReleaseEntry | None:
    """The version heading above the first changelog line that cites one of `numbers`.

    Changelogs are written newest first, with the version as a heading above its entries, so the
    last heading seen before a citation is the release that shipped the change. An "Unreleased"
    heading, or "Unreleased" on the lines under a version heading (the Pallets style), marks the
    entry as not released yet. With ``after`` (the reported version), a citation under that
    version or an older one is skipped: the fix cannot have shipped before the bug was reported.
    """
    if not numbers:
        return None
    mention = _NUMBER_MENTION("|".join(str(n) for n in numbers))
    floor = _version_key(after)
    version: str | None = None
    unreleased = False
    started = False
    since_heading = 99
    for line in text.splitlines():
        stripped = line.strip().strip("*_¶ ")
        since_heading += 1 if stripped else 0
        heading = _HEADING.match(stripped) if len(stripped) < 80 else None
        if heading:
            version, unreleased, started, since_heading = (
                heading.group(1).rstrip(".:)]"), False, True, 0
            )
            unreleased = bool(re.search(r"(?i)\bunreleased\b", stripped))
            continue
        if _UNRELEASED.match(stripped) and since_heading <= 3 and started:
            unreleased = True
            continue
        if _UNRELEASED.match(stripped):
            version, unreleased, started, since_heading = None, True, True, 0
            continue
        if started and mention.search(line):
            if version and floor and not unreleased and (_version_key(version) or ()) <= floor:
                continue
            return ReleaseEntry(version, unreleased)
    return None


def release_from_text(text: str, numbers: list[int]) -> str | None:
    """The released version that a changelog lists the fix under, or None."""
    entry = release_entry(text, numbers)
    return entry.version if entry and not entry.unreleased else None


def _version_key(text: str | None) -> tuple[int, ...] | None:
    match = re.match(r"^v?(\d+(?:\.\d+){0,3})", (text or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


async def fetch_text(url: str) -> str | None:
    """GET a plain-text file (a raw changelog). None for anything but a readable 200."""
    try:
        async with httpx.AsyncClient(timeout=CHANGELOG_TIMEOUT_S, follow_redirects=False) as http:
            async with http.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > CHANGELOG_BYTES:
                        break
                return b"".join(chunks)[:CHANGELOG_BYTES].decode("utf-8", errors="replace")
    except httpx.HTTPError as exc:
        log.debug("changelog fetch failed for %s: %s", url, type(exc).__name__)
        return None


# -- Tavily ----------------------------------------------------------------------------------------


def make_tavily(settings: Settings) -> Any | None:
    """An async Tavily client, or None when no key is configured (evidence is then skipped)."""
    if not settings.tavily_api_key:
        return None
    from tavily import AsyncTavilyClient

    return AsyncTavilyClient(api_key=settings.tavily_api_key)


def search_queries(issue: Issue, package: str | None = None) -> list[str]:
    """The project and the issue's title, nothing else; the second query is asked only when the
    first finds nothing worth keeping.

    The error line of the reproduction test stays out: it is the test's own assertion (`assert
    '00' == 'abc'`), not what the reporter saw, and it pulled in pages about the error in general
    (an OSError tutorial, horoscopes for a pickling error). Identifiers added to the title made
    Tavily return fewer and worse hits, or none, in a trial on 13 real issues. An owner's name
    can take over the query ("pallets/click ..." returned pages about wooden pallets), hence the
    second form, which names the language and the package instead.
    """
    name = package or issue.repo
    return [
        clean_web_text(f"{issue.owner}/{issue.repo} {issue.title}", QUERY_CHARS),
        clean_web_text(f"python {name} {issue.title}", QUERY_CHARS),
    ]


async def search_web(
    tavily: Any, issue: Issue, usage: Usage, raw: list[dict[str, Any]] | None = None, *,
    terms: Terms | None = None, package: str | None = None,
) -> list[Evidence]:
    """A basic Tavily search (1 credit), judged by `judge`; a second one (1 more credit) only
    when the first keeps nothing. ``raw`` receives the hits as they came, so that they can be
    judged again once the fixing pull request is known."""
    terms = terms or issue_terms(issue, package=package)
    results: list[dict[str, Any]] = []
    kept: list[Evidence] = []
    for query in search_queries(issue, package):
        try:
            # no exclude_domains: in the trial it turned good answers into pages about pallets
            response = await tavily.search(
                query=query, search_depth="basic", max_results=TAVILY_MAX_RESULTS,
                topic="general",
            )
        except Exception as exc:  # Tavily errors must never fail a run
            log.warning("Tavily search failed: %s", exc)
            break
        usage.search_credits += 1
        results += [r for r in response.get("results") or [] if isinstance(r, dict)]
        kept = web_results_to_evidence(results, issue, terms)
        if kept:
            break
    if raw is not None:
        raw.extend(results)
    return kept


async def find_release(
    tavily: Any | None, issue: Issue, pr_url: str | None, usage: Usage, *,
    fetch: TextFetcher | None = None, notes_urls: Iterable[str] = (),
    reported_version: str | None = None,
) -> tuple[str | None, Evidence] | None:
    """The release that lists the fix: ``(version, evidence)``, version None when the changelog
    lists it as not released yet.

    1. The project's own changelog file on the default branch (plain HTTP, no credits).
    2. Its release notes: docs changelog pages the search found citing the issue, and the GitHub
       releases page, extracted with Tavily (one credit for up to five pages).
    """
    numbers = [issue.number]
    if pr_url and (match := re.search(r"/pull/(\d+)", pr_url)):
        numbers.append(int(match.group(1)))
    cited = " / ".join(f"#{n}" for n in numbers)

    def found(entry: ReleaseEntry, url: str, source: str) -> tuple[str | None, Evidence]:
        if entry.unreleased:
            where = f" under {entry.version}" if entry.version else ""
            return None, Evidence(
                kind="changelog", title=f"Not released yet (listed{where} as unreleased)",
                url=url, why=f"{source} lists {cited}{where} as unreleased.",
            )
        return entry.version, Evidence(
            kind="changelog", title=f"Released in {entry.version}", url=url,
            why=f"{source} lists {cited} under {entry.version}.",
        )

    fetch = fetch or fetch_text
    base = f"https://raw.githubusercontent.com/{issue.owner}/{issue.repo}/{issue.default_branch}"
    files = [f"{base}/{name}" for name in CHANGELOG_FILES]
    texts = await asyncio.gather(*(fetch(url) for url in files), return_exceptions=True)
    for url, text in zip(files, texts, strict=True):
        if isinstance(text, str) and text:
            entry = release_entry(text, numbers, reported_version)
            if entry:
                name = url.removeprefix(base + "/")
                blob = f"https://github.com/{issue.owner}/{issue.repo}/blob/"
                return found(entry, f"{blob}{issue.default_branch}/{name}",
                             f"The project's {name}")
    if tavily is None:
        return None
    urls = list(dict.fromkeys([*notes_urls,
                               f"https://github.com/{issue.owner}/{issue.repo}/releases"]))
    urls = urls[-MAX_EXTRACT_URLS:] if len(urls) > MAX_EXTRACT_URLS else urls
    try:
        response = await tavily.extract(urls=urls, extract_depth="basic", format="markdown")
    except Exception as exc:
        log.warning("Tavily extract failed: %s", exc)
        return None
    usage.search_credits += math.ceil(len(urls) / EXTRACT_URLS_PER_CREDIT)
    pages = {str(p.get("url")): str(p.get("raw_content") or "")
             for p in response.get("results") or [] if isinstance(p, dict)}
    for url in urls:  # our order, not Tavily's: the project's docs before GitHub's release list
        entry = release_entry(pages.get(url, ""), numbers, reported_version)
        if entry:
            source = "The release notes" if "/releases" in url else "The project's changelog"
            return found(entry, url, source)
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
    test_code: str = "",
    package: str | None = None,
    reported_version: str | None = None,
    fetch: TextFetcher | None = None,
) -> EvidenceResult:
    """Collect evidence for a decided verdict. Every source is optional and failure-tolerant.

    `github=None` skips the GitHub lookups (eval mode has no real issue); a missing Tavily key
    skips the web search and the release-notes extraction.
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
    terms = issue_terms(issue, test_code, package=package)
    duplicates = thread_duplicates(issue)
    raw_hits: list[dict[str, Any]] = []

    async def related() -> list[Evidence]:
        if not github:
            return []
        found = await github.search_related_issues(issue, issue.title)
        return related_issues_to_evidence(found, issue, terms, duplicates)

    async def web() -> None:
        if tavily:
            await search_web(tavily, issue, usage, raw_hits, terms=terms, package=package)

    async def fix() -> tuple[str, str] | None:
        if github is None or not look_for_fix or verdict is not Verdict.FIXED_ON_MAIN:
            return None
        return await github.find_fix_pr(issue)

    similar, _, fix_pr = await asyncio.gather(related(), web(), fix())
    if fix_pr:  # a changelog that cites the pull request cites the fix
        terms = issue_terms(issue, test_code, package=package, pr_url=fix_pr[1])
    web_hits = web_results_to_evidence(raw_hits, issue, terms, duplicates)
    seen: set[str] = set()
    for item in [*web_hits, *similar]:  # a web hit says more than a bare search-result title
        key = _url_key(item.url) if item.url else ""
        if key and key in seen:
            continue
        seen.add(key)
        result.evidence.append(item)
    result.evidence.sort(key=lambda e: _KIND_ORDER.get(e.kind, 9) if e.kind != "log" else -1)

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
        notes = [str(r.get("url")) for r in raw_hits
                 if classify(str(r.get("url") or ""), terms).place == "own_notes"
                 and _host(str(r.get("url") or "")) != "github.com"
                 and terms.cites(_web_text(r), own=True)]
        release = await find_release(
            tavily, issue, pr_url, usage, fetch=fetch, notes_urls=notes[:3],
            reported_version=reported_version,
        )
        if release:
            result.fixed_in_release, release_evidence = release
            result.evidence = [
                e for e in result.evidence
                if not (e.kind == "changelog" and e.url and _url_key(e.url) == _url_key(
                    release_evidence.url or ""))
            ]
            result.evidence.append(release_evidence)
    return result
