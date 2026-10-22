"""Runtime settings, loaded from environment variables and the project-root `.env`.

Secrets live only here. `Settings.__repr__` masks them so a stray `print(settings)` or a
log line cannot leak a key.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# src/fixedyet/config.py -> project root is two levels above the package directory.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Verified against https://docs.tokenfactory.nebius.com (quickstart) and the contree-sdk source.
DEFAULT_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
DEFAULT_CONTREE_BASE_URL = "https://api.tokenfactory.nebius.com/sandboxes/"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_SMALL_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
# Token Factory has no spending limit and charges the card when the balance goes negative, so the
# cap on lifetime model spend lives in our code (see ledger.py).
DEFAULT_TOTAL_BUDGET_USD = 20.0
# Fix mode: the model cost of one fix, every attempt included. A fix attempt is a few calls of
# about 20,000 input tokens each, roughly $0.01 to $0.03 apiece at Nemotron 3 Super prices.
DEFAULT_FIX_BUDGET_USD = 0.25
# Local Docker checkpoints (1.5 GB each) kept after a run; see sandbox/docker.py.
DEFAULT_KEEP_CHECKPOINTS = 6

_ROOT_PATH_RE = re.compile(r"^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")

_SECRET_FIELDS = frozenset(
    {"nebius_api_key", "contree_token", "tavily_api_key", "github_token", "admin_token",
     "judge_codes"}
)


def normalize_root_path(value: str | None) -> str:
    """Clean a URL prefix such as ``fixedyet/`` into ``/fixedyet``; ``""`` and ``/`` mean none.

    The prefix ends up in redirect locations, so only plain path segments are accepted.
    """
    text = (value or "").strip().strip("/")
    if not text:
        return ""
    path = "/" + text
    if not _ROOT_PATH_RE.match(path):
        raise ValueError(f"invalid root path {value!r}: use plain path segments such as /fixedyet")
    return path


class Settings(BaseModel):
    nebius_api_key: str = ""
    nebius_base_url: str = DEFAULT_NEBIUS_BASE_URL
    model: str = DEFAULT_MODEL
    small_model: str = DEFAULT_SMALL_MODEL
    contree_token: str = ""
    contree_base_url: str = DEFAULT_CONTREE_BASE_URL
    tavily_api_key: str = ""
    github_token: str = ""
    # Docker by default: it ran every published result, and Nebius Sandboxes is a beta that
    # needs access granted per account (FIXEDYET_SANDBOX=nebius, with the [nebius] extra).
    sandbox: Literal["nebius", "docker"] = "docker"
    run_budget_usd: float = 0.60
    fix_budget_usd: float = DEFAULT_FIX_BUDGET_USD  # model cost cap of one fix (all attempts)
    total_budget_usd: float = DEFAULT_TOTAL_BUDGET_USD
    max_turns: int = 20
    demo_daily_limit: int = 5
    runs_dir: Path = Path("runs")
    # The lifetime spend ledger. None: a user-level state file (see ledger.ledger_path), so a
    # different runs directory never starts a fresh total.
    ledger_path: Path | None = None
    keep_checkpoints: int = Field(default=DEFAULT_KEEP_CHECKPOINTS, ge=0)
    root_path: str = ""  # URL prefix the demo is served under, e.g. "/fixedyet"
    admin_token: str = ""  # turns on maintainer mode in the web demo; empty means off
    # Web demo access for hackathon judges: comma-separated codes, each 12+ characters. A run
    # started with one counts against its own daily quota, not the public one.
    judge_codes: str = ""
    judge_daily_limit: int = Field(default=20, ge=0)
    # Model budget kept for judges: public runs stop when less than this (plus one run) is left
    # under total_budget_usd. Judges, and the maintainer, may use it.
    judge_reserve_usd: float = Field(default=0.0, ge=0)

    @field_validator("root_path")
    @classmethod
    def _clean_root_path(cls, value: str) -> str:
        return normalize_root_path(value)

    # -- construction ---------------------------------------------------------------------

    @classmethod
    def load(
        cls, env: Mapping[str, str] | None = None, *, dotenv_path: Path | None = None
    ) -> Settings:
        """Build settings from `.env` (never overriding real environment variables) and os.environ.

        Passing `env` skips both and reads only that mapping; tests use this to stay hermetic.
        """
        if env is None:
            load_dotenv(dotenv_path or PROJECT_ROOT / ".env", override=False)
            env = os.environ

        def get(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None  # `KEY=` in a .env file means "not set"

        raw: dict[str, Any] = {
            "nebius_api_key": get("NEBIUS_API_KEY"),
            "nebius_base_url": get("NEBIUS_BASE_URL"),
            "model": get("FIXEDYET_MODEL"),
            "small_model": get("FIXEDYET_SMALL_MODEL"),
            # .env.example: CONTREE_TOKEN is only needed "if different from NEBIUS_API_KEY".
            "contree_token": get("CONTREE_TOKEN") or get("NEBIUS_API_KEY"),
            "contree_base_url": get("CONTREE_BASE_URL"),
            "tavily_api_key": get("TAVILY_API_KEY"),
            "github_token": get("GITHUB_TOKEN"),
            "sandbox": get("FIXEDYET_SANDBOX"),
            "run_budget_usd": get("FIXEDYET_RUN_BUDGET_USD"),
            "fix_budget_usd": get("FIXEDYET_FIX_BUDGET_USD"),
            "total_budget_usd": get("FIXEDYET_TOTAL_BUDGET_USD"),
            "max_turns": get("FIXEDYET_MAX_TURNS"),
            "demo_daily_limit": get("FIXEDYET_DEMO_DAILY_LIMIT"),
            "runs_dir": get("FIXEDYET_RUNS_DIR"),
            "ledger_path": get("FIXEDYET_LEDGER_PATH"),
            "keep_checkpoints": get("FIXEDYET_KEEP_CHECKPOINTS"),
            "root_path": get("FIXEDYET_ROOT_PATH"),
            "admin_token": get("FIXEDYET_ADMIN_TOKEN"),
            "judge_codes": get("FIXEDYET_JUDGE_CODES"),
            "judge_daily_limit": get("FIXEDYET_JUDGE_DAILY_LIMIT"),
            "judge_reserve_usd": get("FIXEDYET_JUDGE_RESERVE_USD"),
        }
        # Unset values fall back to the field defaults rather than being passed as None.
        return cls(**{key: value for key, value in raw.items() if value is not None})

    # -- safe display ---------------------------------------------------------------------

    def __repr_args__(self) -> Any:
        for name, value in super().__repr_args__():
            if name in _SECRET_FIELDS:
                yield name, "<set>" if value else "<unset>"
            else:
                yield name, value

    def has(self, secret: str) -> bool:
        """True if the named secret field (e.g. 'tavily_api_key') is non-empty."""
        return bool(getattr(self, secret))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests call `get_settings.cache_clear()` after changing the env."""
    return Settings.load()
