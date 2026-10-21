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
    checkpoint_image: str | None = None
    log_excerpt: str = ""

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
    regression_test_path: str | None = None  # where the pull request adds it; None: no test dir
    repro_before: SideRun | None = None  # on main before the patch
    repro_after: SideRun | None = None  # on main with the verified patch
    suite_before: SuiteRun | None = None
    suite_after: SuiteRun | None = None
    newly_failing: list[str] = Field(default_factory=list)
    attempts: list[FixAttempt] = Field(default_factory=list)
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


Stage = Literal[
    "intake", "environment", "agent", "run_reported", "run_fix",
    "verdict", "evidence", "report", "backport", "fix", "done", "error",
]


class ProgressEvent(BaseModel):
    stage: Stage
    message: str
    at: datetime = Field(default_factory=utcnow)


ProgressCallback = Callable[[ProgressEvent], Awaitable[None] | None]
