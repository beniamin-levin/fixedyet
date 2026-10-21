from __future__ import annotations

from typing import Any

import pytest

from fixedyet.config import Settings
from fixedyet.evidence import (
    ReleaseEntry,
    classify,
    clean_web_text,
    find_release,
    gather_evidence,
    issue_terms,
    key_error_line,
    make_tavily,
    rank_results,
    related_issues_to_evidence,
    release_entry,
    release_from_text,
    search_queries,
    search_web,
    thread_duplicates,
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
                 number=42, title="parse_items() drops the last item", default_branch="main",
                 body="`ItemParser.parse_items` drops the last item.")


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
        {"url": "https://github.com/acme/widgets/issues/42", "title": "this issue",
         "content": "parse_items drops it"},
        {"url": "https://github.com/acme/widgets/issues/9", "title": "Dup\nissue",
         "content": "same\nbug in parse_items"},
        {"url": "https://stackoverflow.com/q/1", "title": "SO",
         "content": "parse_items() and ItemParser lose the last element"},
    ]
    found = web_results_to_evidence(results, issue, issue_terms(issue))
    assert [(e.kind, e.url) for e in found] == [
        ("similar", "https://github.com/acme/widgets/issues/9"),
        ("related", "https://stackoverflow.com/q/1"),
    ]
    assert found[0].title == "Dup issue"
    assert found[0].why.startswith("github.com: names parse_items. ")
    assert "same bug in parse_items" in found[0].why


def test_a_same_repository_issue_is_a_duplicate_only_when_the_thread_says_so(
    issue: Issue,
) -> None:
    hit = {"url": "https://github.com/acme/widgets/issues/9", "title": "t",
           "content": "parse_items loses the last item"}
    assert web_results_to_evidence([hit], issue)[0].kind == "similar"
    issue.comments.append("Duplicate of #9")
    assert web_results_to_evidence([hit], issue)[0].kind == "duplicate"
    issue.comments.clear()
    closed_as_ours = {**hit, "content": "parse_items again. Duplicate of #42"}
    assert web_results_to_evidence([closed_as_ours], issue)[0].kind == "duplicate"


def test_thread_duplicates_reads_the_usual_phrasings(issue: Issue) -> None:
    issue.body = "Looks like the same bug as #12."
    issue.comments = ["duplicate of https://github.com/acme/widgets/issues/30",
                      "Dup of #31", "closing in favour of #33", "see #99 for context", "#42"]
    assert thread_duplicates(issue) == {12, 30, 31, 33}


def test_web_results_with_a_low_relevance_score_are_dropped(issue: Issue) -> None:
    results = [
        {"url": f"https://widgets.readthedocs.io/{name}", "title": name,
         "content": "parse_items", **({"score": score} if score is not None else {})}
        for name, score in (("relevant", 0.44), ("borderline", 0.1), ("noise", 0.05),
                            ("no-score", None), ("bad-score", "n/a"))
    ]
    titles = {e.title for e in web_results_to_evidence(results, issue)}
    assert titles == {"relevant", "borderline", "no-score", "bad-score"}


def test_pages_that_do_not_name_the_bug_are_dropped(issue: Issue) -> None:
    results = [
        # the project's name is not enough: these are about the project, not this bug
        {"url": "https://github.com/acme/widgets/issues/8", "title": "Docs typo",
         "content": "widgets docs"},
        {"url": "https://widgets.readthedocs.io/en/latest/", "title": "Widgets docs",
         "content": "Welcome to widgets"},
        {"url": "https://blog.example.com/p", "title": "Fixing Widgets crashes",
         "content": "parse_items and ItemParser in widgets"},  # a blog post: needs a citation
        {"url": "https://en.wikipedia.org/wiki/Pallet", "title": "Pallet", "content": "A deck"},
        {"url": "https://tracxn.com/d/widgets-ltd", "title": "Widgets Ltd",
         "content": "parse_items ItemParser"},  # a directory of companies, whatever it says
        # a forum post must name what the title names, and one more thing
        {"url": "https://stackoverflow.com/q/2", "title": "q", "content": "ItemParser only"},
        {"url": "https://stackoverflow.com/q/3", "title": "q", "content": "parse_items only"},
    ]
    assert web_results_to_evidence(results, issue) == []


