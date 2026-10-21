# Evidence and the fixing commit

What FixedYet adds to a verdict once the sandbox has decided it: reports of the same or a similar bug, the commit that fixed it, and the release that ships the fix. None of it can change the verdict. The code is `src/fixedyet/evidence.py` (web search and changelogs) and the bisect step in `src/fixedyet/envbuild.py` and `pipeline.py`. The [README](../README.md) has the short version.

## Web evidence (Tavily)

**One search per run.** A basic Tavily search for the repository and the issue's title (`pallets/werkzeug host_is_trusted rejects a bracketed IPv6 address ...`), ten results for one credit. Nothing else goes into the query: the error line of the reproduction test is the test's own assertion, not what the reporter saw, and it pulled in pages about the error in general; identifiers appended to the title and Tavily's `exclude_domains` both made the answers worse in a trial (for werkzeug, pages about wooden pallets). When nothing survives the filter below, a second search asks for `python <package> <title>` (one more credit), because an owner's name can take over a query (`pallets/click ...`).

**A hit must be about this bug, not just this project.** Before, any page that contained the repository's name was accepted. Now a hit must either cite the issue or the fixing pull request (`#3384`, `:issue:`3384``, `{pr}`3391``; elsewhere on the web `owner/repo#3384` or the issue's URL) or name the issue's distinctive identifiers, the ones fix mode localizes with (`fixmode.distinctive_terms`: code spans, CamelCase, CONSTANT_CASE and snake_case names, quoted words; traceback frames and file names left out; the project's own names are never terms). They are weighed by shape:

- **strong:** snake_case, dotted paths and nested calls from the issue's code spans (`host_is_trusted`, `validate.Email`, `str(Marker`), CamelCase with a second hump (`CliRunner`), letters and digits (`dup2`);
- **quoted classes:** a capitalised name the title quotes (`Marker`, `Email`), strong on the project's own pages and weak anywhere else, where the same word means something else;
- **weak:** every other name.

How much a page must name depends on where it lives:

