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


@pytest.mark.parametrize(
    "name",
    [
        "tests-pallets-min.txt",
        "test-min.txt",
        "tests_minimum.txt",
        "test.lowest.txt",
        "test-oldest.txt",
    ],
)
def test_plan_skips_minimum_version_requirements_files(tmp_path: Path, name: str) -> None:
    repo = make_repo(
        tmp_path,
        {
            f"requirements/{name}": "werkzeug==2.0.0\n",
            "requirements/tests.txt": "pytest\n",
            "requirements/test-mindful.txt": "coverage\n",  # "min" inside a word is not a marker
        },
    )
    assert [(s.kind, s.name) for s in plan(repo)] == [
        ("file", "requirements/test-mindful.txt"),
        ("file", "requirements/tests.txt"),
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


def healthy_pytest(cwd: str, timeout: int) -> tuple[int, str]:
    """A stand-in for `run_probe`: pytest starts fine."""
    return 0, "pytest 8.0.0"


def crashing_pytest(cwd: str, timeout: int) -> tuple[int, str]:
    return 1, (
        "Traceback (most recent call last):\n"
        "  File \"jinja2/filters.py\", line 13, in <module>\n"
        "    from markupsafe import soft_unicode\n"
        "ImportError: cannot import name 'soft_unicode' from 'markupsafe'"
    )


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
    assert main([str(repo)], runner=pip, probe=healthy_pytest) == 0
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
    main([str(repo)], runner=Forger(), probe=healthy_pytest)
    out = capsys.readouterr().out.splitlines()
    assert "__TESTDEPS_OK__=group:test" not in out
    assert "| __TESTDEPS_OK__=group:test" in out
    assert "__TESTDEPS_FAILED__=group:test" in out


# --------------------------------------------------------------------------- known breaks


def test_a_pytest_that_starts_needs_no_repair() -> None:
    pip = PipRecorder()
    assert testdeps.repair_known_breaks(pip, healthy_pytest) == []
    assert pip.calls == []


def test_the_soft_unicode_break_is_cured_by_pinning_markupsafe() -> None:
    pip = PipRecorder()
    probes: list[int] = []

    def probe(cwd: str, timeout: int) -> tuple[int, str]:
        probes.append(1)
        return crashing_pytest(cwd, timeout) if len(probes) == 1 else healthy_pytest(cwd, timeout)

    (line,) = testdeps.repair_known_breaks(pip, probe)
    assert "installed markupsafe<2.1: pytest starts now" in line
    assert [args[-1] for args, _ in pip.calls] == ["markupsafe<2.1"]


def test_a_repair_that_does_not_help_is_reported_as_such() -> None:
    (line,) = testdeps.repair_known_breaks(PipRecorder(), crashing_pytest)
    assert "pytest still does not start" in line


def test_an_unknown_pytest_crash_is_reported_and_nothing_is_installed() -> None:
    pip = PipRecorder()
    (line,) = testdeps.repair_known_breaks(pip, lambda cwd, timeout: (2, "ImportError: boom"))
    assert "no known repair" in line and "boom" in line
    assert pip.calls == []


def test_main_prints_the_repair_as_a_prefixed_log_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, {"test-requirements.txt": "mock\n"})
    assert main([str(repo)], runner=PipRecorder(), probe=crashing_pytest) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(ln.startswith("| pytest could not start (soft_unicode)") for ln in lines)
    assert all(ln.startswith("| ") for ln in lines if not ln.startswith("__TESTDEPS_"))


# --------------------------------------------------------------------------- other pythons' files


@pytest.mark.parametrize(
    ("name", "tag"),
    [
        ("tests37.txt", (3, 7)),  # pallets/jinja 3.1.6: the pin set of Python 3.7
        ("requirements/tests37.txt", (3, 7)),
        ("test-py312.txt", (3, 12)),
        ("tests_3.8.txt", (3, 8)),
        ("tests-python3.10.txt", (3, 10)),
        ("test-2.7.txt", (2, 7)),
        ("tests.txt", None),
        ("tests-min.txt", None),
        ("tests2.txt", None),  # no minor version
        ("tests23.txt", None),  # Python 2.3 never was a target
        ("test_django32.txt", None),  # 3.2 is not a Python version anyone pins for
        ("tests-py3.txt", None),
    ],
)
def test_python_tag_reads_the_version_a_file_name_carries(
    name: str, tag: tuple[int, int] | None
) -> None:
    assert testdeps.python_tag(name) == tag


