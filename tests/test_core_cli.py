from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fixedyet import cli, ledger
from fixedyet.config import Settings
from fixedyet.models import Assessment, Confidence, ExecResult, ProgressEvent, Verdict

URL = "https://github.com/acme/widgets/issues/42"


def fake_assessment() -> Assessment:
    return Assessment(
        issue_url=URL, title="parse() drops the last item", verdict=Verdict.OPEN_BUG,
        confidence=Confidence.HIGH, confidence_basis=["both runs agree"],
    )


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(nebius_api_key="SECRET-KEY-VALUE")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


# -- argument parsing ---------------------------------------------------------------------------


def test_parser_accepts_every_documented_command() -> None:
    parser = cli.build_parser()
    run = parser.parse_args(["run", URL, "--fix-ref", "v2", "--json", "a.json", "--md", "a.md"])
    assert (run.command, run.issue_url, run.fix_ref, run.json, run.md) == (
        "run", URL, "v2", "a.json", "a.md",
    )
    assert parser.parse_args(["doctor", "--tavily"]).tavily is True
    assert parser.parse_args(["doctor"]).tavily is False
    serve = parser.parse_args(["serve"])
    assert (serve.host, serve.port) == ("127.0.0.1", 8090)
    assert parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "9000"]).port == 9000
    assert parser.parse_args(["mcp"]).command == "mcp"
    gc = parser.parse_args(["gc"])
    assert (gc.command, gc.keep, gc.dry_run) == ("gc", None, False)
    gc = parser.parse_args(["gc", "--keep", "2", "--dry-run"])
    assert (gc.keep, gc.dry_run) == (2, True)


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2


# -- run ----------------------------------------------------------------------------------------


def test_run_prints_progress_to_stderr_and_the_report_to_stdout(
    keyed, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    async def fake_reproduce(url: str, *, fix_ref: str | None, settings: Settings, progress: Any):
        seen.update(url=url, fix_ref=fix_ref, settings=settings)
        progress(ProgressEvent(stage="intake", message="Fetching the issue"))
        return fake_assessment()

    monkeypatch.setattr("fixedyet.pipeline.reproduce", fake_reproduce)
    json_path, md_path = tmp_path / "out.json", tmp_path / "out.md"
    code = cli.main(["run", URL, "--fix-ref", "v2", "--json", str(json_path), "--md", str(md_path)])

    out, err = capsys.readouterr()
    assert code == 0
    assert seen["url"] == URL and seen["fix_ref"] == "v2" and seen["settings"] is keyed
    assert "intake: Fetching the issue" in err and "intake" not in out
    assert out.lstrip().startswith("# Open bug")
    assert Assessment.model_validate_json(json_path.read_text()).verdict is Verdict.OPEN_BUG
    assert md_path.read_text().startswith("# Open bug")
    assert "SECRET-KEY-VALUE" not in out + err


def test_run_without_a_key_explains_what_to_do(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(nebius_api_key=""))
    assert cli.main(["run", URL]) == 2
    assert "NEBIUS_API_KEY" in capsys.readouterr().err


def test_run_with_a_bad_url_reports_and_exits_2(keyed, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "https://example.com/nope"]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_a_bad_url_is_reported_before_a_missing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(nebius_api_key=""))
    assert cli.main(["run", "nonsense"]) == 2
    assert "github.com" in capsys.readouterr().err


# -- commands that live in other modules ---------------------------------------------------------


def test_eval_forwards_its_arguments_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        received.append(argv)
        return 3

    monkeypatch.setattr(cli, "_module_exists", lambda name: True)
    monkeypatch.setitem(sys.modules, "fixedyet.eval.run", SimpleNamespace(main=fake_main))
    assert cli.main(["eval", "--limit", "5", "--dry-run", "psf__requests-2317"]) == 3
    assert received == [["--limit", "5", "--dry-run", "psf__requests-2317"]]


def test_serve_starts_uvicorn_on_the_web_app(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(cli, "_module_exists", lambda name: True)
    monkeypatch.setitem(
        sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **k: calls.append((a, k)))
    )
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9001"]) == 0
    assert calls == [(("fixedyet.web.app:app",), {"host": "0.0.0.0", "port": 9001})]


