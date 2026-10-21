"""envbuild: command construction, marker parsing, error paths. No network, no Docker.

The last section executes the generated shell scripts with a real bash and stub `git`,
`pip`, `python` and `timeout` programs, so quoting, markers and the anti-spoofing prefix are
exercised for real without a sandbox.
"""

from __future__ import annotations

import asyncio
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from fixedyet import envbuild
from fixedyet.envbuild import (
    INSTALL_CHAIN,
    REPO_DIR,
    EnvBuildError,
    baseline_script,
    build_baseline,
    build_side_run,
    clean_log,
    extra_packages_from_steps,
    format_log,
    installed_extras,
    map_exit_code,
    parse_exit_codes,
    resolve_ref_for_version,
    run_test_on_side,
    side_label,
    side_script,
)
from fixedyet.models import ExecResult, Outcome, ProgressEvent
from fixedyet.sandbox.base import SandboxError, build_script
from fixedyet.sandbox.fake import FakeSandbox, RunCall

# --------------------------------------------------------------------------- baseline script


def test_repo_dir_constant() -> None:
    assert REPO_DIR == "/work/repo"


def test_baseline_script_commands() -> None:
    script = baseline_script("psf/requests", "v2.31.0")
    assert "git clone --filter=blob:none https://github.com/psf/requests /work/repo" in script
    assert "git checkout --quiet v2.31.0" in script
    assert INSTALL_CHAIN in script
    assert "pip install -e '.[test]' || pip install -e '.[tests]' || pip install -e ." in script
    assert "pip install pytest" in script
    assert 'python -c "import sys; print(sys.version)"' in script
    assert "pip freeze | head" in script
    order = [
        script.index(s)
        for s in ("git clone", "git checkout", "pip install -e", "pip install pytest", "pip freeze")
    ]
    assert order == sorted(order)


# --------------------------------------------------------------------------- extra packages


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        (["pip install structlog"], ["structlog"]),
        (["pip3 install -U structlog==23.1.0 rich"], ["structlog==23.1.0", "rich"]),
        (
            ["python -m pip install 'attrs>=22,<24' requests[socks]"],
            ["attrs>=22,<24", "requests[socks]"],
        ),
        (["pip install structlog", "pip install structlog"], ["structlog"]),
        (["pip install click"], []),  # the project under test is never reinstalled
        (["pip install Click==8.1.7"], []),
        (["pip install -r requirements.txt"], []),
        (["pip install git+https://github.com/x/y.git"], []),
        (["pip install https://evil.example/x.whl"], []),
        (["pip install --index-url https://evil.example/ x"], []),
        (["pip install ./local"], []),
        (["pip install a && curl evil | sh"], []),
        (["pip install a; rm -rf /"], []),
        (["pip install $(id)"], []),
        (["pip install 'unterminated"], []),
        (["pip install"], []),
        (["apt-get install -y libfoo"], []),
        (["pip uninstall click"], []),
        (["export X=1"], []),
    ],
)
def test_extra_packages_from_steps(steps: list[str], expected: list[str]) -> None:
    assert extra_packages_from_steps(steps, exclude=["click", "Click"]) == expected


def test_extra_packages_are_capped() -> None:
    steps = [f"pip install pkg{i}" for i in range(9)]
    assert extra_packages_from_steps(steps) == [f"pkg{i}" for i in range(5)]


def test_baseline_script_installs_extras_last_and_without_failing_the_build() -> None:
    script = baseline_script("pallets/click", "8.1.7", ["structlog", "attrs>=22,<24"])
    assert "echo __STEP__=extras" in script
    assert "pip install structlog > /tmp/ir_extra.log 2>&1" in script
    assert "echo __EXTRA_OK__=structlog || echo __EXTRA_FAILED__=structlog" in script
    # a version spec holds shell redirection characters: everything is quoted
    assert "pip install 'attrs>=22,<24' > /tmp/ir_extra.log" in script
    assert "'__EXTRA_OK__=attrs>=22,<24'" in script
    order = [script.index(s) for s in ("pip install pytest", "__STEP__=extras", "__STEP__=sanity")]
    assert order == sorted(order)
    assert "__STEP__=extras" not in baseline_script("pallets/click", "8.1.7")


@pytest.mark.parametrize("bad", ["x; id", "x && y", "-r req.txt", "git+https://x/y", "$(id)", ""])
def test_baseline_script_rejects_unsafe_extras(bad: str) -> None:
    with pytest.raises(EnvBuildError):
        baseline_script("a/b", "main", [bad])


