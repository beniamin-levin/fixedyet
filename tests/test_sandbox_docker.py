"""DockerSandbox command plumbing, tested against a fake `docker` executable.

The fake records every invocation as a JSON line and answers with canned output, so these
tests need no Docker daemon. Real-daemon tests live in test_sandbox_docker_integration.py.
"""

from __future__ import annotations

import io
import json
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from fixedyet.sandbox.base import SandboxError
from fixedyet.sandbox.docker import CHECKPOINT_REPO, DockerSandbox, build_tar

FAKE_DOCKER = """#!{python}
import json, os, sys, time

args = sys.argv[1:]
log = os.environ["FAKE_DOCKER_LOG"]
stdin = sys.stdin.buffer.read() if args[:2] == ["cp", "-"] else b""
with open(log, "a") as handle:
    handle.write(json.dumps({{"args": args, "stdin_bytes": len(stdin)}}) + "\\n")
if stdin:
    with open(log + ".tar", "wb") as handle:
        handle.write(stdin)
cmd = args[0]
if cmd == "create":
    sys.stderr.write("Unable to find image locally\\n")
    print("sha-of-container-0001")
elif cmd == "start":
    time.sleep(float(os.environ.get("FAKE_START_SLEEP", "0")))
    repeat = int(os.environ.get("FAKE_STDOUT_REPEAT", "0"))
    sys.stdout.write("y" * repeat + "END" if repeat else "hello\\n")
    sys.stderr.write(os.environ.get("FAKE_STDERR", "warn\\n"))
elif cmd == "inspect":
    print(os.environ.get("FAKE_STATE", "exited") + " " + os.environ.get("FAKE_EXIT", "0"))
elif cmd == "commit":
    print("sha256:committed")
elif cmd == "image":
    sys.exit(int(os.environ.get("FAKE_IMAGE_INSPECT_RC", "0")))
elif cmd == "pull":
    sys.exit(int(os.environ.get("FAKE_PULL_RC", "0")))
"""


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DockerSandbox, Path]:
    script = tmp_path / "docker"
    script.write_text(FAKE_DOCKER.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    return DockerSandbox(docker_bin=str(script)), log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line)["args"] for line in log.read_text().splitlines()]


def test_build_tar_roots_files_at_slash() -> None:
    data = build_tar({"/tmp/a.py": "print(1)\n", "/work/repo/tests/t.py": "x"})
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        names = archive.getnames()
        assert names == ["tmp/a.py", "work/repo/tests/t.py"]
        member = archive.getmember("tmp/a.py")
        assert member.mode == 0o644
        extracted = archive.extractfile(member)
        assert extracted is not None
        assert extracted.read() == b"print(1)\n"


def test_build_tar_rejects_relative_paths() -> None:
    with pytest.raises(SandboxError):
        build_tar({"relative.py": "x"})


async def test_run_sequence_and_result(fake_docker: tuple[DockerSandbox, Path]) -> None:
    sb, log = fake_docker
    result = await sb.run("python:3.12", "echo hi", files={"/tmp/f.py": "data"}, workdir="/work")
    assert (result.exit_code, result.stdout, result.stderr) == (0, "hello\n", "warn\n")
    assert result.image is None
    assert not result.timed_out
    recorded = calls(log)
    assert [c[0] for c in recorded] == ["create", "cp", "start", "inspect", "rm"]
    create = recorded[0]
    assert create[:2] == ["create", "python:3.12"]
    assert create[2:4] == ["bash", "-lc"]
    assert "echo hi" in create[4] and "cd /work" in create[4]
    assert "cp /tmp/.ir_upload_0 /tmp/f.py" in create[4]
    assert "--network" not in create
    assert recorded[1] == ["cp", "-", "sha-of-container-0001:/"]
    assert recorded[2] == ["start", "-a", "sha-of-container-0001"]
    assert recorded[4][:3] == ["rm", "-f", "-v"]
    with tarfile.open(str(log) + ".tar") as archive:
        assert archive.getnames() == ["tmp/.ir_upload_0"]  # staged, then copied by the script


