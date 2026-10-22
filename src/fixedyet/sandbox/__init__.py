"""Sandbox backends: local Docker (default) and Nebius Token Factory Sandboxes."""

from __future__ import annotations

from fixedyet.sandbox.base import Sandbox, SandboxError, SandboxSettings

__all__ = ["Sandbox", "SandboxError", "get_sandbox"]


def get_sandbox(settings: SandboxSettings) -> Sandbox:
    """Return the backend chosen by ``settings.sandbox``: ``"nebius"`` or ``"docker"``.

    Backends are imported lazily so choosing one never loads the other's dependencies.
    """
    kind = str(settings.sandbox).strip().lower()
    if kind == "nebius":
        try:
            from fixedyet.sandbox.nebius import NebiusSandbox
        except ModuleNotFoundError as exc:  # contree-sdk is the optional [nebius] extra
            raise SandboxError(
                f"the nebius backend needs contree-sdk ({exc}): pip install 'fixedyet[nebius]', "
                "or set FIXEDYET_SANDBOX=docker"
            ) from exc

        return NebiusSandbox(settings)
    if kind == "docker":
        from fixedyet.sandbox.docker import DockerSandbox

        return DockerSandbox()
    raise SandboxError(f"unknown sandbox backend {settings.sandbox!r}; use 'nebius' or 'docker'")
