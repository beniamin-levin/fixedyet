"""Install a project's test dependencies in the baseline sandbox.

`envbuild.baseline_script` copies THIS FILE into the sandbox and runs it with the sandbox's own
Python after the editable install, so it uses the standard library only and must stay compatible
with old interpreters. The host imports it as a module as well, for the unit tests.

    python ir_testdeps.py /work/repo

What it installs, and only this:

  * PEP 735 ``[dependency-groups]`` named ``test``, ``tests`` or ``testing`` in pyproject.toml,
    and the same names in ``[tool.pdm.dev-dependencies]`` (``dev`` only when the project has no
    test group at all, because dev groups tend to hold linters and documentation tools; uv's
    ``dev-dependencies`` list counts as a ``dev`` group);
  * the same for requirements files: ``requirements/test*.txt``, ``test-requirements.txt`` and a
    few spellings of it (``requirements-dev.txt`` and its kin only as the same fallback). A
    ``requirements/test*.txt`` file whose name says min, minimum, lowest or oldest is skipped.

How it installs: ``pip install --group <name>`` when the pip in the image has ``--group`` (pip
25.1 and newer) and every entry of the group is a plain requirement; otherwise the entries are
read with ``tomllib`` here and installed by name, in one command first and one by one when that
fails. A failure is never fatal: the build goes on, and the markers on stdout say what is missing.

A requirements file whose name carries another interpreter's version (``tests37.txt`` is the
pin set of Python 3.7) is skipped: installed over the right pins, it downgrades them to versions
the interpreter in the sandbox cannot even import.

One repair follows the installs: if ``python -m pytest --version`` crashes on a known
incompatibility among the packages just installed, the one pin that cures it is installed
(``KNOWN_BREAKS``).

Two more sub-commands run after a build and after a checkout of another ref:

    python ir_testdeps.py extras /work/repo cli,other          # optional extras of the project
    python ir_testdeps.py repair /work/repo tests/test_x.py    # make pytest load the conftest

``extras`` installs the extras the issue asked for (``pip install pkg[cli]``) and the ones the
project's own console script asks for when it cannot start (python-dotenv's ``dotenv`` prints
``Run pip install "python-dotenv[cli]"``), each as ``pip install -e .[extra]`` and only for an
extra the project declares. ``repair`` collects the test file and, when that dies while pytest
loads the project's conftest or plugins, fixes the one thing that can be fixed from the project's
own declarations: a missing module whose package some extra, group or requirements file of the
project lists is installed; a package that crashes on import (a stale pin that this interpreter
cannot run, left over from the reported version's test requirements) is upgraded. Both are
bounded, never fatal, and report on stdout.

What it refuses, because a reporter-controlled repository decides these files: a requirement that
is not a plain ``name[extras] version-pins ; marker`` (so no URL, VCS reference, path or pip
option), an ``-r`` include that leaves the repository, a symlink that leaves it, a group or file
other than the ones named above, and more than a few sources or requirements. Refused entries are
counted in ``__TESTDEPS_IGNORED__``. Nothing from the project is executed: pyproject.toml and the
requirements files are read as text.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11 and newer; older interpreters cannot read dependency groups
    import tomllib
except ImportError:  # pragma: no cover - exercised only on an old interpreter
    tomllib = None  # type: ignore[assignment]

TEST_GROUPS = ("test", "tests", "testing")
FALLBACK_GROUPS = ("dev",)
TEST_FILES = (
    "test-requirements.txt",
    "requirements-test.txt",
    "requirements-tests.txt",
    "requirements_test.txt",
    "test_requirements.txt",
    "tests/requirements.txt",
)
FALLBACK_FILES = (
    "requirements-dev.txt",
    "dev-requirements.txt",
    "requirements_dev.txt",
    "requirements/dev.txt",
)
TEST_FILE_GLOB_DIR = "requirements"
_GLOBBED_FILE_RE = re.compile(r"^test[A-Za-z0-9_.-]*\.txt$")
# A file of minimum (or lowest, or oldest) versions describes a second environment that the
# project's CI builds on purpose. Installed here it would downgrade what the project itself
# declares (Flask's requirements/tests-pallets-min.txt pins Werkzeug==2.0.0 under a Flask that
# needs 2.2.2), so such a file is never read.
_MIN_VERSIONS_RE = re.compile(r"(?:^|[-_.])(?:min|minimum|minimal|lowest|oldest)(?=[-_.]|$)")

# A Python version in a requirements file's name: `tests37.txt`, `test-py312.txt`, `tests_3.8.txt`.
# Only versions Python has had count, so `tests23.txt` (a Django 2.3?) names no interpreter.
_PY_TAG_RE = re.compile(
    r"(?:^|[^0-9])(?:py|python)?(?P<major>[23])[._-]?(?P<minor>\d{1,2})(?=$|[^0-9])"
)
_PY_TAG_MINORS = {2: range(6, 8), 3: range(4, 21)}

MAX_SOURCES = 3
MAX_REQUIREMENTS = 120
MAX_FILE_BYTES = 256 * 1024
MAX_INCLUDE_DEPTH = 3
PIP_TIMEOUT_S = 600
TOTAL_BUDGET_S = 900
GROUP_SUPPORT_VERSION = (25, 1)  # the first pip with `pip install --group`

# A pytest start-up crash whose output contains the needle is cured by installing the pin. Jinja2
# older than 3.1 imports `soft_unicode`, which MarkupSafe 2.1 removed, and the test requirements
# of old requests (Flask<2, hence Jinja2<3) pull in exactly that pair, so pytest dies while it
# loads the pytest-httpbin plugin, before any test runs.
KNOWN_BREAKS = (("soft_unicode", "markupsafe<2.1"),)
PROBE_TIMEOUT_S = 120

# One requirement as pip accepts it from a package index: a name, extras, version pins and an
# environment marker. `@`, `/`, `:`, `\`, `$`, backticks, `&`, `|`, `;` (after the marker's own)
# and a leading `-` or `.` never match, so no URL, path, VCS reference or option gets through.
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_EXTRAS = r"\[\s*[A-Za-z0-9._-]+(?:\s*,\s*[A-Za-z0-9._-]+)*\s*\]"
_SPEC = r"(?:===|==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+"
_SPECS = rf"{_SPEC}(?:\s*,\s*{_SPEC})*"
_MARKER = r"[A-Za-z0-9_.\"'()<>=!~,*+\s-]+"
_REQUIREMENT_RE = re.compile(
    rf"^(?P<name>{_NAME})\s*(?P<extras>{_EXTRAS})?\s*"
    rf"(?:\(\s*(?P<paren>{_SPECS})\s*\)|(?P<specs>{_SPECS}))?"
    rf"\s*(?:;\s*(?P<marker>{_MARKER}))?$"
)
_MAX_LINE = 300
_INCLUDE_RE = re.compile(r"^(?:-r|--requirement)(?:\s+|=)?(?P<path>\S+)$")
_PIP_VERSION_RE = re.compile(r"^pip (\d+)\.(\d+)")


def python_tag(name: str) -> tuple[int, int] | None:
    """The Python version a requirements file's name says it is for, or None.

    >>> python_tag("tests37.txt"), python_tag("test-py312.txt"), python_tag("tests.txt")
    ((3, 7), (3, 12), None)
    """
    stem = name.rsplit("/", 1)[-1]
    stem = stem[: -len(".txt")] if stem.lower().endswith(".txt") else stem
    for match in _PY_TAG_RE.finditer(stem):
        major, minor = int(match["major"]), int(match["minor"])
        if minor in _PY_TAG_MINORS[major]:
            return major, minor
    return None


def for_another_python(name: str, running: tuple[int, int] | None = None) -> bool:
    """True when the file's name carries a Python version other than the running interpreter's."""
    tag = python_tag(name)
    version = running or (sys.version_info[0], sys.version_info[1])
    return tag is not None and tag != tuple(version)


def normalize(name: str) -> str:
    """PEP 503 normalisation, which PEP 735 also uses for group names."""
    return re.sub(r"[-_.]+", "-", name).lower()


def safe_requirement(text: object) -> str | None:
    """The requirement as plain text, or None when it is not a plain index requirement."""
    if not isinstance(text, str):
        return None
    line = text.strip()
    if not line or len(line) > _MAX_LINE or "\n" in line or "\r" in line:
        return None
    if _REQUIREMENT_RE.match(line) is None:
        return None
    return line


def dist_name(requirement: str) -> str:
    return normalize(re.split(r"[\[<>=!~;(\s]", requirement, maxsplit=1)[0])


@dataclass
class Source:
    """One thing to install: a dependency group or a requirements file."""

    kind: str  # "group" (PEP 735), "pdm", "uv" or "file"
    name: str  # the group name as written in pyproject.toml, or the file's path in the repo
    requirements: list[str] = field(default_factory=list)
    ignored: int = 0  # entries that were refused (not plain requirements, or the project itself)

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"


# --------------------------------------------------------------------------- reading


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def read_pyproject(repo: Path) -> dict:
    """The parsed pyproject.toml, or {} when it is missing, unreadable or tomllib is absent."""
    path = repo / "pyproject.toml"
    if tomllib is None or not path.is_file() or not _inside(path, repo):
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def project_dist(data: dict) -> str | None:
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return normalize(name) if isinstance(name, str) and name else None


def flatten_group(
    groups: dict, name: str, *, _seen: tuple[str, ...] = ()
) -> tuple[list[str], int]:
    """``(requirements, refused)`` of a group, with ``{include-group = ...}`` resolved.

    An include of a group that does not exist, a cycle, and any entry that is not a plain
    requirement or an include count as refused.
    """
    wanted = normalize(name)
    if wanted in _seen:
        return [], 1
    entries = None
    for key, value in groups.items():
        if normalize(str(key)) == wanted:
            entries = value
            break
    if not isinstance(entries, list):
        return [], 1
    found: list[str] = []
    refused = 0
    for entry in entries:
        if isinstance(entry, dict) and set(entry) == {"include-group"}:
            inner, inner_refused = flatten_group(
                groups, str(entry["include-group"]), _seen=(*_seen, wanted)
            )
            found += inner
            refused += inner_refused
            continue
        requirement = safe_requirement(entry)
        if requirement is None:
            refused += 1
        else:
            found.append(requirement)
    return found, refused


def _unique(requirements: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for requirement in requirements:
        if requirement not in seen:
            seen.add(requirement)
            unique.append(requirement)
    return unique


def _without_project(
    requirements: list[str], project: str | None
) -> tuple[list[str], int]:
    """Drop the project's own name: installing its release from the index would replace the
    checkout. Returns ``(requirements, dropped)``."""
    if project is None:
        return requirements, 0
    kept = [r for r in requirements if dist_name(r) != project]
    return kept, len(requirements) - len(kept)


def group_tables(data: dict) -> dict[str, dict]:
    """The tables of named dependency lists in pyproject.toml, by the kind of source they are.

    ``group`` is PEP 735 ``[dependency-groups]``; ``pdm`` is ``[tool.pdm.dev-dependencies]``
    (what older cattrs, for one, uses); ``uv`` is uv's ``dev-dependencies`` list, seen as a group
    called ``dev``. Only ``group`` can be installed with ``pip install --group``.
    """
    tables: dict[str, dict] = {}
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        tables["group"] = groups
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    pdm = tool.get("pdm") if isinstance(tool.get("pdm"), dict) else {}
    if isinstance(pdm.get("dev-dependencies"), dict):
        tables["pdm"] = pdm["dev-dependencies"]
    uv = tool.get("uv") if isinstance(tool.get("uv"), dict) else {}
    if isinstance(uv.get("dev-dependencies"), list):
        tables["uv"] = {"dev": uv["dev-dependencies"]}
    return tables


def group_candidates(
    tables: dict[str, dict],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(test groups, fallback groups)`` as ``(kind, name as written)``, over every table."""
    tests: list[tuple[str, str]] = []
    fallback: list[tuple[str, str]] = []
    for kind, table in tables.items():
        by_normal = {normalize(str(key)): str(key) for key in table}
        tests += [(kind, by_normal[n]) for n in TEST_GROUPS if n in by_normal]
        fallback += [(kind, by_normal[n]) for n in FALLBACK_GROUPS if n in by_normal]
    return tests, fallback


