"""envbuild against a real Docker daemon, on a small real repository (pypa/sampleproject).

Run with ``pytest -m docker``; CI does (.github/workflows/sandbox-docker.yml). The baseline is
built once per module and every test forks it, which is exactly how the pipeline uses it.
"""

from __future__ import annotations

import shutil

import pytest
import pytest_asyncio

from fixedyet.envbuild import build_baseline, installed_extras, run_test_on_side
from fixedyet.models import ExecResult, Outcome, ProgressEvent
from fixedyet.sandbox.docker import DockerSandbox

pytestmark = [
    pytest.mark.docker,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]

REPO = "pypa/sampleproject"
HEAD_SHA = "621e4974ca25ce531773def586ba3ed8e736b3fc"  # "pyproject: prep 4.0.0"
OLD_SHA = "a1f00056ebc65d17ae69c5aba324ef4f0382138a"  # "Switch to nox", June 2024

PASSING = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING = "def test_bad():\n    assert 1 + 1 == 3\n"
BROKEN = "def test_oops(:\n"  # syntax error: pytest exits 2 at collection
IMPORTS_PACKAGE = "def test_import():\n    import sample\n    assert sample\n"
HEAD_CHECK = """import subprocess


def test_head():
    out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd="/work/repo", text=True)
    assert out.strip() == "{sha}"
"""
MARKER_TEST = """def test_marker():
    with open("/work/repo/ir_marker.txt") as handle:
        assert handle.read() == "patched\\n"
"""
MARKER_PATCH = """diff --git a/ir_marker.txt b/ir_marker.txt
new file mode 100644
--- /dev/null
+++ b/ir_marker.txt
@@ -0,0 +1 @@
+patched
"""


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def built() -> tuple[DockerSandbox, str, ExecResult, list[ProgressEvent]]:
    sandbox = DockerSandbox()
    events: list[ProgressEvent] = []
    image, result = await build_baseline(
        sandbox, repo=REPO, ref=HEAD_SHA, python_version="3.12", progress=events.append
    )
    return sandbox, image, result, events


async def test_build_baseline_returns_a_usable_checkpoint(built) -> None:
    sandbox, image, result, events = built
    assert result.exit_code == 0
    assert "3.12" in result.stdout  # the sanity step printed sys.version
    assert events and {e.stage for e in events} == {"environment"}
    probe = await sandbox.run(
        image,
        "git rev-parse HEAD && python -c 'import sample; print(sample.__name__)' "
        "&& python -m pytest --version",
        workdir="/work/repo",
        timeout_s=120,
    )
    assert probe.exit_code == 0, probe.stderr
    assert probe.stdout.splitlines()[0] == HEAD_SHA
    assert "sample" in probe.stdout
    assert "pytest" in probe.stdout + probe.stderr


async def test_passing_test_gives_pass_pass(built) -> None:
    sandbox, image, _, _ = built
    side = await run_test_on_side(
        sandbox, image, test_path="tests/test_ir_probe.py", test_code=PASSING
    )
    assert side.outcomes == [Outcome.PASS, Outcome.PASS]
    assert side.stable_outcome is Outcome.PASS
    assert side.checkpoint_image == image
    assert side.ref == "HEAD"


async def test_failing_test_gives_fail_fail(built) -> None:
    sandbox, image, _, _ = built
    side = await run_test_on_side(
        sandbox, image, test_path="tests/test_ir_probe.py", test_code=FAILING
    )
    assert side.outcomes == [Outcome.FAIL, Outcome.FAIL]
    assert "assert 1 + 1 == 3" in side.log_excerpt


async def test_collection_error_gives_error(built) -> None:
    sandbox, image, _, _ = built
    side = await run_test_on_side(
        sandbox, image, test_path="tests/test_ir_probe.py", test_code=BROKEN, runs=1
    )
    assert side.outcomes == [Outcome.ERROR]


