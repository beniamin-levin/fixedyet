"""NebiusSandbox through the REAL contree-sdk, against an in-process fake HTTP service.

The fake implements just the endpoints the SDK calls (inspect, import, files, instances,
operations). It lets us check that our SDK calls are valid (argument names, bytes uploads,
state machine, result parsing, timeouts) without credentials. It does not prove the real
service behaves the same: see the `live` test in test_sandbox_nebius.py for that.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from contree_sdk import Contree
from contree_sdk.auth import IAMAuth
from contree_sdk.config import ContreeConfig

from fixedyet.sandbox.base import SandboxError
from fixedyet.sandbox.nebius import NebiusSandbox

AUTH = IAMAuth(token="test-token", project_id="test-project", base_url="https://sandboxes.test/")

ZERO_RESOURCES = {
    "block_input": 0,
    "block_output": 0,
    "cost": 0.0,
    "elapsed_time": 1.5,
    "involuntary_switches": 0,
    "max_rss": 0,
    "monotonic_time": 0.0,
    "page_faults": 0,
    "page_faults_io": 0,
    "shared_memory": 0,
    "signals": 0,
    "swaps": 0,
    "system_cpu_time": 0.0,
    "unshared_memory": 0,
    "user_cpu_time": 0.0,
    "voluntary_switches": 0,
}


def stream(text: str, *, truncated: bool = False) -> dict[str, Any]:
    return {"value": text, "encoding": "ascii", "truncated": truncated}


class FakeService:
    """A tiny in-memory ConTree: images by tag, uploads by sha256, operations by id."""

    def __init__(self) -> None:
        self.base_uuid = str(uuid4())
        self.tags: dict[str, str] = {}
        self.uploads: dict[str, bytes] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.spawns: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        # Scenario knobs for the next spawn.
        self.exit_code = 0
        self.stdout = ""
        self.stderr = ""
        self.timed_out = False
        self.result_image: str | None = None
        self.never_finish = False
        self.fail_with: str | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path.startswith("/sandboxes"):
            path = path.removeprefix("/sandboxes")
        if method == "GET" and path == "/v1/inspect/":
            tag = request.url.params["tag"]
            if tag in self.tags:
                return httpx.Response(200, json={"uuid": self.tags[tag], "tag": tag})
            return httpx.Response(404, json={"status": 404, "error": "no such tag"})
        if method == "POST" and path == "/v1/images/import":
            body = json.loads(request.content)
            self.imports.append(body)
            self.tags[body["tag"]] = self.base_uuid
            op = str(uuid4())
            self.operations[op] = {
                "kind": "image_import",
                "status": "SUCCESS",
                "duration": 1.0,
                "metadata": body,
                "result": {"image": self.base_uuid, "tag": body["tag"]},
            }
            return httpx.Response(200, json={"uuid": op})
        if method == "GET" and path.startswith("/v1/files/"):
            digest = path.rsplit("/", 1)[1]
            if digest in self.uploads:
                return httpx.Response(200, json={"uuid": str(uuid4()), "sha256": digest})
            return httpx.Response(404, json={"status": 404, "error": "no such file"})
        if method == "POST" and path == "/v1/files":
            digest = hashlib.sha256(request.content).hexdigest()
            self.uploads[digest] = request.content
            return httpx.Response(200, json={"uuid": str(uuid4()), "sha256": digest})
        if method == "POST" and path == "/v1/instances":
            return self._spawn(json.loads(request.content))
        if method == "GET" and path.startswith("/v1/operations/"):
            return httpx.Response(200, json=self.operations[path.rsplit("/", 1)[1]])
        if method == "DELETE" and path.startswith("/v1/operations/"):
            self.cancelled.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(500, json={"status": 500, "error": f"unhandled {method} {path}"})

    def _spawn(self, body: dict[str, Any]) -> httpx.Response:
        self.spawns.append(body)
        op = str(uuid4())
        if self.fail_with is not None:
            self.operations[op] = {
                "kind": "instance",
                "status": "FAILED",
                "duration": 0.1,
                "error": self.fail_with,
                "metadata": {**body, "result": None},
            }
        elif self.never_finish:
            self.operations[op] = {
                "kind": "instance",
                "status": "EXECUTING",
                "duration": 0.0,
                "metadata": {**body, "result": None},
            }
        else:
            result = {
                "resources": ZERO_RESOURCES,
                "state": {
                    "continued": False,
                    "core_dump": False,
                    "exit_code": self.exit_code,
                    "pid": 7,
                    "signal": 0,
                    "stopped": False,
                    "timed_out": self.timed_out,
                },
                "stdout": stream(self.stdout),
                "stderr": stream(self.stderr),
            }
            image = None if body["disposable"] else (self.result_image or str(uuid4()))
            self.operations[op] = {
                "kind": "instance",
                "status": "SUCCESS",
                "duration": 1.5,
                "metadata": {**body, "result": result},
                "result": {"image": image, "tag": None},
            }
        return httpx.Response(200, json={"uuid": op})


@pytest.fixture
async def service() -> FakeService:
    return FakeService()


@pytest.fixture
async def sandbox(service: FakeService) -> NebiusSandbox:
    """A NebiusSandbox whose real SDK client talks to ``service`` instead of the network."""
    config = ContreeConfig(auth=AUTH, operation_poll_secs_min=0.01, operation_poll_secs_max=0.05)
    client = Contree(config=config)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(service.handle), base_url="https://sandboxes.test"
    )
    client._api._client_per_loop[asyncio.get_running_loop()] = http  # type: ignore[attr-defined]
    return NebiusSandbox(client=client)


async def test_base_image_imports_python_once_through_the_sdk(
    sandbox: NebiusSandbox, service: FakeService
) -> None:
    first = await sandbox.base_image("3.12")
    assert first == service.base_uuid
    assert len(service.imports) == 1
    assert service.imports[0]["registry"]["url"] == "docker://docker.io/library/python:3.12"
    assert service.imports[0]["tag"] == "python:3.12"
    # A second sandbox object finds the stored tag instead of importing again.
    again = await NebiusSandbox(client=sandbox.client).base_image("3.12")
    assert again == first
    assert len(service.imports) == 1


async def test_run_uploads_bytes_and_parses_the_result(
    sandbox: NebiusSandbox, service: FakeService
) -> None:
    service.exit_code, service.stdout, service.stderr = 1, "1 failed\n", "warn\n"
    result = await sandbox.run(
        service.base_uuid,
        "python -m pytest -q",
        files={"/work/repo/tests/t.py": "def test_x():\n    assert 0\n"},
        workdir="/work/repo",
        timeout_s=321,
    )
    assert (result.exit_code, result.stdout, result.stderr) == (1, "1 failed\n", "warn\n")
    assert result.image is None and not result.timed_out
    (spawn,) = service.spawns
    assert spawn["image"] == service.base_uuid
    assert spawn["timeout"] == 321
    assert spawn["disposable"] is True
    assert spawn["shell"] is True
    assert spawn["truncate_output_at"] == 1024 * 1024
    assert spawn["command"].startswith("bash -lc ")
    assert "python -m pytest -q" in spawn["command"]
    assert "cp /tmp/.ir_upload_0 /work/repo/tests/t.py" in spawn["command"]
    assert list(spawn["files"]) == ["/tmp/.ir_upload_0"]
    uploaded = {content for content in service.uploads.values()}
    assert b"def test_x():\n    assert 0\n" in uploaded


async def test_keep_returns_the_saved_image_id_and_a_fork_can_start_from_it(
    sandbox: NebiusSandbox, service: FakeService
) -> None:
    checkpoint = str(uuid4())
    service.result_image = checkpoint
    made = await sandbox.run(service.base_uuid, "make build", keep=True)
    assert made.image == checkpoint
    assert service.spawns[0]["disposable"] is False
    service.result_image = None
    service.stdout = "forked\n"
    fork = await sandbox.run(made.image or "", "cat /work/out.txt")
    assert fork.stdout == "forked\n"
    assert service.spawns[1]["image"] == checkpoint
    assert service.spawns[1]["disposable"] is True


async def test_image_tags_are_passed_through(sandbox: NebiusSandbox, service: FakeService) -> None:
    await sandbox.run("python:3.12", "true")
    assert service.spawns[0]["image"] == "tag:python:3.12"


async def test_server_reported_timeout_is_mapped(
    sandbox: NebiusSandbox, service: FakeService
) -> None:
    service.exit_code, service.timed_out = 137, True
    result = await sandbox.run(service.base_uuid, "sleep 99", timeout_s=5, keep=True)
    assert result.timed_out and result.exit_code == 137 and result.image is None


async def test_client_side_timeout_cancels_the_operation(
    sandbox: NebiusSandbox, service: FakeService
) -> None:
    service.never_finish = True
    result = await sandbox.run(service.base_uuid, "sleep 99", timeout_s=1)
    assert result.timed_out
    assert result.exit_code == 124
    assert len(service.cancelled) == 1  # the SDK cancels the abandoned operation


async def test_failed_operation_becomes_sandbox_error(
    sandbox: NebiusSandbox, service: FakeService
) -> None:
    service.fail_with = "no capacity in region"
    with pytest.raises(SandboxError, match="no capacity in region"):
        await sandbox.run(service.base_uuid, "true")


async def test_unknown_tag_import_failure_is_sandbox_error(service: FakeService) -> None:
    class Rejecting(FakeService):
        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v1/images/import"):
                return httpx.Response(403, json={"status": 403, "error": "not activated"})
            return super().handle(request)

    rejecting = Rejecting()
    client = Contree(config=ContreeConfig(auth=AUTH))
    client._api._client_per_loop[asyncio.get_running_loop()] = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(rejecting.handle), base_url="https://sandboxes.test"
    )
    with pytest.raises(SandboxError, match="could not import python:3.12"):
        await NebiusSandbox(client=client).base_image(None)
