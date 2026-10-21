"""Build the baseline checkpoint for a repository and run the reproduction test on one side.

`build_baseline` clones the repository inside a sandbox, checks out the reported ref, installs
it in editable mode and saves the result as a checkpoint image. `run_test_on_side` forks that
checkpoint (every sandbox run starts from the image it is given), optionally moves to another
ref or applies a patch, writes the test, and runs pytest several times in ONE sandbox run.

Shell scripts are built by small pure functions so tests can assert on the exact commands.
"""

from __future__ import annotations

import inspect
import logging
import re
import shlex
import time
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from fixedyet import testdeps, verdict
from fixedyet.models import ExecResult, Outcome, ProgressCallback, ProgressEvent, SideRun
from fixedyet.sandbox.base import Sandbox, SandboxError

logger = logging.getLogger(__name__)

REPO_DIR = "/work/repo"
INSTALL_CHAIN = "pip install -e '.[test]' || pip install -e '.[tests]' || pip install -e ."
LOG_EXCERPT_CHARS = 3000
RUN_LOG_TAIL_BYTES = 8000
BUILD_TIMEOUT_S = 1800
TEST_TIMEOUT_S = 900
GITHUB_API = "https://api.github.com"

_TEST_FILE_STAGING = "/tmp/ir_test_file"  # noqa: S108 - inside the sandbox, not on the host
_PATCH_STAGING = "/tmp/ir_patch.diff"  # noqa: S108
_PREFIX_LINES = "awk '{ print \"| \" $0 }'"
"""Prefixes every line, and always ends the last one with a newline (sed would not)."""
_HELPER_LOG_ONLY = "grep '^| '"
"""The sandbox helper prints machine markers and ``| `` log lines; a side run keeps the latter."""

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./+@-]*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_TEST_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_STEP_RE = re.compile(r"^__STEP__=(\w+)$", re.MULTILINE)
_RC_RE = re.compile(r"^__RC__=(-?\d+)$", re.MULTILINE)
_SETUP_FAILED_RE = re.compile(r"^__SETUP_FAILED__=(\w+)$", re.MULTILINE)
_LINE_PREFIX_RE = re.compile(r"^\| ?", re.MULTILINE)
_RUN_START_RE = re.compile(r"^__RUN_START__=\d+$", re.MULTILINE)
_HEAD_RE = re.compile(r"^__HEAD__=([0-9a-f]{40})$", re.MULTILINE)
_HEAD_LINE_RE = re.compile(r"^__HEAD__=.*\n?", re.MULTILINE)
_EXTRA_OK_RE = re.compile(r"^__EXTRA_OK__=(\S+)$", re.MULTILINE)
_EXTRA_FAILED_RE = re.compile(r"^__EXTRA_FAILED__=(\S+)$", re.MULTILINE)
_PROJECT_EXTRA_RE = {
    name: re.compile(rf"^__PROJECT_EXTRA_{name}__=(\S+)$", re.MULTILINE)
    for name in ("OK", "FAILED", "UNKNOWN", "HINT")
}
_REPAIR_RE = {
    name: re.compile(rf"^__REPAIR_{name}__=(.+)$", re.MULTILINE)
    for name in ("OK", "FAILED", "UNRESOLVED")
}
_TESTDEPS_RE = {
    name: re.compile(rf"^__TESTDEPS_{name}__=(.+)$", re.MULTILINE)
    for name in ("OK", "PARTIAL", "FAILED", "MISSING", "IGNORED", "CRASHED")
}

PYTEST_SUMMARY_PATTERN = (
    "[0-9]+ (failed|passed|errors?|skipped|xfailed|xpassed|deselected)|^FAILED "
)
NO_SUMMARY_EXIT_CODE = 70  # recorded instead of pytest's exit 1 when no summary line was printed

