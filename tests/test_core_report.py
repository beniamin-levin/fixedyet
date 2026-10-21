from __future__ import annotations

import re

import pytest

from fixedyet.models import (
    Assessment,
    Confidence,
    Evidence,
    NotReproducedReason,
    Outcome,
    Reproduction,
    SideRun,
    SuiteRun,
    Usage,
    Verdict,
)
from fixedyet.report import HEADLINES, render_markdown


def assessment(**overrides) -> Assessment:
    base = dict(
        issue_url="https://github.com/acme/widgets/issues/42",
        title="parse() drops the last item",
        verdict=Verdict.FIXED_ON_MAIN,
        confidence=Confidence.HIGH,
        confidence_basis=["both runs agree: fail on the reported side", "both runs agree: pass"],
        reproduction=Reproduction(
            repro_test_path="tests/test_fixedyet_repro.py",
            repro_test_code="def test_x():\n    assert parse('a,b') == ['a', 'b']\n",
            reported_side=SideRun(ref="v1.2.0", outcomes=[Outcome.FAIL, Outcome.FAIL],
                                  checkpoint_image="img-baseline", commit="a" * 40,
                                  log_excerpt="E   AssertionError: boom"),
            fix_side=SideRun(ref="main", outcomes=[Outcome.PASS, Outcome.PASS],
                             checkpoint_image="img-baseline", commit="f" * 40),
            fixing_commit="abc123", fixed_in_release="1.3.1",
        ),
        root_cause="parse() slices off the last element.",
        evidence=[Evidence(kind="duplicate", title="#17 same bug",
                           url="https://github.com/acme/widgets/issues/17", why="closed")],
        next_actions=["Close the issue.", "Ask the reporter to upgrade."],
        usage=Usage(llm_calls=12, input_tokens=40_000, output_tokens=5_000, cost_usd=0.0234,
                    sandbox_runs=7, search_credits=2, wall_seconds=211.4),
        model="nvidia/nemotron-3-super-120b-a12b",
    )
    base.update(overrides)
    return Assessment(**base)


def test_full_report_has_every_section_in_order() -> None:
    md = render_markdown(assessment())
    prose = re.sub(r"^```.*?^```$", "", md, flags=re.MULTILINE | re.DOTALL)  # not shell comments
    headings = [line for line in prose.splitlines() if line.startswith("#")]
    assert headings == [
        "# Fixed on main: fails at the reported version, passes on the fix side",
        "## Results",
        "## Reproduction test (`tests/test_fixedyet_repro.py`)",
        "## Output at the reported version (end of the log)",
        "## Evidence",
        "## Root cause",
        "## Next actions",
        "## Usage",
        "## Rerun this yourself",
    ]
    assert "**Confidence:** high" in md and "- both runs agree: fail on the reported side" in md
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_results_table_is_two_sides_by_two_runs() -> None:
    md = render_markdown(assessment())
    assert "| Side | Run 1 | Run 2 |" in md
    assert "| Reported version (`v1.2.0`) | FAIL | FAIL |" in md
    assert "| Fix side (`main`) | PASS | PASS |" in md


def test_commit_release_usage_and_rerun_block() -> None:
    md = render_markdown(assessment())
    assert "Fixing commit: `abc123`" in md and "Fixed in release: `1.3.1`" in md
    assert (
        "12 LLM calls, 45,000 tokens (40,000 in, 5,000 out), $0.0234, 7 sandbox runs, "
        "2 search credits, 211 s wall time, model nvidia/nemotron-3-super-120b-a12b"
    ) in md
    assert "img-baseline" not in md  # checkpoint images are pruned: never offered for a rerun
    rerun = md[md.index("## Rerun this yourself"):]
    assert "Reported version (`v1.2.0` at `aaaaaaaaaaaa`):" in rerun
    assert "Fix side (`main` at `ffffffffffff`):" in rerun
    assert rerun.count("git clone https://github.com/acme/widgets.git fixedyet-rerun") == 2
    assert f"git checkout {'a' * 40}\n" in rerun and f"git checkout {'f' * 40}\n" in rerun
    assert "python -m pytest -x -q -p no:cacheprovider tests/test_fixedyet_repro.py" in rerun
    assert "fixedyet run https://github.com/acme/widgets/issues/42" in rerun


