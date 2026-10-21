"""NebiusSandbox against a fake contree client. No network, no credentials.

The one `live` test at the bottom talks to the real service; run it by hand with
``pytest -m live tests/test_sandbox_nebius.py`` once Sandboxes access is granted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from contree_sdk.auth import IAMAuth, JWTAuth
from contree_sdk.sdk.exceptions import (
    CancelledOperationError,
    FailedOperationError,
    NotFoundError,
    OperationTimedOutError,
)

from fixedyet.sandbox.base import MAX_OUTPUT_BYTES, SandboxError
from fixedyet.sandbox.nebius import (
    SANDBOXES_URL,
    NebiusSandbox,
    build_auth,
)

BASE_UUID = UUID("11111111-1111-1111-1111-111111111111")


class FakeDone:
    """What awaiting `image.run(...)` yields in the SDK."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        uuid: UUID | None = None,
        truncated: bool = False,
        timed_out: bool = False,
    ) -> None:
        state = SimpleNamespace(timed_out=timed_out)
        raw = SimpleNamespace(result=SimpleNamespace(state=state))
        self.uuid = uuid
        self.result = SimpleNamespace(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            _raw=raw,
        )


class FakeRun:
    def __init__(self, outcome: FakeDone | BaseException) -> None:
        self._outcome = outcome

    def __await__(self) -> Any:
        return self._execute().__await__()

    async def _execute(self) -> FakeDone:
        await asyncio.sleep(0)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class FakeImage:
    def __init__(self, client: FakeClient, ref: str) -> None:
        self._client = client
        self.ref = ref

    def run(self, **kwargs: Any) -> FakeRun:
        self._client.runs.append({"image": self.ref, **kwargs})
        outcome = self._client.outcomes.pop(0) if self._client.outcomes else FakeDone()
        return FakeRun(outcome)


class FakeImages:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def use(self, ref: str, strict: bool = False) -> FakeImage:
        return FakeImage(self._client, ref)

    async def oci(self, ref: str, **kwargs: Any) -> SimpleNamespace:
        self._client.oci_calls.append((ref, kwargs))
        if self._client.oci_error is not None:
            raise self._client.oci_error
        return SimpleNamespace(uuid=BASE_UUID, tag=ref)


class FakeClient:
    def __init__(self, outcomes: list[FakeDone | BaseException] | None = None) -> None:
        self.outcomes = outcomes or []
        self.runs: list[dict[str, Any]] = []
        self.oci_calls: list[tuple[str, dict[str, Any]]] = []
        self.oci_error: BaseException | None = None
        self.images = FakeImages(self)


def make_sandbox(
    *outcomes: FakeDone | BaseException, **kwargs: Any
) -> tuple[NebiusSandbox, FakeClient]:
    client = FakeClient(list(outcomes))
    sandbox = NebiusSandbox(SimpleNamespace(), client=client, **kwargs)  # type: ignore[arg-type]
    return sandbox, client


# --------------------------------------------------------------------------- auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NEBIUS_PROJECT_ID", "NEBIUS_API_KEY", "CONTREE_TOKEN", "CONTREE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_build_auth_uses_iam_when_project_known() -> None:
    settings = SimpleNamespace(nebius_api_key="key", nebius_project_id="proj")
    auth = build_auth(settings)
    assert isinstance(auth, IAMAuth)
    assert (auth.token, auth.project_id, auth.base_url) == ("key", "proj", SANDBOXES_URL)
    assert auth.get_headers() == {"Authorization": "Bearer key", "Project": "proj"}


def test_build_auth_reads_project_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "env-proj")
    auth = build_auth(SimpleNamespace(nebius_api_key="key"))
    assert isinstance(auth, IAMAuth)
    assert auth.project_id == "env-proj"


def test_build_auth_prefers_contree_token_and_url_and_unwraps_secrets() -> None:
    class Secret:
        def get_secret_value(self) -> str:
            return "secret-token"

    settings = SimpleNamespace(
        nebius_api_key="inference-key",
        contree_token=Secret(),
        contree_base_url="https://sandboxes.example/",
        nebius_project_id="proj",
    )
    auth = build_auth(settings)
    assert isinstance(auth, IAMAuth)
    assert auth.token == "secret-token"
    assert auth.base_url == "https://sandboxes.example/"


def test_build_auth_legacy_endpoint_uses_jwt_without_a_project() -> None:
    settings = SimpleNamespace(
        contree_token="jwt",
        nebius_api_key="key",
        contree_base_url="https://eu-north.nebius.computer",
    )
    auth = build_auth(settings)
    assert isinstance(auth, JWTAuth)
    assert (auth.token, auth.base_url) == ("jwt", "https://eu-north.nebius.computer")


def test_build_auth_matches_what_config_settings_produce() -> None:
    """Settings.load() copies the API key into contree_token and sets the sandboxes URL."""
    settings = SimpleNamespace(
        nebius_api_key="key", contree_token="key", contree_base_url=SANDBOXES_URL
    )
    with pytest.raises(SandboxError, match="NEBIUS_PROJECT_ID"):
        build_auth(settings)


