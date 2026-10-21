# FixedYet: architecture and module contract

Read this before writing code. `src/fixedyet/models.py` is the shared data model; import from it, do not redefine its types.

## Package layout and owners

| Path | Owner | Purpose |
|---|---|---|
| `src/fixedyet/models.py` | lead | shared Pydantic models (done) |
| `src/fixedyet/config.py` | core | `Settings` loaded from env and `.env` |
| `src/fixedyet/github.py` | core | fetch issue and comments, duplicate search via GitHub search API |
| `src/fixedyet/llm.py` | core | Token Factory client, usage and cost accounting, per-run and total budget guards |
| `src/fixedyet/ledger.py` | core | persistent total-spend ledger (`FIXEDYET_LEDGER_PATH`, default `~/.local/state/fixedyet/spend-ledger.json`; an old `<runs_dir>/spend-ledger.json` is carried over), atomic and locked |
| `src/fixedyet/untrusted.py` | core | `strip_hidden`: removes what GitHub hides from readers (HTML comments outside code, markdown comment definitions, invisible characters, a bot's `<details>`) before thread text reaches a model; `UNTRUSTED_RULE` for every system prompt |
| `src/fixedyet/agent.py` | core | hand-written tool-calling loop that writes the reproduction test |
| `src/fixedyet/verdict.py` | core | pure decision matrix |
| `src/fixedyet/evidence.py` | core | Tavily search and extract, fixing-commit and release lookup |
| `src/fixedyet/report.py` | core | markdown rendering of an `Assessment` |
| `src/fixedyet/pipeline.py` | core | `reproduce()` and `reproduce_instance()` |
| `src/fixedyet/backport.py` | backport | branch detection, cherry-pick and suite gates, `backport()` and `BackportResult` |
| `src/fixedyet/pullrequest.py` | backport | fork, push of the sandbox patch, draft pull request (`--open-pr` only), for backports and fixes |
| `src/fixedyet/fixmode.py` | fix | fix mode for open bugs: localization, model edits, validation, sandbox verification, `attempt_fix()` and `FixResult` |
| `src/fixedyet/cli.py` | core | `fixedyet run|backport|fix|doctor|eval|serve|mcp|gc` |
| `src/fixedyet/sandbox/*` | sandbox | `Sandbox` protocol, Nebius and Docker backends |
| `src/fixedyet/envbuild.py` | sandbox | baseline checkpoint builder |
| `src/fixedyet/testdeps.py` | sandbox | test dependency installer; shipped into the baseline sandbox and run there (standard library only) |
| `src/fixedyet/suite_plugin.py` | backport | pytest plugin shipped into the sandbox by `backport.suite_files()`: the suite's counts and failing node ids as JSON, whatever the verbosity (standard library only) |
| `src/fixedyet/web/*` | web | FastAPI demo app, the reports store (`reports.py`: listing, hidden ids, saved backports) and the static pages (`index.html`, `reports.html`, shared `common.js`) |
| `src/fixedyet/mcp_server.py` | web | MCP server |
| `deploy/*`, `README.md` | web | nginx, systemd, deploy script, README |
| `src/fixedyet/eval/*`, `eval/*` | eval | SWE-bench loader, runner, metrics |
| `.github/workflows/ci.yml` | core | lint and unit tests |
| `.github/workflows/sandbox-docker.yml` | sandbox | Docker backend integration test |

Only touch files you own. If you need a change in someone else's file, write it in your final report instead.

## Public interfaces

```python
# config.py
class Settings: ...            # fields mirror .env.example; Settings.load() reads env + .env
def get_settings() -> Settings

# sandbox/base.py
class Sandbox(Protocol):
    name: str
    async def base_image(self, python_version: str | None) -> str: ...
    async def run(self, image: str, command: str, *, files: dict[str, str] | None = None,
                  workdir: str = "/work", timeout_s: int = 900, keep: bool = False,
                  network: bool = True) -> ExecResult: ...
    # keep=True saves the resulting filesystem as a new image -> ExecResult.image (the checkpoint).
    # Every run starts from `image`, so running from a checkpoint is a fork.
def get_sandbox(settings: Settings) -> Sandbox   # chooses by settings.sandbox: "nebius" | "docker"

# sandbox/docker.py (optional, not part of the Sandbox protocol)
class DockerSandbox:
    async def prune_checkpoints(self, keep: int, *, dry_run: bool = False) -> PruneResult
    # keeps the newest `keep` local images of the `fixedyet-ckpt` repository and removes the rest;
    # never touches another repository, skips images a container uses. pipeline.py calls it through
    # getattr after the last in-flight run (settings.keep_checkpoints); `fixedyet gc` calls it on demand.

# envbuild.py
async def build_baseline(sb: Sandbox, *, repo: str, ref: str, python_version: str | None,
                         progress: ProgressCallback | None = None, extra_packages: Sequence[str] = (),
                         project_extras: Sequence[str] = ()) -> tuple[str, ExecResult]
    # clones github.com/<repo> into /work/repo, checks out ref, installs editable with test extras
    # when available, installs pytest, then the project's test dependencies (testdeps.py: PEP 735
    # groups test/tests/testing, requirements/test*.txt and kin; `pip install --group` when pip has
    # it, else tomllib + install by name; plain requirements only, never fatal; requirements files
    # named for another Python, like tests37.txt, are skipped), then project_extras (optional extras
    # of the project itself, from project_extras_from_text(); testdeps.py adds the ones its console
    # script asks for) and extra_packages, then a repair step that makes pytest able to load the
    # project's conftest (testdeps.py repair), then creates the unprivileged test user TESTER
    # ("fixedyet", /work owned by it, git safe.directory '*' system-wide; TESTER_SETUP, never fatal,
    # tester_ready() reads it back), runs a short sanity import; returns
    # (checkpoint_image, setup_result). installed_test_deps(setup_result), installed_project_extras()
    # and repairs_made() read back what those steps did.
    # Raises EnvBuildError on failure.
REPO_DIR = "/work/repo"
async def run_test_on_side(sb: Sandbox, image: str, *, test_path: str, test_code: str,
                           checkout: str | None = None, patch: str | None = None,
                           runs: int = 2, project_extras: Sequence[str] = ()) -> SideRun
    # optionally checks out another ref (git fetch + checkout + reinstall) or applies a patch,
    # prints __HEAD__ (git rev-parse HEAD -> SideRun.commit), writes the test file, hands files a
    # root step created back to the test user (ir_own), runs `python -m pytest -x -q
    # -p no:cacheprovider <test_path>` `runs` times in ONE sandbox run as that user (ir_as_tester:
    # setpriv, else a Python setuid; root on a checkpoint without the user), maps exit codes
    # (0 PASS, 1 FAIL, else ERROR), returns SideRun with log excerpt.
TESTER_FUNCTIONS: str    # shell: ir_has_tester, ir_own, ir_as_tester; side, suite and agent scripts
    # After a checkout it first reinstalls project_extras and repairs a conftest that cannot load
    # (the other ref has its own test requirements), using the helper shipped with the test file.

# llm.py
class LLM:
    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
                   model: str | None = None, max_tokens: int = 4096) -> ChatResult
    usage: Usage           # accumulated; raises BudgetExceeded past settings.run_budget_usd,
                           # and TotalBudgetExceeded (a subclass) before a call that could pass
                           # settings.total_budget_usd, the cap kept in ledger.py's spend ledger