def test_index_pages_are_never_evidence(issue: Issue) -> None:
    content = "parse_items ItemParser #42"
    urls = [
        "https://github.com/acme/widgets",
        "https://github.com/acme/widgets/issues",
        "https://github.com/acme/widgets/pulls",
        "https://github.com/acme/widgets/commits/main/src/widgets/parse.py",
        "https://github.com/acme/widgets/blob/main/src/widgets/parse.py",
        "https://github.com/acme/widgets/tree/main/src",
        "https://widgets.readthedocs.io/en/latest/_modules/widgets/parse.html",
    ]
    results = [{"url": u, "title": "t", "content": content} for u in urls]
    assert web_results_to_evidence(results, issue) == []


def test_a_page_elsewhere_counts_when_it_cites_the_issue(issue: Issue) -> None:
    results = [
        {"url": "https://blog.example.com/p", "title": "Upgrade notes",
         "content": "We pinned widgets because of acme/widgets#42."},
        {"url": "https://github.com/other/app/issues/5", "title": "Broken by widgets 1.2",
         "content": "See https://github.com/acme/widgets/issues/42"},
        {"url": "https://github.com/other/app/issues/6", "title": "unrelated",
         "content": "#42 is our own issue number"},  # another repository's #42 is not ours
    ]
    found = web_results_to_evidence(results, issue)
    assert [e.url for e in found] == ["https://github.com/other/app/issues/5",
                                      "https://blog.example.com/p"]
    assert all(e.kind == "related" for e in found)


def test_release_notes_count_when_they_cite_the_issue_or_name_its_code(issue: Issue) -> None:
    results = [
        {"url": "https://widgets.readthedocs.io/en/latest/changelog.html", "title": "Changelog",
         "content": "- Fix parse dropping the last item. :issue:`42`"},
        {"url": "https://github.com/acme/widgets/releases", "title": "Releases",
         "content": "1.3.0: ItemParser.parse_items keeps the last item"},
        {"url": "https://github.com/acme/widgets/blob/main/CHANGES.rst", "title": "CHANGES",
         "content": "1.3.0: many fixes"},  # names nothing, cites nothing
    ]
    found = web_results_to_evidence(results, issue)
    assert [(e.kind, e.url.rsplit("/", 1)[-1]) for e in found] == [
        ("changelog", "changelog.html"), ("related", "releases")
    ]
    assert found[0].why.startswith("widgets.readthedocs.io: names #42.")


def test_hits_are_ranked_and_capped_at_five(issue: Issue) -> None:
    results = [{"url": f"https://widgets.readthedocs.io/p{i}", "title": f"p{i}",
                "content": "parse_items"} for i in range(9)]
    results.append({"url": "https://github.com/acme/widgets/issues/7", "title": "issue 7",
                    "content": "ItemParser drops one; parse_items"})
    found = web_results_to_evidence(results, issue)
    assert len(found) == 5
    assert found[0].kind == "similar"  # a similar report comes before documentation


def test_markdown_escapes_do_not_hide_a_name(issue: Issue) -> None:
    hit = {"url": "https://widgets.readthedocs.io/api", "title": "API",
           "content": "widgets.parse\\_items(text)"}
    assert len(web_results_to_evidence([hit], issue)) == 1


def test_related_issues_from_github_search_are_judged_by_their_titles(issue: Issue) -> None:
    terms = issue_terms(issue)
    related = [
        Evidence(kind="duplicate", title="#7 parse_items loses data",
                 url="https://github.com/acme/widgets/issues/7",
                 why="Similar issue in the same repository (open, 2 comments)."),
        Evidence(kind="duplicate", title="#8 Add a dark theme",
                 url="https://github.com/acme/widgets/issues/8", why="x"),
        Evidence(kind="duplicate", title="#42 the issue itself",
                 url="https://github.com/acme/widgets/issues/42", why="x"),
    ]
    kept = related_issues_to_evidence(related, issue, terms, duplicates={7})
    assert [(e.kind, e.url) for e in kept] == [
        ("duplicate", "https://github.com/acme/widgets/issues/7")
    ]
    assert kept[0].why == "Same repository (open, 2 comments). Its title names parse_items."
    assert related_issues_to_evidence(related, issue, terms, set())[0].kind == "similar"


