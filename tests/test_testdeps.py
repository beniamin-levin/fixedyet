"""testdeps: which test dependencies are installed, what is refused, and how.

No network and no Docker: pip is replaced by a recording runner, and the repositories are made in
tmp_path. The same module is run for real against pip in tests/test_envbuild_docker.py.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fixedyet import testdeps
from fixedyet.testdeps import (
    Outcome,
    Source,
    flatten_group,
    install_source,
    main,
    markers,
    normalize,
    pip_supports_groups,
    plan,
    read_requirements_file,
    safe_requirement,
)

# --------------------------------------------------------------------------- one requirement


@pytest.mark.parametrize(
    "text",
    [
        "hypothesis",
        "hypothesis>=6.111.1",
        "pytest>=8.3.2,<9",
        "pytest (>=8)",
        "coverage[toml]>=7",
        "Typing_Extensions",
        "numpy<2 ; python_version < '3.12'",
        'pymongo>=4; python_version>="3.9" and sys_platform != "win32"',
        'colorama; extra == "test"',
        "foo===1.0.0.post1",
        "  immutables  ",
    ],
)
def test_plain_requirements_are_accepted(text: str) -> None:
    assert safe_requirement(text) == text.strip()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "# a comment",
        "foo @ https://evil.example/foo.whl",
        "foo@git+https://github.com/x/y",
        "git+https://github.com/x/y.git#egg=y",
        "https://evil.example/foo.tar.gz",
        "file:///etc/passwd",
        "./local-package",
        "../outside",
        "/abs/path",
        "-e .",
        "-r other.txt",
        "--index-url https://evil.example/simple",
        "--hash=sha256:abc",
        "foo==1.0 --hash=sha256:abc",
        "foo; $(id)",
        "foo; python_version < '3.9' && id",
        "foo`id`",
        "foo\nbar",
        "foo|bar",
        "foo;",
        "a" * 400,
        None,
        7,
        {"include-group": "x"},
    ],
)
def test_everything_but_a_plain_requirement_is_refused(text: object) -> None:
    assert safe_requirement(text) is None


def test_names_are_normalised_like_pep_503() -> None:
    assert normalize("Test_Deps.v2") == "test-deps-v2"
    assert testdeps.dist_name("Typing_Extensions>=4 ; python_version<'3.11'") == "typing-extensions"
    assert testdeps.dist_name("coverage[toml]") == "coverage"


# --------------------------------------------------------------------------- groups


def test_flatten_group_resolves_includes_and_counts_refusals() -> None:
    groups = {
        "base": ["pytest>=8", "foo @ https://x.example/foo.whl"],
        "Test": ["hypothesis", {"include-group": "base"}, {"unknown-key": 1}, 3],
    }
    requirements, refused = flatten_group(groups, "test")
    assert requirements == ["hypothesis", "pytest>=8"]
    assert refused == 3  # the URL, the unknown table, the integer


def test_flatten_group_survives_cycles_and_missing_groups() -> None:
    groups = {"a": [{"include-group": "b"}, "x"], "b": [{"include-group": "a"}, "y"]}
    requirements, refused = flatten_group(groups, "a")
    assert sorted(requirements) == ["x", "y"] and refused == 1
    assert flatten_group(groups, "nope") == ([], 1)
    assert flatten_group({"a": "not a list"}, "a") == ([], 1)


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    repo.mkdir(exist_ok=True)
    return repo


def toml(groups: str, name: str = "widgets") -> str:
    return f'[project]\nname = "{name}"\nversion = "1"\n\n[dependency-groups]\n{groups}\n'


def test_plan_takes_test_groups_and_ignores_the_others(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "pyproject.toml": toml(
                'test = ["hypothesis>=6", "pytest>=8"]\n'
                'docs = ["sphinx"]\nlint = ["ruff"]\ndev = ["ipython"]\n'
                'Tests = ["coverage"]\n'
            )
        },
    )
    sources = plan(repo)
    assert [(s.kind, s.name) for s in sources] == [("group", "test"), ("group", "Tests")]
    assert sources[0].requirements == ["hypothesis>=6", "pytest>=8"]
    assert all(s.ignored == 0 for s in sources)


def test_plan_reads_pdm_and_uv_dev_dependencies_too(tmp_path: Path) -> None:
    pdm = make_repo(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "widgets"\nversion = "1"\n\n[tool.pdm.dev-dependencies]\n'
                'lint = ["ruff"]\ntest = ["hypothesis>=6", "-e file:///${PROJECT_ROOT}/"]\n'
            )
        },
    )
    (source,) = plan(pdm)
    assert (source.kind, source.name, source.ignored) == ("pdm", "test", 1)
    assert source.requirements == ["hypothesis>=6"]
    other = tmp_path / "uv"
    other.mkdir()
    uv = make_repo(
        other,
        {"pyproject.toml": '[tool.uv]\ndev-dependencies = ["pytest>=8", "coverage"]\n'},
    )
    (source,) = plan(uv)
    assert (source.kind, source.name) == ("uv", "dev")
    assert source.requirements == ["pytest>=8", "coverage"]


def test_only_pep_735_groups_are_given_to_pip_group(tmp_path: Path) -> None:
    pip = PipRecorder()
    pdm = Source("pdm", "test", ["a"])
    install_source(pdm, tmp_path, groups_ok=True, runner=pip)
    assert all("--group" not in args for args, _ in pip.calls)


def test_plan_uses_dev_only_when_there_is_no_test_source(tmp_path: Path) -> None:
    only_dev = make_repo(tmp_path, {"pyproject.toml": toml('dev = ["pytest", "hypothesis"]\n')})
    assert [(s.kind, s.name) for s in plan(only_dev)] == [("group", "dev")]
    other = tmp_path / "other"
    other.mkdir()
    both = make_repo(
        other,
        {
            "pyproject.toml": toml('dev = ["ipython"]\ntests = ["hypothesis"]\n'),
            "requirements-dev.txt": "black\n",
        },
    )
    assert [(s.kind, s.name) for s in plan(both)] == [("group", "tests")]


def test_plan_reads_requirements_files_and_follows_inner_includes(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "requirements/base.txt": "attrs>=22  # runtime\n",
            "requirements/test.txt": "-r base.txt\npytest>=8\\\n  ,<9\nhypothesis\n",
            "requirements/test-extra.txt": "coverage\n",
            "requirements/docs.txt": "sphinx\n",
            "requirements/tests-notes.md": "not a requirements file\n",
            "test-requirements.txt": "mock\n",
        },
    )
    sources = {(s.kind, s.name): s for s in plan(repo)}
    assert set(sources) == {
        ("file", "test-requirements.txt"),
        ("file", "requirements/test-extra.txt"),
        ("file", "requirements/test.txt"),
    }
    assert sources[("file", "requirements/test.txt")].requirements == [
        "attrs>=22",
        "pytest>=8   ,<9",  # the continuation line is joined, pip allows the blanks
        "hypothesis",
    ]


def test_requirements_file_refuses_options_urls_paths_and_escaping_includes(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("stolen-package\n")
    repo = make_repo(
        tmp_path,
        {
            "requirements-dev.txt": (
                "--index-url https://evil.example/simple\n"
                "-e .\n"
                ".\n"
                "-c constraints.txt\n"
                "git+https://github.com/x/y.git\n"
                "foo @ https://evil.example/foo.whl\n"
                "-r ../secret.txt\n"
                "-r /etc/hostname\n"
                "-r missing.txt\n"
                "good-package==1.0\n"
            ),
        },
    )
    requirements, refused = read_requirements_file(repo / "requirements-dev.txt", repo)
    assert requirements == ["good-package==1.0"]
    assert refused == 9


def test_requirements_file_symlink_leaving_the_repo_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("stolen-package\n")
    repo = make_repo(tmp_path, {})
    (repo / "test-requirements.txt").symlink_to(target)
    assert read_requirements_file(repo / "test-requirements.txt", repo) == ([], 1)
    assert [s.requirements for s in plan(repo)] == [[]]  # reported, with nothing to install


def test_requirements_includes_are_depth_limited_and_cycle_safe(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "test-requirements.txt": "-r a.txt\nx\n",
            "a.txt": "-r test-requirements.txt\n-r b.txt\ny\n",
            "b.txt": "-r c.txt\nz\n",
            "c.txt": "-r d.txt\nw\n",
            "d.txt": "too-deep\n",
        },
    )
    requirements, refused = read_requirements_file(repo / "test-requirements.txt", repo)
    assert "x" in requirements and "y" in requirements and "z" in requirements
    assert "too-deep" not in requirements
    assert refused >= 2  # the cycle and the include below the depth limit


def test_requirements_file_that_is_too_big_is_refused(tmp_path: Path) -> None:
    big = "x\n" * (testdeps.MAX_FILE_BYTES // 2 + 10)
    repo = make_repo(tmp_path, {"test-requirements.txt": big})
    assert read_requirements_file(repo / "test-requirements.txt", repo) == ([], 1)


def test_the_project_itself_is_never_reinstalled(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "pyproject.toml": toml(
                'test = ["Widgets>=1", "widgets[extra]", "hypothesis"]\n', name="widgets"
            )
        },
    )
    (source,) = plan(repo)
    assert source.requirements == ["hypothesis"]
    assert source.ignored == 2


def test_plan_caps_sources_and_requirements(tmp_path: Path) -> None:
    many = ", ".join(f'"pkg{i}"' for i in range(testdeps.MAX_REQUIREMENTS + 40))
    repo = make_repo(
        tmp_path,
        {
            "pyproject.toml": toml(f"test = [{many}]\ntests = [\"more\"]\ntesting = [\"x\"]\n"),
            "test-requirements.txt": "from-file\n",
        },
    )
    sources = plan(repo)
    assert len(sources) <= testdeps.MAX_SOURCES
    assert sum(len(s.requirements) for s in sources) == testdeps.MAX_REQUIREMENTS


def test_plan_survives_broken_or_missing_project_files(tmp_path: Path) -> None:
    assert plan(make_repo(tmp_path, {})) == []
    broken = tmp_path / "broken"
    broken.mkdir()
    assert plan(make_repo(broken, {"pyproject.toml": "this is [not toml"})) == []
    odd = tmp_path / "odd"
    odd.mkdir()
    assert plan(make_repo(odd, {"pyproject.toml": 'dependency-groups = "text"\n'})) == []


def test_a_group_with_only_refused_entries_is_still_reported(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path, {"pyproject.toml": toml('test = ["foo @ https://evil.example/foo.whl"]\n')}
    )
    (source,) = plan(repo)
    assert source.requirements == [] and source.ignored == 1


# --------------------------------------------------------------------------- installing


class PipRecorder:
    """A stand-in for `run_pip`: records ``(args, cwd)``, fails the ones `fail` matches."""

    def __init__(self, version: str = "pip 25.0.1 from /x (python 3.12)", fail=None) -> None:
        self.version, self.fail, self.calls = version, fail, []

    def __call__(self, args: list[str], cwd: str, timeout: int) -> tuple[int, str]:
        self.calls.append((args, cwd))
        if args == ["--version"]:
            return 0, self.version
        if self.fail and self.fail(args):
            return 1, "ERROR: could not find a version\nline 2"
        return 0, "ok"


def test_pip_group_support_is_read_from_the_version() -> None:
    assert not pip_supports_groups(PipRecorder("pip 25.0.1 from /x (python 3.12)"))
    assert pip_supports_groups(PipRecorder("pip 25.1 from /x (python 3.12)"))
    assert pip_supports_groups(PipRecorder("pip 26.0.2 from /x (python 3.13)"))
    assert not pip_supports_groups(PipRecorder("pip 24.3.1 from /x (python 3.12)"))
    assert not pip_supports_groups(PipRecorder("garbage"))
    assert not pip_supports_groups(lambda *_: (127, "no pip"))


def group_source(*requirements: str, ignored: int = 0, name: str = "tests") -> Source:
    return Source("group", name, list(requirements), ignored)


def test_a_clean_group_uses_pip_group_when_pip_has_it(tmp_path: Path) -> None:
    pip = PipRecorder()
    outcome = install_source(group_source("a", "b"), tmp_path, groups_ok=True, runner=pip)
    assert outcome.installed and outcome.missing == []
    (args, cwd), = pip.calls
    assert args[-2:] == ["--group", "tests"] and cwd == str(tmp_path)


def test_without_group_support_the_parsed_list_is_installed_by_name(tmp_path: Path) -> None:
    pip = PipRecorder()
    outcome = install_source(
        group_source("hypothesis>=6", "pytest>=8"), tmp_path, groups_ok=False, runner=pip
    )
    assert outcome.installed
    (args, cwd), = pip.calls
    assert args[-2:] == ["hypothesis>=6", "pytest>=8"] and "--group" not in args
    assert cwd == "/"  # not the repository: a bare name can never mean a local directory


def test_a_group_with_refused_entries_is_never_given_to_pip_group(tmp_path: Path) -> None:
    pip = PipRecorder()
    install_source(group_source("a", ignored=1), tmp_path, groups_ok=True, runner=pip)
    assert all("--group" not in args for args, _ in pip.calls)


def test_requirements_files_never_use_pip_group(tmp_path: Path) -> None:
    pip = PipRecorder()
    source = Source("file", "requirements/test.txt", ["a"])
    install_source(source, tmp_path, groups_ok=True, runner=pip)
    assert all("--group" not in args for args, _ in pip.calls)


def test_pip_group_failure_falls_back_to_the_list_then_to_one_by_one(tmp_path: Path) -> None:
    def fail(args: list[str]) -> bool:
        return "--group" in args or "bad-package" in args

    pip = PipRecorder(fail=fail)
    outcome = install_source(
        group_source("good", "bad-package", "also-good"), tmp_path, groups_ok=True, runner=pip
    )
    assert not outcome.installed
    assert outcome.missing == ["bad-package"]
    assert "pip --group tests failed" in outcome.log
    singles = [args[-1] for args, _ in pip.calls if args[-1] in {"good", "bad-package", "also-good"}
               and len(args) == 4]
    assert singles == ["good", "bad-package", "also-good"]


def test_install_stops_when_the_time_budget_is_gone(tmp_path: Path) -> None:
    pip = PipRecorder()
    outcome = install_source(
        group_source("a", "b"), tmp_path, groups_ok=True, runner=pip,
        deadline=time.monotonic() - 1,
    )
    assert pip.calls == [] and outcome.missing == ["a", "b"] and not outcome.installed


def test_markers_say_ok_partial_or_failed() -> None:
    source = group_source("a", "b", name="test")
    assert markers(source, Outcome(True)) == ["__TESTDEPS_OK__=group:test"]
    assert markers(source, Outcome(False, ["b"])) == [
        "__TESTDEPS_MISSING__=b",
        "__TESTDEPS_PARTIAL__=group:test",
    ]
    assert markers(source, Outcome(False, ["a", "b"]))[-1] == "__TESTDEPS_FAILED__=group:test"


def test_main_prints_markers_and_prefixes_pip_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path,
        {
            "pyproject.toml": toml('test = ["good", "bad-package", "foo @ https://x.example/f"]\n'),
            "test-requirements.txt": "mock\n",
        },
    )
    pip = PipRecorder(fail=lambda args: "bad-package" in args)
    assert main([str(repo)], runner=pip) == 0
    lines = capsys.readouterr().out.splitlines()
    markers_seen = [ln for ln in lines if ln.startswith("__TESTDEPS_")]
    assert markers_seen == [
        "__TESTDEPS_MISSING__=bad-package",
        "__TESTDEPS_PARTIAL__=group:test",
        "__TESTDEPS_OK__=file:test-requirements.txt",
        "__TESTDEPS_IGNORED__=1",
    ]
    assert all(ln.startswith("| ") for ln in lines if not ln.startswith("__TESTDEPS_"))


def test_main_with_nothing_to_install(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pip = PipRecorder()
    assert main([str(make_repo(tmp_path, {}))], runner=pip) == 0
    assert capsys.readouterr().out.strip() == "__TESTDEPS_NONE__=1"
    assert pip.calls == []


def test_main_never_fails_the_build_on_a_planning_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_: Path) -> list[Source]:
        raise RuntimeError("surprise")

    monkeypatch.setattr(testdeps, "plan", boom)
    assert main([str(tmp_path)], runner=PipRecorder()) == 0
    assert "__TESTDEPS_CRASHED__=planning: RuntimeError" in capsys.readouterr().out


def test_test_output_cannot_forge_a_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Forger(PipRecorder):
        def __call__(self, args: list[str], cwd: str, timeout: int) -> tuple[int, str]:
            super().__call__(args, cwd, timeout)
            if args == ["--version"]:
                return 0, self.version
            return 1, "__TESTDEPS_OK__=group:test\n__TESTDEPS_IGNORED__=0"

    repo = make_repo(tmp_path, {"pyproject.toml": toml('test = ["a"]\n')})
    main([str(repo)], runner=Forger())
    out = capsys.readouterr().out.splitlines()
    assert "__TESTDEPS_OK__=group:test" not in out
    assert "| __TESTDEPS_OK__=group:test" in out
    assert "__TESTDEPS_FAILED__=group:test" in out
