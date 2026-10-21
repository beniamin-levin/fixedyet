"""Draft pull requests for verified backports, against a mocked GitHub API.

No network and no paid call. The GitHub REST API is mocked with respx, the push is a fake that
records what it was asked to do, and one test pushes between local bare repositories with the
real git to show the patch handling and the token handling end to end.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from fixedyet import pullrequest as pr
from fixedyet.models import BackportResult, BackportStatus, Outcome, SideRun, SuiteRun

API = "https://api.github.com"
TOKEN = "ghp_SECRETSECRETSECRET1234"
FIX = "d959898db264aaf07e70ad4eafa254286f9a5185"
BASE = "b" * 40
DISCLOSURE = (
    "Prepared with FixedYet (AI-assisted: NVIDIA Nemotron via Nebius Token Factory); "
    "reviewed and submitted by @alice."
)
PATCH = (
    f"From {FIX} Mon Sep 17 00:00:00 2001\n"
    "From: Dev <dev@example.com>\n"
    "Date: Tue, 1 Sep 2026 10:00:00 +0000\n"
    "Subject: [PATCH] Keep the last item in parse() (#7)\n\n"
    f"(cherry picked from commit {FIX})\n---\n"
    " widgets/__init__.py | 2 +-\n"
    " tests/test_parse.py | 4 ++++\n\n"
    "diff --git a/widgets/__init__.py b/widgets/__init__.py\n"
    "index 111..222 100644\n--- a/widgets/__init__.py\n+++ b/widgets/__init__.py\n"
    "@@ -1,2 +1,2 @@\n def parse(text):\n"
    "-    return text.split(',')[:-1]\n+    return text.split(',')\n"
    "diff --git a/tests/test_parse.py b/tests/test_parse.py\n"
    "new file mode 100644\n--- /dev/null\n+++ b/tests/test_parse.py\n"
    "@@ -0,0 +1,4 @@\n+from widgets import parse\n+\n+\n+def test_parse():\n"
)


def verified(**overrides: object) -> BackportResult:
    data: dict[str, object] = dict(
        status=BackportStatus.VERIFIED,
        reason="verified",
        issue_url="https://github.com/acme/widgets/issues/42",
        title="parse() drops the last item",
        repo="acme/widgets",
        reported_version="8.1.3",
        target_branch="8.1.x",
        target_branch_head=BASE,
        fixing_commit=FIX,
        cherry_picked_commit="c" * 40,
        commit_message=(
            f"Keep the last item in parse() (#7)\n\nbody\n\n(cherry picked from commit {FIX})"
        ),
        patch=PATCH,
        repro_test_path="tests/test_issue_repro.py",
        repro_test_code="def test_x():\n    assert parse('a,b') == ['a', 'b']\n",
        repro_before=SideRun(ref="8.1.x (before)", outcomes=[Outcome.FAIL, Outcome.FAIL]),
        repro_after=SideRun(ref="8.1.x (after)", outcomes=[Outcome.PASS, Outcome.PASS]),
        suite_before=SuiteRun(ran=True, passed=120, failed=1, failing_tests=["t::known"]),
        suite_after=SuiteRun(ran=True, passed=121, failed=1, failing_tests=["t::known"]),
    )
    data.update(overrides)
    return BackportResult(**data)  # type: ignore[arg-type]


# -- names, title, body ------------------------------------------------------------------------


def test_branch_name_follows_the_documented_pattern() -> None:
    assert pr.backport_branch_name(42, "8.1.x") == "fixedyet/backport-42-8.1.x"
    assert pr.backport_branch_name(7, "stable/8.1") == "fixedyet/backport-7-stable/8.1"


@pytest.mark.parametrize("bad", ["a b", "x;rm", "../x", "x.lock", ""])
def test_branch_name_refuses_odd_target_branches(bad: str) -> None:
    with pytest.raises(pr.PullRequestError):
        pr.backport_branch_name(1, bad)


def test_title_is_backport_summary_to_branch() -> None:
    assert pr.pr_title(verified()) == "Backport Keep the last item in parse() to 8.1.x"


def test_title_drops_the_original_pull_request_number_and_stays_short() -> None:
    title = pr.pr_title(verified(commit_message="x" * 300 + " (#99)"))
    assert "(#99)" not in title and len(title) <= pr.TITLE_MAX_CHARS
    assert title.startswith("Backport ") and title.endswith(" to 8.1.x")
    assert pr.pr_title(verified(commit_message="")) == (
        "Backport parse() drops the last item to 8.1.x"
    )


def test_diffstat_counts_added_and_removed_lines_per_file() -> None:
    assert pr.diffstat(PATCH) == [
        ("widgets/__init__.py", 1, 1),
        ("tests/test_parse.py", 4, 0),
    ]
    assert pr.diffstat("") == []


def test_body_has_every_required_part() -> None:
    body = pr.pr_body(verified(), login="alice")
    # links to the issue and the original commit
    assert "https://github.com/acme/widgets/issues/42" in body
    assert f"https://github.com/acme/widgets/commit/{FIX}" in body
    # the reproduction test before and after the cherry-pick, on that branch
    assert "## Reproduction test on `8.1.x`" in body
    assert "| Before the cherry-pick | FAIL | FAIL |" in body
    assert "| After the cherry-pick | PASS | PASS |" in body
    # the suite results
    assert "## Project test suite on `8.1.x`" in body
    assert "| Passed | 120 | 121 |" in body and "| Failed | 1 | 1 |" in body
    assert "did not fail before: none." in body
    # the patch summary
    assert "## Patch summary" in body
    assert "| `widgets/__init__.py` | 1 | 1 |" in body
    assert "2 files changed, 5 insertions(+), 1 deletions(-)." in body
    assert "git cherry-pick -x d959898db264" in body


def test_body_ends_with_the_exact_disclosure_line() -> None:
    body = pr.pr_body(verified(), login="alice")
    assert body.rstrip("\n").splitlines()[-1] == DISCLOSURE
    assert body.count("Prepared with FixedYet") == 1


def test_the_disclosure_cannot_be_switched_off() -> None:
    assert pr.DISCLOSURE.format(login="alice") == DISCLOSURE
    for function in (pr.pr_body, pr.open_backport_pr, pr.PullRequestClient.create_draft_pr):
        names = set(inspect.signature(function).parameters)
        assert not {n for n in names if "disclos" in n or "ai_" in n or "attribution" in n}
    # whatever the result looks like, the line is there
    for result in (
        verified(),
        verified(suite_before=None, suite_after=None, repro_test_code=None, patch=""),
        verified(commit_message="@everyone `x` | y\n\n```\nboom"),
    ):
        assert pr.pr_body(result, login="alice").rstrip().endswith(DISCLOSURE)


def test_the_login_in_the_disclosure_is_the_token_owners() -> None:
    assert pr.pr_body(verified(), login="bob-the-dev").rstrip().endswith(
        "reviewed and submitted by @bob-the-dev."
    )


def test_body_keeps_untrusted_text_from_breaking_out() -> None:
    body = pr.pr_body(
        verified(commit_message="Fix @everyone | now\nsecond line"), login="alice"
    )
    assert "`Fix @everyone \\| now`" in body  # a code span: no mention, no table break


def test_body_includes_the_reproduction_test_for_reviewers() -> None:
    body = pr.pr_body(verified(), login="alice")
    assert "<details><summary>Reproduction test</summary>" in body
    assert "assert parse('a,b') == ['a', 'b']" in body


# -- token and the upstream gate -----------------------------------------------------------------


def test_the_configured_token_is_used_as_is() -> None:
    assert pr.resolve_token(f"  {TOKEN} ", gh=lambda: pytest.fail("gh must not run")) == TOKEN


@pytest.mark.parametrize("configured", ["", "   ", None])
def test_gh_auth_token_is_the_fallback_for_an_empty_token(configured: str | None) -> None:
    assert pr.resolve_token(configured, gh=lambda: f"{TOKEN}\n") == TOKEN


def test_no_token_anywhere_is_an_error_that_says_what_to_do() -> None:
    with pytest.raises(pr.PullRequestError, match="GITHUB_TOKEN"):
        pr.resolve_token("", gh=lambda: "\n")

    def missing() -> str:
        raise FileNotFoundError("gh")

    with pytest.raises(pr.PullRequestError, match="gh auth login"):
        pr.resolve_token(None, gh=missing)


def test_upstream_needs_the_repository_name_typed_back() -> None:
    prompts: list[str] = []

    def typed(answer: str):
        def ask(prompt: str) -> str:
            prompts.append(prompt)
            return answer
        return ask

    assert pr.confirm_upstream("acme/widgets", ask=typed("widgets")) is True
    assert pr.confirm_upstream("acme/widgets", ask=typed(" acme/widgets ")) is True
    assert pr.confirm_upstream("acme/widgets", ask=typed("Widgets")) is True
    for wrong in ("", "yes", "y", "acme", "widget", "other/widgets"):
        assert pr.confirm_upstream("acme/widgets", ask=typed(wrong)) is False
    assert "UPSTREAM" in prompts[0] and "acme/widgets" in prompts[0] and "(widgets)" in prompts[0]


def test_yes_upstream_skips_the_question() -> None:
    def never(_: str) -> str:
        raise AssertionError("must not ask")

    assert pr.confirm_upstream("acme/widgets", assume_yes=True, ask=never) is True


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt, OSError])
def test_a_closed_stdin_is_a_refusal(error: type[BaseException]) -> None:
    def ask(_: str) -> str:
        raise error

    assert pr.confirm_upstream("acme/widgets", ask=ask) is False


# -- the GitHub flow ----------------------------------------------------------------------------


class Pushed:
    def __init__(self) -> None:
        self.requests: list[pr.PushRequest] = []

    async def __call__(self, request: pr.PushRequest) -> str:
        self.requests.append(request)
        return "e" * 40


def mock_github(
    router: respx.MockRouter, *, fork: str = "exists", branch_on_fork: bool = True,
    open_pr: list | None = None, login: str = "alice", sync: int = 200,
) -> dict[str, respx.Route]:
    """Routes for the whole flow. ``fork``: exists | new | clash."""
    routes: dict[str, respx.Route] = {}
    routes["user"] = router.get("/user").respond(json={"login": login, "id": 7, "name": "Alice A"})
    fork_info = {"fork": True, "parent": {"full_name": "acme/widgets"}, "default_branch": "main"}
    if fork == "exists":
        routes["fork_get"] = router.get("/repos/alice/widgets").respond(json=fork_info)
    elif fork == "new":
        routes["fork_get"] = router.get("/repos/alice/widgets").mock(
            side_effect=[
                httpx.Response(404), httpx.Response(404), httpx.Response(200, json=fork_info),
            ]
        )
        routes["fork_post"] = router.post("/repos/acme/widgets/forks").respond(
            202, json={"full_name": "alice/widgets"}
        )
    elif fork == "clash":  # alice has an unrelated repo called widgets
        routes["fork_get"] = router.get("/repos/alice/widgets").respond(
            json={"fork": False, "default_branch": "main"}
        )
        routes["fork_post"] = router.post("/repos/acme/widgets/forks").respond(
            202, json={"full_name": "alice/widgets-1"}
        )
        router.get("/repos/alice/widgets-1").respond(json=fork_info)
        router.get("/repos/alice/widgets-1/git/ref/heads/main").respond(json={})
        router.post("/repos/alice/widgets-1/merge-upstream").respond(200, json={})
        router.get("/repos/alice/widgets-1/branches/8.1.x").respond(
            json={"commit": {"sha": BASE}}
        )
        router.get("/repos/alice/widgets-1/pulls").respond(json=[])
        routes["pr_post"] = router.post("/repos/alice/widgets-1/pulls").respond(
            201, json={"html_url": "https://github.com/alice/widgets-1/pull/3", "number": 3,
                       "draft": True},
        )
        return routes
    router.get("/repos/alice/widgets/git/ref/heads/main").respond(json={})
    routes["sync"] = router.post("/repos/alice/widgets/merge-upstream").respond(sync, json={})
    routes["branch"] = router.get("/repos/alice/widgets/branches/8.1.x").respond(
        status_code=200 if branch_on_fork else 404,
        json={"commit": {"sha": BASE}} if branch_on_fork else {},
    )
    routes["fork_prs"] = router.get("/repos/alice/widgets/pulls").respond(json=open_pr or [])
    routes["upstream_prs"] = router.get("/repos/acme/widgets/pulls").respond(json=open_pr or [])
    routes["pr_fork"] = router.post("/repos/alice/widgets/pulls").respond(
        201,
        json={"html_url": "https://github.com/alice/widgets/pull/5", "number": 5, "draft": True},
    )
    routes["pr_upstream"] = router.post("/repos/acme/widgets/pulls").respond(
        201, json={"html_url": "https://github.com/acme/widgets/pull/99", "number": 99,
                   "draft": True},
    )
    return routes


@pytest.fixture
def api():
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


async def open_pr(result: BackportResult | None = None, **kwargs: object) -> pr.PullRequest:
    kwargs.setdefault("pusher", Pushed())
    return await pr.open_backport_pr(
        result or verified(), token=TOKEN, poll_interval_s=0, fork_timeout_s=0.05,  # type: ignore[arg-type]
        **kwargs,
    )


def requests_made(router: respx.MockRouter) -> list[tuple[str, str]]:
    return [(c.request.method, c.request.url.path) for c in router.calls]


async def test_default_target_is_the_forks_own_branch_and_the_pr_is_a_draft(api) -> None:
    routes = mock_github(api)
    pushed = Pushed()
    pull = await open_pr(pusher=pushed)

    assert pull.url == "https://github.com/alice/widgets/pull/5" and pull.created
    assert pull.base_repo == "alice/widgets" and pull.base_branch == "8.1.x"
    assert pull.head == "alice:fixedyet/backport-42-8.1.x" and pull.fork == "alice/widgets"
    assert pull.draft is True and pull.login == "alice"

    body = json.loads(routes["pr_fork"].calls.last.request.content)
    assert body["draft"] is True
    assert body["base"] == "8.1.x" and body["head"] == "alice:fixedyet/backport-42-8.1.x"
    assert body["title"] == "Backport Keep the last item in parse() to 8.1.x"
    assert body["body"].rstrip().endswith(DISCLOSURE)
    assert not routes["pr_upstream"].called  # nothing reaches the upstream project

    (push,) = pushed.requests
    assert push.fork_url == "https://github.com/alice/widgets.git"
    assert push.upstream_url == "https://github.com/acme/widgets.git"
    assert push.base_sha == BASE and push.base_branch == "8.1.x"
    assert push.branch == "fixedyet/backport-42-8.1.x" and push.patch == PATCH
    assert push.committer_name == "Alice A"
    assert push.committer_email == "7+alice@users.noreply.github.com"
    assert push.push_base_branch is False and push.token == TOKEN


async def test_upstream_target_opens_the_draft_on_the_upstream_branch(api) -> None:
    routes = mock_github(api)
    pull = await open_pr(target="upstream", confirmed_upstream=True)
    assert pull.base_repo == "acme/widgets" and pull.url.endswith("/acme/widgets/pull/99")
    body = json.loads(routes["pr_upstream"].calls.last.request.content)
    assert body["draft"] is True and body["base"] == "8.1.x"
    assert body["head"] == "alice:fixedyet/backport-42-8.1.x"  # the branch lives on the fork
    assert not routes["pr_fork"].called
    assert not routes["sync"].called  # the fork's branch is not needed for an upstream pr


async def test_upstream_target_is_refused_without_confirmation_before_any_request(api) -> None:
    pushed = Pushed()
    with pytest.raises(pr.PullRequestError, match="explicit confirmation"):
        await open_pr(target="upstream", pusher=pushed)
    with pytest.raises(pr.PullRequestError, match="explicit confirmation"):
        await open_pr(target="upstream", confirmed_upstream=False, pusher=pushed)
    assert api.calls.call_count == 0 and pushed.requests == []


async def test_an_unknown_target_is_refused(api) -> None:
    with pytest.raises(pr.PullRequestError, match="unknown pull request target"):
        await open_pr(target="everywhere")  # type: ignore[arg-type]
    assert api.calls.call_count == 0


@pytest.mark.parametrize("target", ["fork", "upstream"])
async def test_every_request_is_in_the_allowed_set_and_nothing_marks_it_ready(
    api, target: str
) -> None:
    mock_github(api, fork="new")
    await open_pr(target=target, confirmed_upstream=True)  # type: ignore[arg-type]
    allowed_gets = {"/user", "/repos/alice/widgets", "/repos/alice/widgets/git/ref/heads/main",
                    "/repos/alice/widgets/branches/8.1.x", "/repos/alice/widgets/pulls",
                    "/repos/acme/widgets/pulls"}
    allowed_posts = {"/repos/acme/widgets/forks", "/repos/alice/widgets/merge-upstream",
                     "/repos/alice/widgets/pulls", "/repos/acme/widgets/pulls"}
    for method, path in requests_made(api):
        assert (method == "GET" and path in allowed_gets) or (
            method == "POST" and path in allowed_posts
        ), (method, path)
    paths = [p for _, p in requests_made(api)]
    assert not [p for p in paths if "ready" in p or "graphql" in p or "comments" in p]
    assert not [m for m, _ in requests_made(api) if m in ("PATCH", "PUT", "DELETE")]
    posts = [c for c in api.calls if c.request.url.path.endswith("/pulls")
             and c.request.method == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0].request.content)["draft"] is True


async def test_the_pull_request_function_has_no_way_to_ask_for_a_ready_pr() -> None:
    names = set(inspect.signature(pr.PullRequestClient.create_draft_pr).parameters)
    assert "draft" not in names and "ready" not in names
    assert not [n for n in dir(pr.PullRequestClient) if "ready" in n or "comment" in n]


async def test_a_new_fork_is_created_and_waited_for(api) -> None:
    routes = mock_github(api, fork="new")
    pull = await open_pr()
    assert routes["fork_post"].called
    body = json.loads(routes["fork_post"].calls.last.request.content)
    assert body == {"default_branch_only": False}
    assert routes["fork_get"].call_count == 3  # the lookup, then two polls
    assert pull.created


async def test_an_existing_fork_is_reused(api) -> None:
    api_routes = mock_github(api, fork="exists")
    await open_pr()
    assert "fork_post" not in api_routes
    assert not [1 for m, p in requests_made(api) if m == "POST" and p.endswith("/forks")]


async def test_a_repo_with_the_same_name_that_is_not_a_fork_gets_a_fork_next_to_it(api) -> None:
    mock_github(api, fork="clash")
    pull = await open_pr()
    assert pull.fork == "alice/widgets-1" and pull.base_repo == "alice/widgets-1"
    assert pull.url.endswith("/alice/widgets-1/pull/3")


async def test_a_fork_that_never_becomes_ready_is_an_error(api) -> None:
    api.get("/user").respond(json={"login": "alice", "id": 7})
    api.get("/repos/alice/widgets").respond(404)
    api.post("/repos/acme/widgets/forks").respond(202, json={"full_name": "alice/widgets"})
    with pytest.raises(pr.PullRequestError, match="not ready"):
        await open_pr()


async def test_a_missing_branch_on_the_fork_is_pushed_along_with_the_backport(api) -> None:
    mock_github(api, branch_on_fork=False, sync=404)
    pushed = Pushed()
    await open_pr(pusher=pushed)
    assert pushed.requests[0].push_base_branch is True


async def test_a_failed_sync_is_not_fatal(api) -> None:
    mock_github(api, sync=409)
    messages: list[str] = []
    pull = await open_pr(say=messages.append)
    assert pull.created and any("fast-forward" in m for m in messages)


async def test_an_open_pull_request_for_the_branch_is_returned_not_duplicated(api) -> None:
    routes = mock_github(
        api, open_pr=[{"html_url": "https://github.com/alice/widgets/pull/4", "number": 4,
                       "draft": True}]
    )
    pushed = Pushed()
    pull = await open_pr(pusher=pushed)
    assert pull.created is False and pull.number == 4
    assert pushed.requests == [] and not routes["pr_fork"].called


async def test_only_a_verified_backport_gets_a_pull_request(api) -> None:
    for status in BackportStatus:
        if status is BackportStatus.VERIFIED:
            continue
        with pytest.raises(pr.PullRequestError, match=status.value):
            await open_pr(verified(status=status))
    assert api.calls.call_count == 0


@pytest.mark.parametrize(
    "overrides",
    [{"patch": ""}, {"target_branch": None}, {"target_branch": "-x"}, {"target_branch_head": None},
     {"target_branch_head": "not-a-sha"}, {"repo": "no-slash"}, {"issue_url": "swe-bench:x-1"}],
)
async def test_an_incomplete_result_is_refused_before_any_request(
    api, overrides: dict[str, object]
) -> None:
    with pytest.raises(pr.PullRequestError):
        await open_pr(verified(**overrides))
    assert api.calls.call_count == 0


async def test_a_token_that_owns_the_upstream_repo_has_no_fork_to_use(api) -> None:
    mock_github(api, login="acme")
    with pytest.raises(pr.PullRequestError, match="owner of acme/widgets"):
        await open_pr()


async def test_api_errors_are_readable_and_never_echo_the_token(api) -> None:
    api.get("/user").respond(401, json={"message": f"Bad credentials for {TOKEN}"})
    with pytest.raises(pr.PullRequestError) as caught:
        await open_pr()
    text = str(caught.value)
    assert "401" in text and "Bad credentials" in text and TOKEN not in text
    assert "fine-grained" in text  # says which permissions are needed


async def test_the_token_is_sent_only_as_an_authorization_header(api) -> None:
    mock_github(api)
    await open_pr()
    for call in api.calls:
        assert call.request.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(call.request.url)


async def test_progress_messages_never_contain_the_token(api) -> None:
    mock_github(api, fork="new")
    messages: list[str] = []
    await open_pr(say=messages.append)
    assert messages and not [m for m in messages if TOKEN in m]
    assert any("@alice" in m for m in messages)


# -- pushing with the real git, between local bare repositories -------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repos(tmp_path: Path):
    """A bare 'upstream' with a maintenance branch, its bare 'fork' (a clone, so it shares the
    base commit like a GitHub fork does), and a real patch made from a real commit."""
    upstream, fork, scratch = tmp_path / "upstream.git", tmp_path / "fork.git", tmp_path / "w"
    git("init", "--bare", "-q", str(upstream))
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=upstream)
    git("init", "-q", "-b", "8.1.x", str(scratch))
    (scratch / "widgets.py").write_text("def parse(t):\n    return t.split(',')[:-1]\n")
    git("add", "-A", cwd=scratch)
    git("commit", "-q", "-m", "base", cwd=scratch)
    base = git("rev-parse", "HEAD", cwd=scratch)
    git("push", "-q", str(upstream), "8.1.x", "HEAD:refs/heads/main", cwd=scratch)
    git("clone", "--bare", "-q", str(upstream), str(fork))
    (scratch / "widgets.py").write_text("def parse(t):\n    return t.split(',')\n")
    git("commit", "-q", "-a", "--author=Original Author <orig@example.com>",
        "-m", "Keep the last item", "-m", "(cherry picked from commit abc)", cwd=scratch)
    patch = git("format-patch", "-1", "--stdout", "--no-signature", "HEAD", cwd=scratch) + "\n"
    return upstream, fork, base, patch


def push_request(repos, **overrides: object) -> pr.PushRequest:
    upstream, fork, base, patch = repos
    data: dict[str, object] = dict(
        upstream_url=f"file://{upstream}", fork_url=f"file://{fork}", base_sha=base,
        base_branch="8.1.x", branch="fixedyet/backport-42-8.1.x", patch=patch,
        committer_name="Alice A", committer_email="7+alice@users.noreply.github.com",
        token=TOKEN, push_base_branch=True,
    )
    data.update(overrides)
    return pr.PushRequest(**data)  # type: ignore[arg-type]


@needs_git
async def test_push_applies_the_patch_on_the_verified_base_and_pushes_to_the_fork(repos) -> None:
    upstream, fork, base, _ = repos
    pushed = await pr.push_with_git(push_request(repos))
    fmt = "%an|%ae|%cn|%ce|%s|%P"
    assert git("log", "-1", f"--format={fmt}", "fixedyet/backport-42-8.1.x", cwd=fork) == (
        f"Original Author|orig@example.com|Alice A|7+alice@users.noreply.github.com|"
        f"Keep the last item|{base}"
    )
    assert git("rev-parse", "fixedyet/backport-42-8.1.x", cwd=fork) == pushed
    assert git("rev-parse", "8.1.x", cwd=fork) == base
    assert "cherry picked from commit abc" in git(
        "log", "-1", "--format=%B", "fixedyet/backport-42-8.1.x", cwd=fork
    )
    # the upstream repository was only read from
    assert "fixedyet" not in git("branch", "--list", cwd=upstream)


@needs_git
async def test_a_base_branch_missing_on_the_fork_is_created_at_the_verified_commit(repos) -> None:
    _, fork, base, _ = repos
    git("update-ref", "-d", "refs/heads/8.1.x", cwd=fork)
    assert "8.1.x" not in git("branch", "--list", cwd=fork)
    await pr.push_with_git(push_request(repos))
    assert git("rev-parse", "8.1.x", cwd=fork) == base


@needs_git
async def test_pushing_the_same_backport_twice_is_harmless(repos) -> None:
    first = await pr.push_with_git(push_request(repos))
    second = await pr.push_with_git(push_request(repos, push_base_branch=False))
    assert first == second  # same patch and committer give the same commit


@needs_git
async def test_a_branch_that_exists_with_other_content_is_not_overwritten(repos) -> None:
    upstream, fork, base, patch = repos
    await pr.push_with_git(push_request(repos))
    other = patch.replace("Keep the last item", "A different subject")
    with pytest.raises(pr.PullRequestError, match="never force-pushes"):
        await pr.push_with_git(push_request(repos, patch=other, push_base_branch=False))


@needs_git
async def test_a_broken_patch_is_an_error_not_a_crash(repos) -> None:
    with pytest.raises(pr.PullRequestError, match="git am failed"):
        await pr.push_with_git(push_request(repos, patch="this is not a patch\n"))


@needs_git
async def test_the_token_is_never_on_a_command_line_and_is_scrubbed_from_errors(
    repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []
    envs: list[dict[str, str]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: str, **kwargs: object):
        seen.append(args)
        envs.append(dict(kwargs.get("env") or {}))  # type: ignore[arg-type]
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    await pr.push_with_git(push_request(repos))
    assert seen and not [a for args in seen for a in args if TOKEN in a]
    with_header = [e for e in envs if "GIT_CONFIG_VALUE_0" in e]
    assert len(with_header) == 1  # only the push carries the credential
    assert TOKEN not in with_header[0]["GIT_CONFIG_VALUE_0"]  # it is base64, not plain
    assert with_header[0]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert all(e.get("GIT_TERMINAL_PROMPT") == "0" for e in envs)

    # An error that quotes the token (here, because it is in the path) is scrubbed.
    with pytest.raises(pr.PullRequestError) as caught:
        await pr.push_with_git(
            push_request(repos, fork_url=f"file:///nonexistent/{TOKEN}.git", branch="x/y")
        )
    assert TOKEN not in str(caught.value) and "***" in str(caught.value)


def test_git_runs_with_hooks_off_and_without_the_users_config(tmp_path: Path) -> None:
    request = pr.PushRequest(
        upstream_url="u", fork_url="f", base_sha=BASE, base_branch="b", branch="x", patch="",
        committer_name="n", committer_email="e", token=TOKEN,
    )
    plain = pr._git_env(tmp_path, request, with_token=False)
    assert "GIT_CONFIG_VALUE_0" not in plain and plain["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert plain["HOME"] == str(tmp_path) and plain["GIT_TERMINAL_PROMPT"] == "0"