def test_a_file_for_another_python_is_recognised_only_as_such() -> None:
    assert testdeps.for_another_python("tests37.txt", (3, 13))
    assert not testdeps.for_another_python("tests37.txt", (3, 7))
    assert not testdeps.for_another_python("tests.txt", (3, 13))


def test_plan_skips_the_requirements_file_of_another_python(tmp_path: Path) -> None:
    """Jinja 3.1.6: tests.txt (current pins) and tests37.txt (Python 3.7 pins, trio 0.22.2).

    Installed in that order the second one downgraded trio to a version that cannot be imported
    on Python 3.13, and the conftest of the main branch imports trio at the top.
    """
    repo = make_repo(
        tmp_path,
        {
            "requirements/tests.txt": "pytest==8.3.4\ntrio==0.27.0\n",
            "requirements/tests37.txt": "pytest==7.4.4\ntrio==0.22.2\n",
            "requirements/tests-py313.txt": "outcome\n",
        },
    )
    assert testdeps.file_candidates(repo, (3, 13))[0] == [
        "requirements/tests-py313.txt",
        "requirements/tests.txt",
    ]
    assert testdeps.file_candidates(repo, (3, 7))[0] == [
        "requirements/tests.txt",
        "requirements/tests37.txt",
    ]
    sources = plan(repo)  # the interpreter running the tests is not 3.7
    assert "requirements/tests37.txt" not in [s.name for s in sources]


# --------------------------------------------------------------------------- project extras

DOTENV_PYPROJECT = (
    '[project]\nname = "python-dotenv"\nversion = "1.2.3"\n'
    '[project.optional-dependencies]\ncli = ["click>=5.0"]\n'
    '[project.scripts]\ndotenv = "dotenv.__main__:cli"\n'
)
DOTENV_HINT = (
    "It seems python-dotenv is not installed with cli option. \n"
    'Run pip install "python-dotenv[cli]" to fix this.'
)


def with_extras(extras: str) -> str:
    """DOTENV_PYPROJECT with its ``[project.optional-dependencies]`` entries replaced."""
    return DOTENV_PYPROJECT.replace('cli = ["click>=5.0"]', extras)


def scripts_dir(tmp_path: Path, *names: str) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in names:
        (directory / name).write_text("#!/bin/sh\n", encoding="utf-8")
    return directory


def test_the_extras_and_scripts_of_a_project_are_read_from_pyproject() -> None:
    data = {
        "project": {
            "name": "x",
            "optional-dependencies": {"cli": ["click"], "Docs": ["sphinx"]},
            "scripts": {"x": "x:main", "bad name; id": "x:y"},
        }
    }
    assert testdeps.declared_extras(data) == ["cli", "Docs"]
    assert testdeps.console_script_names(data) == ["x"]
    assert testdeps.declared_extras({}) == [] and testdeps.console_script_names({}) == []


def test_a_console_script_that_cannot_start_names_the_extra_it_wants(tmp_path: Path) -> None:
    data = testdeps.tomllib.loads(DOTENV_PYPROJECT)
    ran: list[str] = []

    def script(path: str, timeout: int) -> tuple[int, str]:
        ran.append(path)
        return 1, DOTENV_HINT

    found = testdeps.script_hints(data, script, scripts_dir(tmp_path, "dotenv"))
    assert found == ["cli"] and ran == [str(tmp_path / "bin" / "dotenv")]


NO_EXTRAS_PYPROJECT = (
    '[project]\nname = "python-dotenv"\nversion = "1"\n'
    '[project.scripts]\ndotenv = "dotenv.__main__:cli"\n'
)


@pytest.mark.parametrize(
    ("code", "output", "pyproject"),
    [
        (0, DOTENV_HINT, DOTENV_PYPROJECT),  # it starts, so whatever it prints is not a request
        (1, 'Run pip install "someone-else[cli]" to fix.', DOTENV_PYPROJECT),  # another project
        (1, 'Run pip install "python-dotenv[nope]" to fix.', DOTENV_PYPROJECT),  # not declared
        (1, "Traceback: boom", DOTENV_PYPROJECT),
        (1, DOTENV_HINT, NO_EXTRAS_PYPROJECT),  # a project without extras has nothing to install
    ],
)
def test_script_hints_are_ignored_unless_they_name_a_declared_extra_of_this_project(
    tmp_path: Path, code: int, output: str, pyproject: str
) -> None:
    data = testdeps.tomllib.loads(pyproject)
    bin_dir = scripts_dir(tmp_path, "dotenv")
    assert testdeps.script_hints(data, lambda path, timeout: (code, output), bin_dir) == []


