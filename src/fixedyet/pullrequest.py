"""Open a DRAFT pull request for a verified backport, or for a verified fix of an open bug.

Only `fixedyet backport --open-pr`, `fixedyet fix --open-pr` and the web demo's maintainer get
here, and only for a result whose status is `verified`. A backport goes to the maintenance
branch (`open_backport_pr`); a fix goes to the default branch, from `fixedyet/fix-<issue>`, and
also adds the reproduction test as a regression test (`open_fix_pr`). Both share the flow below.

    token (GITHUB_TOKEN, else `gh auth token`)  ->  who owns it?
      -> fork the upstream repository into that account (reuse it if it exists) and wait for it
      -> on the host: a temp clone of the verified base commit, `git am` of the sandbox's patch,
         `git push` of fixedyet/backport-<issue>-<branch> to the FORK, over HTTPS with the token
      -> POST /pulls with draft=true. The base is the fork's own copy of the maintenance branch
         (`fork`, the default, so nothing reaches the upstream project) or the upstream branch
         (`upstream`, which needs the repository name typed back, or --yes-upstream).

What this module never does: mark a pull request ready for review, comment on an issue or a pull
request, push to the upstream repository, force-push, or run code from the project (the host only
applies a text patch with git; the project's tests ran in the sandbox). The disclosure line in the
body is fixed text with no switch to turn it off. The token travels in an environment variable of
the git process and in an Authorization header of the API calls; it is never put in a URL, a
command line or a message, and every error text is scrubbed of it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import subprocess
import tempfile
import textwrap
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import Header
from email.utils import format_datetime, formataddr
from pathlib import Path
from typing import Any, Literal

import httpx

from fixedyet.github import parse_issue_url
from fixedyet.models import BackportResult, BackportStatus, FixResult, FixStatus, SideRun
from fixedyet.report import _cell, _fence, _line, _link

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
GITHUB_WEB = "https://github.com"

PrTarget = Literal["fork", "upstream"]

DISCLOSURE = (
    "Prepared with FixedYet (AI-assisted: NVIDIA Nemotron via Nebius Token Factory); "
    "reviewed and submitted by @{login}."
)
"""The last line of every pull request body this module writes. There is no option to drop it."""

TITLE_MAX_CHARS = 120
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./+-]{0,199}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class PullRequestError(RuntimeError):
    """The draft pull request could not be prepared or opened. The text is safe to print."""


@dataclass
class PullRequest:
    url: str
    number: int
    draft: bool
    base_repo: str  # "owner/name" the pull request is in
    base_branch: str
    head: str  # "login:branch"
    fork: str  # "login/name" of the fork the branch was pushed to
    login: str  # owner of the token
    created: bool  # False when an open pull request for this branch already existed


@dataclass
class GitHubUser:
    login: str
    id: int = 0
    name: str = ""


# -- names and text --------------------------------------------------------------------------


def backport_branch_name(issue_number: int, target_branch: str) -> str:
    """``fixedyet/backport-<issue number>-<target branch>``."""
    name = f"fixedyet/backport-{int(issue_number)}-{target_branch}"
    if (
        not _BRANCH_RE.match(target_branch)
        or not _BRANCH_RE.match(name)
        or ".." in name
        or name.endswith((".lock", "/", "."))
    ):
        raise PullRequestError(f"{name!r} is not a usable branch name")
    return name


def fix_summary(result: BackportResult) -> str:
    """A short phrase for the title: the subject of the fix commit without a trailing (#1234)."""
    for line in result.commit_message.splitlines():
        subject = re.sub(r"\s*\(#\d+\)\s*$", "", line).strip()
        if subject:
            return subject.rstrip(".")
    return result.title.strip()


def pr_title(result: BackportResult) -> str:
    """``Backport <short summary of fix> to <target branch>``."""
    branch = result.target_branch or "the maintenance branch"
    tail = f" to {branch}"
    summary = " ".join(fix_summary(result).split()) or "fix"
    room = max(TITLE_MAX_CHARS - len("Backport ") - len(tail), 20)
    if len(summary) > room:
        summary = summary[: room - 3].rstrip() + "..."
    return f"Backport {summary}{tail}"


def diffstat(patch: str) -> list[tuple[str, int, int]]:
    """``(file, added, removed)`` per file of a unified diff or a format-patch."""
    names: list[str] = []
    added: list[int] = []
    removed: list[int] = []
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            names.append(match.group(2) if match else line[len("diff --git "):])
            added.append(0)
            removed.append(0)
            in_hunk = False
        elif line.startswith("@@") and names:
            in_hunk = True
        elif in_hunk:
            if line.startswith("+") and not line.startswith("+++"):
                added[-1] += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed[-1] += 1
    return list(zip(names, added, removed, strict=True))


def pr_body(result: BackportResult, *, login: str) -> str:
    """The pull request description. Ends with the fixed disclosure line."""
    repo, branch = result.repo, result.target_branch or "?"
    base_sha = result.target_branch_head or "?"
    commit = result.fixing_commit or ""
    commit_url = f"{GITHUB_WEB}/{repo}/commit/{commit}" if commit and repo else None
    lines = [
        f"Backport of the fix for {result.issue_url} to `{_cell(branch)}`.",
        "",
        "| | |",
        "|---|---|",
        f"| Issue | {result.issue_url} |",
        f"| Original fix | {_link(commit[:12], commit_url) if commit else '-'} (on main) |",
        f"| Target branch | `{_cell(branch)}`, verified from `{_cell(base_sha)[:12]}` |",
        f"| Cherry-pick | `git cherry-pick -x {_cell(commit[:12])}`, no conflicts |",
        "",
        f"## Reproduction test on `{_cell(branch)}`",
        "",
    ]
    before, after = result.repro_before, result.repro_after
    runs = max((len(s.outcomes) for s in (before, after) if s), default=1) or 1
    lines += [
        "| | " + " | ".join(f"Run {i}" for i in range(1, runs + 1)) + " |",
        "|---|" + "---|" * runs,
    ]
    for label, side in (("Before the cherry-pick", before), ("After the cherry-pick", after)):
        cells = [o.value.upper() for o in side.outcomes] if side else []
        cells += ["-"] * (runs - len(cells))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "The test was written by FixedYet from the issue. It fails at the reported version and "
        "on this branch before the cherry-pick, and passes after it. It is not part of this "
        "pull request.",
    ]
    if result.repro_test_code:
        lines += [
            "",
            "<details><summary>Reproduction test</summary>",
            "",
            _fence(result.repro_test_code, "python"),
            "",
            "</details>",
        ]

    sb, sa = result.suite_before, result.suite_after
    lines += ["", f"## Project test suite on `{_cell(branch)}`", ""]
    if sb and sa:
        lines += ["| | Before the cherry-pick | After the cherry-pick |", "|---|---|---|"]
        for label, name in (("Passed", "passed"), ("Failed", "failed"), ("Errors", "errors"),
                            ("Skipped", "skipped")):
            lines.append(f"| {label} | {getattr(sb, name)} | {getattr(sa, name)} |")
        newly = ", ".join(f"`{_cell(t)}`" for t in result.newly_failing) or "none"
        lines += ["", f"Tests failing after the cherry-pick that did not fail before: {newly}."]
    else:
        lines.append("The suite was not run.")

    stat = diffstat(result.patch)
    subject = fix_summary(result)
    lines += ["", "## Patch summary", ""]
    if subject:
        lines += [f"`{_cell(subject)}`", ""]
    if stat:
        lines += ["| File | Added | Removed |", "|---|---|---|"]
        lines += [f"| `{_cell(name)}` | {added} | {removed} |" for name, added, removed in stat]
        files = len(stat)
        lines += [
            "",
            f"{files} file{'s' if files != 1 else ''} changed, "
            f"{sum(a for _, a, _ in stat)} insertions(+), {sum(r for _, _, r in stat)} "
            "deletions(-).",
        ]
    lines += ["", "---", DISCLOSURE.format(login=_line(login))]
    return "\n".join(lines) + "\n"


