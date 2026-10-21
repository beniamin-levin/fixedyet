"""envbuild against a real Docker daemon, on a small real repository (pypa/sampleproject).

Run with ``pytest -m docker``; CI does (.github/workflows/sandbox-docker.yml). The baseline is
built once per module and every test forks it, which is exactly how the pipeline uses it.
"""

from __future__ import annotations

import shutil

import pytest
import pytest_asyncio

from fixedyet import envbuild
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
# Kills the interpreter with exit 1 before pytest can print a summary, like a pytest plugin
# that crashes on import: a run in which no test ever decided anything.
CRASHES_PYTEST = "import os\n\n\ndef test_dies():\n    os._exit(1)\n"
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


async def test_pytest_dying_with_exit_one_is_an_error_not_a_failure(built) -> None:
    sandbox, image, _, _ = built
    side = await run_test_on_side(
        sandbox, image, test_path="tests/test_ir_probe.py", test_code=CRASHES_PYTEST, runs=1
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


# --------------------------------------------------------------------------- test dependencies

DEPS_REPO = {
    "/work/repo/pyproject.toml": (
        '[project]\nname = "widgets"\nversion = "1"\n\n'
        "[dependency-groups]\n"
        'tests = ["six==1.16.0", {include-group = "more"}]\n'
        'more = ["iniconfig==2.0.0"]\n'
        'docs = ["mccabe==0.7.0"]\n'
        'dev = ["pyflakes==3.2.0"]\n'
    ),
    "/work/repo/requirements/test-extra.txt": "tomli-w==1.0.0\n# a comment\n",
    "/work/repo/requirements-dev.txt": "colorama==0.4.6\n",
}
DEPS_RUN = (
    "cd /work/repo && python /tmp/ir_testdeps.py /work/repo; echo ---; pip list --format=freeze"
)


@pytest.mark.parametrize("upgrade_pip", [False, True], ids=["pip-from-the-image", "pip-with-group"])
async def test_test_dependency_groups_and_files_are_installed_in_the_sandbox(
    upgrade_pip: bool,
) -> None:
    """Runs the shipped helper with a real pip, with and without ``pip install --group``."""
    sandbox = DockerSandbox()
    base = await sandbox.base_image("3.12")
    upgrade = "pip install --quiet 'pip>=25.1' && " if upgrade_pip else ""
    result = await sandbox.run(
        base,
        upgrade + DEPS_RUN,
        files={**DEPS_REPO, envbuild.TESTDEPS_PATH: envbuild.helper_source()},
        workdir="/work/repo",
        timeout_s=600,
    )
    assert result.exit_code == 0, result.stderr
    markers, _, frozen = result.stdout.partition("---\n")
    installed = {line.split("==")[0].lower().replace("_", "-") for line in frozen.splitlines()}
    assert {"six", "iniconfig", "tomli-w"} <= installed  # the test group, its include, the file
    assert not installed & {"mccabe", "pyflakes", "colorama"}  # docs and dev are left alone
    assert "__TESTDEPS_OK__=group:tests" in markers
    assert "__TESTDEPS_OK__=file:requirements/test-extra.txt" in markers
    assert "__TESTDEPS_FAILED__" not in markers and "__TESTDEPS_CRASHED__" not in markers
    assert ("installed with pip --group tests" in markers) is upgrade_pip


async def test_a_test_dependency_that_cannot_be_installed_does_not_stop_the_others() -> None:
    sandbox = DockerSandbox()
    base = await sandbox.base_image("3.12")
    files = {
        "/work/repo/pyproject.toml": (
            '[project]\nname = "widgets"\nversion = "1"\n\n[dependency-groups]\n'
            'test = ["six==1.16.0", "fixedyet-no-such-package-xyz", '
            '"evil @ https://example.invalid/evil.whl"]\n'
        ),
        envbuild.TESTDEPS_PATH: envbuild.helper_source(),
    }
    result = await sandbox.run(
        base, DEPS_RUN, files=files, workdir="/work/repo", timeout_s=600
    )
    assert result.exit_code == 0, result.stderr
    report = envbuild.installed_test_deps(result)
    assert report.partial == ["group:test"] and report.missing == ["fixedyet-no-such-package-xyz"]
    assert report.ignored == 1  # the URL was refused, not attempted
    assert "six==1.16.0" in result.stdout
    assert "evil" not in result.stdout.split("---")[1]