def read_requirements_file(
    path: Path, root: Path, *, depth: int = 0, _seen: frozenset[Path] = frozenset()
) -> tuple[list[str], int]:
    """``(requirements, refused)`` of a requirements file, following ``-r`` includes that stay
    inside ``root``. Pip options, URLs, paths and editable lines are refused, not run."""
    try:
        real = path.resolve()
    except OSError:
        return [], 1
    if real in _seen or not _inside(path, root) or not path.is_file():
        return [], 1
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return [], 1
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 1
    found: list[str] = []
    refused = 0
    lines = re.sub(r"\\\r?\n", " ", text).splitlines()
    for raw in lines:
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        include = _INCLUDE_RE.match(line)
        if include is not None:
            if depth >= MAX_INCLUDE_DEPTH:
                refused += 1
                continue
            inner, inner_refused = read_requirements_file(
                path.parent / include["path"], root, depth=depth + 1, _seen=_seen | {real}
            )
            found += inner
            refused += inner_refused
            continue
        requirement = safe_requirement(line)
        if requirement is None:
            refused += 1
        else:
            found.append(requirement)
    return found, refused


def file_candidates(
    repo: Path, running: tuple[int, int] | None = None
) -> tuple[list[str], list[str]]:
    """``(test files, fallback files)`` that exist, as paths relative to the repository.

    ``running`` is the interpreter's ``(major, minor)``; a file named for another one is left out.
    """

    def existing(names: tuple[str, ...]) -> list[str]:
        return [n for n in names if (repo / n).is_file()]

    tests = existing(TEST_FILES)
    directory = repo / TEST_FILE_GLOB_DIR
    if directory.is_dir():
        try:
            names = sorted(p.name for p in directory.iterdir() if p.is_file())
        except OSError:
            names = []
        tests += [
            f"{TEST_FILE_GLOB_DIR}/{n}"
            for n in names
            if _GLOBBED_FILE_RE.match(n)
            and not _MIN_VERSIONS_RE.search(n)
            and not for_another_python(n, running)
        ]
    return tests, existing(FALLBACK_FILES)