TESTER = "fixedyet"
TESTER_HOME = f"/home/{TESTER}"
TESTER_FUNCTIONS = f"""\
ir_has_tester() {{ [ "$(id -u)" = 0 ] && id -u {TESTER} >/dev/null 2>&1; }}
ir_own() {{
  if ir_has_tester; then find /work -xdev ! -user {TESTER} -exec chown -h {TESTER}:{TESTER} {{}} + \
    > /dev/null 2>&1; fi
  return 0
}}
ir_as_tester() {{
  if ! ir_has_tester; then "$@"; return; fi
  if command -v setpriv > /dev/null 2>&1; then
    setpriv --reuid="$(id -u {TESTER})" --regid="$(id -g {TESTER})" --clear-groups \
      env HOME={TESTER_HOME} USER={TESTER} LOGNAME={TESTER} "$@"
  else
    python -c 'import os, pwd, sys
p = pwd.getpwnam("{TESTER}")
os.setgroups([]); os.setgid(p.pw_gid); os.setuid(p.pw_uid)
os.environ.update(HOME=p.pw_dir, USER=p.pw_name, LOGNAME=p.pw_name)
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
  fi
}}"""
"""Shell functions for the scripts that run the project's tests. The baseline creates the
unprivileged user `TESTER` and gives it /work; setup steps (clone, checkout, pip) stay root, and
the tests run as that user, so a test that expects a read-only file to be unwritable passes the
way it does on a developer's machine. ``ir_own`` hands files a root step created back to the
user (only those: no copy of the whole tree), ``ir_as_tester CMD...`` runs CMD as the user. On a
checkpoint built before the user existed both fall back to running as root."""

TESTER_SETUP = (
    f"(id -u {TESTER} > /dev/null 2>&1 || useradd -m -d {TESTER_HOME} -s /bin/bash {TESTER} "
    f"|| adduser -D -h {TESTER_HOME} {TESTER}) > /dev/null 2>&1 "
    "&& git config --system --add safe.directory '*' "
    f"&& chown -R {TESTER}:{TESTER} /work && echo __TESTER_OK__={TESTER} "
    "|| echo __TESTER_FAILED__=setup"
)
"""The baseline's last step: the test user, /work owned by it, and git told that root may work in
a repository it does not own (git refuses "dubious ownership" otherwise). Never fatal."""

TESTDEPS_PATH = "/tmp/ir_testdeps.py"  # noqa: S108 - inside the sandbox, not on the host
PROBE_TEST_PATH = "tests/test_fixedyet_probe.py"
"""The stub the baseline's repair step collects: it lives in the directory the real test will."""


class EnvBuildError(RuntimeError):
    """The baseline environment could not be built (or a ref could not be resolved)."""

    def __init__(self, message: str, result: ExecResult | None = None) -> None:
        super().__init__(message)
        self.result = result


# --------------------------------------------------------------------------- progress


async def _emit(progress: ProgressCallback | None, message: str) -> None:
    """Send an ``environment`` progress event; the callback may be sync or async."""
    if progress is None:
        return
    try:
        outcome = progress(ProgressEvent(stage="environment", message=message))
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:  # a broken progress sink must never abort a build
        logger.warning("progress callback raised", exc_info=True)


# --------------------------------------------------------------------------- baseline


def _check_repo(repo: str) -> str:
    if not _REPO_RE.match(repo):
        raise EnvBuildError(f"invalid repository {repo!r}; expected 'owner/name'")
    return repo


def _check_ref(ref: str) -> str:
    if not _REF_RE.match(ref) or ".." in ref:
        raise EnvBuildError(f"invalid git ref {ref!r}")
    return ref


# One requirement as written on a `pip install` line: a name, optional extras, optional version
# pins. URLs, paths, VCS references and pip options never match, so an issue cannot make the
# baseline install anything but a named release from the package index.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:(?:==|>=|<=|~=|!=|<|>)[A-Za-z0-9.*+!_-]+(?:,(?:==|>=|<=|~=|!=|<|>)[A-Za-z0-9.*+!_-]+)*)?$"
)
_PIP_QUIET_FLAGS = {"-U", "--upgrade", "-q", "--quiet"}
MAX_EXTRA_PACKAGES = 5


def _dist_name(requirement: str) -> str:
    """The normalised distribution name of a requirement (PEP 503)."""
    name = re.split(r"[\[<>=!~]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def extra_packages_from_steps(
    setup_steps: Iterable[str], *, exclude: Iterable[str] = ()
) -> list[str]:
    """The packages a reporter's ``pip install ...`` setup steps ask for, and nothing else.

    Only a step that is exactly ``pip install`` (or ``python -m pip install``) followed by plain
    requirements counts; a step with any other option, URL, path, pipe or ``&&`` is ignored whole.
    ``exclude`` names distributions that must not be installed again, above all the project under
    test: installing its release from the index would replace the checkout.
    """
    banned = {_dist_name(name) for name in exclude if name}
    found: list[str] = []
    for step in setup_steps:
        try:
            tokens = shlex.split(step)
        except ValueError:
            continue
        if tokens[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]):
            tokens = tokens[2:]
        if tokens[:1] in (["pip"], ["pip3"]) and tokens[1:2] == ["install"]:
            candidates = [t for t in tokens[2:] if t not in _PIP_QUIET_FLAGS]
        else:
            continue
        if not candidates or not all(_REQUIREMENT_RE.match(c) for c in candidates):
            continue
        for requirement in candidates:
            if _dist_name(requirement) in banned or requirement in found:
                continue
            found.append(requirement)
    return found[:MAX_EXTRA_PACKAGES]