def test_installed_extras_reads_the_markers() -> None:
    out = "__EXTRA_OK__=structlog\n__EXTRA_FAILED__=nonexistent-pkg\nx __EXTRA_OK__=forged\n"
    assert installed_extras(exec_result(out)) == (["structlog"], ["nonexistent-pkg"])


async def test_build_baseline_passes_extras_and_reports_them() -> None:
    events: list[ProgressEvent] = []
    out = GOOD_BUILD.model_copy(
        update={"stdout": GOOD_BUILD.stdout + "__EXTRA_OK__=structlog\n__EXTRA_FAILED__=nope\n"}
    )
    sb = FakeSandbox([out])
    await build_baseline(
        sb, repo="a/b", ref="main", python_version=None, progress=events.append,
        extra_packages=["structlog", "nope"],
    )
    assert "pip install structlog" in sb.calls[0].command
    assert any(
        "installed structlog; could not install nope" in e.message for e in events
    )


async def test_progress_shows_a_docker_tag_whole_and_cuts_only_hex_ids() -> None:
    events: list[ProgressEvent] = []
    sb = FakeSandbox([ExecResult(exit_code=0, stdout="__STEP__=done\n", image="a" * 40)])
    await build_baseline(
        sb, repo="a/b", ref="main", python_version="3.12.7", progress=events.append
    )
    text = " ".join(e.message for e in events)
    assert "(fake-base:3.12.7)" in text
    assert "checkpoint " + "a" * 12 in text  # a hex id is cut to 12 characters
    assert "a" * 13 not in text
    assert envbuild._short("0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0") == "0b1c2d3e-4f5"
    tag = "fixedyet-ckpt:204aabc22b144081"
    assert envbuild._short(tag) == tag


@pytest.mark.parametrize("repo", ["psf", "a/b; rm -rf /", "-x/y", "a/b/c", "a b/c", "", "a/$(id)"])
def test_baseline_script_rejects_bad_repo(repo: str) -> None:
    with pytest.raises(EnvBuildError):
        baseline_script(repo, "main")


@pytest.mark.parametrize("ref", ["", "-rf", "main; id", "a b", "x..y", "$(id)", "`id`"])
def test_baseline_script_rejects_bad_ref(ref: str) -> None:
    with pytest.raises(EnvBuildError):
        baseline_script("psf/requests", ref)


@pytest.mark.parametrize(
    "ref", ["main", "v2.31.0", "release-1.2", "feature/x-y", "a1f00056ebc6", "2.0+local"]
)
def test_baseline_script_accepts_normal_refs(ref: str) -> None:
    assert "git checkout --quiet" in baseline_script("psf/requests", ref)


# --------------------------------------------------------------------------- build_baseline

GOOD_BUILD = ExecResult(
    exit_code=0,
    stdout="__STEP__=clone\n__STEP__=install\n3.12.1\n__STEP__=done\n",
    image="img-baseline-1",
)


async def test_build_baseline_success() -> None:
    sb = FakeSandbox([GOOD_BUILD])
    image, result = await build_baseline(
        sb, repo="psf/requests", ref="v2.31.0", python_version="3.11"
    )
    assert image == "img-baseline-1"
    assert result is GOOD_BUILD
    assert sb.base_image_requests == ["3.11"]
    call = sb.calls[0]
    assert call.image == "fake-base:3.11"
    assert call.keep is True
    assert call.network is True
    assert call.workdir == "/work"
    assert "git clone --filter=blob:none https://github.com/psf/requests" in call.command


async def test_build_baseline_emits_environment_progress_sync_and_async() -> None:
    sync_events: list[ProgressEvent] = []
    async_events: list[ProgressEvent] = []

    def sync_cb(event: ProgressEvent) -> None:
        sync_events.append(event)

    async def async_cb(event: ProgressEvent) -> None:
        await asyncio.sleep(0)
        async_events.append(event)

    for cb in (sync_cb, async_cb):
        await build_baseline(
            FakeSandbox([GOOD_BUILD]),
            repo="psf/requests",
            ref="main",
            python_version=None,
            progress=cb,
        )
    for events in (sync_events, async_events):
        assert len(events) >= 3
        assert {e.stage for e in events} == {"environment"}
        assert "psf/requests" in events[0].message
        assert "ready" in events[-1].message.lower()


