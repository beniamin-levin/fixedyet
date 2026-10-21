"""The backport flow against a real Docker daemon, on a tiny repository built inside the sandbox.

Run with ``pytest -m docker``; CI does (.github/workflows/sandbox-docker.yml). The scripts that
cherry-pick and run the project's tests are shell, so only a real git and a real pytest can show
that they work. Nothing here needs GitHub: ``build_baseline`` is replaced by a repository made in
the sandbox, and the branch list is injected. Everything after that is the production code path.

The repository has a ``main`` line and a ``1.0.x`` maintenance branch, and fix commits on side
branches: a clean fix, a fix that conflicts, a fix that breaks another test, and a merge commit.
"""

from __future__ import annotations

import re
import shutil

import pytest
import pytest_asyncio

from fixedyet import envbuild
from fixedyet.backport import (
    backport,
    parse_pick,
    parse_suite,
    pick_script,
    suite_files,
    suite_script,
)
from fixedyet.config import Settings
from fixedyet.models import (
    Assessment,
    BackportStatus,
    Confidence,
    ExecResult,
    Outcome,
    Reproduction,
    SideRun,
    Verdict,
)
from fixedyet.sandbox.docker import DockerSandbox

pytestmark = [
    pytest.mark.docker,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]

SETUP = r"""
set -e
git config --global user.email dev@example.com
git config --global user.name Dev
git config --global init.defaultBranch main
mkdir -p /work/repo/widgets /work/repo/tests && cd /work/repo
git init -q
cat > pyproject.toml <<'EOF'
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "widgets"
version = "1.0"

[tool.setuptools]
packages = ["widgets"]
EOF
cat > widgets/__init__.py <<'EOF'
def parse(text):
    return text.split(",")[:-1]


def shout(text):
    return text.upper()
EOF
printf '# Changes\n\nmain: nothing yet\n' > CHANGES.md
cat > tests/test_ok.py <<'EOF'
from widgets import shout


def test_shout():
    assert shout("a") == "A"
EOF
cat > tests/test_known_bad.py <<'EOF'
def test_known_failure():
    assert False, "this one fails on every branch"
EOF
git add -A && git commit -q -m "Initial commit"
BASE=$(git rev-parse HEAD)

# the maintenance branch has a release commit of its own, touching the changelog
git checkout -q -b 1.0.x
printf '# Changes\n\nbranch: 1.0.1 released\n' > CHANGES.md
git commit -q -am "Prepare 1.0.1"
git checkout -q main

# fix A: touches only the code and adds its own test
git checkout -q -b fix-a $BASE
sed -i 's/\[:-1\]//' widgets/__init__.py
cat > tests/test_parse.py <<'EOF'
from widgets import parse


def test_parse_keeps_the_last_item():
    assert parse("a,b,c") == ["a", "b", "c"]
EOF
git add -A && git commit -q -m "Keep the last item in parse()

parse() dropped the final element. Fixes the report."
echo "__SHA_A__=$(git rev-parse HEAD)"

# fix B: same code fix, but it also edits the changelog line the branch edited differently
git checkout -q -b fix-b $BASE
sed -i 's/\[:-1\]//' widgets/__init__.py
printf '# Changes\n\nmain: parse() keeps the last item\n' > CHANGES.md
git commit -q -am "Keep the last item in parse(), with changelog"
echo "__SHA_B__=$(git rev-parse HEAD)"

# fix R: fixes parse() but breaks shout()
git checkout -q -b fix-r $BASE
sed -i 's/\[:-1\]//; s/text.upper()/text.lower()/' widgets/__init__.py
git commit -q -am "Keep the last item in parse() and lower-case shout()"
echo "__SHA_R__=$(git rev-parse HEAD)"

# fix M: a real merge commit on a mainline
git checkout -q -b mainline $BASE
git checkout -q -b fix-m
sed -i 's/\[:-1\]//' widgets/__init__.py
git commit -q -am "Keep the last item in parse() (topic branch)"
git checkout -q mainline
git merge -q --no-ff fix-m -m "Merge pull request 7 from topic"
echo "__SHA_M__=$(git rev-parse HEAD)"

git checkout -q 1.0.x
echo "__HEAD__=$(git rev-parse HEAD)"
pip install -q pytest > /dev/null 2>&1
pip install -q -e . > /dev/null 2>&1
echo __SETUP_DONE__
"""