def plan(repo: Path) -> list[Source]:
    """What to install for the repository at ``repo``: test sources first, dev only as fallback,
    capped in number of sources and requirements."""
    data = read_pyproject(repo)
    project = project_dist(data)
    tables = group_tables(data)
    test_groups, fallback_groups = group_candidates(tables)
    test_files, fallback_files = file_candidates(repo)

    ordered = test_groups + [("file", n) for n in test_files]
    if not ordered:
        ordered = fallback_groups + [("file", n) for n in fallback_files]

    sources: list[Source] = []
    budget = MAX_REQUIREMENTS
    for kind, name in ordered[:MAX_SOURCES]:
        if kind != "file":
            requirements, refused = flatten_group(tables[kind], name)
        else:
            requirements, refused = read_requirements_file(repo / name, repo)
        requirements, dropped = _without_project(_unique(requirements), project)
        requirements = requirements[:budget]
        budget -= len(requirements)
        if requirements or refused or dropped:
            sources.append(Source(kind, name, requirements, refused + dropped))
    return sources


# --------------------------------------------------------------------------- installing


def run_pip(args: list[str], cwd: str, timeout: int) -> tuple[int, str]:
    """Run ``python -m pip <args>``; ``(exit code, combined output)``. 124 on a timeout."""
    command = [sys.executable, "-m", "pip", *args]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, requirements validated above
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"pip timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def run_probe(cwd: str, timeout: int) -> tuple[int, str]:
    """Run ``python -m pytest --version``; ``(exit code, combined output)``. 124 on a timeout."""
    command = [sys.executable, "-m", "pytest", "--version"]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"pytest --version timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def repair_known_breaks(runner=run_pip, probe=run_probe) -> list[str]:
    """If pytest cannot start after the installs, apply the pin from `KNOWN_BREAKS` that cures
    the crash. Returns log lines (empty when pytest starts fine). Never raises."""
    code, output = probe("/", PROBE_TIMEOUT_S)
    if code == 0:
        return []
    for needle, pin in KNOWN_BREAKS:
        if needle in output:
            flags = ["install", "--disable-pip-version-check", "--no-input"]
            runner([*flags, pin], "/", PIP_TIMEOUT_S)
            again, _ = probe("/", PROBE_TIMEOUT_S)
            verdict = "pytest starts now" if again == 0 else "pytest still does not start"
            return [f"pytest could not start ({needle}); installed {pin}: {verdict}"]
    return [f"pytest could not start (exit {code}); no known repair\n{_tail(output, 4)}"]