# -- what the issue is about -------------------------------------------------------------------


def test_issue_terms_sort_names_by_shape(issue: Issue) -> None:
    issue.title = "`str(Marker(...))` drops parentheses in `validate.Email`"
    issue.body = (
        "Calling `ItemParser.parse_items` with MAX_ITEMS set loses `payload`.\n"
        "```\nTraceback (most recent call last):\n"
        '  File "/x/_pytest/main.py", line 3, in wrap_session\n'
        "    return _inner_hookexec(config)\n"
        "```\nSee `widgets.py` and `setup.cfg`."
    )
    terms = issue_terms(issue, package="widgets")
    assert {"ItemParser.parse_items", "validate.Email", "str(Marker", "ItemParser",
            "parse_items", "MAX_ITEMS"} <= set(terms.strong)
    assert "Marker" in terms.anchors and "Email" in terms.anchors  # classes the title quotes
    assert "payload" in terms.weak
    everything = {*terms.strong, *terms.anchors, *terms.weak}
    assert "wrap_session" not in everything and "_inner_hookexec" not in everything  # tracebacks
    assert "widgets" not in everything  # the project's own name is not a term
    assert not any(t.endswith((".py", ".cfg")) for t in everything)  # file names are not code


def test_a_quoted_class_is_strong_only_on_the_projects_own_pages(issue: Issue) -> None:
    issue.title = "`Marker` loses its parentheses"
    terms = issue_terms(issue)
    assert terms.hits("Marker", own=True) == (["Marker"], [])
    assert terms.hits("Marker", own=False) == ([], ["Marker"])


@pytest.mark.parametrize(
    ("url", "place"),
    [
        ("https://github.com/acme/widgets/issues/9", "own_issue"),
        ("https://github.com/acme/widgets/pull/9/files", "own_change"),
        ("https://github.com/acme/widgets/commit/abc1234", "own_change"),
        ("https://github.com/acme/widgets/discussions/3", "own_change"),
        ("https://github.com/acme/widgets/releases/tag/1.3.0", "own_notes"),
        ("https://github.com/acme/widgets/blob/main/CHANGES.rst", "own_notes"),
        ("https://github.com/other/app/issues/9", "forum"),
        ("https://github.com/acme/widgets/blob/main/src/w.py", "index"),
        ("https://stackoverflow.com/questions/1/x", "forum"),
        ("https://bugzilla.redhat.com/show_bug.cgi?id=1", "forum"),
        ("https://widgets.readthedocs.io/en/latest/api.html", "docs"),
        ("https://widgets.readthedocs.io/en/latest/changelog.html", "own_notes"),
        ("https://docs.acme.dev/widgets/changes/", "own_notes"),
        ("https://pypi.org/project/widgets/", "docs"),
        ("https://www.rocketreach.co/widgets", "noise"),
        ("https://blog.example.com/2026/widgets-tips", "web"),
    ],
)
def test_classify(issue: Issue, url: str, place: str) -> None:
    assert classify(url, issue_terms(issue)).place == place


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


def test_release_reads_sphinx_and_myst_roles() -> None:
    sphinx = "Version 3.1.4\n-------------\n\nReleased 2026-01-02\n\n- Fix it. :issue:`3312`\n"
    assert release_from_text(sphinx, [3312]) == "3.1.4"
    myst = "## Version 8.4.0\n\nReleased 2026-05-01\n\n- Revert it. {issue}`3384` {pr}`3391`\n"
    assert release_from_text(myst, [3391]) == "8.4.0"
    assert release_from_text(myst, [338]) is None


