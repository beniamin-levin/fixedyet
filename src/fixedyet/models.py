"""Shared data model. Every module imports from here.

This file is the contract between modules. Do not change a field's name or type
without updating every user; adding optional fields is fine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Outcome(StrEnum):
    """Result of running the reproduction test once on one side."""

    PASS = "pass"  # pytest exit code 0
    FAIL = "fail"  # pytest exit code 1: the test ran and an assertion failed
    ERROR = "error"  # anything else: setup failure, collection error, timeout


class Verdict(StrEnum):
    FIXED_ON_MAIN = "fixed_on_main"  # fails at reported version, passes on fix side
    OPEN_BUG = "open_bug"  # fails on both sides
    NOT_REPRODUCED = "not_reproduced"  # passes on both sides
    INCONSISTENT = "inconsistent"  # passes at reported version, fails on fix side, or flaky
    NEEDS_INFO = "needs_info"  # no runnable reproduction in the issue
    UNSUPPORTED = "unsupported"  # environment could not be built


class Confidence(StrEnum):
    HIGH = "high"  # both runs agree on both sides
    LOW = "low"  # runs disagree, or a side errored


class NotReproducedReason(StrEnum):
    ENVIRONMENT = "environment"
    CONFIGURATION = "configuration"
    NOT_AN_ISSUE = "not_an_issue"
    NEEDS_INFO = "needs_info"


class Issue(BaseModel):
    url: str
    owner: str
    repo: str
    number: int
    title: str
    body: str = ""
    comments: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    state: str = "open"
    state_reason: str | None = None  # GitHub's: completed, not_planned, reopened, duplicate
    closed_at: datetime | None = None
    created_at: datetime | None = None
    default_branch: str = "main"


class SweBenchInstance(BaseModel):
    """One SWE-bench Lite or Verified instance, as published on Hugging Face."""

    instance_id: str
    repo: str  # "owner/name"
    base_commit: str
    patch: str  # the gold fix; never shown to the agent
    test_patch: str  # SWE-bench's own tests; never shown to the agent
    problem_statement: str
    version: str = ""
    created_at: str = ""
    environment_setup_commit: str = ""


class ReproContract(BaseModel):
    """What the model extracts from the issue thread at intake."""

    runnable: bool  # False means the run stops with Verdict.NEEDS_INFO
    package: str | None = None  # PyPI distribution name, if known
    reported_version: str | None = None
    python_version: str | None = None
    setup_steps: list[str] = Field(default_factory=list)
    code_snippet: str | None = None
    expected_behaviour: str = ""
    observed_behaviour: str = ""


class ExecResult(BaseModel):
    """Result of one command run inside a sandbox."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    image: str | None = None  # new checkpoint image when the run was kept
    timed_out: bool = False


class SideRun(BaseModel):
    """The reproduction test run on one side, normally twice."""

    ref: str  # tag, commit, branch, or "gold_patch"
    outcomes: list[Outcome] = Field(default_factory=list)
    # The image the runs forked from: the baseline for the reported side; for a side that checks
    # out another ref, the baseline with that ref installed (made with network, then the tests
    # run offline). Old images are pruned: not a way to rerun.
    checkpoint_image: str | None = None
    log_excerpt: str = ""
    commit: str | None = None  # `git rev-parse HEAD` where the test ran; None in older reports
    # The `E` lines of the last run's failure report, read from the whole run log (not the
    # excerpt): what the verdict compares between the sides. None when the run printed none.
    failure_detail: str | None = None

    @property
    def stable_outcome(self) -> Outcome | None:
        """The outcome if every run agreed, otherwise None."""
        if self.outcomes and all(o == self.outcomes[0] for o in self.outcomes):
            return self.outcomes[0]
        return None


class Reproduction(BaseModel):
    repro_test_path: str | None = None
    repro_test_code: str | None = None
    reported_side: SideRun | None = None
    fix_side: SideRun | None = None
    fixing_commit: str | None = None
    fixed_in_release: str | None = None


class Evidence(BaseModel):
    kind: Literal["duplicate", "upstream", "changelog", "log"]
    title: str
    url: str | None = None
    why: str = ""


class LinkedPullRequest(BaseModel):
    """A pull request of the issue's own repository that is linked to the issue."""

    number: int
    url: str
    title: str = ""
    state: Literal["open", "closed", "merged"] = "open"
    draft: bool = False
    merged_at: datetime | None = None
    closes: bool = False  # linked in the Development sidebar, or a closing keyword: merging it
    # closes the issue. False: the pull request only mentions the issue