def pip_supports_groups(runner=run_pip) -> bool:
    """True when the pip in this interpreter has ``pip install --group`` (pip 25.1 and newer)."""
    code, output = runner(["--version"], "/", 60)
    match = _PIP_VERSION_RE.match(output.strip()) if code == 0 else None
    return bool(match) and (int(match[1]), int(match[2])) >= GROUP_SUPPORT_VERSION


def _tail(output: str, lines: int = 12) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


@dataclass
class Outcome:
    installed: bool  # everything in the source is in
    missing: list[str] = field(default_factory=list)
    log: str = ""


def install_source(
    source: Source,
    repo: Path,
    *,
    groups_ok: bool,
    runner=run_pip,
    deadline: float | None = None,
) -> Outcome:
    """Install one source. ``--group`` first when allowed, then the parsed list at once, then
    one requirement at a time. Never raises."""

    def timeout() -> int:
        if deadline is None:
            return PIP_TIMEOUT_S
        return max(30, min(PIP_TIMEOUT_S, int(deadline - time.monotonic())))

    def out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    quiet = ["install", "--disable-pip-version-check", "--no-input"]
    log = ""
    if (
        groups_ok
        and source.kind == "group"
        and source.ignored == 0
        and source.requirements
        and not out_of_time()
    ):
        code, output = runner([*quiet, "--group", source.name], str(repo), timeout())
        if code == 0:
            return Outcome(True, [], f"installed with pip --group {source.name}")
        log = f"pip --group {source.name} failed (exit {code})\n{_tail(output)}"
    if not source.requirements:
        return Outcome(False, [], log)
    if not out_of_time():
        code, output = runner([*quiet, *source.requirements], "/", timeout())
        if code == 0:
            return Outcome(True, [], f"{log}\ninstalled {len(source.requirements)} requirements")
        log = f"{log}\ninstalling the list at once failed (exit {code})\n{_tail(output)}"
    missing: list[str] = []
    for requirement in source.requirements:
        if out_of_time():
            missing.append(requirement)
            continue
        code, _ = runner([*quiet, requirement], "/", timeout())
        if code != 0:
            missing.append(requirement)
    return Outcome(False, missing, log.strip())