def test_build_auth_api_key_without_project_is_a_clear_error() -> None:
    with pytest.raises(SandboxError, match="NEBIUS_PROJECT_ID"):
        build_auth(SimpleNamespace(nebius_api_key="key"))


def test_build_auth_without_any_token_defers_to_the_sdk() -> None:
    assert build_auth(SimpleNamespace(nebius_api_key=None, contree_token="")) is None


# --------------------------------------------------------------------------- base_image


async def test_base_image_imports_once_and_caches() -> None:
    sb, client = make_sandbox(import_timeout_s=123)
    ids = await asyncio.gather(sb.base_image("3.11"), sb.base_image("3.11"), sb.base_image("3.11"))
    assert ids == [str(BASE_UUID)] * 3
    assert await sb.base_image("3.11") == str(BASE_UUID)
    assert client.oci_calls == [("python:3.11", {"timeout": 123})]
    await sb.base_image(None)
    assert client.oci_calls[-1][0] == "python:3.12"
    assert len(client.oci_calls) == 2


async def test_base_image_wraps_sdk_errors() -> None:
    sb, client = make_sandbox()
    client.oci_error = NotFoundError(error="no such image")
    with pytest.raises(SandboxError, match="could not import python:3.12"):
        await sb.base_image(None)


# --------------------------------------------------------------------------- run


async def test_run_builds_the_sdk_call_and_maps_the_result() -> None:
    sb, client = make_sandbox(FakeDone(exit_code=3, stdout=b"out", stderr=b"err"))
    result = await sb.run(
        str(BASE_UUID),
        "pytest -q",
        files={"/tmp/t.py": "assert True\n"},
        workdir="/work/repo",
        timeout_s=77,
    )
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out", "err")
    assert result.image is None
    assert not result.timed_out
    assert result.duration_s >= 0
    call = client.runs[0]
    assert call["image"] == str(BASE_UUID)
    assert call["timeout"] == 77
    assert call["disposable"] is True
    assert call["truncate_output_at"] == MAX_OUTPUT_BYTES
    assert call["stdout"] is bytes and call["stderr"] is bytes
    # Uploaded as bytes (a str would be read as a local path) to a flat staging path ...
    assert call["files"] == {"/tmp/.ir_upload_0": b"assert True\n"}
    assert call["shell"].startswith("bash -lc ")
    # ... then copied into place by the script, which also enters the workdir.
    assert "cp /tmp/.ir_upload_0 /tmp/t.py" in call["shell"]
    assert "cd /work/repo" in call["shell"]
    assert "pytest -q" in call["shell"]


async def test_run_without_files_passes_none() -> None:
    sb, client = make_sandbox()
    await sb.run(str(BASE_UUID), "true")
    assert client.runs[0]["files"] is None


async def test_keep_returns_the_new_image_id_and_runs_non_disposable() -> None:
    new = uuid4()
    sb, client = make_sandbox(FakeDone(uuid=new))
    result = await sb.run(str(BASE_UUID), "make", keep=True)
    assert result.image == str(new)
    assert client.runs[0]["disposable"] is False


async def test_keep_falls_back_to_the_input_image_when_nothing_changed() -> None:
    sb, _ = make_sandbox(FakeDone(uuid=None, exit_code=0))
    result = await sb.run(str(BASE_UUID), "true", keep=True)
    assert result.image == str(BASE_UUID)


async def test_keep_does_not_invent_an_image_for_a_failed_run_or_a_tag_input() -> None:
    sb, _ = make_sandbox(FakeDone(uuid=None, exit_code=1), FakeDone(uuid=None, exit_code=0))
    assert (await sb.run(str(BASE_UUID), "false", keep=True)).image is None
    assert (await sb.run("python:3.12", "true", keep=True)).image is None


async def test_disposable_run_reports_no_image() -> None:
    sb, _ = make_sandbox(FakeDone(uuid=uuid4()))
    assert (await sb.run(str(BASE_UUID), "true")).image is None


async def test_output_is_truncated_to_one_mebibyte_keeping_the_tail() -> None:
    big = b"x" * (MAX_OUTPUT_BYTES + 100) + b"LAST"
    sb, _ = make_sandbox(FakeDone(stdout=big))
    result = await sb.run(str(BASE_UUID), "noisy")
    assert result.stdout.endswith("LAST")
    assert result.stdout.count("x") == MAX_OUTPUT_BYTES - 4
    assert "truncated" in result.stdout.splitlines()[0]


async def test_server_side_truncation_is_flagged() -> None:
    sb, _ = make_sandbox(FakeDone(stdout=b"partial", truncated=True))
    assert "truncated by the sandbox" in (await sb.run(str(BASE_UUID), "x")).stdout


async def test_invalid_utf8_output_does_not_raise() -> None:
    sb, _ = make_sandbox(FakeDone(stdout=b"bad \xff byte"))
    assert "bad" in (await sb.run(str(BASE_UUID), "x")).stdout


