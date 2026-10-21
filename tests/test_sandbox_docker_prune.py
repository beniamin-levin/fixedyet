"""Pruning of old `fixedyet-ckpt` images, tested against a stateful fake `docker` executable.

The fake keeps a list of images in a JSON file, answers `image ls`, `image inspect`, `ps -a` and
`rmi` from it, and records every call. `image ls` deliberately ignores the repository argument
and lists every image, as a misbehaving docker would, so these tests prove that the code itself
never removes an image outside the `fixedyet-ckpt` repository.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from fixedyet.sandbox.base import SandboxError
from fixedyet.sandbox.docker import CHECKPOINT_REPO, DockerSandbox, PruneResult

FAKE_DOCKER = """#!{python}
import json, os, sys

args = sys.argv[1:]
state_path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(state_path))
with open(state_path + ".calls", "a") as handle:
    handle.write(json.dumps(args) + "\\n")

if args[:2] == ["image", "ls"]:
    if state.get("ls_fails"):
        sys.stderr.write("Cannot connect to the Docker daemon\\n")
        sys.exit(1)
    for image in state["images"]:
        print(image["tag"])
elif args[:2] == ["image", "inspect"]:
    tags = args[4:]
    for image in state["images"]:
        if image["tag"] in tags:
            all_tags = [image["tag"], *image.get("also", [])]
            print(image["created"] + "|" + json.dumps(all_tags))
elif args[:2] == ["ps", "-a"]:
    for ref in state.get("containers", []):
        print(ref)
elif args[0] == "rmi":
    tag = args[1]
    if tag in state.get("rmi_fails", []):
        sys.stderr.write("Error response from daemon: conflict: image is being used\\n")
        sys.exit(1)
    state["images"] = [image for image in state["images"] if image["tag"] != tag]
    json.dump(state, open(state_path, "w"))
    print("Untagged: " + tag)
