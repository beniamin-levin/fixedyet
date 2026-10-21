"""Command line: `fixedyet run | backport | fix | doctor | eval | rescore | serve | mcp | gc`.

Progress goes to stderr and the report to stdout, so `fixedyet run URL > report.md` works.
The eval, web and MCP modules are imported only when their command is used.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from fixedyet.config import Settings, get_settings, normalize_root_path
from fixedyet.models import Assessment, BackportResult, FixResult, ProgressEvent

OK, FAIL, SKIP = "ok", "FAIL", "skip"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fixedyet",
        description="Reproduce a GitHub bug report before answering it.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log what each step does")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    run = commands.add_parser("run", help="reproduce one issue and print the report")
    run.add_argument("issue_url", help="https://github.com/<owner>/<repo>/issues/<number>")
    run.add_argument("--fix-ref", help="branch, tag or commit for the fix side (default: main)")
    run.add_argument("--json", metavar="PATH", help="also write the assessment as JSON")
    run.add_argument("--md", metavar="PATH", help="also write the markdown report")

    backport = commands.add_parser(
        "backport",
        help="prepare a verified backport of the fix onto the maintenance branch of the "
        "reported version, and optionally open a DRAFT pull request",
        description="Cherry-pick the fix that closed the issue on main onto the maintenance "
        "branch of the version the reporter used, prove it in the sandbox (the reproduction "
        "test fails before and passes after; the project's own tests show no new failures) and "
        "print a markdown summary. Nothing is pushed unless --open-pr is given.",
    )
    backport.add_argument("issue_url", help="https://github.com/<owner>/<repo>/issues/<number>")
    backport.add_argument(
        "--target-branch", metavar="B",
        help="the branch to backport onto (default: found from the repo's branch names, "
        "for example 8.1.x, stable/8.1, release-8.1)",
    )
    backport.add_argument(
        "--from-assessment", metavar="PATH",
        help="reuse a saved assessment JSON instead of reproducing the issue again (no model "
        "calls, so no NEBIUS_API_KEY is needed)",
    )
    backport.add_argument(
        "--open-pr", action="store_true",
        help="after a VERIFIED backport, push it to your fork and open a DRAFT pull request "
        "(token: GITHUB_TOKEN, else `gh auth token`)",
    )
    backport.add_argument(
        "--pr-target", choices=("fork", "upstream"), default=None,
        help="where the draft pull request goes: your fork's copy of the branch (default, "
        "nothing reaches the project) or the upstream branch (asks you to type the repo name)",
    )
    backport.add_argument(
        "--yes-upstream", action="store_true",
        help="skip the typed confirmation for --pr-target upstream",
    )
    backport.add_argument(
        "--python-version", metavar="X.Y",
        help="Python for the sandbox (default: the sandbox's own, 3.12); old branches may need "
        "an older one",
    )
    backport.add_argument(
        "--pip", action="append", default=[], metavar="REQ",
        help="a package the reproduction test needs besides the project (repeatable)",
    )
    backport.add_argument("--json", metavar="PATH", help="also write the result as JSON")
    backport.add_argument("--md", metavar="PATH", help="also write the markdown summary")

    fix = commands.add_parser(
        "fix",
        help="for an open bug, have the model write a minimal fix, verify it in the sandbox, "
        "and optionally open a DRAFT pull request",
        description="Ask Nemotron for the smallest change that makes the reproduction test pass, "
        "on the default branch, and prove it in the sandbox: the test fails twice without the "
        "change and passes twice with it, and the project's own tests show no new failures. Up "
        "to 3 attempts under FIXEDYET_FIX_BUDGET_USD. Prints a markdown summary. Nothing is "
        "pushed unless --open-pr is given.",
    )
    fix.add_argument(
        "issue_url", nargs="?",
        help="https://github.com/<owner>/<repo>/issues/<number> (optional with --from-assessment)",
    )
    fix.add_argument(
        "--from-assessment", metavar="PATH",
        help="reuse a saved assessment JSON (an open_bug verdict) instead of reproducing the "
        "issue again",
    )
    fix.add_argument(
        "--open-pr", action="store_true",
        help="after a VERIFIED fix, push it to your fork and open a DRAFT pull request "
        "(token: GITHUB_TOKEN, else `gh auth token`)",
    )
    fix.add_argument(
        "--pr-target", choices=("fork", "upstream"), default=None,
        help="where the draft pull request goes: your fork's copy of the default branch "
        "(default, nothing reaches the project) or the upstream project (asks you to type the "
        "repo name)",
    )
    fix.add_argument(
        "--yes-upstream", action="store_true",
        help="skip the typed confirmation for --pr-target upstream",
    )
    fix.add_argument(
        "--budget", type=float, metavar="USD",
        help="model cost cap of this fix, every attempt included (default: "
        "FIXEDYET_FIX_BUDGET_USD, else 0.25)",
    )
    fix.add_argument(
        "--python-version", metavar="X.Y", help="Python for the sandbox (default: 3.12)",
    )
    fix.add_argument(
        "--pip", action="append", default=[], metavar="REQ",
        help="a package the reproduction test needs besides the project (repeatable)",
    )
    fix.add_argument("--json", metavar="PATH", help="also write the result as JSON")
    fix.add_argument("--md", metavar="PATH", help="also write the markdown summary")
    fix.add_argument(
        "--pr-preview", metavar="PATH",
        help="for a verified fix, write what the draft pull request would say (title, body, "
        "diff) without opening it",
    )
    fix.add_argument(
        "--pr-login", metavar="LOGIN",
        help="the GitHub login named in the preview's disclosure line (default: the owner of "
        "the GitHub token, when there is one)",
    )

    doctor = commands.add_parser("doctor", help="check keys, model access and the sandbox")
    doctor.add_argument("--tavily", action="store_true", help="also test Tavily (uses 1 credit)")

    commands.add_parser(
        "eval", help="run the SWE-bench evaluation (arguments are passed to fixedyet.eval.run)",
        add_help=False,
    )

    commands.add_parser(
        "rescore", add_help=False,
        help="run the verdict rules again over saved assessments and eval results, no model "
        "call (arguments are passed to fixedyet.rescore)",
    )

    serve = commands.add_parser("serve", help="run the demo web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.add_argument(
        "--root-path",
        metavar="PREFIX",
        help="URL prefix the app is served under behind a reverse proxy that strips it, "
        "for example /fixedyet (default: FIXEDYET_ROOT_PATH, else none)",
    )

    commands.add_parser("mcp", help="run the MCP server on stdio")

    gc = commands.add_parser(
        "gc", help="remove old fixedyet-ckpt Docker images, keeping the newest few"
    )
    gc.add_argument(
        "--keep",
        type=int,
        metavar="N",
        help="how many of the newest images to keep (default: FIXEDYET_KEEP_CHECKPOINTS, else 6)",
    )
    gc.add_argument(
        "--dry-run", action="store_true", help="list what would be removed, remove nothing"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "eval":  # the eval module owns its own arguments
        return _eval(args_list[1:])
    if args_list and args_list[0] == "rescore":  # so does the rescore module
        from fixedyet.rescore import main as rescore_main

        return int(rescore_main(args_list[1:]) or 0)
    args = build_parser().parse_args(args_list)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "run":
        return _run(args)
    if args.command == "backport":
        return _backport(args)
    if args.command == "fix":
        return _fix(args)
    if args.command == "doctor":
        return asyncio.run(_doctor(get_settings(), tavily=args.tavily))
    if args.command == "serve":
        return _serve(args.host, args.port, args.root_path)
    if args.command == "mcp":
        return _mcp()
    if args.command == "gc":
        return asyncio.run(_gc(get_settings(), keep=args.keep, dry_run=args.dry_run))
    raise AssertionError(f"unhandled command {args.command}")  # argparse enforces the choices


# -- run ---------------------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    from fixedyet.github import parse_issue_url
    from fixedyet.report import render_markdown

    try:
        parse_issue_url(args.issue_url)  # fail on a typo before anything else
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    settings = get_settings()
    if not settings.nebius_api_key:
        print(
            "NEBIUS_API_KEY is not set. Copy .env.example to .env and fill it in, "
            "then check with `fixedyet doctor`.",
            file=sys.stderr,
        )
        return 2
    assessment = asyncio.run(_reproduce(args, settings))
    markdown = render_markdown(assessment)
    if args.json:
        Path(args.json).write_text(assessment.model_dump_json(indent=2), encoding="utf-8")
    if args.md:
        Path(args.md).write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    return 0


async def _reproduce(args: argparse.Namespace, settings: Settings) -> Assessment:
    from fixedyet.pipeline import reproduce

    started = time.monotonic()

    def show(event: ProgressEvent) -> None:
        elapsed = time.monotonic() - started
        print(f"[{elapsed:5.0f}s] {event.stage}: {event.message}", file=sys.stderr, flush=True)

    return await reproduce(args.issue_url, fix_ref=args.fix_ref, settings=settings, progress=show)


def _progress_printer() -> Callable[[ProgressEvent], None]:
    started = time.monotonic()

    def show(event: ProgressEvent) -> None:
        elapsed = time.monotonic() - started
        print(f"[{elapsed:5.0f}s] {event.stage}: {event.message}", file=sys.stderr, flush=True)

    return show


# -- backport ----------------------------------------------------------------------------------


def _backport(args: argparse.Namespace) -> int:
    from fixedyet.github import parse_issue_url
    from fixedyet.pullrequest import PullRequestError, confirm_upstream, resolve_token
    from fixedyet.report import render_backport_markdown

    def fail(message: str) -> int:
        print(f"error: {message}", file=sys.stderr)
        return 2

    try:
        owner, repo, _ = parse_issue_url(args.issue_url)  # fail on a typo before anything else
    except ValueError as exc:
        return fail(str(exc))
    if args.pr_target and not args.open_pr:
        return fail("--pr-target only applies together with --open-pr")
    pr_target = args.pr_target or "fork"
    if args.yes_upstream and not (args.open_pr and pr_target == "upstream"):
        return fail("--yes-upstream only applies to --open-pr --pr-target upstream")

    settings = get_settings()
    assessment = None
    if args.from_assessment:
        try:
            assessment = _load_assessment(args.from_assessment, args.issue_url)
        except ValueError as exc:
            return fail(str(exc))
    elif not settings.nebius_api_key:
        return fail(
            "NEBIUS_API_KEY is not set, and the issue has to be reproduced first. Copy "
            ".env.example to .env and fill it in, or pass --from-assessment PATH."
        )

    # Everything that can stop a pull request is checked before the long sandbox work.
    token = ""
    confirmed = False
    if args.open_pr:
        try:
            token = resolve_token(settings.github_token)
        except PullRequestError as exc:
            return fail(str(exc))
        if pr_target == "upstream":
            confirmed = confirm_upstream(f"{owner}/{repo}", assume_yes=args.yes_upstream)
            if not confirmed:
                return fail("not confirmed: no pull request was opened on the upstream project")

    result = asyncio.run(_run_backport(args, settings, assessment))
    markdown = render_backport_markdown(result)
    if args.json:
        Path(args.json).write_text(result.model_dump_json(indent=2), encoding="utf-8")
    if args.md:
        Path(args.md).write_text(markdown, encoding="utf-8")
    print()
    print(markdown)

    if args.open_pr:
        if result.status.value != "verified":
            print(
                f"No pull request was opened: the backport is {result.status.value}, "
                "and only a verified one gets a draft pull request.",
                file=sys.stderr,
            )
        else:
            code = asyncio.run(_open_pr(result, token, pr_target, confirmed))
            if code:
                return code
    return 0 if result.status.value == "verified" else 1


def _load_assessment(path: str, issue_url: str | None) -> Assessment:
    from pydantic import ValidationError

    from fixedyet.github import parse_issue_url

    try:
        assessment = Assessment.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except ValidationError as exc:
        raise ValueError(f"{path} is not an assessment JSON written by `fixedyet run`") from exc
    if issue_url is None:
        return assessment
    try:
        same = parse_issue_url(assessment.issue_url) == parse_issue_url(issue_url)
    except ValueError:
        same = False
    if not same:
        raise ValueError(
            f"{path} is an assessment of {assessment.issue_url}, not of {issue_url}"
        )
    return assessment


async def _run_backport(
    args: argparse.Namespace, settings: Settings, assessment: Assessment | None
) -> BackportResult:
    from fixedyet.backport import backport

    show = _progress_printer()
    reproduced = assessment is None
    if assessment is None:
        from fixedyet.pipeline import reproduce

        assessment = await reproduce(args.issue_url, settings=settings, progress=show)
        show(ProgressEvent(stage="verdict", message=f"{assessment.verdict.value}, reproduced now"))
    result = await backport(
        assessment, target_branch=args.target_branch, settings=settings, progress=show,
        python_version=args.python_version, extra_packages=args.pip,
    )
    if reproduced:  # this invocation paid for the reproduction too
        used = assessment.usage
        result.usage.llm_calls += used.llm_calls
        result.usage.input_tokens += used.input_tokens
        result.usage.output_tokens += used.output_tokens
        result.usage.cost_usd += used.cost_usd
        result.usage.search_credits += used.search_credits
        result.usage.sandbox_runs += used.sandbox_runs
        result.usage.wall_seconds = round(result.usage.wall_seconds + used.wall_seconds, 2)
    return result


async def _open_pr(result: BackportResult, token: str, target: str, confirmed: bool) -> int:
    from fixedyet.pullrequest import PullRequestError, open_backport_pr

    def say(message: str) -> None:
        print(f"pull request: {message}", file=sys.stderr, flush=True)

    try:
        pr = await open_backport_pr(
            result, token=token, target="upstream" if target == "upstream" else "fork",
            confirmed_upstream=confirmed, say=say,
        )
    except PullRequestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state = "opened" if pr.created else "already open"
    print(f"Draft pull request {state}: {pr.url}")
    print(f"  base {pr.base_repo}:{pr.base_branch}, head {pr.head}, draft: {str(pr.draft).lower()}")
    return 0


# -- fix ---------------------------------------------------------------------------------------


def _fix(args: argparse.Namespace) -> int:
    from fixedyet.github import parse_issue_url
    from fixedyet.pullrequest import PullRequestError, confirm_upstream, resolve_token
    from fixedyet.report import render_fix_markdown

    def fail(message: str) -> int:
        print(f"error: {message}", file=sys.stderr)
        return 2

    if not args.issue_url and not args.from_assessment:
        return fail("give an issue URL, or --from-assessment PATH")
    if args.pr_target and not args.open_pr:
        return fail("--pr-target only applies together with --open-pr")
    pr_target = args.pr_target or "fork"
    if args.yes_upstream and not (args.open_pr and pr_target == "upstream"):
        return fail("--yes-upstream only applies to --open-pr --pr-target upstream")
    if args.budget is not None and not args.budget > 0:
        return fail("--budget must be a positive amount in USD")
    if args.issue_url:
        try:
            parse_issue_url(args.issue_url)  # fail on a typo before anything else
        except ValueError as exc:
            return fail(str(exc))

    settings = get_settings()
    if not settings.nebius_api_key:
        return fail("NEBIUS_API_KEY is not set: fix mode asks the model for the change. Copy "
                    ".env.example to .env and fill it in.")
    assessment = None
    if args.from_assessment:
        try:
            assessment = _load_assessment(args.from_assessment, args.issue_url)
        except ValueError as exc:
            return fail(str(exc))
        if assessment.verdict.value != "open_bug":
            return fail(f"{args.from_assessment} has the verdict {assessment.verdict.value}; "
                        "fix mode needs open_bug")
    issue_url = args.issue_url or assessment.issue_url  # type: ignore[union-attr]
    try:
        owner, repo, _ = parse_issue_url(issue_url)
    except ValueError as exc:
        return fail(str(exc))

    # Everything that can stop a pull request is checked before the long sandbox work.
    token = ""
    confirmed = False
    if args.open_pr:
        try:
            token = resolve_token(settings.github_token)
        except PullRequestError as exc:
            return fail(str(exc))
        if pr_target == "upstream":
            confirmed = confirm_upstream(f"{owner}/{repo}", assume_yes=args.yes_upstream)
            if not confirmed:
                return fail("not confirmed: no pull request was opened on the upstream project")

    result = asyncio.run(_run_fix(args, settings, assessment, issue_url))
    markdown = render_fix_markdown(result)
    if args.json:
        Path(args.json).write_text(result.model_dump_json(indent=2), encoding="utf-8")
    if args.md:
        Path(args.md).write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    verified = result.status.value == "verified"
    if args.pr_preview:
        if not verified:
            print("No pull request preview: only a verified fix has one.", file=sys.stderr)
        else:
            login = args.pr_login or asyncio.run(_token_login(settings))
            from fixedyet.pullrequest import fix_pr_preview

            Path(args.pr_preview).write_text(fix_pr_preview(result, login=login),
                                             encoding="utf-8")
    if args.open_pr:
        if not verified:
            print(f"No pull request was opened: the fix is {result.status.value}, and only a "
                  "verified one gets a draft pull request.", file=sys.stderr)
        else:
            code = asyncio.run(_open_fix_pr(result, token, pr_target, confirmed))
            if code:
                return code
    return 0 if verified else 1


async def _run_fix(
    args: argparse.Namespace, settings: Settings, assessment: Assessment | None, issue_url: str
) -> FixResult:
    from fixedyet.fixmode import attempt_fix

    show = _progress_printer()
    reproduced = assessment is None
    if assessment is None:
        from fixedyet.pipeline import reproduce

        assessment = await reproduce(issue_url, settings=settings, progress=show)
        show(ProgressEvent(stage="verdict", message=f"{assessment.verdict.value}, reproduced now"))
    result = await attempt_fix(
        assessment, settings=settings, progress=show, python_version=args.python_version,
        extra_packages=args.pip, budget_usd=args.budget,
    )
    if reproduced:  # this invocation paid for the reproduction too
        used = assessment.usage
        result.usage.llm_calls += used.llm_calls
        result.usage.input_tokens += used.input_tokens
        result.usage.output_tokens += used.output_tokens
        result.usage.cost_usd += used.cost_usd
        result.usage.search_credits += used.search_credits
        result.usage.sandbox_runs += used.sandbox_runs
        result.usage.wall_seconds = round(result.usage.wall_seconds + used.wall_seconds, 2)
    return result


async def _token_login(settings: Settings) -> str:
    """The login of the GitHub token's owner, for the preview's disclosure line."""
    from fixedyet.pullrequest import PullRequestClient, PullRequestError, resolve_token

    try:
        client = PullRequestClient(resolve_token(settings.github_token))
    except PullRequestError:
        return "<your-github-login>"
    try:
        return (await client.whoami()).login
    except PullRequestError:
        return "<your-github-login>"
    finally:
        await client.aclose()


async def _open_fix_pr(result: FixResult, token: str, target: str, confirmed: bool) -> int:
    from fixedyet.pullrequest import PullRequestError, open_fix_pr

    def say(message: str) -> None:
        print(f"pull request: {message}", file=sys.stderr, flush=True)

    try:
        pr = await open_fix_pr(
            result, token=token, target="upstream" if target == "upstream" else "fork",
            confirmed_upstream=confirmed, say=say,
        )
    except PullRequestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state = "opened" if pr.created else "already open"
    print(f"Draft pull request {state}: {pr.url}")
    print(f"  base {pr.base_repo}:{pr.base_branch}, head {pr.head}, draft: {str(pr.draft).lower()}")
    return 0


# -- doctor ------------------------------------------------------------------------------------

Check = tuple[str, str, str]  # (name, status, detail)


async def _timed(name: str, probe: Callable[[], Awaitable[str]]) -> Check:
    started = time.monotonic()
    try:
        detail = await probe()
    except Exception as exc:  # a doctor reports every failure, whatever its type
        return name, FAIL, f"{type(exc).__name__}: {str(exc)[:200]}"
    return name, OK, f"{detail} ({time.monotonic() - started:.1f} s)"


def _ledger_check(settings: Settings) -> Check:
    """Total model spend so far and what is left under `FIXEDYET_TOTAL_BUDGET_USD`."""
    from fixedyet import ledger

    try:
        spent = ledger.spent_total(settings)
    except ledger.LedgerError as exc:
        return "spend ledger", FAIL, str(exc)[:300]
    budget = ledger.total_budget(settings)
    left = max(budget - spent, 0.0)
    detail = (f"spent ${spent:.4f} of ${budget:.2f}, remaining ${left:.4f} "
              f"({ledger.ledger_path(settings)})")
    if left < settings.run_budget_usd:
        need = f"${settings.run_budget_usd:.2f}"
        return "spend ledger", FAIL, f"{detail}; too little for one run ({need})"
    return "spend ledger", OK, detail


async def _doctor(settings: Settings, *, tavily: bool) -> int:
    checks: list[Check] = []
    for field_name, env_name, required in (
        ("nebius_api_key", "NEBIUS_API_KEY", True),
        ("github_token", "GITHUB_TOKEN", False),
        ("tavily_api_key", "TAVILY_API_KEY", False),
    ):
        present = settings.has(field_name)
        status = OK if present else (FAIL if required else SKIP)
        note = "set" if present else ("missing, required" if required else "not set (optional)")
        checks.append((env_name, status, note))
    checks.append(("sandbox backend", OK, settings.sandbox))
    checks.append(_ledger_check(settings))

    if settings.nebius_api_key:
        checks.append(await _timed("Token Factory chat", lambda: _probe_chat(settings)))
    else:
        checks.append(("Token Factory chat", SKIP, "needs NEBIUS_API_KEY"))
    checks.append(await _timed("sandbox hello world", lambda: _probe_sandbox(settings)))
    checks.append(await _timed("GitHub API", lambda: _probe_github(settings)))
    if tavily and settings.tavily_api_key:
        checks.append(await _timed("Tavily search", lambda: _probe_tavily(settings)))
    else:
        checks.append(("Tavily search", SKIP, "pass --tavily to test (uses 1 credit)"))

    width = max(len(name) for name, _, _ in checks)
    print(f"{'check'.ljust(width)}  status  detail")
    for name, status, detail in checks:
        print(f"{name.ljust(width)}  {status.ljust(6)}  {detail}")
    return 1 if any(status == FAIL for _, status, _ in checks) else 0


async def _probe_chat(settings: Settings) -> str:
    from fixedyet.llm import LLM

    llm = LLM(settings)
    result = await llm.chat(
        [{"role": "user", "content": "Reply with the word ok."}], max_tokens=5, thinking=False
    )
    return f"{settings.model} answered, {result.usage.output_tokens} output tokens"


async def _probe_sandbox(settings: Settings) -> str:
    from fixedyet.sandbox import get_sandbox

    sandbox = get_sandbox(settings)
    image = await sandbox.base_image(None)
    result = await sandbox.run(image, "echo ok", timeout_s=120)
    if result.exit_code != 0 or "ok" not in result.stdout:
        raise RuntimeError(f"echo ok exited {result.exit_code}: {result.stderr[:150]}")
    return f"{sandbox.name}: echo ok worked"


async def _probe_github(settings: Settings) -> str:
    import httpx

    headers = {"User-Agent": "fixedyet"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        response = await client.get("https://api.github.com/rate_limit")  # free, uncounted
    response.raise_for_status()
    core = response.json()["resources"]["core"]
    return f"{core['remaining']}/{core['limit']} requests left this hour"


async def _probe_tavily(settings: Settings) -> str:
    from tavily import AsyncTavilyClient

    client = AsyncTavilyClient(api_key=settings.tavily_api_key)
    response = await client.search(query="pytest", search_depth="basic", max_results=1)
    return f"{len(response.get('results') or [])} result(s), 1 credit used"


# -- the commands that live in other modules ---------------------------------------------------


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError:  # a parent package is missing
        return False


def _eval(argv: list[str]) -> int:
    if not _module_exists("fixedyet.eval.run"):
        print("error: fixedyet.eval.run is not installed", file=sys.stderr)
        return 2
    from fixedyet.eval.run import main as eval_main

    return int(eval_main(argv) or 0)


def _serve(host: str, port: int, root_path: str | None = None) -> int:
    if not _module_exists("fixedyet.web.app"):
        print("error: fixedyet.web.app is not installed", file=sys.stderr)
        return 2
    import uvicorn

    try:
        prefix = (
            normalize_root_path(root_path)
            if root_path is not None
            else get_settings().root_path
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    uvicorn.run("fixedyet.web.app:app", host=host, port=port, root_path=prefix)
    return 0


async def _gc(settings: Settings, *, keep: int | None, dry_run: bool) -> int:
    """Prune the local ``fixedyet-ckpt`` Docker images now (what each run does when it ends)."""
    from fixedyet.sandbox.base import SandboxError
    from fixedyet.sandbox.docker import CHECKPOINT_REPO, DockerSandbox

    keep = settings.keep_checkpoints if keep is None else keep
    if keep < 0:
        print("error: --keep must be 0 or more", file=sys.stderr)
        return 2
    try:
        result = await DockerSandbox().prune_checkpoints(keep, dry_run=dry_run)
    except SandboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    verb = "would remove" if dry_run else "removed"
    for tag in result.removed:
        print(f"{verb} {tag}")
    for tag in result.in_use:
        print(f"skipped {tag} (a container uses it)")
    for tag in result.failed:
        print(f"could not remove {tag}", file=sys.stderr)
    print(
        f"{CHECKPOINT_REPO}: kept {len(result.kept)}, {verb} {len(result.removed)}, "
        f"skipped {len(result.in_use)} in use, {len(result.failed)} failed"
    )
    return 1 if result.failed else 0


def _mcp() -> int:
    if not _module_exists("fixedyet.mcp_server"):
        print("error: fixedyet.mcp_server is not installed", file=sys.stderr)
        return 2
    from fixedyet.mcp_server import main as mcp_main

    mcp_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
