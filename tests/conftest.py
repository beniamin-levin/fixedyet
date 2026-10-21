"""Shared fakes. Other test modules can use the fixtures `settings`, `fake_llm`, `fake_sandbox`,
`fake_envbuild` and `sample_issue` without importing anything from here.

FakeLLM       scripted model: queue ChatResults, strings, dicts (sent as JSON) or exceptions.
FakeSandbox   records every run, can execute shell commands in a real temporary repository
              (so list_dir / read_file / search_code are tested for real), and answers any command
              containing a registered needle with a canned ExecResult.
FakeEnvBuild  patches fixedyet.envbuild so pipeline tests need no sandbox protocol details.

Nothing here touches the network or a paid API.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fixedyet import envbuild, evidence
from fixedyet.config import Settings
from fixedyet.llm import BudgetExceeded, ChatResult, ToolCall, cost_usd
from fixedyet.models import ExecResult, Issue, Outcome, SideRun, Usage

# -- settings ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _relative_runs_dir_is_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings().runs_dir` is the relative `runs`, and the spend ledger defaults to a user-level
    state file. A test that builds a real `LLM` must write neither into the checkout nor into the
    real home: the working directory and XDG_STATE_HOME both go to tmp_path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("FIXEDYET_LEDGER_PATH", raising=False)


@pytest.fixture(autouse=True)
def _repositories_are_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """The web app reads a repository's language from GitHub before it counts a run. Tests never
    touch the network: every repository is Python unless a test says otherwise."""
    from fixedyet.web import app as web_app

    async def python(owner: str, repo: str, *, settings: Any) -> str | None:
        return "Python"

    monkeypatch.setattr(web_app, "fetch_repo_language", python)


@pytest.fixture(autouse=True)
def _no_changelog_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never fetch a project's changelog from GitHub: `evidence.fetch_text` finds nothing.
    A test that wants a changelog passes its own ``fetch``."""

    async def nothing(url: str) -> None:
        return None

    monkeypatch.setattr(evidence, "fetch_text", nothing)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        nebius_api_key="test-key-not-real",
        github_token="",
        tavily_api_key="",
        runs_dir=tmp_path / "runs",
        max_turns=8,
        run_budget_usd=0.60,
    )


@pytest.fixture
def sample_issue() -> Issue:
    return Issue(
        url="https://github.com/acme/widgets/issues/42",
        owner="acme",
        repo="widgets",
        number=42,
        title="parse() drops the last item",
        body=(
            "Version 1.2.0.\n\n```python\nfrom widgets import parse\n"
            "assert parse('a,b,c') == ['a', 'b', 'c']\n```\nGot ['a', 'b'].\n"
        ),
        comments=["@alice: same here on 1.2.0"],
        default_branch="main",
    )


# -- the model -----------------------------------------------------------------------------------