class IssueStatus(BaseModel):
    """Where the issue stands on GitHub, read when the verdict was made (or re-scored)."""

    state: str = "open"  # open | closed
    state_reason: str | None = None  # completed | not_planned | reopened | duplicate
    closed_at: datetime | None = None
    closed_by: str | None = None  # "#123" (a pull request) or a commit sha, when GitHub names it
    closed_by_url: str | None = None
    linked_prs: list[LinkedPullRequest] = Field(default_factory=list)  # open and merged ones
    source: Literal["graphql", "rest"] = "rest"  # GraphQL needs a token; REST sees less
    checked_at: datetime = Field(default_factory=utcnow)

    @property
    def open_prs(self) -> list[LinkedPullRequest]:
        return [pr for pr in self.linked_prs if pr.state == "open"]


class Rescore(BaseModel):
    """Set by `fixedyet rescore --write`: what the verdict was before the rules ran again."""

    rules: str  # the rules version that re-scored it (verdict.RULES_VERSION)
    at: datetime = Field(default_factory=utcnow)
    original_verdict: Verdict
    original_confidence: Confidence
    original_basis: list[str] = Field(default_factory=list)
    original_rules: str = ""  # the rules version of the original verdict ("" = v1)


class Usage(BaseModel):
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    sandbox_runs: int = 0
    search_credits: int = 0
    wall_seconds: float = 0.0


class BackportStatus(StrEnum):
    VERIFIED = "verified"  # reproduction fails before and passes after; no newly failing tests
    CONFLICTS = "conflicts"  # the cherry-pick does not apply cleanly (aborted, never resolved)
    REGRESSION = "regression"  # the cherry-pick fixed the bug but newly broke other tests
    ALREADY_FIXED_ON_BRANCH = "already_fixed_on_branch"  # the test passes on the branch already
    NOT_APPLICABLE = "not_applicable"  # no branch, no known fixing commit, or not fixed on main
    ERROR = "error"  # something could not be run or did not give a usable answer


class SuiteRun(BaseModel):
    """The project's own test suite, run once on one side of the cherry-pick."""

    ran: bool = False  # True only when pytest reached its summary line and exited 0 or 1
    exit_code: int | None = None
    timed_out: bool = False
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failing_tests: list[str] = Field(default_factory=list)  # ids of FAILED and ERROR entries
    duration_s: float = 0.0
    log_excerpt: str = ""

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped


class BackportResult(BaseModel):
    """A fix from main, cherry-picked onto the maintenance branch of the reported version."""

    status: BackportStatus
    reason: str = ""  # one sentence: why the status is not `verified`, or what was checked
    issue_url: str = ""
    title: str = ""  # the issue title
    repo: str = ""  # "owner/name"
    reported_version: str | None = None
    target_branch: str | None = None
    target_branch_head: str | None = None  # commit the verification started from
    fixing_commit: str | None = None  # the commit on main that was cherry-picked
    cherry_picked_commit: str | None = None  # its copy on the target branch (sandbox only)
    commit_message: str = ""  # of the cherry-picked commit, including the `-x` trailer
    patch: str = ""  # `git format-patch` of the cherry-pick
    repro_test_path: str | None = None
    repro_test_code: str | None = None
    repro_before: SideRun | None = None  # on the target branch, before the cherry-pick
    repro_after: SideRun | None = None  # on the target branch, after the cherry-pick
    suite_before: SuiteRun | None = None
    suite_after: SuiteRun | None = None
    newly_failing: list[str] = Field(default_factory=list)  # failing after but not before
    conflicting_files: list[str] = Field(default_factory=list)
    candidate_branches: list[str] = Field(default_factory=list)  # maintenance branches seen
    log_excerpt: str = ""  # git or pip output for the step that went wrong
    usage: Usage = Field(default_factory=Usage)
    created_at: datetime = Field(default_factory=utcnow)


class FixStatus(StrEnum):
    VERIFIED = "verified"  # the test fails before the patch, passes twice after; no new failures
    NOT_FIXED = "not_fixed"  # no attempt gave a verified patch; the attempts say why
    ALREADY_FIXED = "already_fixed"  # the reproduction test passes on main now: nothing to fix
    NOT_APPLICABLE = "not_applicable"  # not an open bug, no reproduction test, not a GitHub issue
    ERROR = "error"  # a step could not be run (build, the check before the patch, the suite)


class FileEdit(BaseModel):
    """One search/replace edit: ``old`` must occur exactly once in ``path``; ``new`` replaces it."""

    path: str
    old: str
    new: str


