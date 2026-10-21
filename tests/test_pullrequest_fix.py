"""Draft pull requests for verified fixes: title, body, the mbox patch with the regression test,
and the GitHub flow against a mocked API. One test applies the patch with the real git."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx

from fixedyet import pullrequest as pr
from fixedyet.models import (
    CallSite,
    FixAttempt,
    FixResult,
    FixStatus,
    Outcome,
    SideRun,
    SuiteRun,
)

API = "https://api.github.com"
TOKEN = "ghp_SECRETSECRETSECRET1234"
BASE = "b" * 40
DISCLOSURE = (
    "Prepared with FixedYet (AI-assisted: NVIDIA Nemotron via Nebius Token Factory); "
    "reviewed and submitted by @alice."
)
ORIGINAL = "def parse(t):\n    return t.split(',')[:-1]\n"
DIFF = (
    "diff --git a/widgets.py b/widgets.py\nindex 1111111..2222222 100644\n--- a/widgets.py\n"
    "+++ b/widgets.py\n@@ -1,2 +1,2 @@\n def parse(t):\n-    return t.split(',')[:-1]\n"
    "+    return t.split(',')\n"
)
TEST_CODE = (
    "from widgets import parse\n\n\ndef test_parse():\n    assert parse('a,b') == ['a', 'b']\n"
)


def verified(**overrides: object) -> FixResult:
    data: dict[str, object] = dict(
        status=FixStatus.VERIFIED, reason="verified",
        issue_url="https://github.com/acme/widgets/issues/42", title="parse() drops the last item",
        repo="acme/widgets", base_branch="main", base_sha=BASE,
        summary="Keep the last item in parse",
        explanation="parse() sliced off the last element. The slice is gone.",
        diff=DIFF, files_changed=["widgets.py"], lines_added=1, lines_removed=1,
        repro_test_path="tests/test_issue_42.py", repro_test_code=TEST_CODE,
        regression_test_path="tests/test_issue_42.py",
        repro_before=SideRun(ref="main (before)", outcomes=[Outcome.FAIL, Outcome.FAIL]),
        repro_after=SideRun(ref="main (after)", outcomes=[Outcome.PASS, Outcome.PASS]),
        suite_before=SuiteRun(ran=True, passed=120, failed=1, failing_tests=["t::known"]),
        suite_after=SuiteRun(ran=True, passed=120, failed=1, failing_tests=["t::known"]),
        attempts=[FixAttempt(number=1, outcome="repro_still_fails"),
                  FixAttempt(number=2, outcome="verified")],
    )
    data.update(overrides)
    return FixResult(**data)  # type: ignore[arg-type]


# -- title, body, patch ------------------------------------------------------------------------


def test_the_branch_and_title() -> None:
    assert pr.fix_branch_name(42) == "fixedyet/fix-42"
    assert pr.fix_pr_title(verified()) == "Keep the last item in parse"
    assert pr.fix_pr_title(verified(summary="")) == "Fix #42: parse() drops the last item"
    assert len(pr.fix_pr_title(verified(summary="x" * 300))) == pr.TITLE_MAX_CHARS


def test_the_body_says_fixes_ai_written_machine_verified_and_ends_with_the_disclosure() -> None:
    body = pr.fix_pr_body(verified(), login="alice")
    assert body.startswith("Fixes #42.\n")
    assert "written by an AI model and verified by machine, not written by a person" in body
    assert "| Without the change | FAIL | FAIL |" in body
    assert "| With the change | PASS | PASS |" in body
    assert "| Passed | 120 | 120 |" in body and "did not fail without it: none." in body
    assert "already failed without the change are the baseline" in body
    assert "adds it as `tests/test_issue_42.py`" in body and TEST_CODE.strip() in body
    assert "| `widgets.py` | 1 | 1 |" in body and "plus the new regression test" in body
    assert "attempt 2 of the model" in body
    assert body.rstrip("\n").splitlines()[-1] == DISCLOSURE
    assert body.count("Prepared with FixedYet") == 1


def test_without_a_test_directory_the_body_says_the_test_is_left_out() -> None:
    body = pr.fix_pr_body(verified(regression_test_path=None), login="alice")
    assert "**not** part of this pull request" in body and TEST_CODE.strip() in body
    assert body.rstrip().endswith(DISCLOSURE)


def test_a_test_that_passes_where_it_would_ship_is_left_out_with_the_reason() -> None:
    note = ("the test does not fail at test/test_issue_42.py before the change (runs: pass, "
            "pass) although it fails at tests/test_fixedyet_repro.py")
    result = verified(regression_test_path=None, regression_test_note=note)
    body = pr.fix_pr_body(result, login="alice")
    assert f"**not** part of this pull request: {note}. It is below for reviewers." in body
    assert "new file mode" not in pr.fix_pr_preview(result, login="alice")


def test_the_body_lists_the_other_callers_of_the_changed_code() -> None:
    # rdflib#3602: the change to _val passed the suite but also changed what MIN and MAX return
    sites = [CallSite(symbol="_val", path="rdflib/plugins/sparql/aggregates.py", line=120,
                      code="  120          return _val(v)")]
    result = verified(changed_functions=["_val"], other_callers=sites, other_callers_total=3)
    body = pr.fix_pr_body(result, login="alice")
    assert "## Other callers of the changed code, not covered by the reproduction" in body
    link = f"https://github.com/acme/widgets/blob/{BASE}/rdflib/plugins/sparql/aggregates.py#L120"
    assert f"- [rdflib/plugins/sparql/aggregates.py:120]({link}) uses `_val`" in body
    assert "- and 2 more" in body
    assert body.index("Other callers") < body.index("## Project test suite")
    none = pr.fix_pr_body(verified(changed_functions=["parse"]), login="alice")
    assert "None found for `parse`" in none
    assert "Other callers" not in pr.fix_pr_body(verified(), login="alice")


def test_model_text_cannot_mention_anyone_or_break_the_layout() -> None:
    result = verified(summary="Fix it @everyone", explanation="Ping @maintainer | now\n## Boom")
    body = pr.fix_pr_body(result, login="alice")
    outside_code = re.sub(r"`[^`\n]*`", "", body)  # a code span never mentions anyone
    assert "@everyone" not in outside_code and "@maintainer" not in body
    assert "@​maintainer" in body and "\n## Boom" not in body
    assert "@everyone" not in pr.fix_pr_title(result)


def test_underscored_names_in_the_explanation_stay_code_not_bold() -> None:
    body = pr.fix_pr_body(verified(explanation="It defines __html__ and calls _iter twice."),
                          login="alice")
    assert "It defines `__html__` and calls `_iter` twice." in body
    assert "`` `" not in body


def test_the_disclosure_has_no_switch_in_the_fix_functions() -> None:
    import inspect

    for function in (pr.fix_pr_body, pr.open_fix_pr, pr.fix_pr_preview):
        names = set(inspect.signature(function).parameters)
        assert not {n for n in names if "disclos" in n or "ai_" in n or "attribution" in n}


def test_the_preview_has_title_base_head_body_and_the_diff() -> None:
    preview = pr.fix_pr_preview(verified(), login="alice")
    assert preview.startswith("# Keep the last item in parse\n")
    assert "base: acme/widgets main  ·  head: alice:fixedyet/fix-42  ·  DRAFT" in preview
    assert DISCLOSURE in preview and "+++ b/tests/test_issue_42.py" in preview


def test_the_new_file_diff() -> None:
    assert pr.new_file_diff("t.py", "a\nb\n") == (
        "diff --git a/t.py b/t.py\nnew file mode 100644\n--- /dev/null\n+++ b/t.py\n"
        "@@ -0,0 +1,2 @@\n+a\n+b\n"
    )
    one = pr.new_file_diff("t.py", "a")
    assert one.endswith("@@ -0,0 +1 @@\n+a\n\\ No newline at end of file\n")


def test_the_commit_message_cannot_be_mistaken_for_the_patch() -> None:
    message = pr.fix_commit_message(verified(explanation="- diff --git is not a patch " * 8))
    for line in message.splitlines()[1:]:
        assert not line.startswith(("-", "diff ", "Index: "))


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


@needs_git
def test_the_patch_applies_with_git_am_authored_by_the_submitter(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "widgets.py").write_text(ORIGINAL)
    (work / "tests").mkdir()
    (work / "tests" / "test_other.py").write_text("")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "base", cwd=work)
    when = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    patch = pr.fix_patch(verified(summary="Keep the last item in parsé"),
                         author_name="Alice A", author_email="7+alice@users.noreply.github.com",
                         when=when)
    (tmp_path / "fix.patch").write_text(patch, encoding="utf-8")
    git("am", "-q", str(tmp_path / "fix.patch"), cwd=work)
    assert (work / "widgets.py").read_text() == ORIGINAL.replace("[:-1]", "")
    assert (work / "tests" / "test_issue_42.py").read_text() == TEST_CODE
    log = git("log", "-1", "--format=%an|%ae|%s", cwd=work)
    assert log == "Alice A|7+alice@users.noreply.github.com|Keep the last item in parsé"
    body = git("log", "-1", "--format=%b", cwd=work)
    assert "Fixes #42." in body and "verified in a sandbox" in body


def test_a_result_without_a_diff_has_no_patch() -> None:
    with pytest.raises(pr.PullRequestError, match="no diff"):
        pr.fix_patch(verified(diff=""), author_name="a", author_email="a@b")


# -- the GitHub flow ---------------------------------------------------------------------------


class Pushed:
    def __init__(self) -> None:
        self.requests: list[pr.PushRequest] = []

    async def __call__(self, request: pr.PushRequest) -> str:
        self.requests.append(request)
        return "e" * 40


@pytest.fixture
def api():
    with respx.mock(base_url=API, assert_all_called=False) as router:
        info = {"fork": True, "parent": {"full_name": "acme/widgets"}, "default_branch": "main"}
        router.get("/user").respond(json={"login": "alice", "id": 7, "name": "Alice A"})
        router.get("/repos/alice/widgets").respond(json=info)
        router.get("/repos/alice/widgets/git/ref/heads/main").respond(json={})
        router.post("/repos/alice/widgets/merge-upstream").respond(200, json={})
        router.get("/repos/alice/widgets/branches/main").respond(json={"commit": {"sha": BASE}})
        router.get("/repos/alice/widgets/pulls").respond(json=[])
        router.get("/repos/acme/widgets/pulls").respond(json=[])
        router.post("/repos/alice/widgets/pulls").respond(
            201, json={"html_url": "https://github.com/alice/widgets/pull/5", "number": 5,
                       "draft": True})
        router.post("/repos/acme/widgets/pulls").respond(
            201, json={"html_url": "https://github.com/acme/widgets/pull/99", "number": 99,
                       "draft": True})
        yield router


async def test_a_fix_pr_goes_from_the_fix_branch_to_the_default_branch_as_a_draft(api) -> None:
    pushed = Pushed()
    pull = await pr.open_fix_pr(verified(), token=TOKEN, pusher=pushed, poll_interval_s=0)
    assert (pull.url, pull.base_repo, pull.base_branch) == (
        "https://github.com/alice/widgets/pull/5", "alice/widgets", "main")
    assert pull.head == "alice:fixedyet/fix-42" and pull.draft
    (request,) = pushed.requests
    assert request.branch == "fixedyet/fix-42" and request.base_sha == BASE
    assert request.base_branch == "main" and request.fork_url.endswith("/alice/widgets.git")
    assert "From: Alice A <7+alice@users.noreply.github.com>" in request.patch
    assert "+++ b/tests/test_issue_42.py" in request.patch
    posts = [c for c in api.calls if c.request.method == "POST"
             and c.request.url.path.endswith("/pulls")]
    (post,) = posts
    sent = json.loads(post.request.content)
    assert sent["draft"] is True and sent["base"] == "main"
    assert sent["head"] == "alice:fixedyet/fix-42"
    assert sent["title"] == "Keep the last item in parse"
    assert sent["body"].rstrip().endswith(DISCLOSURE)
    assert TOKEN not in post.request.content.decode()


async def test_the_upstream_needs_confirmation_before_any_request(api) -> None:
    with pytest.raises(pr.PullRequestError, match="explicit confirmation"):
        await pr.open_fix_pr(verified(), token=TOKEN, target="upstream", pusher=Pushed())
    assert api.calls.call_count == 0
    pull = await pr.open_fix_pr(verified(), token=TOKEN, target="upstream",
                                confirmed_upstream=True, pusher=Pushed(), poll_interval_s=0)
    assert pull.base_repo == "acme/widgets" and pull.url.endswith("/pull/99")


@pytest.mark.parametrize(
    "result",
    [verified(status=FixStatus.NOT_FIXED), verified(diff=""), verified(base_sha=None),
     verified(base_sha="main"), verified(repo="not a repo")],
)
async def test_only_a_complete_verified_fix_gets_a_pull_request(api, result: FixResult) -> None:
    with pytest.raises(pr.PullRequestError):
        await pr.open_fix_pr(result, token=TOKEN, pusher=Pushed())
    assert api.calls.call_count == 0


async def test_an_open_fix_pr_is_returned_not_duplicated(api) -> None:
    api.get("/repos/alice/widgets/pulls").respond(json=[
        {"html_url": "https://github.com/alice/widgets/pull/4", "number": 4, "draft": True}])
    pushed = Pushed()
    pull = await pr.open_fix_pr(verified(), token=TOKEN, pusher=pushed, poll_interval_s=0)
    assert pull.created is False and pull.number == 4 and pushed.requests == []