# -- the token and the confirmation gate -----------------------------------------------------


def resolve_token(
    configured: str | None,
    *,
    gh: Callable[[], str] | None = None,
) -> str:
    """``GITHUB_TOKEN`` if set, otherwise the output of ``gh auth token``.

    The value is a secret: callers must not print it. ``gh`` replaces the subprocess in tests.
    """
    if configured and configured.strip():
        return configured.strip()
    try:
        token = (gh or _gh_auth_token)().strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise PullRequestError(
            "no GitHub token: set GITHUB_TOKEN, or log in with `gh auth login`"
        ) from exc
    if not token:
        raise PullRequestError("no GitHub token: set GITHUB_TOKEN, or log in with `gh auth login`")
    return token


def _gh_auth_token() -> str:
    done = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        ["gh", "auth", "token"], capture_output=True, text=True, timeout=20, check=True,
    )
    return done.stdout


def confirm_upstream(
    repo: str, *, assume_yes: bool = False, ask: Callable[[str], str] | None = None
) -> bool:
    """The gate for a pull request on the upstream project.

    True when ``assume_yes`` is set (``--yes-upstream``) or the person types the repository name
    (``owner/name``, or just ``name``). Anything else, including a closed stdin, is a refusal.
    """
    if assume_yes:
        return True
    name = repo.split("/", 1)[-1]
    try:
        answer = (ask or input)(
            f"This opens a draft pull request on the UPSTREAM project {repo}.\n"
            f"Type the repository name ({name}) to confirm: "
        )
    except (EOFError, KeyboardInterrupt, OSError):  # no terminal to ask on: a refusal
        return False
    return answer.strip().lower() in {repo.lower(), name.lower()}