async def test_build_baseline_survives_a_broken_progress_callback() -> None:
    def broken(event: ProgressEvent) -> None:
        raise RuntimeError("ui exploded")

    image, _ = await build_baseline(
        FakeSandbox([GOOD_BUILD]), repo="a/b", ref="main", python_version=None, progress=broken
    )
    assert image == "img-baseline-1"


async def test_build_baseline_failure_names_the_step() -> None:
    failed = ExecResult(
        exit_code=12,
        stdout="__STEP__=clone\n__STEP__=checkout\n__STEP__=install\nERROR: build failed\n",
        stderr="error: subprocess-exited-with-error",
    )
    events: list[ProgressEvent] = []
    with pytest.raises(EnvBuildError, match=r"failed at step 'install' \(exit 12\)") as info:
        await build_baseline(
            FakeSandbox([failed]),
            repo="a/b",
            ref="main",
            python_version=None,
            progress=events.append,
        )
    assert info.value.result is failed
    assert "subprocess-exited-with-error" in str(info.value)
    assert "install" in events[-1].message


async def test_build_baseline_timeout() -> None:
    timed_out = ExecResult(exit_code=124, stdout="__STEP__=install\n", timed_out=True)
    with pytest.raises(EnvBuildError, match="timed out after 60s during 'install'"):
        await build_baseline(
            FakeSandbox([timed_out]), repo="a/b", ref="main", python_version=None, timeout_s=60
        )


async def test_build_baseline_requires_a_checkpoint_image() -> None:
    no_image = ExecResult(exit_code=0, stdout="__STEP__=done\n", image=None)

    class NoImageSandbox(FakeSandbox):
        async def run(self, image: str, command: str, **kwargs: object) -> ExecResult:  # type: ignore[override]
            self.calls.append(RunCall(image, command))
            return no_image

    with pytest.raises(EnvBuildError, match="no checkpoint image"):
        await build_baseline(NoImageSandbox(), repo="a/b", ref="main", python_version=None)


async def test_build_baseline_wraps_sandbox_errors() -> None:
    with pytest.raises(EnvBuildError, match="sandbox error while building baseline: boom"):
        await build_baseline(
            FakeSandbox([SandboxError("boom")]), repo="a/b", ref="main", python_version=None
        )

    class NoBase(FakeSandbox):
        async def base_image(self, python_version: str | None) -> str:
            raise SandboxError("import failed")

    with pytest.raises(EnvBuildError, match="no base image"):
        await build_baseline(NoBase(), repo="a/b", ref="main", python_version="3.7")


async def test_build_baseline_validates_before_touching_the_sandbox() -> None:
    sb = FakeSandbox()
    with pytest.raises(EnvBuildError):
        await build_baseline(sb, repo="bad repo", ref="main", python_version=None)
    assert sb.calls == [] and sb.base_image_requests == []


# --------------------------------------------------------------------------- resolve_ref


