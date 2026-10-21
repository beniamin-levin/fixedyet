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
    few spellings of it (``requirements-dev.txt`` and its kin only as the same fallback).

How it installs: ``pip install --group <name>`` when the pip in the image has ``--group`` (pip
25.1 and newer) and every entry of the group is a plain requirement; otherwise the entries are
read with ``tomllib`` here and installed by name, in one command first and one by one when that
fails. A failure is never fatal: the build goes on, and the markers on stdout say what is missing.

One repair follows the installs: if ``python -m pytest --version`` crashes on a known
incompatibility among the packages just installed, the one pin that cures it is installed
(``KNOWN_BREAKS``).

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


def file_candidates(repo: Path) -> tuple[list[str], list[str]]:
    """``(test files, fallback files)`` that exist, as paths relative to the repository."""

    def existing(names: tuple[str, ...]) -> list[str]:
        return [n for n in names if (repo / n).is_file()]

    tests = existing(TEST_FILES)
    directory = repo / TEST_FILE_GLOB_DIR
    if directory.is_dir():
        try:
            names = sorted(p.name for p in directory.iterdir() if p.is_file())
        except OSError:
            names = []
        tests += [f"{TEST_FILE_GLOB_DIR}/{n}" for n in names if _GLOBBED_FILE_RE.match(n)]
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


def main(argv: list[str] | None = None, *, runner=run_pip, probe=run_probe) -> int:
    args = sys.argv[1:] if argv is None else argv
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
