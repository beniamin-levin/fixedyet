# Verified backports

A bug that is fixed on `main` is still broken for everyone on the release they use. When a run ends `fixed_on_main` and the fixing commit is known (the merge commit of the pull request that references the issue, or the commit a [bisection in the sandbox](evidence.md#finding-the-fixing-commit-by-bisecting) found and checked), `fixedyet backport` prepares that fix for the **maintenance branch of the reported version** and proves it there, so that a maintainer reviews a tested patch instead of a promise.

```bash
fixedyet backport https://github.com/OWNER/REPO/issues/123            # reproduce, then backport (dry run)
fixedyet backport URL --from-assessment assessment.json              # reuse a saved run: no model calls
fixedyet backport URL --target-branch 8.1.x --json r.json --md r.md  # choose the branch, save the result
fixedyet backport URL --open-pr                                      # also open a DRAFT pull request on your fork
fixedyet backport URL --open-pr --pr-target upstream                 # ... on the upstream project (asks first)
```

A real case, end to end (`fixedyet run`, then `fixedyet backport --open-pr`, Docker sandbox): [pytest-dev/pytest#13784](https://github.com/pytest-dev/pytest/issues/13784), where `capteesys` printed everything twice under `-s` on pytest 8.4.1. The fix was merged to `main` after the `8.4.x` branch was cut and never backported. FixedYet found `8.4.x`, cherry-picked the fix without conflicts, showed the reproduction test failing before and passing after, and showed the project's suite (about 3,250 tests) with no new failure. The draft pull request is [in the owner's fork](https://github.com/beniamin-levin/pytest/pull/1), not in the upstream project. The saved result is `examples/real-backport-pytest-dev-pytest-13784.json` and appears on the demo page.

A second case went upstream: [pydantic/pydantic#13991](https://github.com/pydantic/pydantic/issues/13991), where a parametrized generic model defined across two modules could not be pickled. FixedYet found it fixed on `main` (fixing commit `e1ca332`) and verified the backport onto `v2.14-fixes` (Docker backend): the reproduction test failed twice before the cherry-pick and passed twice after, and the project's suite (6,250 tests) had no new failure. After the owner reviewed it, the draft pull request was opened on the upstream project, [pydantic/pydantic#14043](https://github.com/pydantic/pydantic/pull/14043), and marked ready for review once its CI passed. On 22 October 2026 the maintainer closed it unmerged, as they do the backports themselves before a patch release.

Without `--open-pr` nothing leaves your machine except the sandbox's own clone of the public repository. The command prints a markdown summary and exits 0 only for a verified backport.

## The gates

Each gate runs in the sandbox, from a fresh checkout of the head of the target branch. The model is not involved after the reproduction: a backport costs sandbox time, not tokens.

1. **Find the branch.** From the repository's branch names (`git ls-remote --heads`): `8.1.x`, `v8.1.x`, `8.1`, `stable/8.1`, `release-8.1`, `maint/8.1`, `8.1-maintenance` and similar match a reported `8.1.3`. A line for another minor (`8.2.x`) never matches, and an unversioned `stable` is not guessed at; use `--target-branch` for those. No branch means `not_applicable`, with the reason and the branches that were seen.
2. **The reproduction test must fail on the branch.** If it passes, the branch already has the fix (`already_fixed_on_branch`). If it errors or flips between runs, nothing can be judged (`error`).
3. **`git cherry-pick -x <fixing commit>`**, with `-m 1` for a merge commit. A fix whose commit is unknown (no pull request references the issue and the bisection found no commit it could check) is reported as such.
4. **Conflicts stop it.** The pick is aborted and the conflicting files are listed (`conflicts`). The model never resolves conflicts: a resolved conflict is new code that nobody has reviewed.
5. **Reinstall, and the reproduction test must pass.** If it still fails, the fix depends on other changes the branch does not have (`error`).
6. **The project's own test suite, before and after** (pytest, every test, no `-x`, 900 s each, no network, as the unprivileged user). The counts and the ids of failing tests come from a small pytest plugin that FixedYet ships into the sandbox (`suite_plugin.py`, a JSON report), so they do not depend on what the terminal shows: a project's `addopts = "-q"` plus FixedYet's own `-q` hides pytest's "N passed" line (nedbat/cog), and plugins such as pytest-sugar rewrite it. The summary line is the fallback. A test that fails after the cherry-pick and did not fail before is a `regression`. Tests that already failed on the branch are the baseline and are not blamed on the patch. If the suite cannot run (timeout, collection crash) the backport cannot be called verified (`error`), and the patch is attached unverified.
7. **`git format-patch`** of the cherry-pick is the result's `patch`.

| Status | Meaning |
|---|---|
| `verified` | the test fails before and passes after, and no test newly fails |
| `conflicts` | the cherry-pick does not apply; files listed, nothing resolved |
| `regression` | the fix works but newly breaks other tests; they are named |
| `already_fixed_on_branch` | the branch passes the reproduction test already |
| `not_applicable` | not fixed on main, no known fixing commit, or no branch for that version |
| `error` | a step could not be completed; the reason says which |

The result (`BackportResult`) also carries the cherry-picked commit message, the reproduction test's outcomes before and after, the suite counts and failing test ids before and after, log excerpts, cost and wall time. When one is attached to an assessment, the markdown report gains a "Verified backport" section.

## The draft pull request

`--open-pr` works only on a `verified` result, and only as the owner of the token (`GITHUB_TOKEN`, or `gh auth token` when that is empty):

1. fork the upstream repository into the token's account if there is no fork yet, and wait for it;
2. on the host, apply the sandbox's patch to a throwaway clone of the verified base commit and push the branch `fixedyet/backport-<issue number>-<target branch>` to the **fork**, over HTTPS. Only git runs on the host (`fetch`, `am`, `push`, with hooks off): project code never does;
3. open a **draft** pull request titled `Backport <short summary of fix> to <target branch>`. The body links the issue and the original commit, tabulates the reproduction test before and after the cherry-pick, gives the suite results and a patch summary.

Where the pull request goes:

| `--pr-target` | Base of the pull request |
|---|---|
| `fork` (default) | the fork's own copy of the maintenance branch. Nothing reaches the upstream project |
| `upstream` | the upstream maintenance branch. FixedYet asks you to type the repository name first (skipped by `--yes-upstream`), before any sandbox work starts |

What it never does: mark a pull request ready for review, comment on the upstream issue or pull request, push to the upstream repository, force-push (a rerun finds the open pull request and returns it instead of opening a second one), or print the token (it travels in a request header and in the environment of one git process, never in a URL or on a command line, and error messages are scrubbed of it).

Every pull request body ends with this line, which cannot be turned off:

> Prepared with FixedYet (AI-assisted: NVIDIA Nemotron via Nebius Token Factory); reviewed and submitted by @&lt;token owner login&gt;.

The line says the pull request was prepared with AI, and that a named person is responsible for it. Read the patch before you submit one: a green suite is evidence, not a review.

The token needs to create a fork, push to it and open pull requests: a fine-grained token with *Contents* and *Pull requests* write access on the fork (plus the permission to fork), or a classic token with `public_repo`.

## Limits

- Pytest projects that install with pip, like the rest of FixedYet. Test dependencies come from [the groups and files the environment builder installs](environment.md#test-dependencies); a project whose tests need anything else (a service, a system library, a tox-only dependency list) cannot be fully verified, and its missing tests sit in the baseline.
- The suite and the reproduction test run without network, so a test that needs one fails before and after alike and sits in the baseline. A test that is flaky can still be reported as a regression; the failing ids are listed so that you can see.
- A fixing commit that the bisection found as a merge (the fix came in from the merge's other side, as for werkzeug#3312) is picked with `-m 1`, which brings that side's other changes too, and rarely gives a clean backport. The bisection's evidence names candidate commits on the other side; backport one of those by hand.
- Projects that keep one rolling branch (`stable`) instead of one branch per minor release give `not_applicable` unless you pass `--target-branch`. `pallets/click` is such a project at the time of writing.
