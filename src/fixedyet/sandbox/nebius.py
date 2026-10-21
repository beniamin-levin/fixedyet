"""Sandbox backend on Nebius Token Factory Sandboxes, through contree-sdk 0.3.6.

Research notes (SDK source read 2026-10-21, docs.tokenfactory.nebius.com/sandboxes/*)
=====================================================================================

Access and auth
  * Sandboxes is in Beta; access must be requested and the project activated by Nebius
    (contree@nebius.com). A plain inference key alone may not be enough.
  * The SDK reads ``NEBIUS_API_KEY`` (bearer token) AND ``NEBIUS_PROJECT_ID`` (sent as the
    ``Project`` header) from the environment: ``IAMAuth`` in ``contree_sdk/auth.py``.
  * Default base URL: ``https://api.tokenfactory.nebius.com/sandboxes/`` (this is NOT the
    inference URL ``.../v1/``). Legacy endpoints (for example ``https://eu-north.nebius.computer``)
    use ``JWTAuth`` (``CONTREE_TOKEN`` / ``CONTREE_BASE_URL``) and no project id.
  * ``~/.config/contree/auth.ini`` profiles are also read when nothing else is set.

Client
  * ``from contree_sdk import Contree`` is the async client; ``Contree(token=..., base_url=...)``
    or ``Contree(config=ContreeConfig(auth=IAMAuth(...), ...))``.
  * Images: ``await client.images.oci("python:3.12")`` resolves by tag, importing from Docker
    Hub when it is not stored yet (tag ``python:3.12``). ``await client.images.use(ref)`` is
    lazy (no API call) and accepts an image UUID or a tag. Preloaded catalogue (7,000+
    SWE-bench Verified / SWE-rebench images) is reachable the same way, by tag or UUID.
  * Run: ``image.run(shell="...", files={"/abs/path": b"bytes"}, timeout=secs, cwd=..., env=...,
    disposable=True, truncate_output_at=N, stdout=bytes, stderr=bytes)`` returns a new image-like
    object; ``await`` it to execute. Afterwards ``.exit_code``, ``.stdout``, ``.stderr``,
    ``.result`` (``ContreeResult``: ``exit_code``, ``stdout``, ``stderr``, ``elapsed_time``,
    ``truncated``) and ``.uuid`` are available.
  * ``files`` sources must be ``bytes`` (a ``str`` is treated as a local file path).

Checkpoint and fork
  * ``disposable=False`` saves the resulting filesystem as an immutable image; its UUID is
    ``done.uuid`` (it can be ``None``: if nothing changed the input UUID may come back, a
    failed run may produce no image). ``disposable=True`` (the SDK default) discards changes.
  * Running again from a UUID is a fork. Concurrent forks of one image are independent.
  * Images are immutable; tags are mutable names. Saved images are kept 180 days.

Results, timeouts, output
  * An operation can be ``SUCCESS`` with a non-zero process exit code. ``FAILED`` /
    ``CANCELLED`` operations (platform, capacity, request problems) raise
    ``FailedOperationError`` / ``CancelledOperationError`` from ``await``.
  * ``timeout=`` (seconds) is sent to the server AND used by the SDK's own polling loop, so a
    timeout normally surfaces as ``OperationTimedOutError``; the server may instead report
    ``result.state.timed_out``. Both are mapped to ``ExecResult.timed_out``. The SDK then
    cancels the operation best-effort. The account maximum is in ``whoami().limits``
    (``instance_max_timeout``); the docs state no number.
  * Output: default 1 MiB per stream, maximum 10 MiB (``truncate_output_at``). Streams come
    back base64 or ascii encoded; we ask for ``bytes`` and decode leniently ourselves.
  * Concurrency: 50 simultaneous operations during Beta; we cap at ``max_concurrency``.

Network
  * Enabled by default. The REST field ``networking.enabled=false`` removes the guest NIC, but
    contree-sdk 0.3.6 has no parameter for it. ``run(network=False)`` therefore runs the
    command under ``unshare --net`` (see ``base.isolate_network``); when the microVM does not
    allow that, the command runs WITH network after a note on stderr. Switch to the native flag
    when a newer SDK exposes it.

Files
  * ``files`` are uploaded as bytes to flat ``/tmp/.ir_upload_<n>`` paths and a shell prelude
    copies them to their destinations (creating parent directories), so we do not depend on
    how the service creates parents.

What the user must provide: ``NEBIUS_API_KEY`` and ``NEBIUS_PROJECT_ID`` for a project with
Sandboxes activated; optionally ``CONTREE_TOKEN`` / ``CONTREE_BASE_URL``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from typing import Any
from uuid import UUID

from contree_sdk import Contree
from contree_sdk.auth import IAMAuth, JWTAuth
from contree_sdk.config import ContreeConfig
from contree_sdk.sdk.exceptions import (
    CancelledOperationError,
    ContreeError,
    FailedOperationError,
    OperationTimedOutError,
)
from contree_sdk.sdk.exceptions.api import ApiStatusCodeError

from fixedyet.models import ExecResult
from fixedyet.sandbox.base import (
    MAX_OUTPUT_BYTES,
    SandboxError,
    build_script,
    isolate_network,
    normalize_python_version,
    stage_files,
    truncate_output,
)

logger = logging.getLogger(__name__)

SANDBOXES_URL = "https://api.tokenfactory.nebius.com/sandboxes/"
TOKEN_FACTORY_HOST = "tokenfactory.nebius.com"
TIMEOUT_EXIT_CODE = 124
"""Exit code reported for a timed-out run (the same value coreutils `timeout` uses)."""


def _reveal(value: object) -> str | None:
    """Return a plain string from a str, a pydantic SecretStr, or None/empty."""
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    text = getter() if callable(getter) else str(value)
    return text.strip() or None


def build_auth(settings: object | None) -> IAMAuth | JWTAuth | None:
    """Choose the SDK auth object from settings (duck-typed), or None to use SDK defaults.

    ``config.Settings`` copies ``NEBIUS_API_KEY`` into ``contree_token`` and defaults
    ``contree_base_url`` to the Token Factory sandboxes URL, so both are usually set. Rules,
    first match wins:
      1. no token in settings: None, so the SDK reads its own env vars and auth.ini;
      2. a project id is known (``settings.nebius_project_id`` or env ``NEBIUS_PROJECT_ID``):
         ``IAMAuth`` (bearer token plus ``Project`` header) against ``contree_base_url``;
      3. a base URL that is not Token Factory (a legacy endpoint): ``JWTAuth``;
      4. otherwise `SandboxError`: Token Factory Sandboxes need the project id.
    """
    token = _reveal(getattr(settings, "contree_token", None)) or _reveal(
        getattr(settings, "nebius_api_key", None)
    )
    if token is None:
        return None
    base_url = _reveal(getattr(settings, "contree_base_url", None))
    project = _reveal(getattr(settings, "nebius_project_id", None)) or _reveal(
        os.environ.get("NEBIUS_PROJECT_ID")
    )
    if project:
        return IAMAuth(token=token, project_id=project, base_url=base_url or SANDBOXES_URL)
    if base_url and TOKEN_FACTORY_HOST not in base_url:
        return JWTAuth(token=token, base_url=base_url)
    raise SandboxError(
        "Token Factory Sandboxes need a project id: set NEBIUS_PROJECT_ID "
        "(for a legacy endpoint set CONTREE_BASE_URL to it instead)"
    )


def build_client(settings: object | None, *, import_timeout_s: int = 600) -> Contree:
    """Create the async `Contree` client from settings."""
    auth = build_auth(settings)
    if auth is None:
        return Contree()
    config = ContreeConfig(
        auth=auth,
        transport_timeout=60.0,
        operation_import_timeout=float(import_timeout_s),
    )
    return Contree(config=config)


def _process_timed_out(result: Any) -> bool:
    """True when the server flagged the process as timed out (``state.timed_out``)."""
    raw = getattr(result, "_raw", None)
    inner = getattr(raw, "result", None)
    state = getattr(inner, "state", None)
    return bool(getattr(state, "timed_out", False))


class NebiusSandbox:
    """`Sandbox` implementation on contree-sdk's async client."""

    name = "nebius"

    def __init__(
        self,
        settings: object | None = None,
        *,
        client: Contree | None = None,
        max_concurrency: int = 16,
        import_timeout_s: int = 600,
    ) -> None:
        self._settings = settings
        self._client = client
        self._import_timeout_s = import_timeout_s
        self._slots = asyncio.Semaphore(max_concurrency)
        self._base_images: dict[str, str] = {}
        self._base_locks: dict[str, asyncio.Lock] = {}

    @property
    def client(self) -> Contree:
        if self._client is None:
            self._client = build_client(self._settings, import_timeout_s=self._import_timeout_s)
        return self._client

    async def base_image(self, python_version: str | None) -> str:
        """Resolve (importing from Docker Hub on first use) the full ``python:<ver>`` image.

        The full Debian image is used because it ships git. The UUID is cached per version.
        """
        version = normalize_python_version(python_version)
        cached = self._base_images.get(version)
        if cached is not None:
            return cached
        lock = self._base_locks.setdefault(version, asyncio.Lock())
        async with lock:
            cached = self._base_images.get(version)
            if cached is not None:
                return cached
            try:
                image = await self.client.images.oci(
                    f"python:{version}", timeout=self._import_timeout_s
                )
            except ContreeError as exc:
                raise SandboxError(f"could not import python:{version}: {exc}") from exc
            if image.uuid is None:
                raise SandboxError(f"python:{version} was imported without an image id")
            self._base_images[version] = str(image.uuid)
            return self._base_images[version]

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
        staged, prelude = stage_files(files)
        script = build_script(command, workdir, prelude=prelude)
        if network:
            shell = f"bash -lc {shlex.quote(script)}"
        else:
            shell = f"bash -c {shlex.quote(isolate_network(script))}"
        uploads = {path: content.encode("utf-8") for path, content in staged.items()}
        started = time.monotonic()
        async with self._slots:
            try:
                source = await self.client.images.use(image)
                pending = source.run(
                    shell=shell,
                    files=uploads or None,
                    timeout=timeout_s,
                    disposable=not keep,
                    truncate_output_at=MAX_OUTPUT_BYTES,
                    stdout=bytes,
                    stderr=bytes,
                )
                done = await pending
            except OperationTimedOutError:
                return ExecResult(
                    exit_code=TIMEOUT_EXIT_CODE,
                    stderr=f"sandbox run exceeded its {timeout_s}s timeout and was cancelled",
                    duration_s=time.monotonic() - started,
                    timed_out=True,
                )
            except CancelledOperationError as exc:
                raise SandboxError(f"sandbox operation was cancelled: {exc}") from exc
            except FailedOperationError as exc:
                raise SandboxError(f"sandbox operation failed: {exc.error}") from exc
            except ApiStatusCodeError as exc:
                raise SandboxError(
                    f"sandbox API error {exc.status} for image {image!r}: {exc.error}"
                ) from exc
            except ContreeError as exc:
                raise SandboxError(f"sandbox error: {exc}") from exc
        return self._to_exec_result(
            done, started=started, keep=keep, source_image=image, timeout_s=timeout_s
        )

    @staticmethod
    def _to_exec_result(
        done: Any, *, started: float, keep: bool, source_image: str, timeout_s: int
    ) -> ExecResult:
        """Map a finished SDK image-like object to an `ExecResult`."""
        result = done.result
        timed_out = _process_timed_out(result)
        stdout = truncate_output(result.stdout)
        stderr = truncate_output(result.stderr)
        if getattr(result, "truncated", False):
            stdout = f"{stdout}\n[... output truncated by the sandbox ...]"
        if timed_out:
            stderr = f"{stderr}\nprocess exceeded its {timeout_s}s timeout".lstrip("\n")
        image_id: str | None = None
        if keep and not timed_out:
            if done.uuid is not None:
                image_id = str(done.uuid)
            elif result.exit_code == 0 and _is_uuid(source_image):
                image_id = source_image  # nothing changed, so the input is the checkpoint
        return ExecResult(
            exit_code=int(result.exit_code),
            stdout=stdout,
            stderr=stderr,
            duration_s=time.monotonic() - started,
            image=image_id,
            timed_out=timed_out,
        )


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