# verdict.py
def decide(reported: SideRun | None, fix: SideRun | None, *, runnable: bool,
           env_ok: bool) -> tuple[Verdict, Confidence, list[str]]

# pipeline.py
async def reproduce(issue_url: str, *, fix_ref: str | None = None, settings: Settings | None = None,
                    progress: ProgressCallback | None = None) -> Assessment
async def reproduce_instance(instance: SweBenchInstance, *, settings: Settings | None = None,
                             progress: ProgressCallback | None = None) -> Assessment
    # eval mode: issue text = problem_statement, reported ref = base_commit,
    # fix side = base_commit + instance.patch. The agent never sees patch or test_patch.

# report.py
def render_markdown(a: Assessment) -> str     # adds "## Verified backport" when a.backport is set
def render_backport_markdown(r: BackportResult) -> str
def backport_section(r: BackportResult, level: int = 2) -> list[str]

# backport.py
async def backport(assessment: Assessment, *, target_branch: str | None = None,
                   settings: Settings | None = None, sandbox: Sandbox | None = None,
                   progress: ProgressCallback | None = None, list_branches: BranchLister | None = None,
                   python_version: str | None = None, extra_packages: Sequence[str] = (),
                   suite_timeout_s: int = 900) -> BackportResult
    # never raises for anything the world can do: the answer is a BackportResult whose
    # status/reason say what happened. No model call. See "Backport mode" below.