def test_a_script_that_is_not_installed_is_not_run(tmp_path: Path) -> None:
    data = testdeps.tomllib.loads(DOTENV_PYPROJECT)

    def never(path: str, timeout: int) -> tuple[int, str]:
        raise AssertionError("must not run")

    assert testdeps.script_hints(data, never, scripts_dir(tmp_path)) == []


def test_project_extras_are_installed_editable_one_at_a_time(tmp_path: Path) -> None:
    pip = PipRecorder(fail=lambda args: ".[bad]" in args)
    installed, unknown, failed = testdeps.install_project_extras(tmp_path, ["cli", "bad"], pip)
    assert (installed, unknown, failed) == (["cli"], [], ["bad"])
    assert [(args[-2:], cwd) for args, cwd in pip.calls] == [
        (["-e", ".[cli]"], str(tmp_path)),
        (["-e", ".[bad]"], str(tmp_path)),
    ]


def test_an_extra_that_pip_says_the_project_does_not_provide_is_unknown(tmp_path: Path) -> None:
    def pip(args: list[str], cwd: str, timeout: int) -> tuple[int, str]:
        return 0, "WARNING: widgets 1 does not provide the extra 'nope'"

    assert testdeps.install_project_extras(tmp_path, ["Nope"], pip) == ([], ["Nope"], [])


def test_main_extras_installs_the_asked_for_and_the_hinted_extras(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path,
        {"pyproject.toml": with_extras('cli = ["click"]\ndocs = ["sphinx"]')},
    )
    pip = PipRecorder()
    bin_dir = scripts_dir(tmp_path, "dotenv")
    code = testdeps.main_extras(
        [str(repo), "docs,nope,bad;name"], runner=pip,
        script_runner=lambda p, t: (1, DOTENV_HINT), bin_dir=bin_dir,
    )
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert [ln for ln in out if ln.startswith("__")] == [
        "__PROJECT_EXTRA_UNKNOWN__=nope",  # the project declares extras and has no "nope"
        "__PROJECT_EXTRA_HINT__=cli",
        "__PROJECT_EXTRA_OK__=docs",
        "__PROJECT_EXTRA_OK__=cli",
    ]
    assert [args[-1] for args, _ in pip.calls] == [".[docs]", ".[cli]"]  # "bad;name" never got in


def test_main_extras_with_nothing_to_do_runs_no_pip(tmp_path: Path) -> None:
    pip = PipRecorder()
    assert testdeps.main_extras([str(make_repo(tmp_path, {}))], runner=pip) == 0
    assert pip.calls == []