def github_client(
    tags: set[str], seen: list[httpx.Request] | None = None, status: int | None = None
):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status is not None:
            return httpx.Response(status, json={"message": "nope"})
        tag = request.url.path.split("/git/ref/tags/")[1]
        return httpx.Response(200 if tag in tags else 404, json={"ref": f"refs/tags/{tag}"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_resolve_ref_prefers_v_prefix_then_bare_then_release() -> None:
    seen: list[httpx.Request] = []
    async with github_client({"v1.2.3", "1.2.3"}, seen) as client:
        assert await resolve_ref_for_version("o/r", "1.2.3", client=client) == "v1.2.3"
    assert [r.url.path for r in seen] == ["/repos/o/r/git/ref/tags/v1.2.3"]

    async with github_client({"1.2.3"}) as client:
        assert await resolve_ref_for_version("o/r", "1.2.3", client=client) == "1.2.3"
    async with github_client({"release-1.2.3"}) as client:
        assert await resolve_ref_for_version("o/r", "1.2.3", client=client) == "release-1.2.3"


async def test_resolve_ref_returns_none_when_no_tag_matches() -> None:
    seen: list[httpx.Request] = []
    async with github_client(set(), seen) as client:
        assert await resolve_ref_for_version("o/r", "9.9", client=client) is None
    assert [r.url.path.rsplit("/", 1)[1] for r in seen] == ["v9.9", "9.9", "release-9.9"]


async def test_resolve_ref_accepts_a_version_that_already_has_a_v() -> None:
    async with github_client({"v2.0"}) as client:
        assert await resolve_ref_for_version("o/r", "v2.0", client=client) == "v2.0"


async def test_resolve_ref_sends_token_and_api_version() -> None:
    seen: list[httpx.Request] = []
    async with github_client({"v1"}, seen) as client:
        await resolve_ref_for_version("o/r", "1", token="fake-github-token", client=client)
    assert seen[0].headers["authorization"] == "Bearer fake-github-token"
    assert seen[0].headers["x-github-api-version"] == "2022-11-28"


async def test_resolve_ref_does_not_confuse_rate_limits_with_missing_tags() -> None:
    async with github_client(set(), status=403) as client:
        with pytest.raises(EnvBuildError, match="403.*rate limited"):
            await resolve_ref_for_version("o/r", "1.0", client=client)


async def test_resolve_ref_network_error_and_bad_input() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(EnvBuildError, match="GitHub API request failed"):
            await resolve_ref_for_version("o/r", "1.0", client=client)
    with pytest.raises(EnvBuildError):
        await resolve_ref_for_version("o/r", "1.0; id")
    with pytest.raises(EnvBuildError):
        await resolve_ref_for_version("not a repo", "1.0")


# --------------------------------------------------------------------------- side script


def bash_syntax_ok(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False)


@pytest.mark.parametrize(
    ("checkout", "patch"), [(None, False), ("v2.0", False), (None, True), ("main", True)]
)
def test_side_script_is_valid_bash(checkout: str | None, patch: bool) -> None:
    script = side_script(
        test_path="tests/test_issue.py",
        checkout=checkout,
        patch=patch,
        runs=2,
        per_run_timeout_s=60,
    )
    assert bash_syntax_ok(script).returncode == 0, bash_syntax_ok(script).stderr


def test_side_script_reported_side_has_no_checkout_or_patch() -> None:
    script = side_script(
        test_path="t/test_x.py", checkout=None, patch=False, runs=2, per_run_timeout_s=50
    )
    assert "git fetch" not in script
    assert "git apply" not in script
    assert "pip install" not in script
    assert "for i in $(seq 1 2); do" in script
    assert "timeout -k 5 50 python -m pytest -x -q -p no:cacheprovider t/test_x.py" in script
    assert "__RC__=$rc" in script


def test_side_script_checkout_fetches_and_reinstalls() -> None:
    script = side_script(
        test_path="t/test_x.py", checkout="main", patch=False, runs=1, per_run_timeout_s=50
    )
    assert "git fetch --quiet origin main && git checkout --quiet --force FETCH_HEAD" in script
    assert INSTALL_CHAIN in script
    assert "git apply" not in script
    assert script.index("git fetch") < script.index("pip install") < script.index("pytest")


def test_side_script_patch_applies_before_reinstall() -> None:
    script = side_script(
        test_path="t/test_x.py", checkout=None, patch=True, runs=2, per_run_timeout_s=50
    )
    assert "git apply --whitespace=nowarn /tmp/ir_patch.diff" in script
    assert "git fetch" not in script
    assert script.index("git apply") < script.index("pip install") < script.index("pytest")


@pytest.mark.parametrize(
    "path",
    ["../x.py", "/etc/x.py", "a b.py", "tests/../../x.py", "-k.py", "a//b.py", "x;id.py", ""],
)
def test_side_script_rejects_unsafe_test_paths(path: str) -> None:
    with pytest.raises(EnvBuildError):
        side_script(test_path=path, checkout=None, patch=False, runs=1, per_run_timeout_s=30)


def test_side_script_rejects_unsafe_checkout() -> None:
    with pytest.raises(EnvBuildError):
        side_script(
            test_path="t.py", checkout="main; id", patch=False, runs=1, per_run_timeout_s=30
        )


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        (0, Outcome.PASS),
        (1, Outcome.FAIL),
        (2, Outcome.ERROR),
        (3, Outcome.ERROR),
        (4, Outcome.ERROR),
        (5, Outcome.ERROR),
        (124, Outcome.ERROR),
        (137, Outcome.ERROR),
        (-9, Outcome.ERROR),
    ],
)
def test_map_exit_code(code: int, outcome: Outcome) -> None:
    assert map_exit_code(code) is outcome


def run_output(*codes: int, body: str = "1 failed in 0.1s") -> str:
    parts = []
    for i, code in enumerate(codes, start=1):
        parts += [
            f"__RUN_START__={i}",
            *[f"| {line}" for line in body.splitlines()],
            f"__RC__={code}",
        ]
    return "\n".join(parts) + "\n"