"""


class FakeDocker:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        script = tmp_path / "docker"
        script.write_text(FAKE_DOCKER.format(python=sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.state_path = tmp_path / "state.json"
        self.state: dict = {"images": [], "containers": [], "rmi_fails": []}
        self.save()
        monkeypatch.setenv("FAKE_DOCKER_STATE", str(self.state_path))
        self.sandbox = DockerSandbox(docker_bin=str(script))

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def add(self, tag: str, created: str, **extra: object) -> None:
        self.state["images"].append({"tag": tag, "created": created, **extra})
        self.save()

    def tags(self) -> list[str]:
        return [image["tag"] for image in json.loads(self.state_path.read_text())["images"]]

    def calls(self) -> list[list[str]]:
        path = Path(str(self.state_path) + ".calls")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def removed_by_rmi(self) -> list[str]:
        return [call[1] for call in self.calls() if call[0] == "rmi"]


def ckpt(name: str) -> str:
    return f"{CHECKPOINT_REPO}:{name}"


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    """Five checkpoints (a is the oldest, e the newest) next to images that are not ours."""
    fake = FakeDocker(tmp_path, monkeypatch)
    fake.add("python:3.12", "2024-01-01T00:00:00.000000000Z")
    fake.add(ckpt("c"), "2026-10-21T12:00:03.100000000+03:00")
    fake.add(ckpt("a"), "2026-10-21T12:00:01.000000000+03:00")
    fake.add("issue-repro-ckpt:old1", "2026-10-01T00:00:00.000000000Z")
    fake.add(ckpt("e"), "2026-10-21T09:00:05.000000000Z")  # 12:00:05 local time, the newest
    fake.add(ckpt("b"), "2026-10-21T12:00:02.000000000+03:00")
    fake.add(ckpt("d"), "2026-10-21T12:00:04.000000000+03:00")
    fake.add("fixedyet-ckpt-extra:keepme", "2026-10-01T00:00:00.000000000Z")
    fake.add("registry.example.com:5000/fixedyet-ckpt:x1", "2026-10-01T00:00:00.000000000Z")
    fake.add("fixedyet-ckpt:<none>", "2026-10-01T00:00:00.000000000Z")
    return fake


async def test_checkpoints_are_listed_newest_first_and_only_from_our_repository(
    docker: FakeDocker,
) -> None:
    listed = await docker.sandbox.list_checkpoints()
    assert [c.tag for c in listed] == [ckpt(n) for n in "edcba"]
    assert all(c.created.tzinfo is not None for c in listed)


async def test_prune_keeps_the_newest_and_removes_the_rest(docker: FakeDocker) -> None:
    result = await docker.sandbox.prune_checkpoints(2)
    assert result == PruneResult(
        kept=[ckpt("e"), ckpt("d")], removed=[ckpt("c"), ckpt("b"), ckpt("a")]
    )
    assert docker.removed_by_rmi() == [ckpt("c"), ckpt("b"), ckpt("a")]
    assert ckpt("e") in docker.tags() and ckpt("d") in docker.tags()


async def test_prune_never_touches_an_image_outside_the_fixedyet_ckpt_repository(
    docker: FakeDocker,
) -> None:
    await docker.sandbox.prune_checkpoints(0)  # even when everything of ours may go
    assert sorted(docker.removed_by_rmi()) == sorted(ckpt(n) for n in "abcde")
    assert set(docker.tags()) == {
        "python:3.12",
        "issue-repro-ckpt:old1",
        "fixedyet-ckpt-extra:keepme",
        "registry.example.com:5000/fixedyet-ckpt:x1",
        "fixedyet-ckpt:<none>",
    }
    for call in docker.calls():  # rmi is only ever given exact fixedyet-ckpt:<tag> arguments
        if call[0] == "rmi":
            assert call[1].startswith(f"{CHECKPOINT_REPO}:") and len(call) == 2


async def test_nothing_to_prune_when_there_are_few_enough(docker: FakeDocker) -> None:
    result = await docker.sandbox.prune_checkpoints(5)
    assert result.removed == [] and len(result.kept) == 5
    assert docker.removed_by_rmi() == []
    assert not any(call[0] == "ps" for call in docker.calls())  # no need to look for users


async def test_an_image_a_container_uses_is_skipped(docker: FakeDocker) -> None:
    docker.state["containers"] = [ckpt("b"), "python:3.12"]
    docker.save()
    result = await docker.sandbox.prune_checkpoints(2)
    assert result.in_use == [ckpt("b")]
    assert result.removed == [ckpt("c"), ckpt("a")]
    assert ckpt("b") in docker.tags()


async def test_an_image_docker_refuses_to_remove_is_reported_and_the_rest_still_go(
    docker: FakeDocker,
) -> None:
    docker.state["rmi_fails"] = [ckpt("c")]
    docker.save()
    result = await docker.sandbox.prune_checkpoints(2)
    assert result.failed == [ckpt("c")]
    assert result.removed == [ckpt("b"), ckpt("a")]


async def test_dry_run_removes_nothing(docker: FakeDocker) -> None:
    result = await docker.sandbox.prune_checkpoints(3, dry_run=True)
    assert result.removed == [ckpt("b"), ckpt("a")]
    assert docker.removed_by_rmi() == []
    assert len([t for t in docker.tags() if t.startswith(f"{CHECKPOINT_REPO}:")]) == 6  # + <none>


async def test_a_negative_keep_is_rejected(docker: FakeDocker) -> None:
    with pytest.raises(ValueError):
        await docker.sandbox.prune_checkpoints(-1)
    assert docker.calls() == []


async def test_a_failing_image_listing_is_a_sandbox_error_and_removes_nothing(
    docker: FakeDocker,
) -> None:
    docker.state["ls_fails"] = True
    docker.save()
    with pytest.raises(SandboxError, match="docker image ls failed"):
        await docker.sandbox.prune_checkpoints(1)
    assert docker.removed_by_rmi() == []


async def test_images_created_in_the_same_second_are_ordered_by_their_nanoseconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(tmp_path, monkeypatch)
    fake.add(ckpt("first"), "2026-10-21T09:00:00.100000000Z")
    fake.add(ckpt("third"), "2026-10-21T09:00:00.900000000Z")
    fake.add(ckpt("second"), "2026-10-21T09:00:00.500000000Z")
    result = await fake.sandbox.prune_checkpoints(1)
    assert result.kept == [ckpt("third")]
    assert result.removed == [ckpt("second"), ckpt("first")]


async def test_an_extra_tag_on_a_checkpoint_image_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(tmp_path, monkeypatch)
    fake.add(ckpt("new"), "2026-10-21T09:00:02Z")
    fake.add(ckpt("old"), "2026-10-21T09:00:01Z", also=["mine:pinned"])
    result = await fake.sandbox.prune_checkpoints(1)
    assert result.removed == [ckpt("old")]
    assert fake.removed_by_rmi() == [ckpt("old")]  # only our tag is named, never mine:pinned
