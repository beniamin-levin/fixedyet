"""DockerSandbox against a real Docker daemon. Run with ``pytest -m docker`` (CI does)."""

from __future__ import annotations

import shutil

import pytest

from fixedyet.sandbox.docker import DockerSandbox

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]


@pytest.fixture
def sandbox() -> DockerSandbox:
    return DockerSandbox()


async def test_run_captures_stdout_stderr_and_exit_code(sandbox: DockerSandbox) -> None:
    base = await sandbox.base_image("3.12")
    result = await sandbox.run(base, "echo out; echo err >&2; exit 7", timeout_s=120)
    assert result.exit_code == 7
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.image is None


async def test_base_image_has_git_and_python(sandbox: DockerSandbox) -> None:
    base = await sandbox.base_image("3.12")
    result = await sandbox.run(base, "git --version && python --version", timeout_s=120)
    assert result.exit_code == 0, result.stderr
    assert "Python 3.12" in result.stdout


async def test_files_are_written_before_the_command_runs(sandbox: DockerSandbox) -> None:
    base = await sandbox.base_image("3.12")
    result = await sandbox.run(
        base,
        "cat /opt/deep/dir/note.txt && pwd",
        files={"/opt/deep/dir/note.txt": "from tar\n"},
        workdir="/work/sub",
        timeout_s=120,
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == ["from tar", "/work/sub"]


async def test_keep_checkpoints_and_forks_are_isolated(sandbox: DockerSandbox) -> None:
    base = await sandbox.base_image("3.12")
    made = await sandbox.run(base, "echo checkpointed > /work/marker.txt", keep=True)
    assert made.exit_code == 0 and made.image
    # A fork sees the checkpoint's files and its own changes stay private.
    fork_a = await sandbox.run(made.image, "cat /work/marker.txt; echo a > /work/a.txt")
    fork_b = await sandbox.run(made.image, "cat /work/marker.txt; test ! -e /work/a.txt && echo b")
    assert fork_a.stdout.strip() == "checkpointed"
    assert fork_b.stdout.splitlines() == ["checkpointed", "b"]
    # The base image never saw the marker.
    fresh = await sandbox.run(base, "test ! -e /work/marker.txt && echo clean")
    assert fresh.stdout.strip() == "clean"


async def test_timeout_kills_the_run(sandbox: DockerSandbox) -> None:
    base = await sandbox.base_image("3.12")
    result = await sandbox.run(base, "sleep 60", timeout_s=3, keep=True)
    assert result.timed_out
    assert result.image is None


async def test_network_false_has_no_connectivity(sandbox: DockerSandbox) -> None:
    base = await sandbox.base_image("3.12")
    result = await sandbox.run(
        base,
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\"",
        network=False,
        timeout_s=60,
    )
    assert result.exit_code != 0
