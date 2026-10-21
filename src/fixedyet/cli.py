"""Command line: `fixedyet run | doctor | eval | serve | mcp | gc`.

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
from fixedyet.models import Assessment, ProgressEvent

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

    doctor = commands.add_parser("doctor", help="check keys, model access and the sandbox")
    doctor.add_argument("--tavily", action="store_true", help="also test Tavily (uses 1 credit)")

    commands.add_parser(
        "eval", help="run the SWE-bench evaluation (arguments are passed to fixedyet.eval.run)",
        add_help=False,
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
    args = build_parser().parse_args(args_list)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "run":
        return _run(args)
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
    detail = f"spent ${spent:.4f} of ${budget:.2f}, remaining ${left:.4f}"
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
