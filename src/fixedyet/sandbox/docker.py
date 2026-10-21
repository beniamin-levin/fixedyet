"""Sandbox backend on a local Docker daemon, driven through the `docker` CLI.

No Docker SDK dependency: every call is an asyncio subprocess. One `run()` is

    docker create  ->  docker cp - (staged files, as a tar on stdin)  ->  docker start -a
    ->  docker inspect (exit code)  ->  docker commit (only with keep=True)  ->  docker rm -f

The container is always removed. A checkpoint is a local image tag
(``fixedyet-ckpt:<random hex>``); running from it is a fork because every run creates a new
container from the image. Use this backend for CI and for development without Nebius access.

Checkpoints are about 1.5 GB each, so they are pruned: `DockerSandbox.prune_checkpoints` keeps the
newest N images of the ``fixedyet-ckpt`` repository and removes the rest. It never touches an
image of any other repository. The pipeline calls it after each run and ``fixedyet gc`` calls it
on demand.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fixedyet.models import ExecResult
from fixedyet.sandbox.base import (
    SandboxError,
    build_script,
    normalize_python_version,
    stage_files,
    truncate_output,
)

logger = logging.getLogger(__name__)

TIMEOUT_EXIT_CODE = 124
CHECKPOINT_REPO = "fixedyet-ckpt"

_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


@dataclass
class CliResult:
    """Outcome of one `docker ...` invocation."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Checkpoint:
    """One local checkpoint image: its ``fixedyet-ckpt:<hex>`` tag and when it was committed."""

    tag: str
    created: datetime


@dataclass
class PruneResult:
    """What `DockerSandbox.prune_checkpoints` did (or, with ``dry_run``, would do)."""

    kept: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    in_use: list[str] = field(default_factory=list)  # old enough to go, but a container uses it
    failed: list[str] = field(default_factory=list)  # docker refused to remove it