def find_target_branch(version: str | None, branches: Sequence[str]) -> BranchMatch   # pure
def pick_script(commit: str) -> str ; def parse_pick(result: ExecResult) -> PickOutcome
def suite_script(timeout_s: int = 900) -> str ; def parse_suite(result: ExecResult) -> SuiteRun
def suite_files() -> dict[str, str]   # the plugin file; pass as `files=` with suite_script()
    # parse_suite reads the plugin's JSON report (__SUITE_JSON_B64__) first, the summary line and
    # the short test summary as the fallback; ids are pytest node ids either way

# pullrequest.py
async def open_backport_pr(result: BackportResult, *, token: str, target: "fork"|"upstream" = "fork",
                           confirmed_upstream: bool = False, ...) -> PullRequest
    # only for status == verified; target="upstream" raises unless confirmed_upstream is True;
    # draft=True is hard-wired; there is no ready-for-review, comment or force-push call at all
def pr_title(result) -> str ; def pr_body(result, *, login: str) -> str   # ends with DISCLOSURE
async def open_fix_pr(result: FixResult, *, token: str, target: "fork"|"upstream" = "fork",
                      confirmed_upstream: bool = False, ...) -> PullRequest
    # the same flow as open_backport_pr (one shared core): base = the default branch,
    # head = fixedyet/fix-<issue>, the patch adds the regression test, author = token owner
def fix_pr_title(result) -> str ; def fix_pr_body(result, *, login: str) -> str   # ends with DISCLOSURE
def fix_pr_preview(result, *, login: str) -> str   # title, base/head, body, diff; sends nothing

# fixmode.py
async def attempt_fix(assessment: Assessment, *, settings=None, sandbox=None, llm=None,
                      github=None, progress=None, list_branches=None, python_version=None,
                      extra_packages=(), suite_timeout_s=900, max_attempts=3,
                      budget_usd=None) -> FixResult
    # never raises for anything the world can do; status: verified | not_fixed | already_fixed
    # | not_applicable | error. See "Fix mode" below.
def regression_test_path(tracked, issue_number, testpaths=()) -> str | None   # pure
def test_directory(tracked, testpaths=()) -> str | None ; def pytest_testpaths(configs) -> list[str]
def changed_functions(original, edited) -> list[Block] ; def select_callers(...)   # pure
def apply_edits(edits, originals, tracked) -> EditOutcome   # pure: validation of model edits
def parse_proposal(text: str) -> Proposal                 # pure: the model's answer
def rank_files(...) -> list[Candidate] ; def show_file(...) -> ShownFile | None   # pure
def resolve_token(configured: str | None) -> str      # GITHUB_TOKEN, else `gh auth token`
def confirm_upstream(repo: str, *, assume_yes: bool = False) -> bool       # type the repo name
```

## Decision matrix (verdict.py)

| Reported side | Fix side | Verdict |
|---|---|---|
| FAIL | PASS | fixed_on_main |
| FAIL | FAIL, same kind of failure | open_bug |
| FAIL | FAIL, different kind of failure (say AssertionError, then TypeError) | inconsistent, low |
| PASS | PASS | not_reproduced |
| PASS | FAIL | inconsistent |
| ERROR, or env build failed | any | unsupported |
| contract not runnable | - | needs_info |
| runs disagree on either side | - | inconsistent, confidence low |

Confidence is high only when every run on both sides agrees and neither side errored.

An `unsupported` verdict caused by an errored side says why in its basis (`verdict.error_reason`: pytest could not load the project's conftest or a plugin, could not collect the test file, or the ref needs a newer Python than the sandbox has). `verdict.is_setup_error` is the same reading for the test-writing agent, which is told that such an error is not a failure of its test.

## Backport mode (verified backports and draft pull requests)

Input: an `Assessment` with verdict `fixed_on_main` and `reproduction.fixing_commit`. Output: a `BackportResult` (models.py: `status`, `reason`, `target_branch`, `target_branch_head`, `fixing_commit`, `cherry_picked_commit`, `commit_message`, `patch`, `repro_before`/`repro_after` as `SideRun`, `suite_before`/`suite_after` as `SuiteRun`, `newly_failing`, `conflicting_files`, `log_excerpt`, `usage`). `Assessment.backport` is an optional field that `report.py` renders as the "Verified backport" section.

```
assessment --> not fixed_on_main / no fixing commit / no branch for the version --> not_applicable
           --> branch head (git ls-remote --heads; the only thing that runs on the host)