def test_rerun_without_a_recorded_commit_names_the_ref_and_says_so() -> None:
    a = assessment()
    a.reproduction.fix_side.commit = None  # a report from before commits were recorded
    rerun = render_markdown(a)
    rerun = rerun[rerun.index("## Rerun this yourself"):]
    assert "Fix side (`main`): The commit was not recorded" in rerun
    assert "git checkout main\n" in rerun and f"git checkout {'a' * 40}\n" in rerun


def test_rerun_gives_no_checkout_for_a_side_that_is_not_a_git_ref() -> None:
    a = assessment()
    a.reproduction.fix_side = SideRun(ref="v1.2.0+gold_patch", outcomes=[Outcome.PASS])
    a.reproduction.fix_side.commit = None
    md = render_markdown(a)
    assert "Fix side (`v1.2.0+gold_patch`): not a git ref, so no command is given." in md
    assert "git checkout v1.2.0+gold_patch" not in md


def test_test_code_is_in_a_python_block() -> None:
    md = render_markdown(assessment())
    assert "```python\ndef test_x():\n    assert parse('a,b') == ['a', 'b']\n```" in md


def test_a_fence_inside_the_code_cannot_close_the_block_early() -> None:
    code = 'def test_x():\n    s = """\n```\nnot markdown\n```\n"""\n'
    a = assessment()
    a.reproduction.repro_test_code = code
    md = render_markdown(a)
    assert "````python\n" in md and md.count("````") == 2


def test_untrusted_text_cannot_break_out_of_lists_or_tables() -> None:
    a = assessment(
        title="x\n# injected heading",
        evidence=[Evidence(kind="upstream", title="a](http://evil)[b", url="javascript:alert(1)",
                           why="line one\n## fake section")],
        confidence_basis=["ok\n- sneaky bullet"],
        next_actions=["do it\n\n# not a heading"],
    )
    a.reproduction.reported_side.ref = "v|1"
    md = render_markdown(a)
    assert "\n# injected heading" not in md and "\n## fake section" not in md
    assert "\n# not a heading" not in md and "\n- sneaky bullet" not in md
    assert "javascript:" not in md  # non-http URLs are never turned into links
    assert "v\\|1" in md


def test_needs_info_report_is_short_and_has_no_empty_sections() -> None:
    a = Assessment(
        issue_url="https://github.com/acme/widgets/issues/1", title="How do I?",
        verdict=Verdict.NEEDS_INFO, confidence=Confidence.LOW,
        confidence_basis=["no runnable reproduction"], next_actions=["Ask for a snippet."],
    )
    md = render_markdown(a)
    assert md.startswith("# Needs info")
    for absent in ("## Results", "## Reproduction test", "## Evidence", "## Root cause",
                   "## Rerun this yourself"):
        assert absent not in md
    assert "## Next actions" in md and "## Usage" in md


def test_not_reproduced_reason_is_shown() -> None:
    md = render_markdown(
        assessment(
            verdict=Verdict.NOT_REPRODUCED,
            not_reproduced_reason=NotReproducedReason.ENVIRONMENT,
        )
    )
    assert "`environment`" in md


def test_non_http_issue_url_is_not_linked() -> None:
    md = render_markdown(assessment(issue_url="swe-bench:psf__requests-2317"))
    assert "](swe-bench" not in md
    assert "## Rerun this yourself" not in md  # no repository to clone


@pytest.mark.parametrize("verdict", list(Verdict))
def test_every_verdict_has_a_headline_and_renders(verdict: Verdict) -> None:
    assert verdict in HEADLINES
    md = render_markdown(assessment(verdict=verdict))
    assert md.startswith(f"# {HEADLINES[verdict]}") and f"`{verdict.value}`" in md