async def test_the_package_is_importable_in_the_checkpoint(built) -> None:
    sandbox, image, _, _ = built
    side = await run_test_on_side(
        sandbox, image, test_path="tests/test_ir_probe.py", test_code=IMPORTS_PACKAGE
    )
    assert side.outcomes == [Outcome.PASS, Outcome.PASS]


async def test_checkout_moves_to_another_commit_and_reinstalls(built) -> None:
    sandbox, image, _, _ = built
    at_head = await run_test_on_side(
        sandbox, image, test_path="tests/test_ir_head.py", test_code=HEAD_CHECK.format(sha=HEAD_SHA)
    )
    at_old = await run_test_on_side(
        sandbox,
        image,
        test_path="tests/test_ir_head.py",
        test_code=HEAD_CHECK.format(sha=OLD_SHA),
        checkout=OLD_SHA,
    )
    wrong = await run_test_on_side(
        sandbox,
        image,
        test_path="tests/test_ir_head.py",
        test_code=HEAD_CHECK.format(sha=HEAD_SHA),
        checkout=OLD_SHA,
    )
    assert at_head.outcomes == [Outcome.PASS, Outcome.PASS]
    assert at_old.outcomes == [Outcome.PASS, Outcome.PASS]
    assert at_old.ref == OLD_SHA
    assert wrong.outcomes == [Outcome.FAIL, Outcome.FAIL]


async def test_patch_flips_the_test_and_forks_stay_isolated(built) -> None:
    sandbox, image, _, _ = built
    without = await run_test_on_side(
        sandbox, image, test_path="tests/test_ir_marker.py", test_code=MARKER_TEST
    )
    with_patch = await run_test_on_side(
        sandbox,
        image,
        test_path="tests/test_ir_marker.py",
        test_code=MARKER_TEST,
        patch=MARKER_PATCH,
    )
    assert without.outcomes == [Outcome.FAIL, Outcome.FAIL]
    assert with_patch.outcomes == [Outcome.PASS, Outcome.PASS]
    assert with_patch.ref == "gold_patch"
    # The patch lived only in its own fork: the checkpoint is untouched.
    clean = await sandbox.run(image, "test ! -e /work/repo/ir_marker.txt && echo clean")
    assert clean.stdout.strip() == "clean"


async def test_bad_patch_is_an_error_not_a_crash(built) -> None:
    sandbox, image, _, _ = built
    side = await run_test_on_side(
        sandbox,
        image,
        test_path="tests/test_ir_probe.py",
        test_code=PASSING,
        patch="this is not a diff\n",
    )
    assert side.outcomes == [Outcome.ERROR, Outcome.ERROR]
    assert "__SETUP_FAILED__=patch" in side.log_excerpt


async def test_keep_true_checkpoint_can_be_run_from(built) -> None:
    sandbox, image, _, _ = built
    made = await sandbox.run(image, "echo kept > /work/repo/kept.txt", keep=True)
    assert made.exit_code == 0 and made.image and made.image != image
    forked = await sandbox.run(made.image, "cat /work/repo/kept.txt")
    assert forked.stdout.strip() == "kept"
    side = await run_test_on_side(
        sandbox,
        made.image,
        test_path="tests/test_ir_probe.py",
        test_code="def test_kept():\n    assert open('/work/repo/kept.txt').read() == 'kept\\n'\n",
    )
    assert side.outcomes == [Outcome.PASS, Outcome.PASS]


async def test_extra_packages_are_installed_in_the_checkpoint_and_a_bad_one_is_reported() -> None:
    sandbox = DockerSandbox()
    image, result = await build_baseline(
        sandbox, repo=REPO, ref=HEAD_SHA, python_version="3.12",
        extra_packages=["six==1.16.0", "fixedyet-no-such-package-xyz"],
    )
    assert installed_extras(result) == (["six==1.16.0"], ["fixedyet-no-such-package-xyz"])
    check = "import six\n\n\ndef test_six():\n    assert six.__version__ == '1.16.0'\n"
    side = await run_test_on_side(
        sandbox, image, test_path="tests/test_extra.py", test_code=check, runs=1, network=False
    )
    assert side.outcomes == [Outcome.PASS], side.log_excerpt
