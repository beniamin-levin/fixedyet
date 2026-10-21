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

from fixedyet import testdeps
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

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./+@-]*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_TEST_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_STEP_RE = re.compile(r"^__STEP__=(\w+)$", re.MULTILINE)
_RC_RE = re.compile(r"^__RC__=(-?\d+)$", re.MULTILINE)
_SETUP_FAILED_RE = re.compile(r"^__SETUP_FAILED__=(\w+)$", re.MULTILINE)
_LINE_PREFIX_RE = re.compile(r"^\| ?", re.MULTILINE)
_RUN_START_RE = re.compile(r"^__RUN_START__=\d+$", re.MULTILINE)
_EXTRA_OK_RE = re.compile(r"^__EXTRA_OK__=(\S+)$", re.MULTILINE)
_EXTRA_FAILED_RE = re.compile(r"^__EXTRA_FAILED__=(\S+)$", re.MULTILINE)
_TESTDEPS_RE = {
    name: re.compile(rf"^__TESTDEPS_{name}__=(.+)$", re.MULTILINE)
    for name in ("OK", "PARTIAL", "FAILED", "MISSING", "IGNORED", "CRASHED")
}

PYTEST_SUMMARY_PATTERN = (
    "[0-9]+ (failed|passed|errors?|skipped|xfailed|xpassed|deselected)|^FAILED "
)
NO_SUMMARY_EXIT_CODE = 70  # recorded instead of pytest's exit 1 when no summary line was printed

TESTDEPS_PATH = "/tmp/ir_testdeps.py"  # noqa: S108 - inside the sandbox, not on the host


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


def baseline_script(repo: str, ref: str, extras: Sequence[str] = ()) -> str:
    """Shell script that clones ``repo``, checks out ``ref`` and installs it with pytest.

    After the editable install it runs `fixedyet.testdeps` (shipped to `TESTDEPS_PATH` by
    `build_baseline`): the project's PEP 735 test dependency groups and test requirements files.
    ``extras`` are further requirements (see `extra_packages_from_steps`) installed last; a
    failure to install one of them, or a test dependency, is reported on stdout and does not fail
    the build.
    """
    url = shlex.quote(f"https://github.com/{_check_repo(repo)}")
    quoted_ref = shlex.quote(_check_ref(ref))
    repo_dir = shlex.quote(REPO_DIR)
    extra_lines = _extras_lines(extras)
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
            *extra_lines,
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
) -> tuple[str, ExecResult]:
    """Build and checkpoint the environment for ``repo`` at ``ref``.

    Clones github.com/<repo> into `REPO_DIR`, checks out ``ref``, installs it editable with
    test extras when available, installs pytest, the project's test dependency groups and
    requirements files (`installed_test_deps` reads back what that did) and ``extra_packages``
    (`installed_extras` reads back which of them worked), runs a sanity step, and saves the
    filesystem.
    Returns ``(checkpoint_image, setup_result)``. Raises `EnvBuildError` on any failure.
    """
    script = baseline_script(repo, ref, extra_packages)  # validates the inputs before any call
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
) -> str:
    """Shell script: optional checkout and patch, reinstall, write the test, run pytest N times.

    Each pytest run writes to its own log; only the last bytes of it are echoed, each line
    prefixed with ``| `` so test output can never forge a ``__RC__=`` marker line.
    """
    path = shlex.quote(_check_test_path(test_path))
    lines = [
        "set -u",
        "export GIT_TERMINAL_PROMPT=0 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1",
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
    lines += [
        f'mkdir -p "$(dirname {path})" && cp {_TEST_FILE_STAGING} {path} '
        "|| { echo __SETUP_FAILED__=write_test; exit 24; }",
        f"for i in $(seq 1 {runs}); do",
        '  echo "__RUN_START__=$i"',
        f"  timeout -k 5 {per_run_timeout_s} python -m pytest -x -q -p no:cacheprovider {path}"
        ' > "/tmp/ir_run_$i.log" 2>&1',
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
    chunks = _RUN_START_RE.split(stdout)
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
    return SideRun(
        ref=ref,
        outcomes=outcomes,
        checkpoint_image=image,
        log_excerpt=text[-LOG_EXCERPT_CHARS:],
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
) -> SideRun:
    """Run the reproduction test ``runs`` times on one side, in ONE sandbox run from ``image``.

    ``checkout`` fetches and checks out another ref first; ``patch`` (a unified diff) is applied
    with ``git apply``; either one triggers a reinstall. ``ref`` overrides the label stored in
    `SideRun.ref` (default: the checkout, ``gold_patch``, or ``HEAD``). Exit codes map 0 to PASS,
    1 to FAIL, anything else to ERROR. A setup failure or a sandbox timeout yields ERROR outcomes
    rather than an exception; only a broken sandbox (`SandboxError`) propagates.
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
    )
    files = {_TEST_FILE_STAGING: test_code}
    if patch is not None:
        files[_PATCH_STAGING] = patch if patch.endswith("\n") else patch + "\n"
    result = await sb.run(
        image, script, files=files, workdir=REPO_DIR, timeout_s=timeout_s, network=network
    )
    return build_side_run(result, ref=ref or side_label(checkout, patch), image=image, runs=runs)