REPRO_PATH = "tests/test_issue_repro.py"
REPRO_CODE = """from widgets import parse


def test_parse():
    assert parse("a,b,c") == ["a", "b", "c"]
"""
PASSING_REPRO = "def test_nothing_wrong():\n    assert True\n"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world() -> tuple[DockerSandbox, str, dict[str, str]]:
    sandbox = DockerSandbox()
    built = await sandbox.run(
        await sandbox.base_image("3.12"), SETUP, workdir="/work", keep=True, timeout_s=600
    )
    assert built.exit_code == 0, built.stdout + built.stderr
    assert "__SETUP_DONE__" in built.stdout and built.image
    marks = dict(re.findall(r"^__(SHA_[ABRM]|HEAD)__=(\w+)$", built.stdout, re.MULTILINE))
    assert len(marks) == 5, built.stdout
    return sandbox, built.image, marks


def assessment_for(commit: str, repro: str = REPRO_CODE) -> Assessment:
    return Assessment(
        issue_url="https://github.com/acme/widgets/issues/7",
        title="parse() drops the last item",
        verdict=Verdict.FIXED_ON_MAIN,
        confidence=Confidence.HIGH,
        reproduction=Reproduction(
            repro_test_path=REPRO_PATH,
            repro_test_code=repro,
            reported_side=SideRun(ref="1.0.0", outcomes=[Outcome.FAIL, Outcome.FAIL]),
            fix_side=SideRun(ref="main", outcomes=[Outcome.PASS, Outcome.PASS]),
            fixing_commit=commit,
        ),
    )


@pytest_asyncio.fixture
async def run_backport(world, monkeypatch: pytest.MonkeyPatch):
    sandbox, image, marks = world

    async def prebuilt(sb, *, repo, ref, **_):
        assert repo == "acme/widgets" and ref == marks["HEAD"]
        return image, ExecResult(exit_code=0, image=image)

    async def branches(repo: str) -> dict[str, str]:
        return {"main": "0" * 40, "1.0.x": marks["HEAD"], "stable": "1" * 40}

    monkeypatch.setattr(envbuild, "build_baseline", prebuilt)

    async def go(commit_key: str, repro: str = REPRO_CODE):
        # Pruning would delete the module-scoped repository image the next test needs.
        return await backport(
            assessment_for(marks[commit_key], repro), sandbox=sandbox, list_branches=branches,
            settings=Settings(keep_checkpoints=1000),
        )

    return go


# -- the scripts on their own ----------------------------------------------------------------


async def test_suite_script_counts_and_names_the_failing_tests(world) -> None:
    sandbox, image, _ = world
    result = await sandbox.run(image, suite_script(), workdir="/work/repo", network=False)
    suite = parse_suite(result)
    assert suite.ran and suite.exit_code == 1
    assert (suite.passed, suite.failed, suite.errors) == (1, 1, 0)
    assert suite.failing_tests == ["tests/test_known_bad.py::test_known_failure"]
    assert "short test summary info" in suite.log_excerpt
    assert "\n| " not in suite.log_excerpt  # the marker prefix is gone from what readers see


async def test_suite_script_reads_the_plugin_report_at_any_verbosity(world) -> None:
    # The project's addopts "-q" plus the script's own "-q" make -qq, where pytest prints no
    # "N passed in Xs" line (nedbat/cog). The plugin's report still gives counts and ids.
    sandbox, image, _ = world
    quiet = "printf '[pytest]\\naddopts = -q\\n' > /work/repo/pytest.ini\n"
    result = await sandbox.run(
        image, quiet + suite_script(), files=suite_files(), workdir="/work/repo", network=False
    )
    assert "__SUITE_JSON_B64__=" in result.stdout
    assert not re.search(r"^\| .* passed in ", result.stdout, re.MULTILINE)  # no summary line
    suite = parse_suite(result)
    assert suite.ran and suite.exit_code == 1
    assert (suite.passed, suite.failed, suite.errors) == (1, 1, 0)
    assert suite.failing_tests == ["tests/test_known_bad.py::test_known_failure"]


async def test_the_suite_runs_as_the_unprivileged_user(world) -> None:
    # A test that expects a read-only file to be unwritable fails as root (nedbat/cog's
    # WritabilityTests). With the baseline's test user the suite runs as that user.
    sandbox, image, _ = world
    with_user = await sandbox.run(image, envbuild.TESTER_SETUP, workdir="/work", keep=True)
    assert "__TESTER_OK__=" in with_user.stdout and with_user.image
    readonly = (
        "import os\n\n\ndef test_read_only(tmp_path):\n"
        "    path = tmp_path / 'ro'\n    path.write_text('x')\n    path.chmod(0o444)\n"
        "    assert not os.access(path, os.W_OK)\n"
    )
    files = {**suite_files(), "/work/repo/tests/test_read_only.py": readonly}
    result = await sandbox.run(
        with_user.image, suite_script(), files=files, workdir="/work/repo", network=False
    )
    suite = parse_suite(result)
    assert suite.ran and (suite.passed, suite.failed) == (2, 1), suite.log_excerpt
    assert suite.failing_tests == ["tests/test_known_bad.py::test_known_failure"]