# `python-dotenv[cli]`, `"jinja2[i18n]"`, `requests[socks,security]`: the project's own name
# followed by extras. A bracket without a letter in it (`dotenv[0]`) is an index, not an extra.
_PROJECT_EXTRAS_RE = re.compile(
    r"(?<![A-Za-z0-9._-])(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\[(?P<extras>(?=[^\]]*[A-Za-z])[A-Za-z0-9._-]+(?:\s*,\s*[A-Za-z0-9._-]+)*)\]"
)
_EXTRA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_PROJECT_EXTRAS = 5


def project_extras_from_text(texts: Iterable[str], names: Iterable[str]) -> list[str]:
    """The optional extras of the project itself that ``texts`` ask for, in order of appearance.

    ``pip install "python-dotenv[cli]"`` and "with `python-dotenv[cli]` installed" both name the
    extra ``cli`` of the project. ``names`` are the project's names (repository, distribution);
    an extra of any other package is ignored here (`extra_packages_from_steps` handles those).
    The sandbox installs only the extras the project declares, so a wrong guess costs nothing.
    """
    wanted = {_dist_name(name) for name in names if name}
    found: list[str] = []
    for text in texts:
        for match in _PROJECT_EXTRAS_RE.finditer(text):
            if _dist_name(match["name"]) not in wanted:
                continue
            for extra in re.split(r"\s*,\s*", match["extras"]):
                if extra not in found:
                    found.append(extra)
    return found[:MAX_PROJECT_EXTRAS]


def _extras_arg(extras: Sequence[str]) -> str:
    """The comma list the sandbox helper takes, quoted; the empty string for no extras."""
    for extra in extras:
        if not _EXTRA_NAME_RE.match(extra):
            raise EnvBuildError(f"invalid extra {extra!r}")
    return shlex.quote(",".join(extras)) if extras else ""


def _extras_lines(extras: Sequence[str]) -> list[str]:
    """Install each extra on its own, so one bad name cannot hide the others. Never fatal."""
    lines: list[str] = []
    for requirement in extras:
        if not _REQUIREMENT_RE.match(requirement):
            raise EnvBuildError(f"invalid requirement {requirement!r}")
        ok = shlex.quote(f"__EXTRA_OK__={requirement}")
        failed = shlex.quote(f"__EXTRA_FAILED__={requirement}")
        lines.append(
            f"pip install {shlex.quote(requirement)} > /tmp/ir_extra.log 2>&1 "
            f"&& echo {ok} || echo {failed}"
        )
    return lines


def installed_extras(result: ExecResult) -> tuple[list[str], list[str]]:
    """``(installed, failed)`` requirement names reported by the baseline script."""
    return _EXTRA_OK_RE.findall(result.stdout), _EXTRA_FAILED_RE.findall(result.stdout)


def installed_project_extras(result: ExecResult) -> tuple[list[str], list[str]]:
    """``(installed, failed)`` extras of the project itself, as the baseline script reports them."""
    return (
        _PROJECT_EXTRA_RE["OK"].findall(result.stdout),
        _PROJECT_EXTRA_RE["FAILED"].findall(result.stdout),
    )


def hinted_project_extras(result: ExecResult) -> list[str]:
    """Extras that no one asked for but the project's own console script asked for."""
    return _PROJECT_EXTRA_RE["HINT"].findall(result.stdout)


@dataclass
class RepairReport:
    """What the repair step did so that pytest can load the project's conftest (`testdeps.py`)."""

    installed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.installed or self.failed or self.unresolved)

    def describe(self) -> str:
        parts = []
        if self.installed:
            parts.append(f"installed {', '.join(self.installed)}")
        if self.failed:
            parts.append(f"could not install {', '.join(self.failed)}")
        if self.unresolved:
            parts.append(f"pytest still cannot collect a test here ({self.unresolved[-1]})")
        return "; ".join(parts)