def test_three_runs_make_three_columns() -> None:
    a = assessment()
    a.reproduction.reported_side.outcomes = [Outcome.FAIL, Outcome.PASS, Outcome.ERROR]
    md = render_markdown(a)
    assert "| Side | Run 1 | Run 2 | Run 3 |" in md and "| FAIL | PASS | ERROR |" in md
    assert "| PASS | PASS | - |" in md  # the shorter side is padded


def test_fix_side_output_is_shown_when_the_fix_side_failed() -> None:
    fix = SideRun(ref="main", outcomes=[Outcome.FAIL, Outcome.FAIL],
                  log_excerpt="E   TypeError: unexpected keyword argument 'mix_stderr'")
    for verdict in (Verdict.OPEN_BUG, Verdict.INCONSISTENT):
        a = assessment(verdict=verdict)
        a.reproduction.fix_side = fix
        md = render_markdown(a)
        assert "## Output on the fix side (end of the log)" in md
        assert "TypeError: unexpected keyword argument 'mix_stderr'" in md
    a = assessment()  # fixed_on_main: the fix side passed, there is nothing to show
    a.reproduction.fix_side = fix
    assert "Output on the fix side" not in render_markdown(a)


def test_a_long_log_is_cut_on_a_line_boundary() -> None:
    lines = [f"line {i:03d} " + "x" * 30 for i in range(100)]
    a = assessment()
    a.reproduction.reported_side.log_excerpt = "\n".join(lines)
    block = render_markdown(a).split("## Output at the reported version (end of the log)")[1]
    body = block.split("```")[1].strip("\n").splitlines()
    assert body[-1] == lines[-1]
    assert body[0] in lines  # a whole line, not the tail end of one
    assert len("\n".join(body)) <= 1_500


def test_the_root_cause_says_it_is_the_models_unverified_analysis() -> None:
    md = render_markdown(assessment())
    assert "## Root cause\n\nparse() slices off the last element.\n\n_Written by the model" in md
    assert "did not check it._" in md
    assert "Written by the model" not in render_markdown(assessment(root_cause=""))


# -- verified backport section ------------------------------------------------------------------

FIX_SHA = "d959898db264aaf07e70ad4eafa254286f9a5185"


def backport_result(**overrides):
    from fixedyet.models import BackportResult, BackportStatus

    data = dict(
        status=BackportStatus.VERIFIED,
        reason="the reproduction test fails before and passes after",
        issue_url="https://github.com/acme/widgets/issues/42", title="parse() drops the last item",
        repo="acme/widgets", reported_version="8.1.3", target_branch="8.1.x",
        target_branch_head="b" * 40, fixing_commit=FIX_SHA, cherry_picked_commit="c" * 40,
        commit_message=f"Keep the last item\n\n(cherry picked from commit {FIX_SHA})",
        patch="From x\n---\ndiff --git a/a.py b/a.py\n+x\n",
        repro_before=SideRun(ref="8.1.x (before)", outcomes=[Outcome.FAIL, Outcome.FAIL]),
        repro_after=SideRun(ref="8.1.x (after)", outcomes=[Outcome.PASS, Outcome.PASS]),
        suite_before=SuiteRun(ran=True, passed=120, failed=1, failing_tests=["t::known"]),
        suite_after=SuiteRun(ran=True, passed=121, failed=1, failing_tests=["t::known"]),
        usage=Usage(sandbox_runs=6, wall_seconds=95.0),
    )
    data.update(overrides)
    return BackportResult(**data)


def test_a_report_without_a_backport_has_no_backport_section() -> None:
    assert "Verified backport" not in render_markdown(assessment())