def test_parse_exit_codes() -> None:
    assert parse_exit_codes(run_output(1, 0)) == [1, 0]
    assert parse_exit_codes("no markers here") == []
    assert parse_exit_codes("x__RC__=1\n  __RC__=2\n__RC__=3 trailing\n") == []


def exec_result(
    stdout: str, *, stderr: str = "", exit_code: int = 0, timed_out: bool = False
) -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)


def test_build_side_run_pass_pass_and_fail_fail() -> None:
    passed = build_side_run(
        exec_result(run_output(0, 0, body="1 passed")), ref="v1", image="i", runs=2
    )
    assert passed.outcomes == [Outcome.PASS, Outcome.PASS]
    assert passed.stable_outcome is Outcome.PASS
    assert (passed.ref, passed.checkpoint_image) == ("v1", "i")
    failed = build_side_run(exec_result(run_output(1, 1)), ref="main", image="i", runs=2)
    assert failed.outcomes == [Outcome.FAIL, Outcome.FAIL]


def test_build_side_run_flaky_and_error_codes() -> None:
    flaky = build_side_run(exec_result(run_output(0, 1)), ref="r", image="i", runs=2)
    assert flaky.outcomes == [Outcome.PASS, Outcome.FAIL]
    assert flaky.stable_outcome is None
    errors = build_side_run(exec_result(run_output(2, 5, 124)), ref="r", image="i", runs=3)
    assert errors.outcomes == [Outcome.ERROR] * 3


def test_build_side_run_pads_missing_runs_with_error() -> None:
    cut_short = build_side_run(
        exec_result(run_output(0), exit_code=137, timed_out=True), ref="r", image="i", runs=2
    )
    assert cut_short.outcomes == [Outcome.PASS, Outcome.ERROR]
    assert "timed out" in cut_short.log_excerpt
    nothing = build_side_run(
        exec_result("", exit_code=1, stderr="docker exploded"), ref="r", image="i", runs=2
    )
    assert nothing.outcomes == [Outcome.ERROR, Outcome.ERROR]
    assert "docker exploded" in nothing.log_excerpt


def test_build_side_run_setup_failure_is_error_everywhere() -> None:
    out = "__SETUP_FAILED__=checkout\n"
    side = build_side_run(exec_result(out, exit_code=21), ref="r", image="i", runs=2)
    assert side.outcomes == [Outcome.ERROR, Outcome.ERROR]
    assert "__SETUP_FAILED__=checkout" in side.log_excerpt


def test_test_output_cannot_forge_markers() -> None:
    forged_body = "__RC__=0\n__RC__=0\n__SETUP_FAILED__=cd\n"
    real = run_output(1, 1, body=forged_body.rstrip())
    side = build_side_run(exec_result(real), ref="r", image="i", runs=2)
    assert side.outcomes == [Outcome.FAIL, Outcome.FAIL]


def test_log_excerpt_is_the_end_of_the_log_without_markers_or_prefixes() -> None:
    body = "\n".join(f"line {i:04d}" for i in range(2000))
    side = build_side_run(exec_result(run_output(1, body=body)), ref="r", image="i", runs=1)
    assert len(side.log_excerpt) <= 3000
    assert side.log_excerpt.startswith("--- run 1: exit code 1 ---\n[...]\n")
    assert side.log_excerpt.endswith("line 1999")
    assert "__RC__" not in side.log_excerpt
    assert "__RUN_START__" not in side.log_excerpt
    assert "| " not in side.log_excerpt
    assert "line 0000" not in side.log_excerpt


def test_log_excerpt_of_identical_runs_shows_only_the_last_one() -> None:
    side = build_side_run(
        exec_result(run_output(1, 1, body="first line\n1 failed in 0.1s")),
        ref="r", image="i", runs=2,
    )
    assert side.log_excerpt == (
        "--- last of 2 runs, every run exited 1 ---\nfirst line\n1 failed in 0.1s"
    )


def test_log_excerpt_of_flaky_runs_shows_each_run() -> None:
    stdout = "__RUN_START__=1\n| passed\n__RC__=0\n__RUN_START__=2\n| 1 failed\n__RC__=1\n"
    side = build_side_run(exec_result(stdout), ref="r", image="i", runs=2)
    assert side.log_excerpt == (
        "--- run 1: exit code 0 ---\npassed\n--- run 2: exit code 1 ---\n1 failed"
    )