def repairs_made(result: ExecResult) -> RepairReport:
    """The `RepairReport` read from the baseline script's markers (empty when none apply)."""
    return RepairReport(
        installed=[v.strip() for v in _REPAIR_RE["OK"].findall(result.stdout)],
        failed=[v.strip() for v in _REPAIR_RE["FAILED"].findall(result.stdout)],
        unresolved=[v.strip() for v in _REPAIR_RE["UNRESOLVED"].findall(result.stdout)],
    )


@dataclass
class DepsReport:
    """What the test-dependency step of the baseline script says it did (`testdeps.py`)."""

    installed: list[str] = field(default_factory=list)  # sources fully installed
    partial: list[str] = field(default_factory=list)  # sources with some requirements missing
    failed: list[str] = field(default_factory=list)  # sources nothing could be installed from
    missing: list[str] = field(default_factory=list)  # requirements that did not install
    ignored: int = 0  # entries refused because they were not plain index requirements
    crashed: bool = False

    def __bool__(self) -> bool:
        return bool(self.installed or self.partial or self.failed or self.ignored or self.crashed)

    def describe(self) -> str:
        """One progress line: 'installed group:test; could not install foo, bar; ...'."""
        parts = []
        if self.installed or self.partial:
            parts.append(f"installed {', '.join(self.installed + self.partial)}")
        if self.failed:
            parts.append(f"nothing installed from {', '.join(self.failed)}")
        if self.missing:
            shown = ", ".join(self.missing[:6]) + (", ..." if len(self.missing) > 6 else "")
            parts.append(f"could not install {shown}")
        if self.ignored:
            parts.append(f"ignored {self.ignored} entries that are not plain requirements")
        if self.crashed:
            parts.append("the step itself failed")
        return "; ".join(parts)


def tester_ready(result: ExecResult) -> bool:
    """True when the baseline created the unprivileged test user (`TESTER_SETUP`)."""
    return f"__TESTER_OK__={TESTER}" in result.stdout


def installed_test_deps(result: ExecResult) -> DepsReport:
    """The `DepsReport` read from the baseline script's markers (empty when none apply)."""

    def lines(name: str) -> list[str]:
        return [value.strip() for value in _TESTDEPS_RE[name].findall(result.stdout)]

    ignored = [int(v) for v in lines("IGNORED") if v.isdigit()]
    return DepsReport(
        installed=lines("OK"),
        partial=lines("PARTIAL"),
        failed=lines("FAILED"),
        missing=lines("MISSING"),
        ignored=ignored[-1] if ignored else 0,
        crashed=bool(lines("CRASHED")),
    )


def helper_source() -> str:
    """The text of `fixedyet.testdeps`, which the baseline script runs inside the sandbox."""
    return Path(testdeps.__file__).read_text(encoding="utf-8")


def baseline_script(
    repo: str,
    ref: str,
    extras: Sequence[str] = (),
    project_extras: Sequence[str] = (),
) -> str:
    """Shell script that clones ``repo``, checks out ``ref`` and installs it with pytest.

    After the editable install it runs `fixedyet.testdeps` (shipped to `TESTDEPS_PATH` by
    `build_baseline`): the project's PEP 735 test dependency groups and test requirements files,
    then ``project_extras`` (optional extras of the project itself that the issue names; the
    helper adds the ones the project's console script asks for), then ``extras`` (further
    requirements, see `extra_packages_from_steps`), and last a repair step that makes sure pytest
    can load the project's conftest. A failure of any of these is reported on stdout and does not
    fail the build.
    """
    url = shlex.quote(f"https://github.com/{_check_repo(repo)}")
    quoted_ref = shlex.quote(_check_ref(ref))
    repo_dir = shlex.quote(REPO_DIR)
    extra_lines = _extras_lines(extras)
    project_extras_arg = _extras_arg(project_extras)
    if extra_lines:
        extra_lines = ["echo __STEP__=extras", *extra_lines]
    return "\n".join(
        [
            "set -u",
            "export GIT_TERMINAL_PROMPT=0 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1",
            "export DEBIAN_FRONTEND=noninteractive",
            "echo __STEP__=clone",
            f"git clone --filter=blob:none {url} {repo_dir} || exit 10",
            f"cd {repo_dir} || exit 10",
            "echo __STEP__=checkout",
            f"git checkout --quiet {quoted_ref} || exit 11",
            "echo __STEP__=install",
            f"({INSTALL_CHAIN}) || exit 12",
            "echo __STEP__=pytest",
            "pip install pytest || exit 13",
            "echo __STEP__=testdeps",
            f"python {TESTDEPS_PATH} {repo_dir} || echo __TESTDEPS_CRASHED__=exit",
            "echo __STEP__=project_extras",
            f"python {TESTDEPS_PATH} extras {repo_dir} {project_extras_arg} "
            "|| echo __PROJECT_EXTRA_CRASHED__=exit",
            *extra_lines,
            "echo __STEP__=repair",
            f"python {TESTDEPS_PATH} repair {repo_dir} {PROBE_TEST_PATH} "
            "|| echo __REPAIR_CRASHED__=exit",
            "echo __STEP__=tester",
            TESTER_SETUP,
            "echo __STEP__=sanity",
            'python -c "import sys; print(sys.version)" || exit 14',
            "pip freeze | head -n 30",
            "echo __STEP__=done",
            "",
        ]
    )