def markers(source: Source, outcome: Outcome) -> list[str]:
    """The machine-readable lines for one source (see `envbuild.installed_test_deps`)."""
    lines = [f"__TESTDEPS_MISSING__={requirement}" for requirement in outcome.missing]
    if outcome.installed:
        lines.append(f"__TESTDEPS_OK__={source.label}")
    elif len(outcome.missing) < len(source.requirements):
        lines.append(f"__TESTDEPS_PARTIAL__={source.label}")
    else:
        lines.append(f"__TESTDEPS_FAILED__={source.label}")
    return lines


# --------------------------------------------------------------------------- project extras

EXTRA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_EXTRAS = 5
MAX_SCRIPTS = 3
SCRIPT_TIMEOUT_S = 30
EXTRA_TIMEOUT_S = 600
_NO_SUCH_EXTRA_RE = re.compile(r"does not provide the extra ['\"]?([A-Za-z0-9._-]+)")
# What a console script says when an optional dependency is missing, in the words the project
# chose: `Run pip install "python-dotenv[cli]" to fix this.`
_HINT_RE = re.compile(
    r"pip3?\s+install\s+[\"']?(?P<dist>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\[(?P<extras>[A-Za-z0-9._,\s-]+)\]"
)


def declared_extras(data: dict) -> list[str]:
    """The extras of ``[project.optional-dependencies]``, as written."""
    project = data.get("project")
    table = project.get("optional-dependencies") if isinstance(project, dict) else None
    return [str(key) for key in table] if isinstance(table, dict) else []


def console_script_names(data: dict) -> list[str]:
    """The project's ``[project.scripts]`` names that are safe to run, at most `MAX_SCRIPTS`."""
    project = data.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict):
        return []
    return [str(n) for n in scripts if EXTRA_NAME_RE.match(str(n))][:MAX_SCRIPTS]