def _parse_created(text: str) -> datetime:
    """Parse docker's RFC 3339 timestamp (nanoseconds, ``Z`` or an offset)."""
    try:
        parsed = datetime.fromisoformat(_FRACTION_RE.sub(r"\1", text.strip()))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_tar(files: dict[str, str]) -> bytes:
    """Pack ``{absolute path: text}`` into a tar stream rooted at ``/`` for `docker cp -`."""
    buffer = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path, content in files.items():
            if not path.startswith("/"):
                raise SandboxError(f"file destination must be an absolute path, got {path!r}")
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=path.lstrip("/"))
            info.size = len(data)
            info.mode = 0o644
            info.mtime = now
            info.uid = info.gid = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class DockerSandbox:
    """`Sandbox` implementation on the local Docker daemon."""

    name = "docker"

    def __init__(self, *, docker_bin: str = "docker", max_concurrency: int = 8) -> None:
        self._bin = docker_bin
        self._slots = asyncio.Semaphore(max_concurrency)
        self._base_images: dict[str, str] = {}

    async def _docker(
        self, *args: str, stdin: bytes | None = None, limit_s: float | None = None
    ) -> CliResult:
        """Run one docker CLI command; kill it if ``limit_s`` seconds pass."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin,
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SandboxError(f"docker CLI not found ({self._bin!r}); install Docker") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=limit_s)
        except TimeoutError:
            await self._reap(proc)
            return CliResult(returncode=-9, timed_out=True)
        except asyncio.CancelledError:
            await self._reap(proc)
            raise
        return CliResult(returncode=proc.returncode or 0, stdout=out, stderr=err)

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    async def base_image(self, python_version: str | None) -> str:
        """Return ``python:<version>`` (full image, has git), pulling it if it is missing."""
        version = normalize_python_version(python_version)
        if version in self._base_images:
            return self._base_images[version]
        ref = f"python:{version}"
        inspect = await self._docker("image", "inspect", "--format", "{{.Id}}", ref)
        if inspect.returncode != 0:
            pull = await self._docker("pull", ref, limit_s=1200)
            if pull.returncode != 0:
                raise SandboxError(f"docker pull {ref} failed: {pull.stderr_text.strip()[-500:]}")
        self._base_images[version] = ref
        return ref

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
        if not image or image.startswith("-"):
            raise SandboxError(f"invalid image reference {image!r}")
        staged, prelude = stage_files(files)
        script = build_script(command, workdir, prelude=prelude)
        started = time.monotonic()
        container: str | None = None
        async with self._slots:
            try:
                container = await self._create(image, script, network)
                if staged:
                    await self._copy_in(container, staged)
                start = await self._docker("start", "-a", container, limit_s=timeout_s)
                if start.timed_out:
                    await self._docker("kill", container, limit_s=30)
                    return ExecResult(
                        exit_code=TIMEOUT_EXIT_CODE,
                        stdout=truncate_output(start.stdout),
                        stderr=f"container exceeded its {timeout_s}s timeout and was killed",
                        duration_s=time.monotonic() - started,
                        timed_out=True,
                    )
                exit_code = await self._exit_code(container, start)
                checkpoint = await self._commit(container) if keep else None
                return ExecResult(
                    exit_code=exit_code,
                    stdout=truncate_output(start.stdout),
                    stderr=truncate_output(start.stderr),
                    duration_s=time.monotonic() - started,
                    image=checkpoint,
                )
            finally:
                if container is not None:
                    await self._remove(container)

    async def _create(self, image: str, script: str, network: bool) -> str:
        args = ["create"]
        if not network:
            args += ["--network", "none"]
        args += [image, "bash", "-lc", script]
        created = await self._docker(*args)
        if created.returncode != 0:
            raise SandboxError(f"docker create failed: {created.stderr_text.strip()[-500:]}")
        lines = [line for line in created.stdout_text.splitlines() if line.strip()]
        if not lines:
            raise SandboxError("docker create returned no container id")
        return lines[-1].strip()

    async def _copy_in(self, container: str, files: dict[str, str]) -> None:
        copied = await self._docker("cp", "-", f"{container}:/", stdin=build_tar(files))
        if copied.returncode != 0:
            raise SandboxError(f"docker cp failed: {copied.stderr_text.strip()[-500:]}")

    async def _exit_code(self, container: str, start: CliResult) -> int:
        info = await self._docker(
            "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", container
        )
        parts = info.stdout_text.split()
        if info.returncode != 0 or len(parts) != 2:
            raise SandboxError(f"docker inspect failed: {info.stderr_text.strip()[-500:]}")
        status, code = parts
        if status != "exited":
            detail = start.stderr_text.strip()[-500:]
            raise SandboxError(f"container did not run to completion (state {status}): {detail}")
        return int(code)

    async def _commit(self, container: str) -> str:
        tag = f"{CHECKPOINT_REPO}:{uuid.uuid4().hex[:16]}"
        committed = await self._docker(
            "commit", "--change", 'CMD ["bash"]', container, tag, limit_s=600
        )
        if committed.returncode != 0:
            raise SandboxError(f"docker commit failed: {committed.stderr_text.strip()[-500:]}")
        return tag

    # -- checkpoint pruning -----------------------------------------------------------------

    async def list_checkpoints(self) -> list[Checkpoint]:
        """Every local image tagged ``fixedyet-ckpt:<tag>``, newest first.

        Only the ``fixedyet-ckpt`` repository is ever listed, so no other image can be pruned.
        """
        listing = await self._docker(
            "image", "ls", "--format", "{{.Repository}}:{{.Tag}}", CHECKPOINT_REPO, limit_s=60
        )
        if listing.returncode != 0:
            raise SandboxError(f"docker image ls failed: {listing.stderr_text.strip()[-500:]}")
        tags: list[str] = []
        for line in listing.stdout_text.splitlines():
            repo, _, tag = line.strip().rpartition(":")
            if repo == CHECKPOINT_REPO and tag and tag != "<none>":
                tags.append(f"{repo}:{tag}")
        if not tags:
            return []
        # One call for all tags. If a tag vanished meanwhile docker still prints the others.
        inspected = await self._docker(
            "image", "inspect", "--format", "{{.Created}}|{{json .RepoTags}}", *tags, limit_s=120
        )
        wanted = set(tags)
        found: dict[str, datetime] = {}
        for line in inspected.stdout_text.splitlines():
            created, _, raw_tags = line.partition("|")
            try:
                image_tags = json.loads(raw_tags)
            except ValueError:
                continue
            for tag in image_tags if isinstance(image_tags, list) else []:
                if tag in wanted:
                    found[tag] = _parse_created(created)
        checkpoints = [Checkpoint(tag, created) for tag, created in found.items()]
        checkpoints.sort(key=lambda c: (c.created, c.tag), reverse=True)
        return checkpoints

    async def _images_in_use(self) -> set[str]:
        """Image references of every container, running or not (a created one is about to run)."""
        listing = await self._docker("ps", "-a", "--format", "{{.Image}}", limit_s=60)
        if listing.returncode != 0:
            raise SandboxError(f"docker ps failed: {listing.stderr_text.strip()[-500:]}")
        return {line.strip() for line in listing.stdout_text.splitlines() if line.strip()}

    async def prune_checkpoints(self, keep: int, *, dry_run: bool = False) -> PruneResult:
        """Keep the newest ``keep`` checkpoint images and remove the older ones.

        An image that a container still uses is skipped, and an image docker refuses to remove
        is reported in ``failed``; neither stops the others from being removed. ``dry_run``
        removes nothing and lists in ``removed`` what would go.
        """
        if keep < 0:
            raise ValueError("keep must be 0 or more")
        checkpoints = await self.list_checkpoints()
        result = PruneResult(kept=[c.tag for c in checkpoints[:keep]])
        stale = [c.tag for c in checkpoints[keep:]]
        if not stale:
            return result
        busy = await self._images_in_use()
        for tag in stale:
            if tag in busy:
                result.in_use.append(tag)
            elif dry_run:
                result.removed.append(tag)
            elif (await self._docker("rmi", tag, limit_s=300)).returncode == 0:
                result.removed.append(tag)
            else:
                logger.warning("could not remove checkpoint image %s", tag)
                result.failed.append(tag)
        return result

    async def _remove(self, container: str) -> None:
        """Remove the container even if the caller is being cancelled."""
        with contextlib.suppress(Exception):
            await asyncio.shield(self._docker("rm", "-f", "-v", container, limit_s=60))