def _short(image: str) -> str:
    """An image reference for a progress line: a hex id or UUID is cut to 12 characters."""
    return image[:12] if re.fullmatch(r"(?:sha256:)?[0-9a-f-]{20,}", image) else image


def _failure_step(stdout: str) -> str:
    steps = _STEP_RE.findall(stdout)
    return steps[-1] if steps else "start"


async def build_baseline(
    sb: Sandbox,
    *,
    repo: str,
    ref: str,
    python_version: str | None,
    progress: ProgressCallback | None = None,
    timeout_s: int = BUILD_TIMEOUT_S,
    extra_packages: Sequence[str] = (),
    project_extras: Sequence[str] = (),
) -> tuple[str, ExecResult]:
    """Build and checkpoint the environment for ``repo`` at ``ref``.

    Clones github.com/<repo> into `REPO_DIR`, checks out ``ref``, installs it editable with
    test extras when available, installs pytest, the project's test dependency groups and
    requirements files (`installed_test_deps` reads back what that did), ``project_extras`` (the
    project's own optional extras, plus those its console script asks for;
    `installed_project_extras` reads back which worked) and ``extra_packages``
    (`installed_extras` reads back which of them worked), repairs what stops pytest from loading
    the project's conftest (`repairs_made`), runs a sanity step, and saves the filesystem.
    Returns ``(checkpoint_image, setup_result)``. Raises `EnvBuildError` on any failure.
    """
    # validates the inputs before any call
    script = baseline_script(repo, ref, extra_packages, project_extras)
    started = time.monotonic()
    py = python_version or "default"
    await _emit(progress, f"Building baseline for {repo} at {ref} (Python {py})")
    try:
        base = await sb.base_image(python_version)
    except SandboxError as exc:
        await _emit(progress, f"Could not get a base image: {exc}")
        raise EnvBuildError(f"no base image for Python {python_version}: {exc}") from exc
    await _emit(progress, f"Base image ready ({_short(base)}); cloning, installing, checkpointing")
    try:
        result = await sb.run(
            base, script, files={TESTDEPS_PATH: helper_source()}, workdir="/work",
            timeout_s=timeout_s, keep=True, network=True,
        )
    except SandboxError as exc:
        await _emit(progress, f"Sandbox failed during baseline build: {exc}")
        raise EnvBuildError(f"sandbox error while building baseline: {exc}") from exc

    step = _failure_step(result.stdout)
    if result.timed_out:
        await _emit(progress, f"Baseline build timed out during step '{step}'")
        raise EnvBuildError(f"baseline build timed out after {timeout_s}s during '{step}'", result)
    if result.exit_code != 0:
        tail = (result.stdout[-900:] + "\n" + result.stderr[-900:]).strip()
        await _emit(progress, f"Baseline build failed at step '{step}' (exit {result.exit_code})")
        raise EnvBuildError(
            f"baseline build failed at step '{step}' (exit {result.exit_code}): {tail}", result
        )
    if not result.image:
        await _emit(progress, "Baseline built but the sandbox returned no checkpoint image")
        raise EnvBuildError("sandbox returned no checkpoint image for the baseline", result)
    deps = installed_test_deps(result)
    if deps:
        await _emit(progress, f"Test dependencies of the project: {deps.describe()}")
    installed, failed = installed_extras(result)
    if installed or failed:
        note = f"installed {', '.join(installed) or 'none'}"
        note += f"; could not install {', '.join(failed)}" if failed else ""
        await _emit(progress, f"Extra packages named in the issue: {note}")
    extras_ok, extras_failed = installed_project_extras(result)
    if extras_ok or extras_failed:
        note = f"installed {', '.join(extras_ok) or 'none'}"
        note += f"; could not install {', '.join(extras_failed)}" if extras_failed else ""
        hinted = hinted_project_extras(result)
        note += f" (the project's own script asked for {', '.join(hinted)})" if hinted else ""
        await _emit(progress, f"Optional extras of {repo}: {note}")
    repaired = repairs_made(result)
    if repaired:
        await _emit(progress, f"Test environment repair: {repaired.describe()}")
    if not tester_ready(result):
        await _emit(progress, "Could not create the unprivileged test user; tests run as root")
    await _emit(
        progress,
        f"Baseline ready in {time.monotonic() - started:.0f}s, checkpoint {_short(result.image)}",
    )
    return result.image, result


