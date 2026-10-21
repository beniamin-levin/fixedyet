"""The `Sandbox` protocol, its error type, and helpers shared by every backend.

A sandbox runs one shell command inside a fresh microVM or container that starts from an
image. With ``keep=True`` the resulting filesystem is saved as a new image (the checkpoint)
and its id comes back in ``ExecResult.image``. Because every run starts from the image it is
given, running from a checkpoint is a fork: runs never affect the image they started from.
"""

from __future__ import annotations

import re
import shlex
from typing import Protocol, runtime_checkable

from fixedyet.models import ExecResult

MAX_OUTPUT_BYTES = 1024 * 1024
"""Captured stdout and stderr are each truncated to this many bytes (1 MiB)."""

DEFAULT_PYTHON_VERSION = "3.12"

_PYTHON_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")


class SandboxError(RuntimeError):
    """The sandbox backend itself failed (auth, capacity, missing image, CLI error).

    A command that exits non-zero or times out is NOT an error: it comes back as an
    `ExecResult`. This exception means the command could not be run at all.
    """


@runtime_checkable
class Sandbox(Protocol):
    """Contract between the pipeline and a sandbox backend (see docs/architecture.md)."""

    name: str

    async def base_image(self, python_version: str | None) -> str:
        """Return an image id that has git and the requested Python (default 3.12)."""
        ...

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
        """Run ``command`` (executed by ``bash -lc``) in a fresh sandbox started from ``image``.

        ``files`` maps absolute destination paths to text content and is written before the
        command starts. ``keep=True`` saves the resulting filesystem as a new image and
        returns its id in ``ExecResult.image``.
        """
        ...


class SandboxSettings(Protocol):
    """The one attribute `get_sandbox` needs; `config.Settings` satisfies it."""

    @property
    def sandbox(self) -> str: ...


def normalize_python_version(python_version: str | None) -> str:
    """Return a clean ``X``, ``X.Y`` or ``X.Y.Z`` string, defaulting to 3.12.

    Raises `SandboxError` for anything else, so the value is safe to put in an image name.
    """
    if python_version is None or not python_version.strip():
        return DEFAULT_PYTHON_VERSION
    cleaned = python_version.strip().removeprefix("python").removeprefix("Python").strip()
    if not _PYTHON_VERSION_RE.match(cleaned):
        raise SandboxError(f"unsupported Python version {python_version!r}")
    return cleaned


def decode_output(data: bytes | str | None) -> str:
    """Decode captured output leniently; invalid UTF-8 never raises."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def truncate_output(data: bytes | str | None, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Decode ``data`` and keep at most the last ``limit`` bytes.

    The tail is kept because test summaries and exit markers come last. A short note at
    the start says how many bytes were dropped.
    """
    if data is None:
        return ""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) <= limit:
        return decode_output(raw)
    dropped = len(raw) - limit
    return f"[... {dropped} bytes of earlier output truncated ...]\n" + decode_output(raw[-limit:])


STAGE_PREFIX = "/tmp/.ir_upload_"  # noqa: S108 - a path inside the sandbox, not on the host


def stage_files(files: dict[str, str] | None) -> tuple[dict[str, str], str]:
    """Plan how ``files`` reach the sandbox, the same way for every backend.

    Backends upload each file to a flat staging path under ``/tmp`` (a directory that always
    exists), and a shell prelude then creates the destination's parent directories, copies the
    file into place and removes the staged copy. This makes "write /work/repo/tests/t.py" work
    even when ``tests/`` does not exist yet, without relying on how a backend creates parents.

    Returns ``(uploads, prelude)``: ``uploads`` maps staging paths to content, ``prelude`` is
    shell text to run before anything else (empty without files).
    """
    if not files:
        return {}, ""
    uploads: dict[str, str] = {}
    lines: list[str] = []
    for index, (dest, content) in enumerate(files.items()):
        if not dest.startswith("/"):
            raise SandboxError(f"file destination must be an absolute path, got {dest!r}")
        staged = f"{STAGE_PREFIX}{index}"
        uploads[staged] = content
        target = shlex.quote(dest)
        failure = shlex.quote(f"cannot write {dest}")
        lines.append(
            f'mkdir -p "$(dirname {target})" && cp {staged} {target} && rm -f {staged} '
            f"|| {{ echo {failure} >&2; exit 98; }}"
        )
    return uploads, "\n".join(lines) + "\n"


def build_script(command: str, workdir: str, *, prelude: str = "") -> str:
    """Wrap ``command``: run ``prelude`` (staged files), enter ``workdir`` (created if needed)."""
    if not workdir.startswith("/"):
        raise SandboxError(f"workdir must be an absolute path, got {workdir!r}")
    quoted = shlex.quote(workdir)
    return (
        f"{prelude}"
        f"mkdir -p {quoted} && cd {quoted} || {{ echo 'cannot enter workdir' >&2; exit 97; }}\n"
        f"{command}\n"
    )


def isolate_network(script: str) -> str:
    """Wrap ``script`` so it runs in a network namespace with no connectivity (best effort).

    Used by backends that cannot switch the network off natively (contree-sdk 0.3.6 has no
    parameter for it). If ``unshare --net`` is not permitted, the script still runs, with network
    access, after a note on stderr: a missing network is a convenience for reproducibility, and
    failing every run would be worse than saying so.
    """
    inner = shlex.quote(script)
    return (
        "if unshare --net true >/dev/null 2>&1; then\n"
        '  exec unshare --net sh -c \'ip link set lo up >/dev/null 2>&1; exec "$0" "$@"\' '
        f"bash -lc {inner}\n"
        "fi\n"
        "echo '[sandbox] network isolation is unavailable here; running with network access' >&2\n"
        f"exec bash -lc {inner}\n"
    )