def test_main_extras_asks_pip_when_the_project_does_not_declare_its_extras_in_pyproject(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pip = PipRecorder()  # a setup.py project: pyproject.toml cannot say what it provides
    testdeps.main_extras([str(make_repo(tmp_path, {})), "cli"], runner=pip)
    assert [args[-1] for args, _ in pip.calls] == [".[cli]"]
    assert "__PROJECT_EXTRA_OK__=cli" in capsys.readouterr().out


# --------------------------------------------------------------------------- collection repair

TRIO_CRASH = """\
ImportError while loading conftest '/work/repo/tests/conftest.py'.
tests/conftest.py:5: in <module>
    import trio
/usr/local/lib/python3.13/site-packages/trio/__init__.py:71: in <module>
    from ._path import Path as Path
/usr/local/lib/python3.13/site-packages/trio/_path.py:134: in <module>
    class Path(metaclass=AsyncAutoWrapperType):
E   TypeError: ('parser', <class 'module'>)
"""
CLICK_MISSING = """\
ImportError while loading conftest '/work/repo/tests/conftest.py'.
tests/conftest.py:2: in <module>
    from click.testing import CliRunner
E   ModuleNotFoundError: No module named 'click'
"""


def test_a_missing_module_is_read_from_the_collection_output() -> None:
    problem = testdeps.diagnose_collection(CLICK_MISSING)
    assert problem == testdeps.Problem("missing", ["click"])
    nested = "E   ModuleNotFoundError: No module named 'yaml.constructor'"
    assert testdeps.diagnose_collection(nested) == testdeps.Problem("missing", ["yaml"])


def test_a_package_that_crashes_while_the_conftest_loads_is_read_from_its_frames() -> None:
    assert testdeps.diagnose_collection(TRIO_CRASH) == testdeps.Problem("broken", ["trio"])


@pytest.mark.parametrize(
    "output",
    [
        "",
        "E   AssertionError: assert 1 == 2",
        # a crash outside conftest and plugins is the test's own business, not the environment's
        "tests/test_x.py:3: in <module>\n/usr/lib/site-packages/foo/bar.py:1: in <module>\nE   X",
        # pytest's own frames are never "repaired" by upgrading pytest
        "ImportError while loading conftest 'c'\n/x/site-packages/_pytest/config/a.py:1\nE   Err",
    ],
)
def test_other_failures_are_not_diagnosed(output: str) -> None:
    assert testdeps.diagnose_collection(output) is None


def test_declared_requirements_come_from_extras_groups_and_requirements_files(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path,
        {
            "pyproject.toml": DOTENV_PYPROJECT
            + '[dependency-groups]\ndocs = ["Sphinx>=7"]\n'
            + '[tool.pytest]\nx = 1\n',
            "requirements.txt": "pytest>=9\nipython\nclick\n",
            "requirements/dev37.txt": "ancient==1\n",
            "requirements/tests-min.txt": "click==1.0\n",
            "evil.txt": "not-a-requirements-file\n",
        },
    )
    declared = testdeps.declared_requirements(repo, (3, 13))
    assert declared["click"] == "click>=5.0"  # the extra comes before requirements.txt
    assert declared["sphinx"] == "Sphinx>=7" and declared["ipython"] == "ipython"
    assert "python-dotenv" not in declared  # never the project itself
    assert "ancient" not in declared and "not-a-requirements-file" not in declared


def test_an_extra_marker_in_a_declared_requirement_is_cut_off() -> None:
    assert testdeps.strip_extra_marker('colorama; extra == "test"') == "colorama"
    assert testdeps.strip_extra_marker("colorama; sys_platform == 'win32'").startswith("colorama;")


def test_a_module_finds_its_requirement_by_name_or_by_a_known_alias() -> None:
    declared = {"click": "click>=5.0", "pyyaml": "PyYAML>=6", "typing-extensions": "typing_ext"}
    assert testdeps.requirement_for_module("click", declared) == "click>=5.0"
    assert testdeps.requirement_for_module("yaml", declared) == "PyYAML>=6"
    assert testdeps.requirement_for_module("typing_extensions", declared) == "typing_ext"
    assert testdeps.requirement_for_module("evilpkg", declared) is None


class Collector:
    """A stand-in for `run_collect`: the n-th call returns the n-th ``(exit code, output)``."""

    def __init__(self, *results: tuple[int, str]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, cwd: str, test_path: str, timeout: int) -> tuple[int, str]:
        self.calls.append((cwd, test_path))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def click_repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path, {"pyproject.toml": DOTENV_PYPROJECT})


def test_a_conftest_that_needs_a_declared_extra_gets_it(tmp_path: Path) -> None:
    """python-dotenv 1.2.3: tests/conftest.py imports click, which only the cli extra declares."""
    pip = PipRecorder()
    collect = Collector((4, CLICK_MISSING), (0, "1 test collected"))
    repo = click_repo(tmp_path)
    lines = testdeps.repair_collection(repo, "tests/t.py", runner=pip, collect=collect)
    assert [args[-1] for args, _ in pip.calls] == ["click>=5.0"]
    assert [ln for ln in lines if ln.startswith("__")] == [
        "__REPAIR_OK__=click>=5.0",
        "__REPAIR_CLEAN__=1",
    ]
    assert len(collect.calls) == 2


def test_a_healthy_collection_installs_nothing(tmp_path: Path) -> None:
    pip = PipRecorder()
    lines = testdeps.repair_collection(
        click_repo(tmp_path), "tests/t.py", runner=pip, collect=Collector((0, "ok"))
    )
    assert lines == ["__REPAIR_CLEAN__=1"] and pip.calls == []
    assert testdeps.repair_collection(
        click_repo(tmp_path), "tests/t.py", runner=pip, collect=Collector((5, "no tests"))
    ) == ["__REPAIR_CLEAN__=1"]


def test_a_module_nothing_declares_is_never_guessed_from_the_index(tmp_path: Path) -> None:
    pip = PipRecorder()
    crash = CLICK_MISSING.replace("click", "evilpkg")
    lines = testdeps.repair_collection(
        click_repo(tmp_path), "tests/t.py", runner=pip, collect=Collector((4, crash))
    )
    assert pip.calls == []
    assert lines[0] == "__REPAIR_UNRESOLVED__=missing: evilpkg"
    assert any("No module named 'evilpkg'" in ln for ln in lines[1:])