# --------------------------------------------------------------------------- ref lookup


async def resolve_ref_for_version(
    repo: str,
    version: str,
    *,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Find the git tag for a released ``version``: tries ``v{version}``, ``{version}``,
    ``release-{version}`` in that order against the GitHub API (exact tag lookup).

    Returns the tag name, or None when none exists. Raises `EnvBuildError` when GitHub cannot
    answer (rate limit, network), so "no such tag" is never confused with "could not ask".
    ``token`` is an optional GitHub token for the rate limit; ``client`` lets tests inject a
    mock transport.
    """
    _check_repo(repo)
    if not _VERSION_RE.match(version):
        raise EnvBuildError(f"invalid version {version!r}")
    bare = version[1:] if re.match(r"^v\d", version) else version
    candidates = [f"v{bare}", bare, f"release-{bare}"]
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    owns_client = client is None
    http = client or httpx.AsyncClient(base_url=GITHUB_API, timeout=20.0)
    try:
        for tag in candidates:
            url = f"{GITHUB_API}/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='')}"
            try:
                response = await http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise EnvBuildError(f"GitHub API request failed for {repo}: {exc}") from exc
            if response.status_code == 200:
                return tag
            if response.status_code == 404:
                continue
            hint = " (rate limited? set GITHUB_TOKEN)" if response.status_code in (403, 429) else ""
            raise EnvBuildError(
                f"GitHub API returned {response.status_code} while looking up {tag} in {repo}{hint}"
            )
    finally:
        if owns_client:
            await http.aclose()
    return None


# --------------------------------------------------------------------------- one side


def _check_test_path(test_path: str) -> str:
    segments = test_path.split("/")
    if not _TEST_PATH_RE.match(test_path) or ".." in segments or "" in segments:
        raise EnvBuildError(f"invalid test path {test_path!r}; use a relative path inside the repo")
    return test_path


def side_label(checkout: str | None, patch: str | None) -> str:
    """The `SideRun.ref` for a side: the checkout, ``gold_patch``, both, or ``HEAD``."""
    if checkout and patch:
        return f"{checkout}+gold_patch"
    if checkout:
        return checkout
    if patch:
        return "gold_patch"
    return "HEAD"


def side_script(
    *,
    test_path: str,
    checkout: str | None,
    patch: bool,
    runs: int,
    per_run_timeout_s: int,
    project_extras: Sequence[str] = (),
) -> str:
    """Shell script: optional checkout and patch, reinstall, write the test, run pytest N times.

    Another ref has its own test requirements and its own conftest, so after a checkout the
    sandbox helper (`TESTDEPS_PATH`, shipped by `run_test_on_side`) installs ``project_extras``
    again and, with the test file in place, repairs whatever stops pytest from loading the
    conftest of that ref (a module the baseline never needed, a package pinned at a version this
    interpreter cannot import). Only the helper's ``| `` log lines are echoed, never its markers.

    Each pytest run writes to its own log; only the last bytes of it are echoed, each line
    prefixed with ``| `` so test output can never forge a ``__RC__=`` marker line. The setup
    steps run as root; the test runs as the unprivileged `TESTER` (see `TESTER_FUNCTIONS`).
    """
    path = shlex.quote(_check_test_path(test_path))
    repo_dir = shlex.quote(REPO_DIR)
    extras_arg = _extras_arg(project_extras)
    lines = [
        "set -u",
        "export GIT_TERMINAL_PROMPT=0 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1",
        TESTER_FUNCTIONS,
        f"cd {shlex.quote(REPO_DIR)} || {{ echo __SETUP_FAILED__=cd; exit 20; }}",
    ]
    if checkout:
        ref = shlex.quote(_check_ref(checkout))
        lines.append(
            f"git fetch --quiet origin {ref} && git checkout --quiet --force FETCH_HEAD "
            "|| { echo __SETUP_FAILED__=checkout; exit 21; }"
        )
    if patch:
        lines.append(
            f"git apply --whitespace=nowarn {_PATCH_STAGING} "
            "|| { echo __SETUP_FAILED__=patch; exit 23; }"
        )
    if checkout or patch:
        lines.append(
            f"({INSTALL_CHAIN}) > /tmp/ir_reinstall.log 2>&1 "
            "|| { echo __SETUP_FAILED__=reinstall; "
            f"tail -n 40 /tmp/ir_reinstall.log | {_PREFIX_LINES}; exit 22; }}"
        )
    if checkout:
        lines.append(
            f"python {TESTDEPS_PATH} extras {repo_dir} {extras_arg} 2>&1 | {_HELPER_LOG_ONLY}"
        )
    lines.append('echo "__HEAD__=$(git rev-parse HEAD 2>/dev/null)"')  # the commit it runs on
    lines.append(
        f'mkdir -p "$(dirname {path})" && cp {_TEST_FILE_STAGING} {path} '
        "|| { echo __SETUP_FAILED__=write_test; exit 24; }"
    )
    if checkout:
        lines.append(
            f"python {TESTDEPS_PATH} repair {repo_dir} {path} 2>&1 | {_HELPER_LOG_ONLY}"
        )
    lines += [
        "ir_own",
        f"for i in $(seq 1 {runs}); do",
        '  echo "__RUN_START__=$i"',
        f"  ir_as_tester timeout -k 5 {per_run_timeout_s} python -m pytest -x -q "
        f'-p no:cacheprovider {path} > "/tmp/ir_run_$i.log" 2>&1',
        "  rc=$?",
        # pytest exits 1 for a failed test, but also when it dies with a Python traceback before
        # it ran anything (a pytest plugin that cannot be imported, for one). Only a run that
        # printed a pytest summary line really ran tests; any other exit 1 is an error.
        f'  if [ "$rc" -eq 1 ] && ! grep -qE "{PYTEST_SUMMARY_PATTERN}" "/tmp/ir_run_$i.log"; '
        f"then rc={NO_SUMMARY_EXIT_CODE}; fi",
        f'  tail -c {RUN_LOG_TAIL_BYTES} "/tmp/ir_run_$i.log" | {_PREFIX_LINES}',
        '  echo "__RC__=$rc"',
        "done",
        "exit 0",
        "",
    ]
    return "\n".join(lines)


def map_exit_code(code: int) -> Outcome:
    """pytest exit code to outcome: 0 PASS, 1 FAIL, anything else ERROR (collection error...).

    The side script turns an exit 1 without a pytest summary line (pytest crashed) into 70.
    """
    if code == 0:
        return Outcome.PASS
    if code == 1:
        return Outcome.FAIL
    return Outcome.ERROR


def parse_exit_codes(stdout: str) -> list[int]:
    """All ``__RC__=<n>`` marker values in order of appearance."""
    return [int(m) for m in _RC_RE.findall(stdout)]


def clean_log(text: str) -> str:
    """Remove the ``| `` line prefix that protects marker lines."""
    return _LINE_PREFIX_RE.sub("", text)


def _run_sections(stdout: str) -> tuple[str, list[tuple[int | None, str]]]:
    """Split the raw script output into the setup text and one ``(exit code, text)`` per run."""
    chunks = _RUN_START_RE.split(_HEAD_LINE_RE.sub("", stdout, count=1))
    sections: list[tuple[int | None, str]] = []
    for chunk in chunks[1:]:
        codes = _RC_RE.findall(chunk)
        body = _RC_RE.sub("", chunk)
        sections.append((int(codes[-1]) if codes else None, clean_log(body).strip("\n")))
    return clean_log(chunks[0]).strip("\n"), sections


def _tail(text: str, room: int) -> str:
    """At most ``room`` characters from the end of ``text``, starting on a line boundary."""
    if len(text) <= room:
        return text
    marker = "[...]\n"
    cut = text[-max(room - len(marker), 0) :] if room > len(marker) else ""
    newline = cut.find("\n")
    return marker + (cut[newline + 1 :] if 0 <= newline < len(cut) - 1 else cut)


def format_log(stdout: str, limit: int = LOG_EXCERPT_CHARS) -> str:
    """The reader-facing log of a side: no harness markers, the end of each run that matters.

    When every run exited alike only the last one is shown (they print the same thing); when they
    differ, each run gets an equal share so a flaky test can be seen for what it is.
    """
    setup, sections = _run_sections(stdout)
    if not sections:
        return _tail(setup, limit)
    codes = [code for code, _ in sections]
    if len(sections) > 1 and len(set(codes)) == 1:
        shown = [(f"last of {len(sections)} runs, every run exited {codes[0]}", sections[-1][1])]
    else:
        shown = [
            (f"run {i}: exit code {code if code is not None else 'missing'}", body)
            for i, (code, body) in enumerate(sections, 1)
        ]
    setup_room = min(len(setup), limit // 4)
    headers = sum(len(title) + 12 for title, _ in shown)
    room = max(200, (limit - setup_room - headers) // len(shown))
    parts = [_tail(setup, setup_room)] if setup else []
    parts += [f"--- {title} ---\n{_tail(body, room)}" for title, body in shown]
    return "\n".join(parts)[-limit:]


def build_side_run(result: ExecResult, *, ref: str, image: str, runs: int) -> SideRun:
    """Turn the raw sandbox result of `side_script` into a `SideRun`."""
    setup_failed = _SETUP_FAILED_RE.search(result.stdout)
    if setup_failed:
        outcomes = [Outcome.ERROR] * runs
    else:
        codes = parse_exit_codes(result.stdout)[:runs]
        outcomes = [map_exit_code(c) for c in codes]
        outcomes += [Outcome.ERROR] * (runs - len(outcomes))
    text = format_log(result.stdout)
    if result.stderr.strip():
        text = f"{text}\n--- stderr ---\n{result.stderr}"
    if result.timed_out:
        text = f"{text}\n[sandbox run timed out]"
    head = _HEAD_RE.search(result.stdout.split("__RUN_START__=", 1)[0])
    _, sections = _run_sections(result.stdout)
    return SideRun(
        ref=ref,
        outcomes=outcomes,
        checkpoint_image=image,
        log_excerpt=text[-LOG_EXCERPT_CHARS:],
        commit=head.group(1) if head else None,
        # the last run's whole log (its last RUN_LOG_TAIL_BYTES), not the excerpt above
        failure_detail=verdict.failure_lines(sections[-1][1]) if sections else None,
    )


async def run_test_on_side(
    sb: Sandbox,
    image: str,
    *,
    test_path: str,
    test_code: str,
    checkout: str | None = None,
    patch: str | None = None,
    runs: int = 2,
    ref: str | None = None,
    timeout_s: int = TEST_TIMEOUT_S,
    network: bool = True,
    project_extras: Sequence[str] = (),
) -> SideRun:
    """Run the reproduction test ``runs`` times on one side, in ONE sandbox run from ``image``.

    ``checkout`` fetches and checks out another ref first; ``patch`` (a unified diff) is applied
    with ``git apply``; either one triggers a reinstall. ``ref`` overrides the label stored in
    `SideRun.ref` (default: the checkout, ``gold_patch``, or ``HEAD``). Exit codes map 0 to PASS,
    1 to FAIL, anything else to ERROR. A setup failure or a sandbox timeout yields ERROR outcomes
    rather than an exception; only a broken sandbox (`SandboxError`) propagates.

    After a ``checkout`` the sandbox helper also installs ``project_extras`` again and repairs a
    conftest that cannot load (see `side_script`), which is why it travels with the test file.
    """
    if runs < 1:
        raise EnvBuildError("runs must be at least 1")
    setup_budget = 300 if (checkout or patch) else 30
    per_run = max(30, (timeout_s - setup_budget) // runs)
    script = side_script(
        test_path=test_path,
        checkout=checkout,
        patch=patch is not None,
        runs=runs,
        per_run_timeout_s=per_run,
        project_extras=project_extras,
    )
    files = {_TEST_FILE_STAGING: test_code}
    if checkout:
        files[TESTDEPS_PATH] = helper_source()
    if patch is not None:
        files[_PATCH_STAGING] = patch if patch.endswith("\n") else patch + "\n"
    result = await sb.run(
        image, script, files=files, workdir=REPO_DIR, timeout_s=timeout_s, network=network
    )
    return build_side_run(result, ref=ref or side_label(checkout, patch), image=image, runs=runs)