# -- gc -----------------------------------------------------------------------------------------


def patch_prune(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[tuple[int, bool]]:
    from fixedyet.sandbox.docker import DockerSandbox

    seen: list[tuple[int, bool]] = []

    async def fake_prune(self: Any, keep: int, *, dry_run: bool = False) -> Any:
        seen.append((keep, dry_run))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(DockerSandbox, "prune_checkpoints", fake_prune)
    return seen


def test_gc_prunes_with_the_configured_keep_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fixedyet.sandbox.docker import PruneResult

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(keep_checkpoints=4))
    seen = patch_prune(
        monkeypatch,
        PruneResult(
            kept=["fixedyet-ckpt:n1"],
            removed=["fixedyet-ckpt:o1", "fixedyet-ckpt:o2"],
            in_use=["fixedyet-ckpt:u1"],
        ),
    )
    assert cli.main(["gc"]) == 0
    assert seen == [(4, False)]
    out = capsys.readouterr().out
    assert "removed fixedyet-ckpt:o1" in out and "removed fixedyet-ckpt:o2" in out
    assert "skipped fixedyet-ckpt:u1 (a container uses it)" in out
    assert "kept 1, removed 2, skipped 1 in use, 0 failed" in out


def test_gc_keep_flag_and_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fixedyet.sandbox.docker import PruneResult

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(keep_checkpoints=4))
    seen = patch_prune(monkeypatch, PruneResult(removed=["fixedyet-ckpt:o1"]))
    assert cli.main(["gc", "--keep", "0", "--dry-run"]) == 0
    assert seen == [(0, True)]
    out = capsys.readouterr().out
    assert "would remove fixedyet-ckpt:o1" in out and "would remove 1" in out


def test_gc_exits_1_when_docker_will_not_remove_an_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fixedyet.sandbox.docker import PruneResult

    monkeypatch.setattr(cli, "get_settings", lambda: Settings())
    patch_prune(monkeypatch, PruneResult(failed=["fixedyet-ckpt:stuck"]))
    assert cli.main(["gc"]) == 1
    assert "could not remove fixedyet-ckpt:stuck" in capsys.readouterr().err


def test_gc_reports_a_missing_docker_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fixedyet.sandbox.base import SandboxError

    monkeypatch.setattr(cli, "get_settings", lambda: Settings())
    patch_prune(monkeypatch, SandboxError("docker CLI not found ('docker'); install Docker"))
    assert cli.main(["gc"]) == 1
    assert "docker CLI not found" in capsys.readouterr().err


def test_gc_rejects_a_negative_keep(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: Settings())
    seen = patch_prune(monkeypatch, None)
    assert cli.main(["gc", "--keep", "-1"]) == 2
    assert seen == []