class FixAttempt(BaseModel):
    """One answer of the model in fix mode, and what the checks made of it.

    ``outcome`` is one of: ``verified``, ``invalid_edits`` (rejected before anything ran),
    ``repro_still_fails``, ``regression``, ``suite_not_run``, ``apply_failed``, ``no_edits``
    (the answer held no edit in the required format) or ``gave_up`` (the model said it cannot
    fix it, for example because it judged the test wrong).
    """

    number: int
    outcome: str
    reason: str = ""
    summary: str = ""  # the model's one-line summary of its change
    explanation: str = ""  # the model's account of the root cause and the change
    edits: list[FileEdit] = Field(default_factory=list)
    diff: str = ""  # `git diff` of the change in the sandbox (empty when it was never applied)
    lines_changed: int = 0
    repro_after: SideRun | None = None
    suite_after: SuiteRun | None = None
    newly_failing: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0  # model cost of this answer
    answer: str = ""  # the start of the model's answer, for reading what went wrong


class CallSite(BaseModel):
    """A line outside a change that uses a function the change edits, found by name (git grep):
    code the reproduction test does not exercise, which a reviewer should look at."""

    symbol: str  # the function or method the change edits
    path: str
    line: int
    code: str = ""  # the line and its neighbours, numbered


class FixResult(BaseModel):
    """A fix for an open bug, written by the model and verified in the sandbox on main."""

    status: FixStatus
    reason: str = ""  # one sentence: what was verified, or why there is no verified fix
    issue_url: str = ""
    title: str = ""  # the issue title
    repo: str = ""  # "owner/name"
    base_branch: str | None = None  # the upstream default branch: the base of the pull request
    base_sha: str | None = None  # the commit the verification ran on
    summary: str = ""  # one line about the verified change (model-written, sanitised)
    explanation: str = ""  # the model's account of the root cause (unchecked text)
    diff: str = ""  # `git diff` of the verified change to the project's source (no test)
    files_changed: list[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    context_files: list[str] = Field(default_factory=list)  # the files the model was shown
    repro_test_path: str | None = None  # where the test ran in every check
    repro_test_code: str | None = None
    regression_test_path: str | None = None  # where the pull request adds it; None: no test
    regression_test_note: str = ""  # why the pull request carries no test, when it carries none
    repro_before: SideRun | None = None  # on main before the patch
    repro_after: SideRun | None = None  # on main with the verified patch
    suite_before: SuiteRun | None = None
    suite_after: SuiteRun | None = None
    newly_failing: list[str] = Field(default_factory=list)
    attempts: list[FixAttempt] = Field(default_factory=list)
    changed_functions: list[str] = Field(default_factory=list)  # what the verified change edits
    other_callers: list[CallSite] = Field(default_factory=list)  # their other uses (the first few)
    other_callers_total: int = 0  # how many other uses were found in all
    log_excerpt: str = ""  # output of the step that stopped it
    model: str = ""
    budget_usd: float = 0.0  # the model cost cap this fix ran under
    usage: Usage = Field(default_factory=Usage)
    created_at: datetime = Field(default_factory=utcnow)


class Assessment(BaseModel):
    issue_url: str
    title: str
    verdict: Verdict
    confidence: Confidence
    confidence_basis: list[str] = Field(default_factory=list)
    reproduction: Reproduction = Field(default_factory=Reproduction)
    not_reproduced_reason: NotReproducedReason | None = None
    root_cause: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    backport: BackportResult | None = None  # set when a verified backport was prepared
    fix: FixResult | None = None  # set when fix mode ran on an open bug
    created_at: datetime = Field(default_factory=utcnow)
    # What the rules need to be run again later without a model call (`fixedyet rescore`).
    contract: ReproContract | None = None  # what the model extracted from the issue at intake
    issue_status: IssueStatus | None = None  # live issues only; None in eval mode
    rules: str = ""  # the version of the rule table that decided the verdict; "" = v1
    rescored: Rescore | None = None  # set when `fixedyet rescore --write` changed this file

    def open_pr_warning(self) -> str | None:
        """One line for fix mode: open pull requests that already address the issue."""
        prs = self.issue_status.open_prs if self.issue_status else []
        if not prs:
            return None
        listed = ", ".join(f"#{pr.number}{' (draft)' if pr.draft else ''}" for pr in prs[:5])
        return (
            f"warning: open pull request(s) already linked to this issue: {listed}. Read them "
            "before proposing another fix"
        )


Stage = Literal[
    "intake", "environment", "agent", "run_reported", "run_fix",
    "verdict", "evidence", "report", "backport", "fix", "done", "error",
]


class ProgressEvent(BaseModel):
    stage: Stage
    message: str
    at: datetime = Field(default_factory=utcnow)


ProgressCallback = Callable[[ProgressEvent], Awaitable[None] | None]
