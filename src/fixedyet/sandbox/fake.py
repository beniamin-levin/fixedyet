"""An in-memory `Sandbox` for tests: records every call and returns scripted results.

    sb = FakeSandbox([ExecResult(exit_code=0, stdout="ok")])
    await sb.run("img", "echo ok", keep=True)   # -> exit 0, image "fake-ckpt-1"
    sb.calls[0].command                          # "echo ok"

Each scripted item is an `ExecResult`, an exception instance (raised), or a callable taking
the `RunCall` and returning one of those. When the script runs out, ``default`` is returned.
No network and no Docker are involved, so pipeline tests can use it freely.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from fixedyet.models import ExecResult
from fixedyet.sandbox.base import DEFAULT_PYTHON_VERSION


@dataclass
class RunCall:
    """One recorded `Sandbox.run` call."""

    image: str
    command: str
    files: dict[str, str] | None = None
    workdir: str = "/work"
    timeout_s: int = 900
    keep: bool = False
    network: bool = True


Scripted = ExecResult | BaseException | Callable[[RunCall], "ExecResult | BaseException"]


@dataclass
class FakeSandbox:
    """Scripted stand-in implementing the `Sandbox` protocol."""

    script: Iterable[Scripted] = ()
    default: ExecResult = field(default_factory=lambda: ExecResult(exit_code=0))
    name: str = "fake"
    calls: list[RunCall] = field(default_factory=list)
    base_image_requests: list[str | None] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queue: list[Scripted] = list(self.script)
        self._checkpoints = 0

    async def base_image(self, python_version: str | None) -> str:
        self.base_image_requests.append(python_version)
        return f"fake-base:{python_version or DEFAULT_PYTHON_VERSION}"

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
        call = RunCall(image, command, files, workdir, timeout_s, keep, network)
        self.calls.append(call)
        item: Scripted = self._queue.pop(0) if self._queue else self.default
        if callable(item) and not isinstance(item, ExecResult | BaseException):
            item = item(call)
        if isinstance(item, BaseException):
            raise item
        if keep and item.image is None and item.exit_code == 0:
            self._checkpoints += 1
            item = item.model_copy(update={"image": f"fake-ckpt-{self._checkpoints}"})
        return item