def run_script(path: str, timeout: int) -> tuple[int, str]:
    """Run ``<script> --help``; ``(exit code, combined output)``. 124 on a timeout."""
    try:
        done = subprocess.run(  # noqa: S603 - a console script of the project, fixed argv
            [path, "--help"], cwd="/", capture_output=True, text=True, timeout=timeout,
            check=False, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{path} --help timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def script_hints(data: dict, runner=run_script, bin_dir: Path | None = None) -> list[str]:
    """Extras the project's own console scripts ask for when they cannot start.

    A script that exits non-zero on ``--help`` and tells the reader to ``pip install
    "<this project>[extra]"`` names an extra, and only an extra the project declares counts.
    """
    project = project_dist(data)
    declared = {normalize(name): name for name in declared_extras(data)}
    if not project or not declared:
        return []
    directory = bin_dir or Path(sys.executable).parent
    found: list[str] = []
    for name in console_script_names(data):
        script = directory / name
        if not script.is_file():
            continue
        code, output = runner(str(script), SCRIPT_TIMEOUT_S)
        if code == 0:
            continue
        for match in _HINT_RE.finditer(output):
            if normalize(match["dist"]) != project:
                continue
            for extra in re.split(r"\s*,\s*", match["extras"].strip()):
                as_written = declared.get(normalize(extra))
                if as_written and as_written not in found:
                    found.append(as_written)
    return found[:MAX_EXTRAS]


def install_project_extras(
    repo: Path, extras: list[str], runner=run_pip
) -> tuple[list[str], list[str], list[str]]:
    """``(installed, unknown, failed)``: ``pip install -e .[extra]`` for each, one at a time.

    The editable install is the project's own, so the extra's requirements are exactly what the
    checkout declares. pip answers an extra the project does not have with a warning and exit 0,
    which is read from its output.
    """
    installed: list[str] = []
    unknown: list[str] = []
    failed: list[str] = []
    for extra in extras:
        args = ["install", "--disable-pip-version-check", "--no-input", "-e", f".[{extra}]"]
        code, output = runner(args, str(repo), EXTRA_TIMEOUT_S)
        if code != 0:
            failed.append(extra)
        elif normalize(extra) in {normalize(m) for m in _NO_SUCH_EXTRA_RE.findall(output)}:
            unknown.append(extra)
        else:
            installed.append(extra)
    return installed, unknown, failed


def main_extras(
    args: list[str], *, runner=run_pip, script_runner=run_script, bin_dir: Path | None = None
) -> int:
    """``extras <repo> [a,b]``: install the requested and the hinted extras, print markers."""
    repo = Path(args[0]) if args else Path.cwd()
    requested = [e for e in (args[1].split(",") if len(args) > 1 else []) if EXTRA_NAME_RE.match(e)]
    data = read_pyproject(repo)
    declared = {normalize(name): name for name in declared_extras(data)}
    wanted: list[str] = []
    for extra in requested:
        # a project with a pyproject declares its extras; one without is asked and pip decides
        if declared and normalize(extra) not in declared:
            print(f"__PROJECT_EXTRA_UNKNOWN__={extra}")
            continue
        wanted.append(declared.get(normalize(extra), extra))
    hinted = script_hints(data, script_runner, bin_dir)
    for extra in hinted:
        print(f"__PROJECT_EXTRA_HINT__={extra}")
        if extra not in wanted:
            wanted.append(extra)
    if not wanted:
        return 0
    installed, unknown, failed = install_project_extras(repo, wanted[:MAX_EXTRAS], runner)
    for extra in installed:
        print(f"__PROJECT_EXTRA_OK__={extra}")
        print(f"| installed the extra [{extra}] of the project")
    for extra in unknown:
        print(f"__PROJECT_EXTRA_UNKNOWN__={extra}")
        print(f"| the project has no extra called [{extra}]")
    for extra in failed:
        print(f"__PROJECT_EXTRA_FAILED__={extra}")
        print(f"| could not install the extra [{extra}] of the project")
    return 0


# --------------------------------------------------------------------------- collection repair

PROBE_STUB = "def test_fixedyet_probe():\n    pass\n"
COLLECT_TIMEOUT_S = 120
MAX_REPAIR_ROUNDS = 3
MAX_REQUIREMENT_FILES = 12
# Packages that are the machinery itself: never "repaired" by upgrading them.
NEVER_UPGRADE = frozenset(
    {"pytest", "_pytest", "pluggy", "py", "pip", "setuptools", "wheel", "pkg_resources",
     "_distutils_hack"}
)
# Import names that differ from the distribution that provides them (declared names only).
IMPORT_ALIASES = {
    "yaml": "pyyaml", "PIL": "pillow", "dateutil": "python-dateutil", "bs4": "beautifulsoup4",
    "cv2": "opencv-python", "sklearn": "scikit-learn", "attr": "attrs", "OpenSSL": "pyopenssl",
    "jwt": "pyjwt", "dotenv": "python-dotenv", "git": "gitpython", "serial": "pyserial",
    "zmq": "pyzmq", "docx": "python-docx", "magic": "python-magic", "Crypto": "pycryptodome",
}
_MISSING_MODULE_RE = re.compile(r"No module named ['\"]?(?P<module>[A-Za-z_][\w.]*)")
_LOADING_RE = re.compile(r"ImportError while loading (?:conftest|plugin)|Error importing plugin")
_SITE_PACKAGE_RE = re.compile(r"(?:site|dist)-packages/(?P<pkg>[A-Za-z_]\w*)(?:/|\.py)")
_REQUIREMENTS_NAME_RE = re.compile(r"^(?:requirements|test|dev)[A-Za-z0-9_.-]*\.txt$")
_TEST_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


@dataclass
class Problem:
    """What stopped pytest while it loaded the project's conftest and plugins."""

    kind: str  # "missing": a module is not installed; "broken": installed packages crash on import
    names: list[str]  # import names (missing) or top-level package directories (broken)


def diagnose_collection(output: str) -> Problem | None:
    """Read a failed ``pytest --collect-only`` for the one thing that can be fixed; else None."""
    missing = _MISSING_MODULE_RE.search(output)
    if missing:
        return Problem("missing", [missing["module"].split(".")[0]])
    if _LOADING_RE.search(output):
        packages: list[str] = []
        for match in _SITE_PACKAGE_RE.finditer(output):
            package = match["pkg"]
            if package not in NEVER_UPGRADE and package not in packages:
                packages.append(package)
        if packages:
            return Problem("broken", packages[:MAX_EXTRAS])
    return None


def strip_extra_marker(requirement: str) -> str:
    """``colorama; extra == "test"`` is read by pip as "skip me" outside that extra: cut it."""
    if re.search(r";.*\bextra\b", requirement):
        return requirement.split(";", 1)[0].strip()
    return requirement


def requirement_files(repo: Path, running: tuple[int, int] | None = None) -> list[str]:
    """Every requirements file that could declare something a test run needs, relative paths."""
    names: list[str] = [
        n for n in (*TEST_FILES, *FALLBACK_FILES, "requirements.txt") if (repo / n).is_file()
    ]
    for directory in (".", TEST_FILE_GLOB_DIR):
        folder = repo / directory
        if not folder.is_dir():
            continue
        try:
            listing = sorted(p.name for p in folder.iterdir() if p.is_file())
        except OSError:
            continue
        for n in listing:
            if not _REQUIREMENTS_NAME_RE.match(n):
                continue
            if _MIN_VERSIONS_RE.search(n) or for_another_python(n, running):
                continue
            path = n if directory == "." else f"{directory}/{n}"
            if path not in names:
                names.append(path)
    return names[:MAX_REQUIREMENT_FILES]


def declared_requirements(
    repo: Path, running: tuple[int, int] | None = None
) -> dict[str, str]:
    """Every plain requirement the project declares anywhere, by distribution name (first wins).

    Extras, dependency groups, runtime dependencies and requirements files all count: the point is
    to find which declared requirement provides a module that a test run turned out to need.
    """
    data = read_pyproject(repo)
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    pool: list[object] = []
    extras = project.get("optional-dependencies")
    if isinstance(extras, dict):
        for entries in extras.values():
            pool += entries if isinstance(entries, list) else []
    for table in group_tables(data).values():
        for name in table:
            pool += flatten_group(table, str(name))[0]
    dependencies = project.get("dependencies")
    pool += dependencies if isinstance(dependencies, list) else []
    for name in requirement_files(repo, running):
        pool += read_requirements_file(repo / name, repo)[0]
    own = project_dist(data)
    declared: dict[str, str] = {}
    for text in pool:
        requirement = safe_requirement(text)
        if requirement is None:
            continue
        requirement = strip_extra_marker(requirement)
        name = dist_name(requirement)
        if name != own and name not in declared:
            declared[name] = requirement
    return declared


def requirement_for_module(module: str, declared: dict[str, str]) -> str | None:
    """The declared requirement that provides ``module``: by its name, or a well-known alias."""
    for candidate in (module, IMPORT_ALIASES.get(module, "")):
        found = declared.get(normalize(candidate)) if candidate else None
        if found:
            return found
    return None


def distribution_of(package: str) -> str:
    """The distribution that owns the top-level import ``package`` (itself when unknown)."""
    try:
        from importlib.metadata import packages_distributions

        owners = packages_distributions().get(package)
    except Exception:  # Python 3.9 and older do not have it; the directory name is a good guess
        owners = None
    return owners[0] if owners else package


def run_collect(cwd: str, test_path: str, timeout: int) -> tuple[int, str]:
    """``pytest --collect-only`` on one test file; ``(exit code, combined output)``."""
    command = [
        sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
        test_path,
    ]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, the path is validated by the caller
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"pytest --collect-only timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def repair_actions(
    problem: Problem, repo: Path, *, declared: dict[str, str] | None = None
) -> list[tuple[str, list[str]]]:
    """``(label, pip arguments)`` that answer the problem; empty when nothing declared does."""
    quiet = ["install", "--disable-pip-version-check", "--no-input"]
    if problem.kind == "missing":
        found = declared if declared is not None else declared_requirements(repo)
        requirement = requirement_for_module(problem.names[0], found)
        return [(requirement, [*quiet, requirement])] if requirement else []
    own = project_dist(read_pyproject(repo))
    actions: list[tuple[str, list[str]]] = []
    for package in problem.names:
        dist = distribution_of(package)
        if safe_requirement(dist) is None or normalize(dist) == own:
            continue
        actions.append((f"{dist} (upgraded)", [*quiet, "-U", dist]))
    return actions


