"""Baseline build and a two-sided run on the real Nebius sandbox. Costs a few cents.

Run by hand once Sandboxes access works: ``pytest -m live tests/test_envbuild_live.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from fixedyet.envbuild import build_baseline, run_test_on_side
from fixedyet.models import Outcome
from fixedyet.sandbox.nebius import NebiusSandbox

pytestmark = pytest.mark.live

REPO = "pypa/sampleproject"
SHA = "621e4974ca25ce531773def586ba3ed8e736b3fc"


async def test_live_baseline_then_concurrent_forks() -> None:
    from fixedyet.config import get_settings

    settings = get_settings()
    if not (settings.contree_token or settings.nebius_api_key):
        pytest.skip("no Sandboxes credentials configured")
    sb = NebiusSandbox(settings)
    image, result = await build_baseline(sb, repo=REPO, ref=SHA, python_version="3.12")
    assert result.exit_code == 0 and image
    passing, failing = await asyncio.gather(
        run_test_on_side(
            sb,
            image,
            test_path="tests/test_ir_probe.py",
            test_code="def test_a():\n    assert True\n",
        ),
        run_test_on_side(
            sb,
            image,
            test_path="tests/test_ir_probe.py",
            test_code="def test_b():\n    assert False\n",
        ),
    )
    assert passing.outcomes == [Outcome.PASS, Outcome.PASS]
    assert failing.outcomes == [Outcome.FAIL, Outcome.FAIL]