sandbox:   baseline checkout of that head (envbuild.build_baseline, pinned to the commit)
  a. repro test x2 (envbuild.run_test_on_side)   PASS -> already_fixed_on_branch
                                                  not a clean FAIL -> error
  c. git cherry-pick -x [-m 1] <commit>          (fork of the baseline image, keep=True)
  d. conflict -> abort, list files -> conflicts   (no model, no retry)
  e. reinstall, repro test x2 on the picked image  not PASS -> error
  b. project suite on the baseline image, then on the picked image
     (pytest -q --maxfail=0 --continue-on-collection-errors, 900 s each, network off, as the
     test user; counts and failing ids from suite_plugin's JSON report, summary line as fallback)
  f. newly failing ids (or a higher failure count) -> regression; suite did not run -> error
  g. git format-patch -1 --stdout -> patch        all clear -> verified
```

Design decisions:

- **Pure scripts, pure parsers.** `pick_script`, `suite_script`, `parse_pick` and `parse_suite` are plain functions, so unit tests check the exact commands and feed canned output through the parsers (`tests/test_backport.py`, with a scripted sandbox), and `tests/test_backport_docker.py` runs the same scripts for real on a small repository in Docker (clean pick, conflict, merge commit, regression, already fixed).
- **Markers cannot be forged.** The scripts print machine-readable lines (`__PICKED__=...`, `__RC__=...`) on their own, and prefix every line of git or pytest output with `| `, the way `envbuild` does for the reproduction runs. The patch and the commit message travel as base64.
- **Cheap gates first.** The suites (up to 900 s each) run after the conflict and reproduction gates, from forks of the untouched baseline image and of the cherry-picked image, so the baseline is still the branch as it was.
- **Conservative about "verified".** A suite that did not finish, an unreadable summary, a failure count that rose without readable test ids, or a reproduction test that does not flip all stop short of `verified`.
- **No model.** The model is not called after the reproduction; conflicts are never resolved by it.
- **Housekeeping.** `backport()` runs inside `pipeline.run_scope`, the same in-flight counter the reproduction uses, so the two share one rule for pruning old Docker checkpoint images and never prune each other's.

Pull requests (`pullrequest.py`) are a separate step that the CLI takes only with `--open-pr`, only for `verified`:

```
token (GITHUB_TOKEN | gh auth token) -> GET /user (login = the token's owner)
  -> fork upstream (reuse an existing fork, wait until it has git data)        POST /repos/{o}/{r}/forks
  -> fork target: fast-forward the fork's branch                               POST .../merge-upstream
  -> an open PR for fixedyet/backport-<issue>-<branch> already? return it      GET  .../pulls
  -> host: temp dir, `git fetch --depth 1` of the verified base commit, `git am` of the patch,
           `git push` of the branch to the FORK (HTTPS; token only in the push's environment)
  -> POST /repos/{base}/pulls  with "draft": true     base = the fork (default) or upstream
```

- `--pr-target fork` (default) opens the draft against the fork's copy of the maintenance branch. `--pr-target upstream` raises in `open_backport_pr` unless the caller passes `confirmed_upstream=True`; the CLI gets that by making the person type the repository name (or `--yes-upstream`) before the sandbox work starts.
- The module has no ready-for-review call, no comment call, no force-push and no push to upstream; `tests/test_pullrequest.py` asserts the set of requests a run makes, and that every `POST /pulls` carries `draft: true`.
- The body ends with `DISCLOSURE` ("Prepared with FixedYet (AI-assisted: NVIDIA Nemotron via Nebius Token Factory); reviewed and submitted by @<login>."). It is a constant, not an option.
- The host runs only `git` (`init`, `fetch`, `checkout`, `am`, `push`, `rev-parse`) with hooks off and without the user's git config. Nothing from the project is executed on the host.
- The MCP tool `propose_backport` never imports `pullrequest`. The web app imports it in one place, the maintainer-only backport job (`web/app.py: open_draft_pr`), which passes `target="fork"`, or `target="upstream"` with `confirmed_upstream=True` only after the maintainer typed the upstream repository's name into the step-2 request (`confirm`); any other text is a 400 before GitHub is called.

## Fix mode (open bugs, model-written fixes, draft pull requests)

Input: an `Assessment` with verdict `open_bug` and a reproduction test. Output: a `FixResult` (models.py: `status`, `reason`, `base_branch`, `base_sha`, `summary`, `explanation`, `diff`, `files_changed`, `lines_added`/`lines_removed`, `context_files`, `regression_test_path`, `regression_test_note`, `repro_before`/`repro_after`, `suite_before`/`suite_after`, `newly_failing`, `attempts` as `FixAttempt`, `changed_functions`, `other_callers` as `CallSite` (`symbol`, `path`, `line`, `code`), `other_callers_total`, `log_excerpt`, `budget_usd`, `usage`). `Assessment.fix` is optional and `report.py` renders it as the "Fix" section.

```
assessment --> not open_bug / no test / not a GitHub issue --> not_applicable
           --> issue thread (GitHub API) and head of the default branch (git ls-remote, host)
sandbox:   baseline checkout of that commit (envbuild.build_baseline, pinned)
  survey   git ls-files + git grep for definitions of the names the test and the issue use,
           per-file counts of the issue's distinctive terms (whole words, and as "quoted"),
           and the pytest config files (testpaths)
  a. repro test x2 at the assessment's own path (reused when the assessment ran it at this
     commit)   PASS -> already_fixed if the branch moved since the assessment, error if the
     assessment saw it fail at this commit; not FAIL -> error
  a2. repro test x2 where the PR adds it (test_directory(): <dir>/test_issue_<n>.py)
     FAIL -> verify there; else verify at the assessment's path, PR carries no test (note)
host:      localize (pure): project frames in the failing traceback, files the issue names,
           symbol scores (distinctive_terms -> symbol_scores: rare terms, breadth, registry
           entries), files defining what the test imports; <= 4 files, ~60 KB, far-behind
           files dropped; a big file as windows around the anchors and the term mentions
sandbox:   git grep -w: other callers of the functions around the strongest anchors (<= 10)
loop, <= 3 attempts, model cost <= FIXEDYET_FIX_BUDGET_USD:
  model    SUMMARY / EXPLANATION / FILE + SEARCH/REPLACE blocks; or NEED (once, at any point,
           <= 3 files);
           or CANNOT_FIX: <why> (ends the loop, not_fixed)
  host     apply_edits (text only): old block exactly once (also after a uniform re-indent),
           source .py files only (no tests, CI, packaging, docs, vendored code, new files),
           <= 80 changed lines, still parses (ast), no alternatives for the same lines
  sandbox  the edited files written over the checkout, git diff, keep=True (no network)
  c. repro test x2 on the patched image                 not PASS -> feedback (diff + output)
  d. suite on the baseline (once) and on the patched image (900 s, no network)
     newly failing / count rose -> feedback (ids + log); baseline suite cannot run -> error
  all clear -> verified; then the functions the edit touches (changed_functions) and their
     other callers (git grep -w, no tests/defs/imports) -> other_callers (report, web, PR body)
```

Design decisions:

- **Search/replace, not diffs.** The model never writes a unified diff. It names a file and an exact block, and the host finds that block exactly once. The diff is made by `git diff` in the sandbox, so it is always well formed.
- **Validation is text, execution is sandbox.** `apply_edits` is a pure function over strings; `ast.parse` checks syntax without running anything. Model-written code runs only in sandbox forks without network.
- **Feedback that teaches.** A rejected edit comes back with the reason, the format and the caps restated, and, when a block matches nowhere or targets a file the model was not shown, that file itself (windowed around where it aimed) and the closest lines; a test that still fails comes back with the diff and the output; a regression with the newly failing ids and the end of the suite log. Every attempt starts again from the original code.
- **Room to think, then brevity.** Each call may use 16,000 tokens (reasoning included) with a 420 s request timeout; an answer cut off by the token limit before any edit is named as such, and the next call asks without the reasoning.
- **The test is the spec, and it can be wrong.** The model may answer `CANNOT_FIX` when it judges the test wrong; nothing is changed then.
- **Checked the way the assessment ran it, verified where it ships.** The first run is at the assessment's own path: a `conftest.py` or package `__init__.py` around another path can change what the test checks (RDFLib/pySHACL#332, where `test/__init__.py` imports the package before the test sets an environment variable read at import time). A pass there is `already_fixed` only when the branch has moved; at the commit where the assessment saw it fail it is an `error`. The checks then run at the path the pull request adds it, chosen from where the project keeps its tests (the `testpaths`, then the directory with the most test modules, an `issues/` subdirectory first), when the test fails there too; otherwise at the assessment's path, and the pull request carries no test.
- **Verified is not safe.** The suite passing says nothing about code the test does not run. The functions a verified change edits and their other callers (by name, best effort, at most 10) are listed for the reviewer; the model sees the callers of the likely functions before it answers, with a rule to keep their behaviour.
- **Pull requests** reuse the backport flow (`pullrequest._open_draft`): fork, push of `fixedyet/fix-<issue>` from a temp clone of the verified commit with `git am`, draft pull request against the default branch, typed confirmation for upstream, idempotent per target, `DISCLOSURE` last. The commit is authored by the token's owner; its message and the body say the change was written by AI and verified by machine.

### Web: reports page and maintainer mode

`GET /api/reports` merges `examples/*.json` with every readable `<runs_dir>/<stamp>-<owner>-<repo>-<number>/assessment.json`, newest first; `GET /api/reports/{id}` returns the assessment, the markdown and the saved backport. Ids are checked against `[A-Za-z0-9][A-Za-z0-9._-]*` without `..`, and every path built from one is resolved and checked to stay under its directory. Maintainer mode exists only when `FIXEDYET_ADMIN_TOKEN` is set (otherwise every admin endpoint is a 404; a wrong token is a bare 401, compared with `hmac.compare_digest`). Backporting is two steps. `POST /api/reports/{id}/backport` starts a job in the same `Run`/`RunStore` as live runs (kind `backport`, one slot, same server-sent events, not counted against the daily limit): `backport()` in the sandbox and no pull request, then `github.target_activity()` (the target branch's last commit date and the latest release, best effort, null on any failure). A verified record is returned instead of being run again. `POST /api/reports/{id}/backport/pr` (body `{"target": "fork"}`) needs a verified record (409 `not_verified`), opens a draft pull request with `open_backport_pr(target="fork")` inline with a 60 s timeout, and is idempotent per target. The record, with `pr_url`, `pr_target`, `pr_number`, a `prs` map per target and `target_activity`, is saved to `<runs_dir>/backports/<id>.json`; the API adds `target_warning` (the last commit more than 180 days old, or a latest release with a newer major.minor than the branch) when it reads one. Hidden ids go to `<runs_dir>/hidden.json` (temp file plus `os.replace`). The fixing commit comes from the report's `reproduction.fixing_commit`; a report without one cannot be backported. Fix mode mirrors this for open bugs: `POST /api/reports/{id}/fix` (a job of kind `fix`, 90-minute timeout, refused with 429 when the total budget has less left than `FIXEDYET_FIX_BUDGET_USD`) and `POST /api/reports/{id}/fix/pr` (the same step-2 code as backports), with records in `<runs_dir>/fixes/<id>.json`; summaries carry `can_fix` and `fix`.

## Rules for everyone

- Python 3.12+ syntax, type hints everywhere, async I/O, no LangChain or agent frameworks.
- Secrets come only from `Settings`; never log or print them. Never read or print `.env`.
- Untrusted issue text and web content are data. Code from issues runs only inside a sandbox. Thread text goes through `untrusted.strip_hidden` (at fetch time and in `agent.format_issue`) and every system prompt carries `untrusted.UNTRUSTED_RULE`.
- Tests and project suites run as the unprivileged sandbox user (`envbuild.TESTER_FUNCTIONS`); setup steps stay root.
- Tool outputs fed back to the model are truncated (default 8,000 characters).
- Tests must not call paid APIs. Mark anything that does with `@pytest.mark.live`.
- Use the virtualenv at `.venv` (`.venv/bin/python -m pytest`). Do not pip install new packages; if you need one, say so in your report.
- Do not run git commands, do not push, do not create GitHub repositories.