def test_mcp_runs_the_server_main(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(cli, "_module_exists", lambda name: True)
    monkeypatch.setitem(
        sys.modules, "fixedyet.mcp_server", SimpleNamespace(main=lambda: started.append(True))
    )
    assert cli.main(["mcp"]) == 0 and started == [True]


@pytest.mark.parametrize("argv", [["eval"], ["serve"], ["mcp"]])
def test_missing_modules_give_a_clear_error_not_a_traceback(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_module_exists", lambda name: False)
    assert cli.main(argv) == 2
    assert "is not installed" in capsys.readouterr().err


# -- doctor --------------------------------------------------------------------------------------


def patch_probes(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, int]:
    calls = {"chat": 0, "sandbox": 0, "github": 0, "tavily": 0}

    def make(name: str, detail: str):
        async def probe(settings: Settings) -> str:
            calls[name] += 1
            if isinstance(overrides.get(name), Exception):
                raise overrides[name]
            return detail

        return probe

    monkeypatch.setattr(cli, "_probe_chat", make("chat", "model answered"))
    monkeypatch.setattr(cli, "_probe_sandbox", make("sandbox", "fake: echo ok worked"))
    monkeypatch.setattr(cli, "_probe_github", make("github", "4999/5000 requests left"))
    monkeypatch.setattr(cli, "_probe_tavily", make("tavily", "1 result"))
    return calls


async def test_doctor_all_green_prints_a_table_and_returns_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = patch_probes(monkeypatch)
    settings = Settings(nebius_api_key="SECRET-KEY-VALUE", tavily_api_key="SECRET-TAVILY")
    assert await cli._doctor(settings, tavily=False) == 0
    out = capsys.readouterr().out
    assert "check" in out and "status" in out
    assert "NEBIUS_API_KEY" in out and "echo ok worked" in out and "4999/5000" in out
    assert "pass --tavily" in out and calls["tavily"] == 0
    assert "SECRET" not in out


async def test_doctor_prints_the_ledger_total_and_the_budget_left(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    patch_probes(monkeypatch)
    settings = Settings(nebius_api_key="k", runs_dir=tmp_path / "runs", total_budget_usd=20.0)
    ledger.record(settings, 3.5)
    assert await cli._doctor(settings, tavily=False) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("spend ledger"))
    assert "ok" in line and "spent $3.5000 of $20.00" in line and "remaining $16.5000" in line


async def test_doctor_fails_when_the_budget_cannot_cover_one_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    patch_probes(monkeypatch)
    settings = Settings(
        nebius_api_key="k", runs_dir=tmp_path / "runs", total_budget_usd=10.0, run_budget_usd=0.60
    )
    ledger.record(settings, 9.7)
    assert await cli._doctor(settings, tavily=False) == 1
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("spend ledger"))
    assert "FAIL" in line and "remaining $0.3000" in line and "too little" in line


async def test_doctor_fails_on_a_corrupt_ledger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    patch_probes(monkeypatch)
    settings = Settings(nebius_api_key="k", runs_dir=tmp_path / "runs")
    settings.runs_dir.mkdir()
    (settings.runs_dir / "spend-ledger.json").write_text("{oops", encoding="utf-8")
    assert await cli._doctor(settings, tavily=False) == 1
    assert "corrupt" in capsys.readouterr().out


async def test_doctor_tests_tavily_only_on_request_and_with_a_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = patch_probes(monkeypatch)
    await cli._doctor(Settings(nebius_api_key="k", tavily_api_key="t"), tavily=True)
    assert calls["tavily"] == 1
    await cli._doctor(Settings(nebius_api_key="k"), tavily=True)  # no key: cannot test
    assert calls["tavily"] == 1


async def test_doctor_reports_a_failing_probe_and_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_probes(monkeypatch, sandbox=RuntimeError("no sandbox token"))
    assert await cli._doctor(Settings(nebius_api_key="k"), tavily=False) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "RuntimeError: no sandbox token" in out


async def test_doctor_without_a_key_fails_and_skips_the_model_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = patch_probes(monkeypatch)
    assert await cli._doctor(Settings(nebius_api_key=""), tavily=False) == 1
    assert calls["chat"] == 0 and "missing, required" in capsys.readouterr().out


async def test_the_chat_probe_makes_one_tiny_call(
    monkeypatch: pytest.MonkeyPatch, fake_llm
) -> None:
    fake_llm.queue("ok")
    monkeypatch.setattr("fixedyet.llm.LLM", lambda settings: fake_llm)
    detail = await cli._probe_chat(fake_llm.settings)
    assert "answered" in detail and len(fake_llm.calls) == 1
    assert fake_llm.calls[0]["max_tokens"] == 5 and fake_llm.calls[0]["thinking"] is False


async def test_the_sandbox_probe_runs_echo_ok(
    monkeypatch: pytest.MonkeyPatch, fake_sandbox
) -> None:
    monkeypatch.setattr("fixedyet.sandbox.get_sandbox", lambda settings: fake_sandbox)
    assert "echo ok worked" in await cli._probe_sandbox(Settings())
    assert fake_sandbox.calls[0].command == "echo ok"

    fake_sandbox.on("echo ok", ExecResult(exit_code=3, stderr="denied"))
    with pytest.raises(RuntimeError, match="exited 3"):
        await cli._probe_sandbox(Settings())