# -- the GitHub REST API ---------------------------------------------------------------------


class PullRequestClient:
    """The few REST calls a draft backport needs. All of them act as the token's owner."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = API_ROOT,
        poll_interval_s: float = 3.0,
        fork_timeout_s: float = 180.0,
    ) -> None:
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=30.0)
        self._poll = poll_interval_s
        self._fork_timeout = fork_timeout_s

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fixedyet",
            "Authorization": f"Bearer {self._token}",
        }

    async def _call(
        self, method: str, path: str, *, ok: tuple[int, ...] = (200,),
        json: dict[str, Any] | None = None, params: dict[str, str] | None = None,
        allow: tuple[int, ...] = (),
    ) -> httpx.Response:
        """One API call. Statuses in ``allow`` are returned to the caller; other failures raise."""
        try:
            response = await self._client.request(
                method, path, headers=self._headers(), json=json, params=params
            )
        except httpx.HTTPError as exc:
            raise PullRequestError(f"GitHub request failed: {self._scrub(str(exc))}") from exc
        if response.status_code in ok or response.status_code in allow:
            return response
        message = ""
        try:
            message = str(response.json().get("message", ""))
        except (ValueError, AttributeError):
            message = response.text[:200]
        hint = ""
        if response.status_code in (401, 403):
            hint = (
                " The token needs to create forks, push to the fork and open pull requests "
                "(fine-grained: contents and pull requests write on the fork, plus the fork "
                "permission; classic: public_repo)."
            )
        raise PullRequestError(
            f"GitHub returned {response.status_code} for {method} {path}: "
            f"{self._scrub(message)[:300]}.{hint}"
        )

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def whoami(self) -> GitHubUser:
        data = (await self._call("GET", "/user")).json()
        login = str(data.get("login") or "")
        if not login:
            raise PullRequestError("GitHub did not say who owns the token")
        return GitHubUser(
            login=login, id=int(data.get("id") or 0), name=str(data.get("name") or "")
        )

    async def ensure_fork(self, upstream: str, login: str, *, say: Callable[[str], None]) -> str:
        """``login/name`` of the token owner's fork of ``upstream``; created and awaited if new."""
        name = upstream.split("/", 1)[1]
        mine = f"{login}/{name}"
        found = await self._call("GET", f"/repos/{mine}", allow=(404,))
        if found.status_code == 200:
            info = found.json()
            parent = (info.get("parent") or {}).get("full_name", "")
            if info.get("fork") and parent.lower() == upstream.lower():
                say(f"Reusing the fork {mine}")
                return mine
        say(f"Forking {upstream} into {login}'s account")
        created = await self._call(
            "POST", f"/repos/{upstream}/forks", ok=(200, 201, 202),
            json={"default_branch_only": False},
        )
        fork = str(created.json().get("full_name") or mine)
        await self._wait_for_fork(fork, say)
        return fork

    async def _wait_for_fork(self, fork: str, say: Callable[[str], None]) -> None:
        waited = 0.0
        while True:
            response = await self._call("GET", f"/repos/{fork}", allow=(404,))
            if response.status_code == 200:
                default = response.json().get("default_branch") or "main"
                ref = await self._call(
                    "GET", f"/repos/{fork}/git/ref/heads/{default}", allow=(404, 409)
                )
                if ref.status_code == 200:
                    return
            if waited >= self._fork_timeout:
                raise PullRequestError(
                    f"the fork {fork} was not ready after {self._fork_timeout:.0f} s; "
                    "try again in a minute"
                )
            say(f"Waiting for the fork {fork}")
            await asyncio.sleep(self._poll)
            waited += max(self._poll, 0.001)

    async def branch_sha(self, repo: str, branch: str) -> str | None:
        response = await self._call(
            "GET", f"/repos/{repo}/branches/{branch}", allow=(404,)
        )
        if response.status_code != 200:
            return None
        return str((response.json().get("commit") or {}).get("sha") or "") or None

    async def sync_fork_branch(self, fork: str, branch: str) -> bool:
        """Fast-forward the fork's copy of ``branch`` to upstream. False when it could not."""
        response = await self._call(
            "POST", f"/repos/{fork}/merge-upstream", json={"branch": branch},
            allow=(404, 409, 422),
        )
        return response.status_code == 200

    async def find_open_pr(self, repo: str, head: str, base: str) -> dict[str, Any] | None:
        response = await self._call(
            "GET", f"/repos/{repo}/pulls",
            params={"state": "open", "head": head, "base": base},
        )
        items = response.json()
        return items[0] if isinstance(items, list) and items else None

    async def create_draft_pr(
        self, repo: str, *, title: str, head: str, base: str, body: str
    ) -> dict[str, Any]:
        """POST /pulls. ``draft`` is hard-wired to true: this module has no way to ask for more."""
        response = await self._call(
            "POST", f"/repos/{repo}/pulls", ok=(201,),
            json={"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        return response.json()


# -- pushing from the host, without running project code --------------------------------------


@dataclass
class PushRequest:
    upstream_url: str  # where the verified base commit is fetched from (no credentials)
    fork_url: str  # where the branch is pushed (the token is attached only for this)
    base_sha: str
    base_branch: str
    branch: str
    patch: str
    committer_name: str
    committer_email: str
    token: str
    push_base_branch: bool = False  # also create base_branch on the fork at base_sha


Pusher = Callable[[PushRequest], Awaitable[str]]
"""Pushes the backport branch and returns the sha of the commit it pushed. Tests replace it."""


def _git_env(home: Path, request: PushRequest, *, with_token: bool) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_COMMITTER_NAME": request.committer_name,
        "GIT_COMMITTER_EMAIL": request.committer_email,
        "LC_ALL": "C",
    }
    if with_token:
        basic = base64.b64encode(f"x-access-token:{request.token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}",
        )
    return env


async def push_with_git(request: PushRequest) -> str:
    """Apply the patch to a throwaway clone of the verified base commit and push it to the fork.

    The host runs only git: `fetch`, `checkout`, `am` (a text patch), `push`. Hooks are switched
    off and the user's git config is not read.
    """
    secrets = [request.token, base64.b64encode(f"x-access-token:{request.token}".encode()).decode()]

    def scrub(text: str) -> str:
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    async def git(*args: str, cwd: Path, env: dict[str, str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", f"core.hooksPath={os.devnull}", *args, cwd=cwd, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except TimeoutError as exc:
            proc.kill()
            raise PullRequestError(f"git {args[0]} timed out") from exc
        if proc.returncode != 0:
            detail = scrub(err.decode("utf-8", errors="replace")).strip()[-400:]
            raise PullRequestError(f"git {args[0]} failed: {detail}")
        return out.decode("utf-8", errors="replace").strip()

    with tempfile.TemporaryDirectory(prefix="fixedyet-pr-") as tmp:
        root = Path(tmp)
        home, work = root / "home", root / "work"
        home.mkdir()
        work.mkdir()
        (root / "backport.patch").write_text(request.patch, encoding="utf-8", newline="")
        plain = _git_env(home, request, with_token=False)
        authed = _git_env(home, request, with_token=True)

        await git("init", "--quiet", cwd=work, env=plain)
        await git("fetch", "--quiet", "--depth", "1", request.upstream_url, request.base_sha,
                  cwd=work, env=plain)
        await git("checkout", "--quiet", "-b", request.branch, "FETCH_HEAD", cwd=work, env=plain)
        await git("am", "--quiet", "--committer-date-is-author-date", str(root / "backport.patch"),
                  cwd=work, env=plain)
        pushed = await git("rev-parse", "HEAD", cwd=work, env=plain)
        refspecs = [f"HEAD:refs/heads/{request.branch}"]
        if request.push_base_branch:
            refspecs.append(f"{request.base_sha}:refs/heads/{request.base_branch}")
        try:
            await git("push", "--quiet", request.fork_url, *refspecs, cwd=work, env=authed)
        except PullRequestError as exc:
            if re.search(r"rejected|non-fast-forward|fetch first", str(exc)):
                raise PullRequestError(
                    f"the branch {request.branch} already exists on the fork with different "
                    "content; delete it or rename it, then try again (FixedYet never force-pushes)"
                ) from exc
            raise
        return pushed


# -- the whole thing -------------------------------------------------------------------------


def _pull_request(
    data: dict[str, Any], base_repo: str, branch: str, head: str, fork: str, login: str,
    *, created: bool,
) -> PullRequest:
    return PullRequest(
        url=str(data.get("html_url", "")), number=int(data.get("number") or 0),
        draft=bool(data.get("draft", True)), base_repo=base_repo, base_branch=branch,
        head=head, fork=fork, login=login, created=created,
    )


async def open_backport_pr(
    result: BackportResult,
    *,
    token: str,
    target: PrTarget = "fork",
    confirmed_upstream: bool = False,
    client: httpx.AsyncClient | None = None,
    pusher: Pusher | None = None,
    say: Callable[[str], None] | None = None,
    poll_interval_s: float = 3.0,
    fork_timeout_s: float = 180.0,
    web_base: str = GITHUB_WEB,
) -> PullRequest:
    """Fork, push the backport branch to the fork and open a DRAFT pull request.

    ``target="fork"`` (default) opens it against the fork's own copy of the maintenance branch,
    so nothing reaches the upstream project. ``target="upstream"`` opens it on the upstream
    project and is refused unless ``confirmed_upstream`` is True (the caller asks the person).
    """
    if result.status is not BackportStatus.VERIFIED:
        raise PullRequestError(
            f"only a verified backport gets a pull request; this one is {result.status.value}"
        )
    branch, base_sha = result.target_branch, result.target_branch_head
    if not (result.patch and branch and base_sha and _SHA_RE.match(base_sha)):
        raise PullRequestError("the backport result has no patch or no base commit")
    if not _REPO_RE.match(result.repo) or not _BRANCH_RE.match(branch):
        raise PullRequestError("the backport result names an invalid repository or branch")
    _check_target(result.repo, target, confirmed_upstream)
    try:
        _, _, issue_number = parse_issue_url(result.issue_url)
    except ValueError as exc:
        raise PullRequestError(f"not a GitHub issue URL: {result.issue_url!r}") from exc
    return await _open_draft(
        repo=result.repo, base_branch=branch, base_sha=base_sha,
        pr_branch=backport_branch_name(issue_number, branch), title=pr_title(result),
        make_body=lambda me: pr_body(result, login=me.login),
        make_patch=lambda me: result.patch,
        token=token, target=target, client=client, pusher=pusher, say=say,
        poll_interval_s=poll_interval_s, fork_timeout_s=fork_timeout_s, web_base=web_base,
    )


def _check_target(repo: str, target: str, confirmed_upstream: bool) -> None:
    if target not in ("fork", "upstream"):
        raise PullRequestError(f"unknown pull request target {target!r}")
    if target == "upstream" and not confirmed_upstream:
        raise PullRequestError(
            f"a pull request on the upstream project {repo} needs explicit confirmation"
        )


async def _open_draft(
    *,
    repo: str,
    base_branch: str,
    base_sha: str,
    pr_branch: str,
    title: str,
    make_body: Callable[[GitHubUser], str],
    make_patch: Callable[[GitHubUser], str],
    token: str,
    target: str,
    client: httpx.AsyncClient | None,
    pusher: Pusher | None,
    say: Callable[[str], None] | None,
    poll_interval_s: float,
    fork_timeout_s: float,
    web_base: str,
) -> PullRequest:
    """The shared flow: who owns the token, the fork, an existing pull request, the push of
    ``pr_branch`` to the fork, and the draft pull request against ``base_branch``. The caller has
    validated the result and the target already."""
    say = say or (lambda _message: None)
    gh = PullRequestClient(
        token, client=client, poll_interval_s=poll_interval_s, fork_timeout_s=fork_timeout_s
    )
    try:
        me = await gh.whoami()
        say(f"The token belongs to @{me.login}")
        owner = repo.split("/", 1)[0]
        if me.login.lower() == owner.lower():
            raise PullRequestError(
                f"the token belongs to the owner of {repo}, so there is no fork to push to"
            )
        fork = await gh.ensure_fork(repo, me.login, say=say)
        head = f"{me.login}:{pr_branch}"

        push_base = False
        if target == "fork":
            synced = await gh.sync_fork_branch(fork, base_branch)
            if not synced:
                say(f"Could not fast-forward {fork}:{base_branch} to upstream")
            push_base = await gh.branch_sha(fork, base_branch) is None
        base_repo = fork if target == "fork" else repo

        existing = await gh.find_open_pr(base_repo, head, base_branch)
        if existing:
            say("An open pull request for this branch already exists")
            return _pull_request(
                existing, base_repo, base_branch, head, fork, me.login, created=False
            )

        say(f"Pushing {pr_branch} to {fork}")
        email = noreply_email(me)
        request = PushRequest(
            upstream_url=f"{web_base}/{repo}.git", fork_url=f"{web_base}/{fork}.git",
            base_sha=base_sha, base_branch=base_branch, branch=pr_branch,
            patch=make_patch(me), committer_name=me.name or me.login, committer_email=email,
            token=token, push_base_branch=push_base,
        )
        await (pusher or push_with_git)(request)

        say(f"Opening a draft pull request on {base_repo}")
        created = await gh.create_draft_pr(
            base_repo, title=title, head=head, base=base_branch, body=make_body(me),
        )
        return _pull_request(created, base_repo, base_branch, head, fork, me.login, created=True)
    finally:
        await gh.aclose()


def noreply_email(me: GitHubUser) -> str:
    """The token owner's GitHub noreply address, which keeps their real address private."""
    if me.id:
        return f"{me.id}+{me.login}@users.noreply.github.com"
    return f"{me.login}@users.noreply.github.com"


# -- a verified fix of an open bug ------------------------------------------------------------

FIX_ATTRIBUTION = (
    "Written with FixedYet: the change was proposed by an AI model (NVIDIA Nemotron via Nebius "
    "Token Factory) and verified in a sandbox. The regression test fails before it and passes "
    "after it, and the project's test suite has no newly failing test."
)


def fix_branch_name(issue_number: int) -> str:
    """``fixedyet/fix-<issue number>``."""
    return f"fixedyet/fix-{int(issue_number)}"


def _issue_number(result: FixResult) -> int:
    try:
        return parse_issue_url(result.issue_url)[2]
    except ValueError as exc:
        raise PullRequestError(f"not a GitHub issue URL: {result.issue_url!r}") from exc


def _no_mentions(text: str) -> str:
    """Model-written text cannot ping anyone: a zero-width space after every ``@name``'s @."""
    return re.sub(r"(?<![\w`])@(?=[A-Za-z0-9])", "@\u200b", text)


def _code_names(text: str) -> str:
    """Dunder and snake_case-with-leading-underscore names in model prose as code spans, so that
    markdown does not turn ``__html__`` into a bold "html"."""
    return re.sub(r"(?<![`\w])(_{1,2}\w+?_{0,2})(?![`\w])",
                  lambda m: f"`{m.group(1)}`" if m.group(1).startswith("_") else m.group(1), text)


def fix_pr_title(result: FixResult) -> str:
    """The one-line summary of the change, or ``Fix #<n>: <issue title>``."""
    summary = " ".join(result.summary.split()) or f"Fix #{_issue_number(result)}: {result.title}"
    summary = _no_mentions(summary)
    if len(summary) > TITLE_MAX_CHARS:
        summary = summary[: TITLE_MAX_CHARS - 3].rstrip() + "..."
    return summary


def _runs_table(rows: list[tuple[str, SideRun | None]]) -> list[str]:
    runs = max((len(s.outcomes) for _, s in rows if s), default=1) or 1
    out = [
        "| | " + " | ".join(f"Run {i}" for i in range(1, runs + 1)) + " |",
        "|---|" + "---|" * runs,
    ]
    for label, side in rows:
        cells = [o.value.upper() for o in side.outcomes] if side else []
        cells += ["-"] * (runs - len(cells))
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    return out


def fix_pr_body(result: FixResult, *, login: str) -> str:
    """The pull request description of a verified fix. Ends with the fixed disclosure line."""
    number = _issue_number(result)
    branch = result.base_branch or "main"
    sha = result.base_sha or ""
    commit_url = f"{GITHUB_WEB}/{result.repo}/commit/{sha}" if sha and result.repo else None
    stat = diffstat(result.diff)
    attempt = next((a.number for a in result.attempts if a.outcome == "verified"),
                   len(result.attempts))
    lines = [
        f"Fixes #{number}.",
        "",
        "**This change was written by an AI model and verified by machine, not written by a "
        "person.** FixedYet asked NVIDIA Nemotron for the smallest change that makes the "
        "regression test below pass, then checked it in a sandbox: the test fails on "
        f"`{_cell(branch)}` without the change and passes with it (twice each), and the "
        "project's own test suite has no newly failing test. A passing suite is evidence, not a "
        "review.",
        "",
        "| | |",
        "|---|---|",
        f"| Issue | {result.issue_url} |",
        f"| Verified on | `{_cell(branch)}` at {_link(sha[:12], commit_url) if sha else '-'} |",
        f"| Change | {len(stat)} file{'s' if len(stat) != 1 else ''}, "
        f"+{sum(a for _, a, _ in stat)} / -{sum(r for _, _, r in stat)} lines, attempt "
        f"{attempt} of the model |",
        "",
        "## What the change does",
        "",
        "_The model's own account. The sandbox checked the behaviour, not this text._",
        "",
        _code_names(_no_mentions(_line(result.explanation))) or "(no explanation was given)",
        "",
        f"## Regression test on `{_cell(branch)}`",
        "",
        *_runs_table([("Without the change", result.repro_before),
                      ("With the change", result.repro_after)]),
        "",
    ]
    test_path = result.regression_test_path
    if test_path:
        lines.append(
            f"The test was written by FixedYet (by the model) from the issue. This pull request "
            f"adds it as `{_cell(test_path)}`, where it ran in every check above."
        )
    else:
        lines.append(
            "The test was written by FixedYet (by the model) from the issue. The repository has "
            "no `tests/`, `test/` or `testing/` directory, so it is **not** part of this pull "
            "request; it is below for reviewers."
        )
    if result.repro_test_code:
        lines += [
            "",
            "<details><summary>Regression test</summary>",
            "",
            _fence(result.repro_test_code, "python"),
            "",
            "</details>",
        ]
    sb, sa = result.suite_before, result.suite_after
    lines += ["", f"## Project test suite on `{_cell(branch)}`", ""]
    if sb and sa:
        lines += ["| | Without the change | With the change |", "|---|---|---|"]
        for label, name in (("Passed", "passed"), ("Failed", "failed"), ("Errors", "errors"),
                            ("Skipped", "skipped")):
            lines.append(f"| {label} | {getattr(sb, name)} | {getattr(sa, name)} |")
        newly = ", ".join(f"`{_cell(t)}`" for t in result.newly_failing) or "none"
        lines += ["", f"Tests failing with the change that did not fail without it: {newly}."]
        if sb.failed or sb.errors:
            lines.append("Tests that already failed without the change are the baseline and are "
                         "not blamed on it.")
    else:
        lines.append("The suite was not run.")
    lines += ["", "## Patch summary", ""]
    if result.summary:
        lines += [f"`{_cell(result.summary)}`", ""]
    if stat:
        lines += ["| File | Added | Removed |", "|---|---|---|"]
        lines += [f"| `{_cell(name)}` | {added} | {removed} |" for name, added, removed in stat]
        lines += [
            "",
            f"{len(stat)} source file{'s' if len(stat) != 1 else ''} changed, "
            f"{sum(a for _, a, _ in stat)} insertions(+), {sum(r for _, _, r in stat)} "
            "deletions(-)" + (", plus the new regression test." if test_path else "."),
        ]
    lines += ["", "---", DISCLOSURE.format(login=_line(login))]
    return "\n".join(lines) + "\n"


def new_file_diff(path: str, text: str) -> str:
    """A git diff that creates ``path`` with ``text``."""
    body = text.splitlines()
    count = len(body)
    span = "1" if count == 1 else f"1,{count}"
    out = [f"diff --git a/{path} b/{path}", "new file mode 100644", "--- /dev/null",
           f"+++ b/{path}", f"@@ -0,0 +{span} @@", *(f"+{line}" for line in body)]
    tail = "" if text.endswith("\n") else "\\ No newline at end of file\n"
    return "\n".join(out) + "\n" + tail


def fix_commit_message(result: FixResult) -> str:
    """Subject, ``Fixes #n.``, the model's explanation (wrapped) and the attribution. No line
    starts with ``-`` or ``diff`` so that `git am` cannot take the message for the patch."""
    number = _issue_number(result)
    subject = fix_pr_title(result)
    parts = [subject, "", f"Fixes #{number}."]
    if result.explanation:
        parts += ["", *textwrap.wrap(_no_mentions(_line(result.explanation)), 72)]
    parts += ["", *textwrap.wrap(FIX_ATTRIBUTION, 72)]
    safe = [(" " + p) if re.match(r"^(-|diff |Index: )", p) else p for p in parts]
    return "\n".join(safe)


def fix_patch(
    result: FixResult, *, author_name: str, author_email: str, when: datetime | None = None,
) -> str:
    """The verified change plus the new regression test as one mbox patch for `git am`, authored
    by the person who submits it."""
    if not result.diff.strip():
        raise PullRequestError("the fix result has no diff")
    message = fix_commit_message(result)
    subject, _, body = message.partition("\n")
    subject_header = subject if subject.isascii() else Header(subject, "utf-8").encode()
    diff = result.diff if result.diff.endswith("\n") else result.diff + "\n"
    if result.regression_test_path and result.repro_test_code:
        diff += new_file_diff(result.regression_test_path, result.repro_test_code)
    return (
        "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
        f"From: {formataddr((author_name, author_email))}\n"
        f"Date: {format_datetime(when or datetime.now(UTC))}\n"
        f"Subject: [PATCH] {subject_header}\n"
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n"
        "Content-Transfer-Encoding: 8bit\n"
        f"\n{body.lstrip(chr(10))}\n---\n\n{diff}-- \nFixedYet\n"
    )


def fix_pr_preview(result: FixResult, *, login: str) -> str:
    """What the draft pull request would look like: title, base and head, body, and the diff that
    GitHub shows under "Files changed". Nothing is sent anywhere."""
    number = _issue_number(result)
    head = f"{login}:{fix_branch_name(number)}"
    patch = result.diff if result.diff.endswith("\n") else result.diff + "\n"
    if result.regression_test_path and result.repro_test_code:
        patch += new_file_diff(result.regression_test_path, result.repro_test_code)
    return (
        f"# {fix_pr_title(result)}\n\n"
        f"base: {result.repo} {result.base_branch or 'main'}  \u00b7  head: {head}  "
        "\u00b7  DRAFT\n\n"
        "---\n\n"
        f"{fix_pr_body(result, login=login)}\n"
        "---\n\n"
        "## Files changed (what GitHub shows in the diff tab)\n\n"
        f"{_fence(patch, 'diff')}\n"
    )


async def open_fix_pr(
    result: FixResult,
    *,
    token: str,
    target: PrTarget = "fork",
    confirmed_upstream: bool = False,
    client: httpx.AsyncClient | None = None,
    pusher: Pusher | None = None,
    say: Callable[[str], None] | None = None,
    poll_interval_s: float = 3.0,
    fork_timeout_s: float = 180.0,
    web_base: str = GITHUB_WEB,
) -> PullRequest:
    """Fork, push ``fixedyet/fix-<issue>`` (the verified change and the regression test, authored
    by the token's owner) and open a DRAFT pull request against the default branch.

    ``target="fork"`` (default) opens it against the fork's own copy of the default branch, so
    nothing reaches the upstream project. ``target="upstream"`` opens it on the upstream project
    and is refused unless ``confirmed_upstream`` is True (the caller asks the person).
    """
    if result.status is not FixStatus.VERIFIED:
        raise PullRequestError(
            f"only a verified fix gets a pull request; this one is {result.status.value}"
        )
    branch, base_sha = result.base_branch, result.base_sha
    if not (result.diff.strip() and branch and base_sha and _SHA_RE.match(base_sha)):
        raise PullRequestError("the fix result has no diff or no base commit")
    if not _REPO_RE.match(result.repo) or not _BRANCH_RE.match(branch):
        raise PullRequestError("the fix result names an invalid repository or branch")
    _check_target(result.repo, target, confirmed_upstream)
    number = _issue_number(result)

    def make_patch(me: GitHubUser) -> str:
        return fix_patch(result, author_name=me.name or me.login, author_email=noreply_email(me))

    return await _open_draft(
        repo=result.repo, base_branch=branch, base_sha=base_sha,
        pr_branch=fix_branch_name(number), title=fix_pr_title(result),
        make_body=lambda me: fix_pr_body(result, login=me.login), make_patch=make_patch,
        token=token, target=target, client=client, pusher=pusher, say=say,
        poll_interval_s=poll_interval_s, fork_timeout_s=fork_timeout_s, web_base=web_base,
    )