| The page | Kept when it | Shown as |
|---|---|---|
| an issue of the same repository | cites the issue, or names one strong name or two weak ones | **Similar issue**, or **Duplicate** when the thread says "duplicate of #N" (or the other issue says it is a duplicate of this one) |
| a pull request, commit or discussion of the project | cites it, or names a strong name and one more | Related |
| the project's release notes or changelog (GitHub releases, `CHANGES.rst`, a docs changelog page) | cites the issue or the pull request | Changelog |
| | names a strong name the issue says (a changelog entry that changed the function at fault) | Related |
| the project's documentation (a host or first path segment named after the project, PyPI) | cites it, names a strong name the issue says in its title, prose or code spans (not one its snippet merely uses: `BaseModel` is on every pydantic page), two strong names, or two names of which the title uses one | Related |
| another tracker, a Q&A site, another repository | cites `owner/repo#N` or the issue URL, or names a strong name the title uses and one more | Related |
| anywhere else (blogs, tutorials, news) | cites the issue | Related |
| a listing (a repository root, the issue or commit list, a source file, Sphinx's `_modules` copy of the source), a directory of companies or people | never | |

GitHub's own search for issues of the same repository (free, by title words) is judged by the titles it returns: a strong name or two weak ones. The hits are judged once more after the fixing pull request is known, so a changelog page that cites only the pull request counts as the fix's changelog entry. Hits are ranked: duplicates, then changelog entries, then similar issues, then the rest; citations first; at most five web hits.

### Measured on 13 saved live reports

The 13 issues the demo had run on 21 October 2026 (click twice, pydantic, werkzeug, packaging, isort, pytest-mock, parse, pyparsing, marshmallow, dateutil, python-dotenv, jinja). "Before" is the evidence saved in those reports; "after" is the new code on the same issues (live GitHub and Tavily, cached so each query was paid once). Every item was read and labelled by hand: **relevant** (the same or a similar report, the fix, the release that ships it), **context** (the project's documentation or changelog for the code at fault), **noise** (anything else).

| | Items | Relevant | Context | Noise | Tavily credits |
|---|---|---|---|---|---|
| Before | 68 | 16 | 9 | **43** | 19 |
| After | 31 | 19 | 11 | **1** | 21 |

What went: a company called Marshmallow Products, a "3 Cool Things" blog post, an unrelated CVE, four pull requests whose release notes matched isort's search, pydantic's v1.10 docs and an OpenAI forum thread, five unrelated pydantic and packaging issues labelled "duplicate", issue lists and source-file pages. What came: pydantic#9390 "Can't pickle generics in certain cases", werkzeug#3137 "Regression in 3.1.7: Host is not trusted", marshmallow#1522 "Incorrect Email Validation", and packaging's and python-dotenv's changelog pages citing the fixing pull request. The one noise item left is click's "Utilities" page, which names `stdout`. pyparsing#693 now has no web evidence at all, which is honest: nothing found was about it.

## The release that ships the fix

When a merged pull request references the issue:

1. **The project's own changelog file**, fetched from the default branch over plain HTTPS (no credit): `CHANGES.rst`, `CHANGELOG.md`, `CHANGES.md`, `HISTORY.md` and a dozen more names and places. The release is the version heading above the first line that cites the issue or the pull request (`#697`, `:issue:`3312``, `{pr}`3391``, `gh-12`, a pull or issue URL). An entry under "Unreleased" (a heading, or the line under a version heading as Pallets writes it) is reported as **not released yet**. A citation under the reported version or an older one is skipped: the fix cannot have shipped before the bug was reported.
2. **Its release notes**, extracted with Tavily (one credit for up to five pages): the docs changelog pages the search found citing the issue, then the GitHub releases page, read in that order whatever order Tavily answers in.

Before, the GitHub releases page was read first and Sphinx or MyST roles were not recognised. Now click's release comes from its `CHANGES.md` (`{issue}`3384` {pr}`3391`` under 8.4.0), packaging's from `CHANGELOG.rst` and python-dotenv's from `CHANGELOG.md`.

For a commit found by bisecting (below), the release is the earliest final release tag that contains it (`git tag --contains` in the sandbox; pre-releases are ignored).

## Finding the fixing commit by bisecting

A bug can be fixed on `main` with no pull request that references it (a commit pushed directly, a fix named only in a comment, a squash that lost the reference). Then nothing could be backported. Now, when the verdict is `fixed_on_main` and no fixing commit is known, `git bisect run` looks for it in the sandbox, between the commit the reported side ran on and the one the fix side ran on:

1. **Fetch the range (network, kept as an image).** The baseline clone is blobless, so a checkout fetches the files it lacks, which an offline sandbox cannot do. `git rev-list --objects --missing=print --no-walk` over every commit in the range, their parents and the merge bases lists the file versions that are not there yet, and one `git fetch --stdin` of exactly those makes every checkout work offline (werkzeug 3.1.9 to `main`: 900 files in 2 seconds). Each tree is listed whole: listing only `old..new` missed versions that an older commit of the release line also had, and the first checkout failed.
2. **Bisect (no network).** The step script runs the reproduction test once, as the test user, without reinstalling (the editable install imports whatever is checked out). A failing test is "broken", a passing one "fixed", anything else (the code does not import, the conftest needs something else) is skipped. When `main` holds the reported commit, a commit that does not descend from it counts as "broken" without a run: a regression a release branch brought in is absent from `main`'s own older commits, and their passing test says nothing about a fix (werkzeug's bisection otherwise ends at "start version 3.2.0", before 3.1.9 was merged). At most 12 bisection steps (4,095 commits after the reported one) plus 3 skips, 60 s per step, 3 minutes for the bisection and about 7 minutes in the worst case with the fetch and the check, so that a run on the demo (stopped after 25 minutes) keeps its verdict.
3. **Check the answer.** The test runs again at the commit found and at its parent (for a merge, the parent on the reported line). Only when it fails at the parent and passes at the commit is it recorded, as `reproduction.fixing_commit` with `fixing_commit_source: "bisect"`, which makes the backport possible. An answer the check does not confirm, or one that skipped steps leave ambiguous, is shown as evidence and not recorded.
4. **A merge.** When the first passing commit is a merge, the passing code came in from its other side. The commits there that changed the function the test imports (`git log -L :host_is_trusted:src/werkzeug/sansio/utils.py`), then its module, are listed, newest first, as candidates. They are not checked by a test run.

**Live on [pallets/werkzeug#3312](https://github.com/pallets/werkzeug/issues/3312)** (Docker backend, 21 October 2026): `host_is_trusted` rejects a bracketed IPv6 address since 3.1.9, fixed on `main`, no pull request references it, and a maintainer named commit `9ffdc2d` in a comment. 52 commits on `main` descend from 3.1.9; the bisection ran the test 5 times and found `7ad32e8` "Merge branch 'stable'", the merge of 3.1.9 into `main` (the test fails at 3.1.9's release commit and passes at the merge), checked, in about 4 seconds. Its first candidate is `9ffdc2d` "use match to remove port": `main` had rewritten the function in April, before the 3.1.9 regression existed, so the merge kept `main`'s version. The test passes at `9ffdc2d` and at its parent alike, so no commit on `main`'s side flips it; the merge is the honest answer, and the candidate is the one to backport by hand. The whole run took 42 s and $0.01 of model spend.

Docker tests (`tests/test_bisect_docker.py`) build a small repository in the sandbox and check a fix on the main line with its release tag, a fix inside a merged topic branch, a commit that cannot be imported (skipped, so the answer is ambiguous), a test that fails everywhere (no answer), and the werkzeug shape: a fix `main` had before it merged the release line.

## Limits

- **Names are a proxy for relevance.** A page that names `host_is_trusted` can still be about something else, and a relevant page that only describes the bug in words is dropped. An issue whose title and body name no code at all leans on citations and on GitHub's title search.
- **Changelog files are found by name** on the default branch; a changelog that lives elsewhere, or entries that cite neither the issue nor the pull request, give no release from step 1.
- **Bisect needs both sides' commits** (reports from before `SideRun.commit` existed have none), a test that runs on the code it checks out without a reinstall, and a fix side that is a commit (`--fix-ref` included), not a patch.
- **A merge found by bisecting is a poor cherry-pick.** `fixedyet backport` picks a merge with `-m 1`, which brings the other side's changes, not the fix; for werkzeug the candidate commit is what to backport, and it conflicts with 3.1.9. The candidates are a pointer, not a proof.