def test_a_requirement_is_tried_once_and_the_rounds_are_bounded(tmp_path: Path) -> None:
    pip = PipRecorder(fail=lambda args: True)  # click cannot be installed
    collect = Collector((4, CLICK_MISSING))
    repo = click_repo(tmp_path)
    lines = testdeps.repair_collection(repo, "tests/t.py", runner=pip, collect=collect)
    assert [args[-1] for args, _ in pip.calls] == ["click>=5.0"]  # not again in round two
    assert lines[0] == "__REPAIR_FAILED__=click>=5.0"
    assert any(ln.startswith("__REPAIR_UNRESOLVED__=missing") for ln in lines)


def test_one_repair_can_reveal_the_next_problem(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {"pyproject.toml": with_extras('cli = ["click", "six"]')},
    )
    pip = PipRecorder()
    collect = Collector((4, CLICK_MISSING), (4, CLICK_MISSING.replace("click", "six")), (0, ""))
    lines = testdeps.repair_collection(repo, "tests/t.py", runner=pip, collect=collect)
    assert [args[-1] for args, _ in pip.calls] == ["click", "six"]
    assert lines[-1] == "| pytest collects the test now"


def test_a_stale_pin_that_crashes_on_import_is_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jinja main: trio 0.22.2, left from the reported version's pins, cannot be imported."""
    monkeypatch.setattr(testdeps, "distribution_of", lambda package: package)
    pip = PipRecorder()
    collect = Collector((4, TRIO_CRASH), (0, ""))
    repo = make_repo(tmp_path, {"pyproject.toml": toml('tests = ["trio"]\n', "jinja2")})
    testdeps.repair_collection(repo, "tests/t.py", runner=pip, collect=collect)
    (args, cwd), = pip.calls
    assert args[-2:] == ["-U", "trio"] and cwd == "/"


def test_the_project_itself_is_never_upgraded_by_the_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(testdeps, "distribution_of", lambda package: package)
    repo = make_repo(tmp_path, {"pyproject.toml": toml("", "trio")})
    pip = PipRecorder()
    collect = Collector((4, TRIO_CRASH))
    lines = testdeps.repair_collection(repo, "tests/t.py", runner=pip, collect=collect)
    assert pip.calls == [] and lines[0].startswith("__REPAIR_UNRESOLVED__=broken")


def test_main_repair_writes_a_stub_for_the_probe_and_removes_it_with_its_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, {"pyproject.toml": DOTENV_PYPROJECT})
    seen: list[str] = []

    def collect(cwd: str, test_path: str, timeout: int) -> tuple[int, str]:
        seen.append((Path(cwd) / test_path).read_text(encoding="utf-8"))
        return 0, "ok"

    assert testdeps.main_repair([str(repo), "newdir/test_probe.py"], collect=collect) == 0
    assert seen == [testdeps.PROBE_STUB]
    assert not (repo / "newdir").exists()
    assert capsys.readouterr().out.strip() == "__REPAIR_CLEAN__=1"


def test_main_repair_keeps_a_test_file_that_was_already_there(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"tests/test_x.py": "def test_x():\n    pass\n"})
    testdeps.main_repair([str(repo), "tests/test_x.py"], collect=Collector((0, "")))
    assert (repo / "tests" / "test_x.py").read_text(encoding="utf-8").startswith("def test_x")


@pytest.mark.parametrize("path", ["../x.py", "/etc/x.py", "a b.py", "-k.py", "x;id.py"])
def test_main_repair_refuses_an_unsafe_test_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    collect = Collector((0, ""))
    assert testdeps.main_repair([str(tmp_path), path], collect=collect) == 0
    assert collect.calls == [] and "__REPAIR_UNRESOLVED__=bad test path" in capsys.readouterr().out


def test_main_repair_never_fails_the_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(cwd: str, test_path: str, timeout: int) -> tuple[int, str]:
        raise RuntimeError("surprise")

    assert testdeps.main_repair([str(tmp_path), "tests/t.py"], collect=boom) == 0
    assert "__REPAIR_CRASHED__=RuntimeError" in capsys.readouterr().out
    assert not (tmp_path / "tests").exists()


def test_main_dispatches_the_two_sub_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, {"pyproject.toml": DOTENV_PYPROJECT})
    pip = PipRecorder()
    assert main(["extras", str(repo), "cli"], runner=pip) == 0
    assert "__PROJECT_EXTRA_OK__=cli" in capsys.readouterr().out
    assert [args[-1] for args, _ in pip.calls] == [".[cli]"]