async def test_client_side_timeout_maps_to_timed_out_result() -> None:
    sb, _ = make_sandbox(OperationTimedOutError(operation_uuid=uuid4()))
    result = await sb.run(str(BASE_UUID), "sleep 999", timeout_s=5, keep=True)
    assert result.timed_out
    assert result.exit_code == 124
    assert result.image is None
    assert "5s" in result.stderr


async def test_server_side_timeout_flag_maps_to_timed_out_result() -> None:
    sb, _ = make_sandbox(FakeDone(exit_code=137, timed_out=True, uuid=uuid4()))
    result = await sb.run(str(BASE_UUID), "sleep 999", timeout_s=5, keep=True)
    assert result.timed_out
    assert result.exit_code == 137
    assert result.image is None  # a killed run is never offered as a checkpoint


async def test_platform_failures_become_sandbox_error() -> None:
    op = uuid4()
    sb, _ = make_sandbox(
        FailedOperationError(operation_uuid=op, error="no capacity"),
        CancelledOperationError(operation_uuid=op),
        NotFoundError(error="image missing"),
    )
    with pytest.raises(SandboxError, match="no capacity"):
        await sb.run(str(BASE_UUID), "x")
    with pytest.raises(SandboxError, match="cancelled"):
        await sb.run(str(BASE_UUID), "x")
    with pytest.raises(SandboxError, match="404"):
        await sb.run(str(BASE_UUID), "x")


async def test_network_false_runs_under_unshare_with_a_fallback_note() -> None:
    sb, client = make_sandbox(FakeDone(), FakeDone())
    await sb.run(str(BASE_UUID), "curl example.com", network=False)
    await sb.run(str(BASE_UUID), "curl example.com", network=True)
    isolated, plain = client.runs[0]["shell"], client.runs[1]["shell"]
    assert isolated.startswith("bash -c ")
    assert "unshare --net" in isolated
    assert "network isolation is unavailable" in isolated
    assert "curl example.com" in isolated
    assert plain.startswith("bash -lc ") and "unshare" not in plain


async def test_concurrency_is_capped() -> None:
    active = 0
    peak = 0

    class SlowRun(FakeRun):
        async def _execute(self) -> FakeDone:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return FakeDone()

    sb, client = make_sandbox(max_concurrency=2)

    def slow_run(**kwargs: Any) -> FakeRun:
        return SlowRun(FakeDone())

    class SlowImage(FakeImage):
        run = staticmethod(slow_run)  # type: ignore[assignment]

    async def use(ref: str, strict: bool = False) -> FakeImage:
        return SlowImage(client, ref)

    client.images.use = use  # type: ignore[method-assign]
    await asyncio.gather(*(sb.run(str(BASE_UUID), "true") for _ in range(6)))
    assert peak == 2


# --------------------------------------------------------------------------- live


@pytest.mark.live
async def test_live_checkpoint_and_fork() -> None:
    """Needs Sandboxes access: NEBIUS_API_KEY + NEBIUS_PROJECT_ID in the environment or .env.

    Imports python:3.12, writes a file with keep=True, then forks from the checkpoint and
    checks that the fork sees the file while the base image does not. Also checks the
    timeout path and that two forks run concurrently.
    """
    from fixedyet.config import get_settings

    settings = get_settings()
    if not (settings.contree_token or settings.nebius_api_key):
        pytest.skip("no Sandboxes credentials configured")
    sb = NebiusSandbox(settings)
    base = await sb.base_image("3.12")
    tools = await sb.run(base, "git --version && python --version", timeout_s=120)
    assert tools.exit_code == 0, tools.stderr
    made = await sb.run(base, "echo checkpointed > /work/marker.txt", keep=True, timeout_s=120)
    assert made.exit_code == 0 and made.image, made.stderr
    fork_a, fork_b, fresh = await asyncio.gather(
        sb.run(made.image, "cat /work/marker.txt", timeout_s=120),
        sb.run(made.image, "cat /work/marker.txt && echo b > /work/b.txt", timeout_s=120),
        sb.run(base, "test ! -e /work/marker.txt && echo clean", timeout_s=120),
    )
    assert fork_a.stdout.strip() == "checkpointed"
    assert fork_b.stdout.split() == ["checkpointed"]
    assert fresh.stdout.strip() == "clean"
    staged = await sb.run(
        base, "cat /opt/new/dir/f.txt", files={"/opt/new/dir/f.txt": "staged\n"}, timeout_s=120
    )
    assert staged.stdout == "staged\n"
    timed = await sb.run(base, "sleep 60", timeout_s=5)
    assert timed.timed_out
    offline = await sb.run(
        base,
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\"",
        network=False,
        timeout_s=60,
    )
    # Either the connection failed (isolation worked) or the fallback note says it is unavailable.
    assert offline.exit_code != 0 or "isolation is unavailable" in offline.stderr