async def test_pick_script_applies_a_clean_fix_and_exports_it(world) -> None:
    sandbox, image, marks = world
    result = await sandbox.run(
        image, pick_script(marks["SHA_A"]), workdir="/work/repo", keep=True
    )
    pick = parse_pick(result)
    assert result.exit_code == 0 and result.image
    assert pick.head == marks["HEAD"]
    assert pick.picked and pick.picked != marks["SHA_A"]
    assert not pick.merge and not pick.pick_failed and not pick.conflicts
    assert pick.message.startswith("Keep the last item in parse()")
    assert f"(cherry picked from commit {marks['SHA_A']})" in pick.message
    assert pick.patch.startswith("From ") and "Subject: [PATCH] Keep the last item" in pick.patch
    assert "diff --git a/widgets/__init__.py" in pick.patch
    assert "CHANGES.md" not in pick.patch


async def test_pick_script_reports_conflicts_and_aborts(world) -> None:
    sandbox, image, marks = world
    result = await sandbox.run(
        image, pick_script(marks["SHA_B"]), workdir="/work/repo", keep=True
    )
    pick = parse_pick(result)
    assert result.exit_code == 30
    assert pick.pick_failed and pick.conflicts == ["CHANGES.md"]
    assert not pick.picked and not pick.patch
    assert "CONFLICT" in pick.log
    # The pick was aborted: no unmerged paths, and HEAD is where it was.
    after = await sandbox.run(
        result.image,
        "git status --porcelain --untracked-files=no; git log -1 --format=%s",
        workdir="/work/repo",
    )
    assert after.stdout.strip() == "Prepare 1.0.1"


async def test_pick_script_uses_the_first_parent_for_a_merge_commit(world) -> None:
    sandbox, image, marks = world
    result = await sandbox.run(
        image, pick_script(marks["SHA_M"]), workdir="/work/repo", keep=True
    )
    pick = parse_pick(result)
    assert result.exit_code == 0, result.stdout + result.stderr
    assert pick.merge and pick.picked
    assert "diff --git a/widgets/__init__.py" in pick.patch


# -- the whole backport ----------------------------------------------------------------------


async def test_a_clean_fix_is_verified(run_backport) -> None:
    result = await run_backport("SHA_A")
    assert result.status is BackportStatus.VERIFIED, result.reason
    assert result.target_branch == "1.0.x"
    assert result.repro_before.stable_outcome is Outcome.FAIL
    assert result.repro_after.stable_outcome is Outcome.PASS
    assert (result.suite_before.passed, result.suite_before.failed) == (1, 1)
    assert (result.suite_after.passed, result.suite_after.failed) == (2, 1)  # the fix's own test
    assert result.newly_failing == []
    assert result.suite_after.failing_tests == ["tests/test_known_bad.py::test_known_failure"]
    assert "cherry picked from commit" in result.commit_message
    assert result.patch.startswith("From ")
    assert result.usage.sandbox_runs >= 5


async def test_a_merge_commit_is_cherry_picked_with_m_1(run_backport) -> None:
    result = await run_backport("SHA_M")
    assert result.status is BackportStatus.VERIFIED, result.reason


async def test_a_conflicting_fix_reports_the_files_and_resolves_nothing(run_backport) -> None:
    result = await run_backport("SHA_B")
    assert result.status is BackportStatus.CONFLICTS
    assert result.conflicting_files == ["CHANGES.md"]
    assert result.patch == "" and result.repro_after is None and result.suite_after is None


async def test_a_fix_that_breaks_another_test_is_a_regression(run_backport) -> None:
    result = await run_backport("SHA_R")
    assert result.status is BackportStatus.REGRESSION, result.reason
    assert result.repro_after.stable_outcome is Outcome.PASS
    assert result.newly_failing == ["tests/test_ok.py::test_shout"]
    assert result.patch  # still attached, for a person to look at


async def test_a_test_that_already_passes_on_the_branch_needs_no_backport(run_backport) -> None:
    result = await run_backport("SHA_A", PASSING_REPRO)
    assert result.status is BackportStatus.ALREADY_FIXED_ON_BRANCH
    assert result.repro_before.stable_outcome is Outcome.PASS
    assert result.patch == ""