def repair_collection(
    repo: Path, test_path: str, *, runner=run_pip, collect=run_collect
) -> list[str]:
    """Collect ``test_path`` and repair what stops pytest from loading the project's conftest.

    Returns the lines to print: markers (``__REPAIR_OK__=``, ``__REPAIR_FAILED__=``,
    ``__REPAIR_UNRESOLVED__=``, ``__REPAIR_CLEAN__=1``) and ``| `` log lines. At most
    `MAX_REPAIR_ROUNDS` rounds; each requirement is tried once.
    """
    lines: list[str] = []
    tried: set[str] = set()
    declared: dict[str, str] | None = None
    for round_number in range(MAX_REPAIR_ROUNDS + 1):
        code, output = collect(str(repo), test_path, COLLECT_TIMEOUT_S)
        if code in (0, 5):  # collected, or nothing to collect: pytest itself starts fine
            lines.append("__REPAIR_CLEAN__=1")
            if tried:
                lines.append("| pytest collects the test now")
            return lines
        problem = diagnose_collection(output)
        if problem is None:
            lines.append("__REPAIR_UNRESOLVED__=unknown")
            lines += [f"| {ln}" for ln in _tail(output, 6).splitlines()]
            return lines
        if declared is None and problem.kind == "missing":
            declared = declared_requirements(repo)
        actions = [a for a in repair_actions(problem, repo, declared=declared) if a[0] not in tried]
        if not actions or round_number == MAX_REPAIR_ROUNDS:
            lines.append(f"__REPAIR_UNRESOLVED__={problem.kind}: {', '.join(problem.names)}")
            lines += [f"| {ln}" for ln in _tail(output, 6).splitlines()]
            return lines
        for label, args in actions:
            tried.add(label)
            result, pip_output = runner(args, "/", PIP_TIMEOUT_S)
            if result == 0:
                lines.append(f"__REPAIR_OK__={label}")
                lines.append(f"| pytest could not load the project's conftest ({problem.kind}); "
                             f"installed {label}")
            else:
                lines.append(f"__REPAIR_FAILED__={label}")
                lines += [f"| {ln}" for ln in _tail(pip_output, 4).splitlines()]
    return lines  # pragma: no cover - the loop always returns