class FakeLLM:
    """Scripted stand-in for `fixedyet.llm.LLM`.

    >>> llm.queue(FakeLLM.json({"runnable": True}), FakeLLM.tool("submit", code="x"))
    Items: ChatResult | str (content) | dict or list (sent as JSON) | Exception (raised) |
    callable(messages, tools, model) returning one of those.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.usage = Usage()
        self.calls: list[dict[str, Any]] = []
        self._script: list[Any] = []

    # builders ---------------------------------------------------------------------------------

    @staticmethod
    def text(content: str) -> ChatResult:
        return ChatResult(content=content, finish_reason="stop")

    @staticmethod
    def json(payload: Any) -> ChatResult:
        return ChatResult(content=json.dumps(payload), finish_reason="stop")

    @staticmethod
    def tool(name: str, call_id: str | None = None, **arguments: Any) -> ChatResult:
        call = ToolCall(id=call_id or f"call_{name}", name=name, arguments=arguments)
        return ChatResult(tool_calls=[call], finish_reason="tool_calls")

    def queue(self, *items: Any) -> FakeLLM:
        self._script.extend(items)
        return self

    # the LLM interface ------------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        thinking: bool | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        model = model or self.settings.model
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": tools,
                "model": model,
                "max_tokens": max_tokens,
                "thinking": thinking,
            }
        )
        if not self._script:
            raise AssertionError(f"FakeLLM script exhausted at call {len(self.calls)}")
        item = self._script.pop(0)
        if callable(item):
            item = item(messages, tools, model)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            item = self.text(item)
        elif isinstance(item, dict | list):
            item = self.json(item)
        result: ChatResult = item.model_copy(deep=True)
        result.model = model
        tokens_in, tokens_out = 1_000, 200
        result.usage = Usage(
            llm_calls=1,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=cost_usd(model, tokens_in, tokens_out),
        )
        self.usage.llm_calls += 1
        self.usage.input_tokens += tokens_in
        self.usage.output_tokens += tokens_out
        self.usage.cost_usd += result.usage.cost_usd
        if self.usage.cost_usd > self.settings.run_budget_usd:
            raise BudgetExceeded(self.usage.cost_usd, self.settings.run_budget_usd)
        return result


@pytest.fixture
def fake_llm(settings: Settings) -> FakeLLM:
    return FakeLLM(settings)


# -- the sandbox ---------------------------------------------------------------------------------

REPO_DIR = "/work/repo"
SAMPLE_REPO = {
    "README.md": "# widgets\n",
    "src/widgets/__init__.py": (
        "def parse(text):\n"
        "    parts = text.split(',')\n"
        "    return parts[:-1]  # BUG: drops the last item\n"
    ),
    "src/widgets/util.py": "def helper():\n    return 1\n",
    "tests/test_basic.py": "def test_ok():\n    assert True\n",
}


@dataclass
class RunCall:
    image: str
    command: str
    files: dict[str, str] | None
    workdir: str
    timeout_s: int
    keep: bool
    network: bool


@dataclass
class FakeSandbox:
    """Implements the `Sandbox` protocol. See the module docstring."""

    repo_dir: Path
    name: str = "fake"
    calls: list[RunCall] = field(default_factory=list)
    handlers: list[tuple[str, ExecResult | Callable[[RunCall], ExecResult]]] = field(
        default_factory=list
    )
    _checkpoints: int = 0

    def __post_init__(self) -> None:
        for relative, content in SAMPLE_REPO.items():
            target = self.repo_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def on(self, needle: str, result: ExecResult | Callable[[RunCall], ExecResult]) -> FakeSandbox:
        """Answer every command containing `needle` with `result` (checked in order added)."""
        self.handlers.append((needle, result))
        return self

    async def base_image(self, python_version: str | None) -> str:
        return f"fake-base:{python_version or 'default'}"

    async def run(
        self,
        image: str,
        command: str,
        *,
        files: dict[str, str] | None = None,
        workdir: str = "/work",
        timeout_s: int = 900,
        keep: bool = False,
        network: bool = True,
    ) -> ExecResult:
        call = RunCall(image, command, files, workdir, timeout_s, keep, network)
        self.calls.append(call)
        for needle, result in self.handlers:
            if needle in command:
                return result(call) if callable(result) else result
        result = await self._shell(call)
        if keep:
            self._checkpoints += 1
            result = result.model_copy(update={"image": f"fake-ckpt-{self._checkpoints}"})
        return result

    async def _shell(self, call: RunCall) -> ExecResult:
        """Run the command with bash in the temporary repository (the fork's filesystem)."""
        for path, content in (call.files or {}).items():
            if path.startswith(f"{REPO_DIR}/"):
                target = self.repo_dir / path.removeprefix(f"{REPO_DIR}/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", call.command, cwd=self.repo_dir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return ExecResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )


@pytest.fixture
def fake_sandbox(tmp_path: Path) -> FakeSandbox:
    repo = tmp_path / "repo"
    repo.mkdir()
    return FakeSandbox(repo_dir=repo)


# -- the environment builder ---------------------------------------------------------------------


@dataclass
class FakeEnvBuild:
    """Replaces build_baseline, run_test_on_side and resolve_ref_for_version in `envbuild`.

    Set `reported` / `fix` to the outcomes the two sides should produce, `build_error` to make the
    baseline fail, `tags` to the versions that resolve to a git tag.
    """

    reported: list[Outcome] = field(default_factory=lambda: [Outcome.FAIL, Outcome.FAIL])
    fix: list[Outcome] = field(default_factory=lambda: [Outcome.PASS, Outcome.PASS])
    build_error: str | None = None
    build_stdout: str = ""  # what the baseline script printed, e.g. __EXTRA_OK__= markers
    reported_log: str | None = None  # the log excerpt of the reported side (default: by outcome)
    fix_log: str | None = None  # likewise for the fix side
    extra_calls: list[list[str]] = field(default_factory=list)
    project_extra_calls: list[list[str]] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=lambda: {"1.2.0": "v1.2.0"})
    build_calls: list[dict[str, Any]] = field(default_factory=list)
    side_calls: list[dict[str, Any]] = field(default_factory=list)
    resolve_calls: list[tuple[str, str]] = field(default_factory=list)

    async def build_baseline(self, sb: Any, *, repo: str, ref: str, python_version: str | None,
                             progress: Any = None, extra_packages: Any = (),
                             project_extras: Any = (), **_: Any) -> tuple[str, ExecResult]:
        self.build_calls.append({"repo": repo, "ref": ref, "python_version": python_version})
        self.extra_calls.append(list(extra_packages))
        self.project_extra_calls.append(list(project_extras))
        if self.build_error:
            raise envbuild.EnvBuildError(self.build_error)
        return "baseline-image", ExecResult(
            exit_code=0, image="baseline-image", stdout=self.build_stdout
        )

    async def run_test_on_side(self, sb: Any, image: str, *, test_path: str, test_code: str,
                               checkout: str | None = None, patch: str | None = None,
                               runs: int = 2, ref: str | None = None,
                               project_extras: Any = (), **_: Any) -> SideRun:
        self.side_calls.append({"checkout": checkout, "patch": patch, "ref": ref,
                                "test_code": test_code, "test_path": test_path,
                                "project_extras": list(project_extras)})
        fix_side = bool(checkout or patch)
        outcomes = self.fix if fix_side else self.reported
        failing = [o for o in outcomes if o is Outcome.FAIL]
        log = "E   AssertionError: assert ['a', 'b'] == ['a', 'b', 'c']" if failing else "1 passed"
        override = self.fix_log if fix_side else self.reported_log
        if override is not None:
            log = override
        return SideRun(
            ref=ref or checkout or "HEAD", outcomes=list(outcomes),
            checkpoint_image=image, log_excerpt=log,
        )

    async def resolve_ref_for_version(self, repo: str, version: str, **_: Any) -> str | None:
        self.resolve_calls.append((repo, version))
        return self.tags.get(version)


@pytest.fixture
def fake_envbuild(monkeypatch: pytest.MonkeyPatch) -> FakeEnvBuild:
    fake = FakeEnvBuild()
    monkeypatch.setattr(envbuild, "build_baseline", fake.build_baseline)
    monkeypatch.setattr(envbuild, "run_test_on_side", fake.run_test_on_side)
    monkeypatch.setattr(envbuild, "resolve_ref_for_version", fake.resolve_ref_for_version)
    return fake