def test_an_entry_under_unreleased_is_not_a_release() -> None:
    pallets = "Version 3.2.0\n-------------\n\nUnreleased\n\n- Fix hosts. :issue:`3312`\n"
    assert release_entry(pallets, [3312]) == ReleaseEntry("3.2.0", unreleased=True)
    assert release_from_text(pallets, [3312]) is None
    keep_a_changelog = "## [Unreleased]\n- Fix (#7)\n\n## [1.0.0] - 2026-01-01\n- Old (#3)\n"
    assert release_entry(keep_a_changelog, [7]) == ReleaseEntry(None, unreleased=True)
    assert release_entry(keep_a_changelog, [3]) == ReleaseEntry("1.0.0", unreleased=False)
    sphinx_html = "### UNRELEASED\u00b6\n\n- #617: fixed it\n\n### 3.16.0\u00b6\n- #600\n"
    assert release_entry(sphinx_html, [617]) == ReleaseEntry(None, unreleased=True)
    assert release_entry(sphinx_html, [600]) == ReleaseEntry("3.16.0", unreleased=False)


def test_a_citation_at_or_below_the_reported_version_is_skipped() -> None:
    text = "## 1.4.0\n- fix again (#48)\n\n## 1.3.1\n- Fix parse (#48)\n"
    assert release_entry(text, [48], after="1.3.1") == ReleaseEntry("1.4.0", unreleased=False)
    assert release_entry("## 1.3.1\n- Fix parse (#48)\n", [48], after="v1.3.1") is None


# -- Tavily steps ------------------------------------------------------------------------------


async def test_search_web_asks_once_with_the_title_and_counts_one_credit(issue: Issue) -> None:
    hit = {"url": "https://stackoverflow.com/q/1", "title": "parse_items() loses one",
           "content": "ItemParser.parse_items drops the last item"}
    tavily = FakeTavily(results=[hit])
    usage = Usage()
    found = await search_web(tavily, issue, usage)
    (call,) = tavily.search_calls
    assert call["search_depth"] == "basic" and call["max_results"] == 10
    assert call["query"] == "acme/widgets parse_items() drops the last item"
    assert "exclude_domains" not in call
    assert usage.search_credits == 1 and len(found) == 1


async def test_search_web_asks_again_in_other_words_when_nothing_is_kept(issue: Issue) -> None:
    tavily = FakeTavily(results=[{"url": "https://en.wikipedia.org/wiki/Pallet", "title": "x"}])
    usage, raw = Usage(), []
    assert await search_web(tavily, issue, usage, raw, package="widgets-lib") == []
    assert [c["query"] for c in tavily.search_calls] == [
        "acme/widgets parse_items() drops the last item",
        "python widgets-lib parse_items() drops the last item",
    ]
    assert usage.search_credits == 2 and len(raw) == 2  # every hit is kept for a second look


async def test_search_web_truncates_a_huge_query(issue: Issue) -> None:
    issue.title = "E" * 2_000
    assert all(len(q) <= 400 for q in search_queries(issue))


async def test_search_web_failure_returns_nothing_and_costs_nothing(issue: Issue) -> None:
    usage = Usage()
    assert await search_web(FakeTavily(fail=True), issue, usage) == []
    assert usage.search_credits == 0


def fetcher(files: dict[str, str]) -> tuple[Any, list[str]]:
    asked: list[str] = []

    async def fetch(url: str) -> str | None:
        asked.append(url)
        return files.get(url.rsplit("/main/", 1)[-1])

    return fetch, asked


async def test_find_release_prefers_the_projects_own_changelog_and_spends_no_credit(
    issue: Issue,
) -> None:
    fetch, asked = fetcher({"CHANGELOG.md": CHANGELOG_MD, "CHANGES.rst": "Version 9.9\n- x\n"})
    tavily, usage = FakeTavily(), Usage()
    found = await find_release(tavily, issue, "https://github.com/acme/widgets/pull/48", usage,
                               fetch=fetch)
    assert found is not None and found[0] == "1.3.1"
    assert found[1].url == "https://github.com/acme/widgets/blob/main/CHANGELOG.md"
    assert "CHANGELOG.md lists #42 / #48 under 1.3.1" in found[1].why
    assert asked[0] == "https://raw.githubusercontent.com/acme/widgets/main/CHANGES.rst"
    assert not tavily.extract_calls and usage.search_credits == 0