def test_format_log_keeps_setup_text_and_cuts_on_a_line_boundary() -> None:
    setup = "__SETUP_FAILED__=reinstall\n| ERROR: no matching distribution\n"
    assert format_log(setup) == "__SETUP_FAILED__=reinstall\nERROR: no matching distribution"
    lines = "".join(f"| {'x' * 50} {i}\n" for i in range(500))
    long_run = f"__RUN_START__=1\n{lines}__RC__=1\n"
    cut = format_log(long_run, 600)
    assert len(cut) <= 600
    assert all(line.startswith(("--- ", "[...]", "x" * 50)) for line in cut.splitlines())


def test_clean_log_strips_only_the_marker_prefix() -> None:
    assert clean_log("| a\n|b\nplain\n") == "a\nb\nplain\n"


def test_side_label() -> None:
    assert side_label(None, None) == "HEAD"
    assert side_label("v1", None) == "v1"
    assert side_label(None, "diff") == "gold_patch"
    assert side_label("abc123", "diff") == "abc123+gold_patch"


# --------------------------------------------------------------------------- run_test_on_side


async def test_run_test_on_side_stages_files_and_builds_side_run() -> None:
    sb = FakeSandbox([exec_result(run_output(1, 1))])
    side = await run_test_on_side(
        sb, "ckpt-9", test_path="tests/test_issue_1.py", test_code="def test_x():\n    assert 0\n"
    )
    assert side.outcomes == [Outcome.FAIL, Outcome.FAIL]
    assert side.ref == "HEAD"
    assert side.checkpoint_image == "ckpt-9"
    (call,) = sb.calls
    assert call.image == "ckpt-9"
    assert call.workdir == REPO_DIR
    assert call.keep is False
    assert call.files == {"/tmp/ir_test_file": "def test_x():\n    assert 0\n"}
    assert "git fetch" not in call.command and "git apply" not in call.command
    assert "seq 1 2" in call.command


async def test_run_test_on_side_checkout_runs_and_ref_override() -> None:
    sb = FakeSandbox([exec_result(run_output(0, 0, 0))])
    side = await run_test_on_side(
        sb, "ckpt", test_path="t.py", test_code="x", checkout="main", runs=3, ref="main@abc"
    )
    assert side.outcomes == [Outcome.PASS] * 3
    assert side.ref == "main@abc"
    assert "git fetch --quiet origin main" in sb.calls[0].command
    assert "seq 1 3" in sb.calls[0].command
    only_checkout = await run_test_on_side(
        FakeSandbox([exec_result(run_output(0, 0))]),
        "c",
        test_path="t.py",
        test_code="x",
        checkout="v2",
    )
    assert only_checkout.ref == "v2"


async def test_run_test_on_side_patch_gets_trailing_newline_and_label() -> None:
    sb = FakeSandbox([exec_result(run_output(0, 0))])
    side = await run_test_on_side(sb, "ckpt", test_path="t.py", test_code="x", patch="--- a\n+++ b")
    assert side.ref == "gold_patch"
    assert sb.calls[0].files is not None
    assert sb.calls[0].files["/tmp/ir_patch.diff"] == "--- a\n+++ b\n"
    assert "git apply" in sb.calls[0].command


async def test_run_test_on_side_timeout_budget_is_split_across_runs() -> None:
    sb = FakeSandbox([exec_result(run_output(0, 0))])
    await run_test_on_side(
        sb, "c", test_path="t.py", test_code="x", timeout_s=630, runs=2, checkout="main"
    )
    assert sb.calls[0].timeout_s == 630
    assert "timeout -k 5 165 python" in sb.calls[0].command  # (630 - 300 setup) / 2


async def test_run_test_on_side_sandbox_timeout_gives_error_outcomes() -> None:
    sb = FakeSandbox([exec_result("", exit_code=124, timed_out=True)])
    side = await run_test_on_side(sb, "c", test_path="t.py", test_code="x")
    assert side.outcomes == [Outcome.ERROR, Outcome.ERROR]


async def test_run_test_on_side_propagates_broken_sandbox() -> None:
    with pytest.raises(SandboxError):
        await run_test_on_side(
            FakeSandbox([SandboxError("down")]), "c", test_path="t.py", test_code="x"
        )


