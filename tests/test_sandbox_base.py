"""Protocol, helpers, factory and FakeSandbox. No network, no Docker."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from fixedyet.models import ExecResult
from fixedyet.sandbox import SandboxError, base, get_sandbox
from fixedyet.sandbox.base import (
    MAX_OUTPUT_BYTES,
    Sandbox,
    build_script,
    isolate_network,
    normalize_python_version,
    stage_files,
    truncate_output,
)
from fixedyet.sandbox.docker import DockerSandbox
from fixedyet.sandbox.fake import FakeSandbox
from fixedyet.sandbox.nebius import NebiusSandbox


def test_normalize_python_version() -> None:
    assert normalize_python_version(None) == "3.12"
    assert normalize_python_version("") == "3.12"
    assert normalize_python_version("3.9") == "3.9"
    assert normalize_python_version(" 3.11.4 ") == "3.11.4"
    assert normalize_python_version("python3.10") == "3.10"
    assert normalize_python_version("3") == "3"


@pytest.mark.parametrize("bad", ["3.12; rm -rf /", "latest", "3.x", "3.12-slim", "../3.12"])
def test_normalize_python_version_rejects_unsafe_values(bad: str) -> None:
    with pytest.raises(SandboxError):
        normalize_python_version(bad)


def test_truncate_output_keeps_short_text_unchanged() -> None:
    assert truncate_output("hello") == "hello"
    assert truncate_output(b"hello") == "hello"
    assert truncate_output(None) == ""


def test_truncate_output_keeps_the_tail_and_notes_the_drop() -> None:
    text = "A" * 100 + "TAIL"
    out = truncate_output(text, limit=10)
    assert out.endswith("AAAAAATAIL")
    assert "94 bytes of earlier output truncated" in out
    assert len(out.encode()) < 100


def test_truncate_output_default_limit_is_one_mebibyte() -> None:
    assert MAX_OUTPUT_BYTES == 1024 * 1024
    out = truncate_output(b"x" * (MAX_OUTPUT_BYTES + 5))
    assert out.count("x") == MAX_OUTPUT_BYTES


def test_truncate_output_survives_invalid_utf8() -> None:
    assert "�" in truncate_output(b"ok \xff\xfe bytes")


def test_build_script_enters_workdir_first() -> None:
    script = build_script("pytest -q", "/work/my dir")
    first, second = script.splitlines()[:2]
    assert first.startswith("mkdir -p '/work/my dir' && cd '/work/my dir' ||")
    assert "exit 97" in first
    assert second == "pytest -q"


def test_build_script_rejects_relative_workdir() -> None:
    with pytest.raises(SandboxError):
        build_script("true", "work")


def test_build_script_runs_the_prelude_before_entering_workdir() -> None:
    script = build_script("pytest", "/work", prelude="touch staged\n")
    assert script.splitlines()[0] == "touch staged"
    assert script.splitlines()[1].startswith("mkdir -p /work && cd /work")


def test_stage_files_plans_flat_uploads_and_a_copy_prelude() -> None:
    uploads, prelude = stage_files({"/work/repo/tests/t.py": "a", "/etc/with space.txt": "b"})
    assert uploads == {"/tmp/.ir_upload_0": "a", "/tmp/.ir_upload_1": "b"}
    lines = prelude.splitlines()
    assert lines[0].startswith(
        'mkdir -p "$(dirname /work/repo/tests/t.py)" && cp /tmp/.ir_upload_0 '
    )
    assert "'/etc/with space.txt'" in lines[1]
    assert stage_files(None) == ({}, "")
    assert stage_files({}) == ({}, "")
    with pytest.raises(SandboxError):
        stage_files({"relative.py": "x"})


def test_stage_files_prelude_really_creates_parents_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base, "STAGE_PREFIX", f"{tmp_path}/.stage_")
    dest = tmp_path / "does" / "not" / "exist yet" / "t.py"
    uploads, prelude = stage_files({str(dest): "print('hi')\n"})
    for path, content in uploads.items():
        Path(path).write_text(content)
    done = subprocess.run(["bash", "-c", prelude], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert dest.read_text() == "print('hi')\n"
    assert not Path(next(iter(uploads))).exists()


def test_stage_files_prelude_fails_loudly_when_it_cannot_write(tmp_path: Path) -> None:
    uploads, prelude = stage_files({"/proc/definitely/not/writable.txt": "x"})
    assert uploads
    done = subprocess.run(["bash", "-c", prelude], capture_output=True, text=True, check=False)
    assert done.returncode == 98
    assert "cannot write /proc/definitely/not/writable.txt" in done.stderr


def test_isolate_network_script_is_valid_and_runs_the_command() -> None:
    script = isolate_network("echo 'it ran'; exit 3")
    syntax = subprocess.run(
        ["bash", "-n"], input=script, capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr
    assert "unshare --net" in script
    # Where unshare is not permitted (macOS, unprivileged CI) the fallback path runs the command.
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert done.returncode == 3
    assert done.stdout.strip() == "it ran"


def test_get_sandbox_selects_backend() -> None:
    assert isinstance(get_sandbox(SimpleNamespace(sandbox="docker")), DockerSandbox)
    assert isinstance(get_sandbox(SimpleNamespace(sandbox="Nebius")), NebiusSandbox)


def test_get_sandbox_rejects_unknown_backend() -> None:
    with pytest.raises(SandboxError, match="unknown sandbox backend"):
        get_sandbox(SimpleNamespace(sandbox="firecracker"))


def test_backends_satisfy_the_protocol() -> None:
    assert isinstance(DockerSandbox(), Sandbox)
    assert isinstance(NebiusSandbox(SimpleNamespace()), Sandbox)
    assert isinstance(FakeSandbox(), Sandbox)


async def test_fake_sandbox_records_calls_and_replays_script() -> None:
    boom = SandboxError("boom")
    sb = FakeSandbox(
        [
            ExecResult(exit_code=0, stdout="one"),
            ExecResult(exit_code=2, stderr="two"),
            lambda call: ExecResult(exit_code=0, stdout=call.command.upper()),
            boom,
        ]
    )
    first = await sb.run("img", "echo one", files={"/a": "x"}, keep=True)
    assert first.stdout == "one"
    assert first.image == "fake-ckpt-1"  # keep=True on a successful run mints a checkpoint
    second = await sb.run("img", "bad", keep=True)
    assert second.exit_code == 2
    assert second.image is None  # failed runs mint no checkpoint
    third = await sb.run("img", "abc")
    assert third.stdout == "ABC"
    with pytest.raises(SandboxError):
        await sb.run("img", "x")
    assert (await sb.run("img", "after script")).exit_code == 0  # default result
    assert [c.command for c in sb.calls] == ["echo one", "bad", "abc", "x", "after script"]
    assert sb.calls[0].files == {"/a": "x"}
    assert sb.calls[0].keep is True
    assert await sb.base_image(None) == "fake-base:3.12"
    assert sb.base_image_requests == [None]