def test_a_report_with_a_backport_gets_the_section_before_usage() -> None:
    md = render_markdown(assessment(backport=backport_result()))
    headings = [line for line in md.splitlines() if line.startswith("## ")]
    assert headings.index("## Verified backport") == headings.index("## Usage") - 1
    assert "**Status:** `verified`" in md and "**Target branch:** `8.1.x`" in md
    assert f"https://github.com/acme/widgets/commit/{FIX_SHA}" in md


def test_the_section_shows_the_test_before_and_after_and_the_suite() -> None:
    md = render_markdown(assessment(backport=backport_result()))
    assert "| Reproduction test on `8.1.x` | Run 1 | Run 2 |" in md
    assert "| Before the cherry-pick | FAIL | FAIL |" in md
    assert "| After the cherry-pick | PASS | PASS |" in md
    assert "| Passed | 120 | 121 |" in md and "| Failed | 1 | 1 |" in md
    assert "| Newly failing tests | - | none |" in md
    assert "(cherry picked from commit" in md and "diff --git a/a.py b/a.py" in md
    assert "6 sandbox runs, 95 s wall time, $0.0000 model cost." in md


def test_newly_failing_tests_are_listed() -> None:
    from fixedyet.report import render_backport_markdown

    md = render_backport_markdown(backport_result(
        status="regression", newly_failing=["tests/test_b.py::test_new"],
    ))
    assert "| Newly failing tests | - | `tests/test_b.py::test_new` |" in md
    assert md.startswith("# Backport not safe: the fix breaks other tests on 8.1.x")


def test_every_status_has_its_own_headline() -> None:
    from fixedyet.models import BackportStatus
    from fixedyet.report import render_backport_markdown

    heads = {
        status: render_backport_markdown(backport_result(status=status)).splitlines()[0]
        for status in BackportStatus
    }
    assert len(set(heads.values())) == len(BackportStatus)
    assert heads[BackportStatus.VERIFIED].startswith("# Verified backport")
    assert heads[BackportStatus.CONFLICTS] == "# Backport blocked: the fix conflicts with 8.1.x"
    assert heads[BackportStatus.NOT_APPLICABLE] == "# No backport possible"


def test_conflicts_name_the_files_and_say_nothing_was_resolved() -> None:
    from fixedyet.report import render_backport_markdown

    md = render_backport_markdown(backport_result(
        status="conflicts", conflicting_files=["CHANGES.rst", "src/a.py"], patch="",
        repro_after=None, suite_before=None, suite_after=None,
        log_excerpt="CONFLICT (content): Merge conflict in CHANGES.rst",
    ))
    assert "- `CHANGES.rst`" in md and "nothing was resolved" in md
    assert "CONFLICT (content)" in md and "Patch (" not in md


def test_a_suite_that_did_not_finish_is_not_shown_as_zero() -> None:
    from fixedyet.report import render_backport_markdown

    md = render_backport_markdown(backport_result(
        status="error", suite_before=SuiteRun(ran=False, timed_out=True), suite_after=None,
    ))
    assert "| Passed | did not finish | not run |" in md
    assert "| Newly failing tests | - | - |" in md


def test_untrusted_text_in_a_backport_cannot_break_the_markdown() -> None:
    from fixedyet.report import render_backport_markdown

    md = render_backport_markdown(backport_result(
        reason="line one\n## Fake heading\n| a | b |",
        commit_message="```\nescape attempt\n```",
        patch="+```\n+more\n",
        reported_version="8.1|3",
    ))
    assert "\n## Fake heading" not in md
    assert md.count("\n## ") == 1  # only the real section heading
    assert "8.1\\|3" in md


def test_a_long_patch_is_cut_in_the_markdown_but_not_in_the_result() -> None:
    from fixedyet.report import PATCH_MARKDOWN_CHARS, render_backport_markdown

    big = "diff --git a/a b/a\n" + "+line of added code\n" * 2_000
    result = backport_result(patch=big)
    md = render_backport_markdown(result)
    assert "The patch is cut here" in md and len(md) < PATCH_MARKDOWN_CHARS + 6_000
    assert result.patch == big