async def test_run_test_on_side_validates_inputs() -> None:
    sb = FakeSandbox()
    with pytest.raises(EnvBuildError):
        await run_test_on_side(sb, "c", test_path="../evil.py", test_code="x")
    with pytest.raises(EnvBuildError):
        await run_test_on_side(sb, "c", test_path="t.py", test_code="x", runs=0)
    assert sb.calls == []


# --------------------------------------------------------------------------- real bash, stub tools

STUB_PYTHON = """#!/bin/bash
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  n=$(cat "$FAKE_STATE/count" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$FAKE_STATE/count"
  rc=$(echo "$FAKE_RCS" | cut -d, -f"$n")
  echo "collected 1 item"
  printf '__RC__=0\\n__SETUP_FAILED__=cd\\npartial line without newline'
  exit "$rc"
fi
echo "stub python: $*" >&2
exit 0
"""
STUB_GIT = """#!/bin/bash
echo "git $*" >> "$FAKE_STATE/commands.log"
if [ "${FAKE_GIT_FAIL:-}" = "$1" ]; then echo "fatal: git $1 failed" >&2; exit 1; fi
exit 0
"""
STUB_PIP = """#!/bin/bash
echo "pip $*" >> "$FAKE_STATE/commands.log"
if [ -n "${FAKE_PIP_FAIL:-}" ]; then echo "ERROR: pip failed"; exit 1; fi
case " $* " in *" ${FAKE_PIP_FAIL_PKG:-@none@} "*) echo "ERROR: no such package"; exit 1;; esac
exit 0
"""
STUB_TIMEOUT = """#!/bin/bash
shift 3
exec "$@"
"""


class LocalBashSandbox:
    """Runs the script with the host bash and stub tools; writes `files` to the host."""

    name = "local-bash"

    def __init__(self, bindir: Path, state: Path, env: dict[str, str]) -> None:
        self.bindir, self.state, self.env = bindir, state, env
        self.commands: list[str] = []

    async def base_image(self, python_version: str | None) -> str:
        return "local"

    async def run(
        self,
        image: str,
        command: str,
        *,
        files: dict[str, str] | None = None,
        workdir: str = "/work",
        timeout_s: int = 900,
        keep: bool = False,
        network: bool = True,
    ) -> ExecResult:
        for path, content in (files or {}).items():
            await asyncio.to_thread(Path(path).write_text, content)
        env = {"PATH": f"{self.bindir}:/usr/bin:/bin", "FAKE_STATE": str(self.state), **self.env}
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            build_script(command, workdir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=out.decode(),
            stderr=err.decode(),
            duration_s=time.monotonic() - started,
        )


@dataclass
class LocalEnv:
    """Fixture value: stub tool dir, state dir, fake repo dir; call it to get a sandbox."""

    bindir: Path
    state: Path
    repo: Path

    def __call__(self, **env: str) -> LocalBashSandbox:
        return LocalBashSandbox(self.bindir, self.state, env)


@pytest.fixture
def local_bash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalEnv:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    bindir, state, repo = tmp_path / "bin", tmp_path / "state", tmp_path / "repo"
    for directory in (bindir, state, repo):
        directory.mkdir()
    stubs = {"python": STUB_PYTHON, "git": STUB_GIT, "pip": STUB_PIP, "timeout": STUB_TIMEOUT}
    for name, body in stubs.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755 | stat.S_IEXEC)
    monkeypatch.setattr(envbuild, "REPO_DIR", str(repo))
    return LocalEnv(bindir, state, repo)


async def test_bash_end_to_end_fail_fail(local_bash: LocalEnv) -> None:
    sb = local_bash(FAKE_RCS="1,1")
    side = await run_test_on_side(
        sb, "local", test_path="tests/test_issue.py", test_code="# the test\n"
    )
    assert side.outcomes == [Outcome.FAIL, Outcome.FAIL]
    written = local_bash.repo / "tests" / "test_issue.py"
    assert written.read_text() == "# the test\n"
    assert "partial line without newline" in side.log_excerpt


async def test_bash_end_to_end_pass_then_flaky_and_error_codes(local_bash: LocalEnv) -> None:
    flaky = await run_test_on_side(
        local_bash(FAKE_RCS="0,1"), "local", test_path="t/test_a.py", test_code="x"
    )
    assert flaky.outcomes == [Outcome.PASS, Outcome.FAIL]
    (local_bash.state / "count").unlink()
    errored = await run_test_on_side(
        local_bash(FAKE_RCS="5,124"), "local", test_path="t/test_a.py", test_code="x"
    )
    assert errored.outcomes == [Outcome.ERROR, Outcome.ERROR]