async def test_no_cp_without_files(fake_docker: tuple[DockerSandbox, Path]) -> None:
    sb, log = fake_docker
    await sb.run("img", "true")
    assert [c[0] for c in calls(log)] == ["create", "start", "inspect", "rm"]


async def test_nonzero_exit_code_is_returned_not_raised(
    fake_docker: tuple[DockerSandbox, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sb, _ = fake_docker
    monkeypatch.setenv("FAKE_EXIT", "3")
    result = await sb.run("img", "exit 3")
    assert result.exit_code == 3


async def test_keep_commits_a_checkpoint_tag(fake_docker: tuple[DockerSandbox, Path]) -> None:
    sb, log = fake_docker
    result = await sb.run("img", "touch /work/x", keep=True)
    assert result.image is not None
    assert result.image.startswith(f"{CHECKPOINT_REPO}:")
    commit = [c for c in calls(log) if c[0] == "commit"][0]
    assert commit == ["commit", "--change", 'CMD ["bash"]', "sha-of-container-0001", result.image]
    assert calls(log)[-1][0] == "rm"  # the container is removed after the commit


async def test_network_false_uses_network_none(fake_docker: tuple[DockerSandbox, Path]) -> None:
    sb, log = fake_docker
    await sb.run("img", "true", network=False)
    assert calls(log)[0][:3] == ["create", "--network", "none"]


async def test_timeout_kills_and_removes_the_container(
    fake_docker: tuple[DockerSandbox, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sb, log = fake_docker
    monkeypatch.setenv("FAKE_START_SLEEP", "10")
    result = await sb.run("img", "sleep 999", timeout_s=1, keep=True)
    assert result.timed_out
    assert result.exit_code == 124
    assert result.image is None
    names = [c[0] for c in calls(log)]
    assert names == ["create", "start", "kill", "rm"]
    assert result.duration_s < 8


async def test_container_that_never_ran_is_a_sandbox_error_and_still_removed(
    fake_docker: tuple[DockerSandbox, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sb, log = fake_docker
    monkeypatch.setenv("FAKE_STATE", "created")
    with pytest.raises(SandboxError, match="did not run to completion"):
        await sb.run("img", "true")
    assert calls(log)[-1][0] == "rm"


async def test_output_is_truncated(
    fake_docker: tuple[DockerSandbox, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sb, _ = fake_docker
    monkeypatch.setenv("FAKE_STDOUT_REPEAT", str(1024 * 1024 + 10))
    result = await sb.run("img", "noisy")
    assert result.stdout.endswith("END")
    assert "truncated" in result.stdout.splitlines()[0]


async def test_base_image_pulls_only_when_missing_and_caches(
    fake_docker: tuple[DockerSandbox, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sb, log = fake_docker
    monkeypatch.setenv("FAKE_IMAGE_INSPECT_RC", "1")
    assert await sb.base_image("3.11") == "python:3.11"
    assert await sb.base_image("3.11") == "python:3.11"
    assert [c[:2] for c in calls(log)] == [["image", "inspect"], ["pull", "python:3.11"]]
    monkeypatch.setenv("FAKE_IMAGE_INSPECT_RC", "0")
    assert await sb.base_image(None) == "python:3.12"
    assert calls(log)[-1][0] == "image"  # present locally, so no second pull


async def test_pull_failure_is_a_sandbox_error(
    fake_docker: tuple[DockerSandbox, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sb, _ = fake_docker
    monkeypatch.setenv("FAKE_IMAGE_INSPECT_RC", "1")
    monkeypatch.setenv("FAKE_PULL_RC", "1")
    with pytest.raises(SandboxError, match="docker pull python:3.12 failed"):
        await sb.base_image(None)


async def test_missing_docker_binary_is_a_clear_error() -> None:
    sb = DockerSandbox(docker_bin="/nonexistent/docker-cli")
    with pytest.raises(SandboxError, match="docker CLI not found"):
        await sb.run("img", "true")


async def test_option_like_image_names_are_rejected(
    fake_docker: tuple[DockerSandbox, Path],
) -> None:
    sb, log = fake_docker
    with pytest.raises(SandboxError, match="invalid image"):
        await sb.run("--privileged", "true")
    assert not log.exists()