def main_repair(args: list[str], *, runner=run_pip, collect=run_collect) -> int:
    """``repair <repo> <test file>``: print what `repair_collection` did.

    When the test file does not exist yet (the baseline build) a stub test is written there for
    the probe and removed again, with the directories made for it.
    """
    repo = Path(args[0]) if args else Path.cwd()
    test_path = args[1] if len(args) > 1 else "tests/test_fixedyet_probe.py"
    if not _TEST_PATH_RE.match(test_path) or ".." in test_path.split("/"):
        print("__REPAIR_UNRESOLVED__=bad test path")
        return 0
    target = repo / test_path
    created_dirs: list[Path] = []
    created_file = False
    lines: list[str] = []
    try:
        if not target.exists():
            for parent in reversed(target.parents):
                if repo in parent.parents and not parent.exists():
                    parent.mkdir()
                    created_dirs.append(parent)
            target.write_text(PROBE_STUB, encoding="utf-8")
            created_file = True
        lines = repair_collection(repo, test_path, runner=runner, collect=collect)
    except Exception as exc:  # the repair must never fail the build
        print(f"__REPAIR_CRASHED__={type(exc).__name__}")
    finally:
        if created_file:
            try:
                target.unlink()
            except OSError:
                pass
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                break
    for line in lines:
        print(line)
    return 0


def main(argv: list[str] | None = None, *, runner=run_pip, probe=run_probe) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "extras":
        return main_extras(args[1:], runner=runner)
    if args and args[0] == "repair":
        return main_repair(args[1:], runner=runner)
    repo = Path(args[0]) if args else Path.cwd()
    try:
        sources = plan(repo)
    except Exception as exc:  # a malformed project file must never fail the build
        print(f"__TESTDEPS_CRASHED__=planning: {type(exc).__name__}")
        return 0
    if not sources:
        print("__TESTDEPS_NONE__=1")
        return 0
    groups_ok = pip_supports_groups(runner)
    deadline = time.monotonic() + TOTAL_BUDGET_S
    ignored = 0
    for source in sources:
        ignored += source.ignored
        try:
            outcome = install_source(
                source, repo, groups_ok=groups_ok, runner=runner, deadline=deadline
            )
        except Exception as exc:  # pragma: no cover - install_source does not raise
            print(f"__TESTDEPS_CRASHED__={source.label}: {type(exc).__name__}")
            continue
        for line in outcome.log.strip().splitlines():
            print(f"| {line}")  # prefixed, so pip's own text can never forge a marker
        for line in markers(source, outcome):
            print(line)
    try:
        for line in repair_known_breaks(runner, probe):
            for part in line.splitlines():
                print(f"| {part}")
    except Exception as exc:  # pragma: no cover - the repair must never fail the build
        print(f"__TESTDEPS_CRASHED__=repair: {type(exc).__name__}")
    if ignored:
        print(f"__TESTDEPS_IGNORED__={ignored}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