async def test_bash_forged_markers_in_test_output_are_ignored(local_bash: LocalEnv) -> None:
    side = await run_test_on_side(
        local_bash(FAKE_RCS="1,1"), "local", test_path="t.py", test_code="x"
    )
    assert side.outcomes == [Outcome.FAIL, Outcome.FAIL]  # the stub printed __RC__=0 itself


async def test_bash_checkout_and_patch_flow(local_bash: LocalEnv) -> None:
    sb = local_bash(FAKE_RCS="0,0")
    side = await run_test_on_side(
        sb, "local", test_path="t.py", test_code="x", checkout="v9.9", patch="diff --git a/f b/f"
    )
    assert side.outcomes == [Outcome.PASS, Outcome.PASS]
    log = (local_bash.state / "commands.log").read_text().splitlines()
    assert log[0] == "git fetch --quiet origin v9.9"
    assert log[1] == "git checkout --quiet --force FETCH_HEAD"
    assert log[2] == "git apply --whitespace=nowarn /tmp/ir_patch.diff"
    assert log[3] == "pip install -e .[test]"


@pytest.mark.parametrize(
    ("env", "marker"),
    [
        ({"FAKE_GIT_FAIL": "fetch"}, "checkout"),
        ({"FAKE_GIT_FAIL": "apply"}, "patch"),
        ({"FAKE_PIP_FAIL": "1"}, "reinstall"),
    ],
)
async def test_bash_setup_failures_become_error_outcomes(
    local_bash: LocalEnv, env: dict[str, str], marker: str
) -> None:
    sb = local_bash(FAKE_RCS="0,0", **env)
    side = await run_test_on_side(
        sb, "local", test_path="t.py", test_code="x", checkout="main", patch="diff --git a/f b/f"
    )
    assert side.outcomes == [Outcome.ERROR, Outcome.ERROR]
    assert f"__SETUP_FAILED__={marker}" in side.log_excerpt
    assert not (local_bash.state / "count").exists()  # pytest never ran


async def test_bash_baseline_script_runs_through_all_steps(local_bash: LocalEnv) -> None:
    sb = local_bash()
    result = await sb.run(
        "local", baseline_script("psf/requests", "v2.31.0"), workdir=str(local_bash.repo)
    )
    assert result.exit_code == 0, result.stderr
    steps = [line for line in result.stdout.splitlines() if line.startswith("__STEP__=")]
    assert steps == [
        "__STEP__=clone",
        "__STEP__=checkout",
        "__STEP__=install",
        "__STEP__=pytest",
        "__STEP__=sanity",
        "__STEP__=done",
    ]
    commands = (local_bash.state / "commands.log").read_text().splitlines()
    assert commands[0].startswith("git clone --filter=blob:none https://github.com/psf/requests ")
    assert "git checkout --quiet v2.31.0" in commands
    assert "pip install pytest" in commands


async def test_bash_extras_install_one_by_one_and_a_failure_does_not_stop_the_build(
    local_bash: LocalEnv,
) -> None:
    extras = ["structlog", "nonexistent", "attrs>=22,<24"]
    script = baseline_script("pallets/click", "8.1.7", extras)
    sb = local_bash(FAKE_PIP_FAIL_PKG="nonexistent")
    result = await sb.run("local", script, workdir=str(local_bash.repo))
    assert result.exit_code == 0, result.stderr
    assert installed_extras(result) == (["structlog", "attrs>=22,<24"], ["nonexistent"])
    assert result.stdout.rstrip().endswith("__STEP__=done")
    commands = (local_bash.state / "commands.log").read_text().splitlines()
    assert "pip install attrs>=22,<24" in commands  # one argument: the shell did not split it


async def test_bash_baseline_failure_exit_codes(local_bash: LocalEnv) -> None:
    script = baseline_script("psf/requests", "v1")
    workdir = str(local_bash.repo)
    cloned = await local_bash(FAKE_GIT_FAIL="clone").run("local", script, workdir=workdir)
    assert cloned.exit_code == 10
    checked = await local_bash(FAKE_GIT_FAIL="checkout").run("local", script, workdir=workdir)
    assert checked.exit_code == 11
    installed = await local_bash(FAKE_PIP_FAIL="1").run("local", script, workdir=workdir)
    assert installed.exit_code == 12
