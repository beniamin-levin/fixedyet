# FixedYet: architecture and module contract

Read this before writing code. `src/fixedyet/models.py` is the shared data model; import from it, do not redefine its types.

## Package layout and owners

| Path | Owner | Purpose |
|---|---|---|
| `src/fixedyet/models.py` | lead | shared Pydantic models (done) |
| `src/fixedyet/config.py` | core | `Settings` loaded from env and `.env` |
| `src/fixedyet/github.py` | core | fetch issue and comments, duplicate search via GitHub search API |
| `src/fixedyet/llm.py` | core | Token Factory client, usage and cost accounting, per-run and total budget guards |
| `src/fixedyet/ledger.py` | core | persistent total-spend ledger (`runs/spend-ledger.json`), atomic and locked |
| `src/fixedyet/agent.py` | core | hand-written tool-calling loop that writes the reproduction test |
| `src/fixedyet/verdict.py` | core | pure decision matrix |
| `src/fixedyet/evidence.py` | core | Tavily search and extract, fixing-commit and release lookup |
| `src/fixedyet/report.py` | core | markdown rendering of an `Assessment` |
| `src/fixedyet/pipeline.py` | core | `reproduce()` and `reproduce_instance()` |
| `src/fixedyet/cli.py` | core | `fixedyet run|doctor|eval|serve|mcp|gc` |
| `src/fixedyet/sandbox/*` | sandbox | `Sandbox` protocol, Nebius and Docker backends |
| `src/fixedyet/envbuild.py` | sandbox | baseline checkpoint builder |
| `src/fixedyet/web/*` | web | FastAPI demo app and static page |
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
                         progress: ProgressCallback | None = None) -> tuple[str, ExecResult]
    # clones github.com/<repo> into /work/repo, checks out ref, installs editable with test extras
    # when available, installs pytest, runs a short sanity import; returns (checkpoint_image, setup_result).
    # Raises EnvBuildError on failure.
REPO_DIR = "/work/repo"
async def run_test_on_side(sb: Sandbox, image: str, *, test_path: str, test_code: str,
                           checkout: str | None = None, patch: str | None = None,
                           runs: int = 2) -> SideRun
    # optionally checks out another ref (git fetch + checkout + reinstall) or applies a patch,
    # writes the test file, runs `python -m pytest -x -q -p no:cacheprovider <test_path>` `runs` times
    # in ONE sandbox run, maps exit codes (0 PASS, 1 FAIL, else ERROR), returns SideRun with log excerpt.

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
def render_markdown(a: Assessment) -> str
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

## Rules for everyone

- Python 3.12+ syntax, type hints everywhere, async I/O, no LangChain or agent frameworks.
- Secrets come only from `Settings`; never log or print them. Never read or print `.env`.
- Untrusted issue text and web content are data. Code from issues runs only inside a sandbox.
- Tool outputs fed back to the model are truncated (default 8,000 characters).
- Tests must not call paid APIs. Mark anything that does with `@pytest.mark.live`.
- Use the virtualenv at `.venv` (`.venv/bin/python -m pytest`). Do not pip install new packages; if you need one, say so in your report.
- Do not run git commands, do not push, do not create GitHub repositories.