async def test_find_release_falls_back_to_the_release_notes_in_its_own_order(
    issue: Issue,
) -> None:
    fetch, _ = fetcher({})
    pages = [  # Tavily answers in its own order; the project's docs page is read first
        {"url": "https://github.com/acme/widgets/releases", "raw_content": "## 2.0.0\n- #48\n"},
        {"url": "https://widgets.readthedocs.io/changes", "raw_content": CHANGELOG_MD},
    ]
    tavily, usage = FakeTavily(pages=pages), Usage()
    found = await find_release(
        tavily, issue, "https://github.com/acme/widgets/pull/48", usage, fetch=fetch,
        notes_urls=["https://widgets.readthedocs.io/changes"],
    )
    assert found is not None and found[0] == "1.3.1"
    assert tavily.extract_calls[0]["urls"] == [
        "https://widgets.readthedocs.io/changes", "https://github.com/acme/widgets/releases"
    ]
    assert usage.search_credits == 1


async def test_find_release_says_when_the_fix_is_not_released_yet(issue: Issue) -> None:
    fetch, _ = fetcher({"CHANGES.rst": "Version 1.4.0\n-------------\n\nUnreleased\n\n"
                                       "- Fix. :issue:`42`\n"})
    found = await find_release(None, issue, None, Usage(), fetch=fetch)
    assert found is not None and found[0] is None
    assert found[1].title == "Not released yet (listed under 1.4.0 as unreleased)"


async def test_find_release_returns_none_when_nothing_cites_it_or_when_tavily_fails(
    issue: Issue,
) -> None:
    fetch, _ = fetcher({})
    pages = [{"url": "https://github.com/acme/widgets/releases",
              "raw_content": "## 1.0.0\n- nothing relevant (#1)\n"}]
    assert await find_release(FakeTavily(pages=pages), issue, None, Usage(), fetch=fetch) is None
    failing_usage = Usage()
    assert await find_release(FakeTavily(fail=True), issue, None, failing_usage,
                              fetch=fetch) is None
    assert failing_usage.search_credits == 0
    assert await find_release(None, issue, None, Usage(), fetch=fetch) is None


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
        related=[Evidence(kind="duplicate", title="#17 parse_items drops one",
                          url="https://github.com/acme/widgets/issues/17", why="search hit")],
        fix=("abc123def456789", "https://github.com/acme/widgets/pull/48"),
    )
    tavily = FakeTavily(
        results=[
            {"url": "https://github.com/acme/widgets/issues/17", "title": "dup again",
             "content": "ItemParser.parse_items drops the last one"},
            {"url": "https://stackoverflow.com/q/1", "title": "SO", "content": ""},
            # cites only the pull request: evidence once the fix is known
            {"url": "https://widgets.readthedocs.io/en/latest/changelog.html", "title": "Changes",
             "content": "- Keep the last item (#48)"},
        ],
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
    assert kinds[0] == "log" and kinds.count("log") == 1
    assert kinds.count("changelog") == 2 and "upstream" in kinds and "similar" in kinds
    (docs_changes,) = [e for e in result.evidence if e.url and "readthedocs" in e.url]
    assert docs_changes.kind == "changelog" and "#48" in docs_changes.why
    urls = [e.url for e in result.evidence if e.url]
    assert len(urls) == len(set(urls)), "the GitHub and Tavily duplicates were merged"
    # the changelog files cost nothing; one search and one extract of the release notes
    assert usage.search_credits == 2
    assert tavily.extract_calls[0]["urls"] == [
        "https://widgets.readthedocs.io/en/latest/changelog.html",
        "https://github.com/acme/widgets/releases",
    ]


async def test_the_projects_changelog_names_the_release_without_tavily(issue: Issue) -> None:
    fetch, _ = fetcher({"HISTORY.md": "## 1.4.0 (2026-01-01)\n- Fix (#42)\n"})
    github = FakeGitHub(fix=("sha1", "https://github.com/acme/widgets/pull/48"))
    result = await gather_evidence(
        issue=issue, verdict=Verdict.FIXED_ON_MAIN, reported_log="", usage=Usage(),
        settings=Settings(tavily_api_key=""), github=github, fetch=fetch,
        reported_version="1.3.0",
    )
    assert result.fixed_in_release == "1.4.0"
    assert result.evidence[-1].url == "https://github.com/acme/widgets/blob/main/HISTORY.md"


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
