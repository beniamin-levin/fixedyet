"""Fix mode: edit validation, localization, the sandbox scripts and the attempt loop.

No network, no Docker and no paid call. The sandbox is a scripted fake that answers each kind of
script (baseline, survey, file read, apply, reproduction run, test suite) in the exact format the
real scripts print, and the model is a fake that returns canned answers in turn.
"""

from __future__ import annotations

import base64
import difflib
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from fixedyet import fixmode as fm
from fixedyet.config import Settings
from fixedyet.llm import ChatResult, LLMError
from fixedyet.models import (
    Assessment,
    Confidence,
    ExecResult,
    FileEdit,
    FixStatus,
    Issue,
    Outcome,
    ProgressEvent,
    Reproduction,
    SideRun,
    Usage,
    Verdict,
)
from fixedyet.report import render_fix_markdown
from fixedyet.sandbox.base import SandboxError

ISSUE = "https://github.com/acme/widgets/issues/42"
HEAD = "a" * 40
CORE = "def parse(text):\n    return text.split(',')[:-1]\n\n\ndef other():\n    return 1\n"
FILES = {
    "widgets/__init__.py": "from widgets.core import parse\n",
    "widgets/core.py": CORE,
    "tests/test_core.py": "def test_ok():\n    assert True\n",
    "tests/conftest.py": "",
    "setup.py": "from setuptools import setup\nsetup()\n",
    "pyproject.toml": "[project]\nname = 'widgets'\n",
    "docs/conf.py": "project = 'widgets'\n",
    ".github/workflows/ci.yml": "on: push\n",
    "README.md": "# widgets\n",
}
TRACKED = set(FILES)
TEST_CODE = "from widgets import parse\n\n\ndef test_x():\n    assert parse('a,b') == ['a', 'b']\n"
GOOD = """SUMMARY: Keep the last item in parse
EXPLANATION: parse() slices off the last element. Return the whole split.
FILE: widgets/core.py
<<<<<<< SEARCH
    return text.split(',')[:-1]
=======
    return text.split(',')
>>>>>>> REPLACE
"""
WRONG = GOOD.replace("=======\n    return text.split(',')\n",
                     "=======\n    return text.split(';')\n")


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# -- which files may change --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("tests/test_core.py", "test file"),
        ("widgets/test_parse.py", "test file"),
        ("widgets/conftest.py", "test file"),
        ("setup.py", "packaging"),
        ("pyproject.toml", "not a Python source file"),
        (".github/workflows/ci.yml", "not a Python source file"),
        ("docs/conf.py", "docs/"),
        ("widgets/_vendor/six.py", "_vendor/"),
        ("widgets/new_module.py", "not a file of the repository"),
        ("../etc/passwd.py", "not a plain path"),
        ("README.md", "not a Python source file"),
    ],
)
def test_only_the_projects_source_files_may_change(path: str, fragment: str) -> None:
    problem = fm.path_problem(path, TRACKED | {"widgets/_vendor/six.py"})
    assert problem is not None and fragment in problem


def test_a_source_file_may_change() -> None:
    assert fm.path_problem("widgets/core.py", TRACKED) is None
    assert fm.source_files(FILES) == ["widgets/__init__.py", "widgets/core.py"]


def test_paths_are_normalised_from_what_models_write() -> None:
    assert fm.normalize_path(" `./widgets/core.py` ") == "widgets/core.py"
    assert fm.normalize_path("/work/repo/widgets/core.py") == "widgets/core.py"


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (FILES, "tests/test_issue_42.py"),
        ({"test/test_a.py": "", "pkg/a.py": ""}, "test/test_issue_42.py"),
        ({"testing/test_a.py": "", "pkg/a.py": ""}, "testing/test_issue_42.py"),
        ({"pkg/a.py": "", "pkg/tests/test_a.py": ""}, "pkg/tests/test_issue_42.py"),
        ({"tests/test_issue_42.py": "", "pkg/a.py": ""}, "tests/test_issue_42_2.py"),
        # nedbat/cog keeps its tests inside the package
        ({"cogapp/__init__.py": "", "cogapp/cogapp.py": "", "cogapp/test_cogapp.py": "",
          "cogapp/test_makefiles.py": "", "docs/conf.py": ""}, "cogapp/test_issue_42.py"),
        # the whole tree counts: tests/ beats tests/unit/, and a stray root test loses
        ({"test_setup.py": "", "tests/test_a.py": "", "tests/unit/test_b.py": "",
          "tests/unit/test_c.py": "", "pkg/a.py": ""}, "tests/test_issue_42.py"),
        # a directory for issue tests is where a test for one issue goes (pySHACL, rdflib)
        ({"test/test_a.py": "", "test/issues/test_008.py": "", "pkg/a.py": ""},
         "test/issues/test_issue_42.py"),
        ({"test/test_a.py": "", "test/test_issues/test_issue1003.py": "", "pkg/a.py": ""},
         "test/test_issues/test_issue_42.py"),
        ({"src/pkg/a.py": "", "src/pkg/a_test.py": ""}, "src/pkg/test_issue_42.py"),
        ({"test_mod.py": "", "mod.py": ""}, "test_issue_42.py"),  # only a root test module
        ({"tests/conftest.py": "", "pkg/a.py": ""}, "tests/test_issue_42.py"),  # no tests yet
        ({"pkg/a.py": "", "setup.py": ""}, None),  # no tests anywhere
    ],
)
def test_the_regression_test_goes_where_the_project_keeps_its_tests(
    files: dict[str, str], expected: str | None
) -> None:
    assert fm.regression_test_path(files, 42) == expected


def test_testpaths_in_the_pytest_configuration_decide_the_directory() -> None:
    files = ["pkg/a.py", "pkg/test_inline.py", "pkg/test_more.py", "qa/test_x.py"]
    assert fm.regression_test_path(files, 7) == "pkg/test_issue_7.py"
    assert fm.regression_test_path(files, 7, ["qa"]) == "qa/test_issue_7.py"
    assert fm.regression_test_path(["pkg/a.py", "spec/helpers.py"], 7, ["spec"]) == \
        "spec/test_issue_7.py"


@pytest.mark.parametrize(
    ("configs", "expected"),
    [
        ({"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["test", "docs"]\n'},
         ["test", "docs"]),
        ({"pyproject.toml": '[tool.pytest]\ntestpaths = ["tests/"]\n'}, ["tests"]),
        ({"pytest.ini": "[pytest]\ntestpaths = tests\n    integration\n"},
         ["tests", "integration"]),
        ({"setup.cfg": "[tool:pytest]\ntestpaths = t\n", "tox.ini": "[tox]\n"}, ["t"]),
        ({"pytest.ini": "[pytest]\naddopts = -q\n",
          "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = "spec"\n'}, ["spec"]),
        ({"pyproject.toml": "[tool.pytest.ini_options\nbroken"}, []),
        ({"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["*/tests", "../x"]\n'},
         []),
        ({}, []),
    ],
)
def test_testpaths_are_read_from_the_config_files(
    configs: dict[str, str], expected: list[str]
) -> None:
    assert fm.pytest_testpaths(configs) == expected


def test_the_survey_reads_the_pytest_config_files() -> None:
    script = fm.survey_script(["parse"])
    for name in fm.PYTEST_CONFIGS:
        assert f"echo __FILE__={name};" in script
    toml = '[tool.pytest.ini_options]\ntestpaths = ["test"]\n'
    out = (f"__HEAD__={'a' * 40}\n__LS__=pyproject.toml\n__LS__=test/test_a.py\n"
           f"__FILE__=pyproject.toml\n__B64__={b64(toml)}\n__MISSING__=pytest.ini\n")
    survey = fm.parse_survey(ExecResult(exit_code=0, stdout=out))
    assert survey.configs == {"pyproject.toml": toml}
    assert survey.files == ["pyproject.toml", "test/test_a.py"]


# -- the blast radius --------------------------------------------------------------------------

EVAL = """class Aggregate:
    def _val(self, v):
        return v

    def minimum(self, values):
        return min(self._val(v) for v in values)


def unrelated():
    return 2
"""


def test_changed_functions_are_the_defs_around_the_changed_lines() -> None:
    edited = EVAL.replace("        return v\n", "        return str(v)\n")
    assert [b.name for b in fm.changed_functions(EVAL, edited)] == ["_val"]
    added = EVAL.replace("    return 2\n", "    x = 1\n    return 2 + x\n")
    assert [b.name for b in fm.changed_functions(EVAL, added)] == ["unrelated"]
    assert fm.changed_functions("def broken(:\n", "x") == []


def test_callers_script_greps_whole_words_and_skips_dunders_and_odd_names() -> None:
    script = fm.callers_script(["_val", "__init__", "x; rm -rf /", "_val", "ab"])
    assert script.count("git -c core.quotepath=off grep -n -I -w -F -C 1 -e _val -- '*.py'") == 1
    assert "__init__" not in script and "rm -rf" not in script and "-e ab " not in script
    assert "__CALL__=_val\\t" in script


GREP = """__CALL__=_val\tpkg/eval.py-2-    def _val(self, v):
__CALL__=_val\tpkg/eval.py:3:        return _val(v - 1) if v else v
__CALL__=_val\tpkg/eval.py-4-
__CALL__=_val\tpkg/eval.py:6:        return min(self._val(v) for v in values)
__CALL__=_val\tpkg/aggregates.py-10-    # MIN and MAX
__CALL__=_val\tpkg/aggregates.py:11:        best = _val(value)
__CALL__=_val\tpkg/aggregates.py-12-        return best
__CALL__=_val\t--
__CALL__=_val\tpkg/aggregates.py:30:from pkg.eval import _val
__CALL__=_val\tpkg/evaluate.py:43:    _val,
__CALL__=_val\tpkg/aggregates.py:31:    # _val is the value
__CALL__=_val\ttests/test_eval.py:5:    assert _val(1) == 1
__CALL__=_val\tdocs/example.py:2:print(_val(3))
__CALL__=other\tpkg/x-y.py:9:    other()
"""


def test_other_callers_leave_out_tests_definitions_imports_and_the_function_itself() -> None:
    found = fm.parse_callers(ExecResult(exit_code=0, stdout=GREP))
    sources = ["pkg/eval.py", "pkg/aggregates.py", "pkg/x-y.py", "pkg/evaluate.py"]
    sites, total = fm.select_callers(found, sources, {"_val": [("pkg/eval.py", 2, 3)]})
    assert [(s.symbol, s.path, s.line) for s in sites] == [
        ("_val", "pkg/eval.py", 6), ("other", "pkg/x-y.py", 9), ("_val", "pkg/aggregates.py", 11),
    ]
    assert total == 3
    assert sites[2].code == ("   10      # MIN and MAX\n   11          best = _val(value)\n"
                             "   12          return best")
    capped, total = fm.select_callers(found, sources, {}, limit=2)
    assert len(capped) == 2 and total == 4  # the recursive call counts once nothing is excluded


async def test_a_verified_fix_lists_the_other_callers_of_the_changed_function() -> None:
    files = {**FILES, "widgets/cli.py": (
        "from widgets.core import parse\n\n\ndef main(argv):\n    return parse(argv[0])\n")}
    result, sandbox, llm = await run_fix([GOOD], World(files=files))
    assert result.status is FixStatus.VERIFIED, result.reason
    assert result.changed_functions == ["parse"]
    assert [(s.path, s.line, s.symbol) for s in result.other_callers] == [
        ("widgets/cli.py", 5, "parse"),
    ]
    assert result.other_callers_total == 1
    assert "return parse(argv[0])" in result.other_callers[0].code
    # the model saw them before it answered, with the rule to keep their behaviour
    system, user = llm.prompts[0][0]["content"], llm.prompts[0][1]["content"]
    assert "other callers" in system and "must not alter" in system
    assert "Other callers of parse" in user and "widgets/cli.py:5 uses parse" in user
    markdown = render_fix_markdown(result)
    assert "**Other callers of the changed code, not covered by the reproduction**" in markdown
    assert "`widgets/cli.py:5` uses `parse`" in markdown


async def test_no_other_callers_is_an_empty_list_not_an_error() -> None:
    result, _, llm = await run_fix([GOOD])
    assert result.status is FixStatus.VERIFIED
    assert result.changed_functions == ["parse"] and result.other_callers == []
    assert "Other callers" not in llm.prompts[0][1]["content"]


async def test_a_failed_caller_lookup_never_fails_the_fix() -> None:
    world = World()
    sandbox = ScriptedSandbox(world)
    original = sandbox.run

    async def flaky(image: str, command: str, **kwargs: Any) -> ExecResult:
        if "__CALL__=" in command:
            raise SandboxError("docker hiccup")
        return await original(image, command, **kwargs)

    sandbox.run = flaky  # type: ignore[method-assign]
    result = await fm.attempt_fix(
        assessment(), sandbox=sandbox, llm=FakeLLM([GOOD]), github=FakeGitHub(),
        list_branches=lister, settings=Settings(nebius_api_key="k"),
    )
    assert result.status is FixStatus.VERIFIED and result.other_callers == []


# -- the model's answer ------------------------------------------------------------------------


def test_a_well_formed_answer_is_parsed() -> None:
    p = fm.parse_proposal(GOOD)
    assert p.summary == "Keep the last item in parse"
    assert p.explanation.startswith("parse() slices off")
    assert p.edits == [FileEdit(path="widgets/core.py", old="    return text.split(',')[:-1]\n",
                                new="    return text.split(',')\n")]
    assert not p.problems and not p.needs and p.gave_up is None


def test_fences_bold_labels_and_a_bare_path_line_are_tolerated() -> None:
    text = (
        "**SUMMARY:** Fix it\n\n**FILE:** `widgets/core.py`\n```python\n<<<<<<< SEARCH\n"
        "    return text.split(',')[:-1]\n=======\n    return text.split(',')\n"
        ">>>>>>> REPLACE\n```\n"
        "widgets/__init__.py\n```\n<<<<<<< SEARCH\nfrom widgets.core import parse\n=======\n"
        "from widgets.core import parse  # noqa\n>>>>>>> REPLACE\n```\n"
    )
    p = fm.parse_proposal(text)
    assert [e.path for e in p.edits] == ["widgets/core.py", "widgets/__init__.py"]
    assert p.summary == "Fix it"


def test_an_explanation_said_twice_is_kept_once() -> None:
    p = fm.parse_proposal("EXPLANATION: A is wrong. B fixes it.\nA is wrong. B fixes it.\n")
    assert p.explanation == "A is wrong. B fixes it."


def test_an_answer_given_twice_keeps_its_first_summary_and_explanation() -> None:
    twice = GOOD + "\nNote: done.\n\n" + GOOD.replace("Keep the last item", "Keep it").replace(
        "parse() slices off", "It drops")
    p = fm.parse_proposal(twice)
    assert p.summary == "Keep the last item in parse"
    assert p.explanation.startswith("parse() slices off") and "It drops" not in p.explanation
    assert len(p.edits) == 2 and fm.apply_edits(p.edits, FILES, TRACKED).ok  # same edit twice


def test_need_and_cannot_fix_are_read() -> None:
    p = fm.parse_proposal("NEED: widgets/core.py parse, other\nNEED: widgets/__init__.py\n")
    assert p.needs == [("widgets/core.py", ["parse", "other"]), ("widgets/__init__.py", [])]
    assert fm.parse_proposal("CANNOT_FIX: the test compares a ParseResults to a list").gave_up \
        == "the test compares a ParseResults to a list"


def test_broken_blocks_are_reported_not_guessed() -> None:
    assert "no =======" in fm.parse_proposal("FILE: a.py\n<<<<<<< SEARCH\nx\n").problems[0]
    assert "REPLACE" in fm.parse_proposal("FILE: a.py\n<<<<<<< SEARCH\nx\n=======\ny\n").problems[0]
    p = fm.parse_proposal("<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n")
    assert p.edits == [] and "which FILE" in p.problems[0]


def test_the_summary_is_cleaned_for_a_commit_subject() -> None:
    assert fm.clean_summary("**Fix `parse`** @everyone", "fallback") == "Fix parse everyone"
    assert fm.clean_summary("Remove __html__ from ChainableUndefined", "f") == (
        "Remove __html__ from ChainableUndefined")
    assert fm.clean_summary("", "Fix #42: parse drops") == "Fix #42: parse drops"
    assert len(fm.clean_summary("x" * 200, "f")) == 72


# -- validating the edits ----------------------------------------------------------------------


def edit(old: str, new: str, path: str = "widgets/core.py") -> FileEdit:
    return FileEdit(path=path, old=old, new=new)


def test_an_edit_whose_old_block_occurs_once_is_applied() -> None:
    one = edit("    return text.split(',')[:-1]\n", "    return text.split(',')\n")
    out = fm.apply_edits([one], FILES, TRACKED)
    assert out.ok and out.lines_changed == 2
    assert out.files == {"widgets/core.py": CORE.replace("[:-1]", "")}


def test_an_old_block_that_does_not_occur_is_rejected_with_the_reason() -> None:
    out = fm.apply_edits([edit("    return text.split(';')\n", "x\n")], FILES, TRACKED)
    assert not out.ok and "does not occur" in out.problems[0]


def test_a_repeated_identical_edit_is_applied_once() -> None:
    one = edit("    return text.split(',')[:-1]\n", "    return text.split(',')\n")
    out = fm.apply_edits([one, one], FILES, TRACKED)
    assert out.ok and out.files["widgets/core.py"] == CORE.replace("[:-1]", "")


def test_alternative_edits_of_the_same_lines_are_rejected_with_the_reason() -> None:
    old = "    return text.split(',')[:-1]\n"
    out = fm.apply_edits([edit(old, "    return text.split(',')\n"),
                          edit("  " + old, "    return list(text.split(','))\n")], FILES, TRACKED)
    assert not out.ok and "same lines as edit 1" in out.problems[0]


def test_a_near_miss_comes_back_with_the_lines_the_model_probably_meant() -> None:
    regex = 'X = (\n    r"(?:[A-Z]{2,6}"\n    r"|[A-Z0-9-]{2,})\\Z"\n)\n'
    files = {**FILES, "widgets/core.py": regex}
    out = fm.apply_edits([edit('    r"(?:[A-Z]{2,6}"\n    r"|[A-Z0-9-]{2,})\\Z\n', "x\n")],
                         files, TRACKED)
    assert not out.ok and "does not occur" in out.problems[0]
    assert out.hints and '    r"|[A-Z0-9-]{2,})\\Z"' in out.hints[0]
    assert fm.closest_lines("a\nb\n", "something else entirely\n") is None


def test_an_old_block_that_occurs_twice_is_rejected() -> None:
    files = {**FILES, "widgets/core.py": "x = 1\ny = 2\nx = 1\n"}
    out = fm.apply_edits([edit("x = 1\n", "x = 3\n")], files, TRACKED)
    assert not out.ok and "occurs 2 times" in out.problems[0]


def test_an_old_block_must_match_whole_lines() -> None:
    files = {**FILES, "widgets/core.py": "value = 10\nalue = 1\n"}
    out = fm.apply_edits([edit("alue = 1\n", "alue = 2\n")], files, TRACKED)
    assert out.ok and out.files["widgets/core.py"] == "value = 10\nalue = 2\n"


def test_trailing_whitespace_differences_are_forgiven_but_nothing_else() -> None:
    files = {**FILES, "widgets/core.py": "def f():   \n    return 1\n"}
    assert fm.apply_edits([edit("def f():\n    return 1\n", "def f():\n    return 2\n")],
                          files, TRACKED).ok
    assert not fm.apply_edits([edit("def f():\n  return 1\n", "def f():\n  return 2\n")],
                              files, TRACKED).ok


def test_a_block_indented_differently_matches_once_and_the_replacement_follows_it() -> None:
    old = "        return text.split(',')[:-1]\n"  # eight spaces where the file has four
    out = fm.apply_edits([edit(old, "        return text.split(',')\n")], FILES, TRACKED)
    assert out.ok and out.files["widgets/core.py"] == CORE.replace("[:-1]", "")
    files = {**FILES, "widgets/core.py": "def a():\n    x = 1\n\n\ndef b():\n        x = 1\n"}
    out = fm.apply_edits([edit("  x = 1\n", "  x = 2\n")], files, TRACKED)
    assert not out.ok and "occurs 2 times" in out.problems[0]  # two places: no guessing


def test_edits_to_one_file_apply_in_order() -> None:
    out = fm.apply_edits([
        edit("    return text.split(',')[:-1]\n", "    return text.split(',')\n"),
        edit("    return 1\n", "    return 2\n"),
    ], FILES, TRACKED)
    assert out.ok and "return 2" in out.files["widgets/core.py"]
    assert "[:-1]" not in out.files["widgets/core.py"]


@pytest.mark.parametrize(
    ("path", "fragment"),
    [("tests/test_core.py", "test file"), ("setup.py", "packaging"),
     (".github/workflows/ci.yml", "not a Python source file"),
     ("widgets/brand_new.py", "not a file of the repository")],
)
def test_edits_outside_the_source_are_rejected(path: str, fragment: str) -> None:
    out = fm.apply_edits([edit("x\n", "y\n", path)], FILES, TRACKED)
    assert not out.ok and fragment in out.problems[0]


def test_a_change_bigger_than_the_cap_is_rejected() -> None:
    old = "    return text.split(',')[:-1]\n"
    new = "".join(f"    x{i} = {i}\n" for i in range(fm.MAX_CHANGED_LINES)) + old
    assert fm.apply_edits([edit(old, new)], FILES, TRACKED).ok  # exactly at the cap
    new = "    y = 0\n" + new
    out = fm.apply_edits([edit(old, new)], FILES, TRACKED)
    assert not out.ok and f"the limit is {fm.MAX_CHANGED_LINES}" in out.problems[-1]


def test_a_change_that_breaks_the_syntax_is_rejected() -> None:
    out = fm.apply_edits([edit("    return text.split(',')[:-1]\n", "    return text.split(',\n")],
                         FILES, TRACKED)
    assert not out.ok and "syntax error at line" in out.problems[0]
    assert out.hints and "count the brackets" in out.hints[0]
    assert "    2      return text.split('," in out.hints[0]  # numbered, as the file now is


def test_empty_noop_and_missing_edits_are_rejected() -> None:
    assert "holds no edit" in fm.apply_edits([], FILES, TRACKED).problems[0]
    assert "is empty" in fm.apply_edits([edit("\n", "x\n")], FILES, TRACKED).problems[0]
    same = edit("    return 1\n", "    return 1\n")
    assert "are the same" in fm.apply_edits([same], FILES, TRACKED).problems[0]
    many = [edit(f"x{i}\n", "y\n") for i in range(fm.MAX_EDITS + 1)]
    assert "too many" in fm.apply_edits(many, FILES, TRACKED).problems[0]


def test_crlf_files_keep_their_line_endings_and_a_missing_final_newline_stays_missing() -> None:
    files = {**FILES, "widgets/core.py": "a = 1\r\nb = 2\r\n"}
    out = fm.apply_edits([edit("b = 2\n", "b = 3\n")], files, TRACKED)
    assert out.files["widgets/core.py"] == "a = 1\r\nb = 3\r\n"
    files = {**FILES, "widgets/core.py": "a = 1\nb = 2"}
    out = fm.apply_edits([edit("b = 2\n", "b = 3\n")], files, TRACKED)
    assert out.files["widgets/core.py"] == "a = 1\nb = 3"


# -- localization ------------------------------------------------------------------------------

LONG_TB = """
    def test_x():
>       assert parse('a,b') == ['a', 'b']

tests/test_issue_42.py:5:
_ _ _ _ _
widgets/core.py:2: in parse
    return helper(text)
/work/repo/widgets/__init__.py:9: in helper
    raise ValueError(text)
/usr/lib/python3.12/site-packages/attr/_make.py:10: in x
E   ValueError: a,b
  File "/work/repo/widgets/core.py", line 5, in other
"""


def test_traceback_frames_keep_only_the_projects_source() -> None:
    frames = fm.traceback_frames(LONG_TB, TRACKED)
    assert frames == [("widgets/core.py", 2), ("widgets/__init__.py", 9), ("widgets/core.py", 5)]


def test_files_named_in_the_issue_are_found_by_path_or_unique_name() -> None:
    text = "The bug is in `widgets/core.py`; see also __init__.py and setup.py and nothere.py"
    assert fm.issue_named_files(text, TRACKED) == ["widgets/core.py", "widgets/__init__.py"]


def test_identifiers_come_from_the_issues_code_only() -> None:
    text = ("The method `_iter` is wrong, and so is the parser:\n```python\nrrule = rrulestr(x)\n"
            "for i in range(3): print(rrule._byweekday)\n```\nThe self value is fine.")
    names = fm.issue_code_names(text)
    assert {"_iter", "rrule", "rrulestr", "_byweekday"} <= set(names)
    assert not {"print", "range", "for", "self", "method", "parser"} & set(names)


def test_what_the_test_uses_is_read_with_ast() -> None:
    names = fm.test_names(
        "import pytest\nfrom marshmallow import validate, ValidationError\nimport pkg.sub\n"
        "def test_x():\n    validate.Email()('x')\n    parser.ignore(1)\n"
    )
    assert names.imported == ["validate", "ValidationError"]
    assert names.imported_modules == ["marshmallow.validate", "marshmallow.ValidationError"]
    assert names.modules == ["marshmallow", "pkg.sub"]
    assert {"Email", "ignore"} <= set(names.attributes)
    assert fm.test_names("def broken(:\n").imported == []


def test_files_are_ranked_traceback_first_then_issue_then_definitions() -> None:
    tracked = ["src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/helpers.py", "src/pkg/util.py",
               "src/pkg/validate.py", "src/pkg/other.py", "tests/test_core.py"]
    ranked = fm.rank_files(
        tracked=tracked,
        frames=[("src/pkg/util.py", 10)],
        issue_files=["src/pkg/other.py"],
        names=fm.test_names("from pkg import nested_expr, validate\n"),
        issue_names=["ignore"],
        definitions=[("src/pkg/helpers.py", 30, "def nested_expr(opener):"),
                     ("src/pkg/core.py", 400, "    def ignore(self, other):"),
                     ("tests/test_core.py", 3, "def nested_expr():")],
        term_weights={"nested_expr": 1.0},  # the issue names it too: not incidental API
    )
    paths = [c.path for c in ranked]
    assert paths[:2] == ["src/pkg/util.py", "src/pkg/other.py"]
    assert set(paths[2:]) == {"src/pkg/helpers.py", "src/pkg/validate.py"}  # 4 files at most
    helpers = next(c for c in ranked if c.path == "src/pkg/helpers.py")
    assert helpers.anchors == {30: 20.0}
    assert "tests/test_core.py" not in paths


def test_a_small_file_is_shown_whole_and_a_big_one_as_windows() -> None:
    whole = fm.show_file("widgets/core.py", CORE, [2], 10_000)
    assert whole is not None and whole.whole and whole.text == CORE
    body = "".join(f"def f{i}(x):\n    y = x + {i}\n    return y\n\n\n" for i in range(800))
    big = "import os\n\n\n" + body
    shown = fm.show_file("pkg/big.py", big, [big.splitlines().index("def f500(x):") + 1], 6_000)
    assert shown is not None and not shown.whole
    assert "def f500(x):" in shown.text and "import os" in shown.text
    assert "def f200(x):" not in shown.text.split("Outline of")[0]
    assert re.search(r"\.\.\. \[lines \d+-\d+ not shown\] \.\.\.", shown.text)
    assert "Outline of pkg/big.py" in shown.text
    assert len(shown.text) <= 6_000


def test_windows_follow_the_enclosing_function() -> None:
    source = ("x = 1\n\n\nclass A:\n    def m(self):\n        a = 1\n        b = 2\n"
              "        return a\n")
    blocks = fm.code_blocks(fm._parse(source))
    assert fm.window_for(7, blocks, 8) == (5, 8)  # the method, not the class
    assert fm.window_for(4, blocks, 8) == (4, 8)  # a class anchor: the head of the class


def test_symbol_lines_find_defs_classes_and_assignments() -> None:
    source = "A = 1\n\nclass B:\n    C = 2\n\n    def d(self):\n        pass\n"
    assert fm.symbol_lines(source, ["A", "B", "C", "d", "missing"]) == [1, 3, 4, 6]


# -- the scripts and their parsers -------------------------------------------------------------


def test_the_survey_lists_files_and_greps_definitions_with_marked_lines() -> None:
    script = fm.survey_script(["parse", "nested_expr", "bad name; rm -rf /"])
    assert "git -c core.quotepath=off ls-files" in script and '"__LS__=" $0' in script
    assert "git -c core.quotepath=off grep -n -I -E" in script and '"__DEF__=" $0' in script
    assert "parse|nested_expr" in script and "rm -rf" not in script
    assert "grep" not in fm.survey_script([])


def test_survey_output_is_parsed() -> None:
    out = ExecResult(exit_code=0, stdout=(
        f"__HEAD__={HEAD}\n__LS__=widgets/core.py\n__LS__=tests/test_core.py\n"
        "__DEF__=widgets/core.py:1:def parse(text):\n__DEF__=garbage\n"
        "| __LS__=forged\n"
    ))
    survey = fm.parse_survey(out)
    assert survey.head == HEAD and survey.files == ["widgets/core.py", "tests/test_core.py"]
    assert survey.definitions == [("widgets/core.py", 1, "def parse(text):")]
    assert survey.failed is None
    assert fm.parse_survey(ExecResult(exit_code=20, stdout="__SETUP_FAILED__=cd\n")).failed == "cd"


def test_files_are_read_as_base64_and_odd_paths_are_refused() -> None:
    script = fm.read_script(["widgets/core.py"])
    assert "base64 < widgets/core.py" in script and "__MISSING__=widgets/core.py" in script
    with pytest.raises(fm.EnvBuildError):
        fm.read_script(["../../etc/passwd"])
    with pytest.raises(fm.EnvBuildError):
        fm.read_script(["a.py; rm -rf /"])
    out = ExecResult(exit_code=0, stdout=(
        f"__FILE__=widgets/core.py\n__B64__={b64(CORE)}\n__MISSING__=gone.py\n"
        f"__FILE__=bin.py\n__B64__={base64.b64encode(bytes([255, 254])).decode()}\n"
    ))
    assert fm.parse_read(out) == {"widgets/core.py": CORE}


def test_the_apply_script_diffs_only_the_edited_files() -> None:
    script = fm.apply_script(["widgets/core.py"])
    assert "git -c core.quotepath=off diff --no-color --no-ext-diff --no-textconv" in script
    assert "-- widgets/core.py" in script and "__DIFF_B64__" in script
    with pytest.raises(fm.EnvBuildError):
        fm.apply_script(["../x.py"])


def test_diff_counts() -> None:
    diff = git_diff("widgets/core.py", CORE, CORE.replace("[:-1]", ""))
    assert fm.diff_counts(diff) == (["widgets/core.py"], 1, 1)


# -- the scripted world ------------------------------------------------------------------------


def git_diff(path: str, before: str, after: str) -> str:
    body = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        f"a/{path}", f"b/{path}",
    ))
    return f"diff --git a/{path} b/{path}\n{body}"


def repro_output(codes: list[int], log: str = "1 failed in 0.01s") -> ExecResult:
    out = "".join(f"__RUN_START__={i}\n| {log}\n__RC__={rc}\n" for i, rc in enumerate(codes, 1))
    return ExecResult(exit_code=0, stdout=out)


def suite_output(*, passed: int = 120, failed: tuple[str, ...] = (), rc: int | None = None,
                 summary: str | None = None) -> ExecResult:
    lines = ["tests/test_a.py ....", "=== short test summary info ==="]
    lines += [f"FAILED {t} - AssertionError" for t in failed]
    if summary is None:
        summary = (f"{len(failed)} failed, " if failed else "") + f"{passed} passed in 3.21s"
    lines.append(summary)
    code = rc if rc is not None else (1 if failed else 0)
    return ExecResult(exit_code=0, stdout="".join(f"| {ln}\n" for ln in lines) + f"__RC__={code}\n")


@dataclass
class World:
    """What each kind of sandbox run answers. The defaults make the first good edit verified."""

    files: dict[str, str] = field(default_factory=lambda: dict(FILES))
    build: ExecResult | BaseException = field(
        default_factory=lambda: ExecResult(exit_code=0, stdout="__STEP__=done\n", image="img-base")
    )
    before: ExecResult = field(default_factory=lambda: repro_output([1, 1]))
    before_at: dict[str, ExecResult] = field(default_factory=dict)  # test path -> answer
    suite_before: ExecResult = field(default_factory=lambda: suite_output(passed=120))
    # what the reproduction test and the suite do with a given edited core.py
    good_core: str = CORE.replace("[:-1]", "")
    suite_with_good: ExecResult = field(default_factory=lambda: suite_output(passed=121))
    suite_with_other: ExecResult | None = None


class ScriptedSandbox:
    name = "scripted"

    def __init__(self, world: World | None = None) -> None:
        self.world = world or World()
        self.calls: list[dict[str, Any]] = []
        self.patched: dict[str, dict[str, str]] = {}  # image -> edited files

    async def base_image(self, python_version: str | None) -> str:
        return f"base:{python_version or 'default'}"

    def _record(self, kind: str, image: str, **extra: Any) -> None:
        self.calls.append({"kind": kind, "image": image, **extra})

    async def run(
        self, image: str, command: str, *, files: dict[str, str] | None = None,
        workdir: str = "/work", timeout_s: int = 900, keep: bool = False, network: bool = True,
    ) -> ExecResult:
        w = self.world
        meta = {"network": network, "keep": keep, "timeout_s": timeout_s, "files": files}
        if "__STEP__=clone" in command:
            self._record("build", image, command=command, **meta)
            if isinstance(w.build, BaseException):
                raise w.build
            return w.build
        if "ls-files" in command:
            self._record("survey", image, command=command, **meta)
            out = f"__HEAD__={HEAD}\n" + "".join(f"__LS__={p}\n" for p in w.files)
            for path, text in w.files.items():
                for number, line in enumerate(text.splitlines(), 1):
                    if re.match(r"\s*def\s+parse\b", line):
                        out += f"__DEF__={path}:{number}:{line}\n"
            return ExecResult(exit_code=0, stdout=out)
        if "__MISSING__" in command:
            self._record("read", image, command=command, **meta)
            out = ""
            for path in re.findall(r"__MISSING__=(\S+?)'?;", command):
                if path in w.files:
                    out += f"__FILE__={path}\n__B64__={b64(w.files[path])}\n"
                else:
                    out += f"__MISSING__={path}\n"
            return ExecResult(exit_code=0, stdout=out)
        if "__CALL__=" in command:  # git grep -n -w -C 1 for each name, over the world's files
            self._record("callers", image, command=command, **meta)
            out = ""
            for name in re.findall(r"-e (\w+) --", command):
                word = re.compile(rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])")
                for path, text in w.files.items():
                    if not path.endswith(".py"):
                        continue
                    lines = text.splitlines()
                    hits = [n for n, line in enumerate(lines, 1) if word.search(line)]
                    shown = sorted({k for n in hits for k in (n - 1, n, n + 1)
                                    if 1 <= k <= len(lines)})
                    for k in shown:
                        sep = ":" if k in hits else "-"
                        out += f"__CALL__={name}\t{path}{sep}{k}{sep}{lines[k - 1]}\n"
            return ExecResult(exit_code=0, stdout=out)
        if "__DIFF_B64__" in command:
            self._record("apply", image, **meta)
            edited = {k.removeprefix("/work/repo/"): v for k, v in (files or {}).items()}
            diff = "".join(git_diff(p, w.files[p], t) for p, t in sorted(edited.items()))
            new_image = f"img-patched-{len(self.patched) + 1}"
            self.patched[new_image] = edited
            return ExecResult(exit_code=0, stdout=f"__DIFF_B64__={b64(diff)}\n", image=new_image)
        if "__RUN_START__" in command:
            path = re.search(r"cp /tmp/ir_test_file (\S+) ", command)
            meta["path"] = path.group(1) if path else None
            if image == "img-base":
                self._record("repro-before", image, **meta)
                return w.before_at.get(meta["path"], w.before)
            self._record("repro-after", image, **meta)
            good = self.patched[image].get("widgets/core.py") == w.good_core
            return repro_output([0, 0], "1 passed") if good else repro_output([1, 1])
        if "--maxfail=0" in command:
            if image == "img-base":
                self._record("suite-before", image, **meta)
                return w.suite_before
            self._record("suite-after", image, **meta)
            good = self.patched[image].get("widgets/core.py") == w.good_core
            if good:
                return w.suite_with_good
            return w.suite_with_other or w.suite_with_good
        raise AssertionError(f"unexpected script: {command[:200]}")  # pragma: no cover

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls]


CUT_OFF = "<cut off>"  # an answer that ran out of tokens before it said anything


class FakeLLM:
    """Answers in turn; each answer costs ``cost`` dollars."""

    def __init__(self, answers: list[str | BaseException], cost: float = 0.01) -> None:
        self.answers = list(answers)
        self.usage = Usage()
        self.cost = cost
        self.prompts: list[list[dict[str, Any]]] = []
        self.thinking: list[bool | None] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
        self.prompts.append([dict(m) for m in messages])
        self.thinking.append(kwargs.get("thinking"))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        self.usage.llm_calls += 1
        self.usage.cost_usd += self.cost
        if answer == CUT_OFF:
            return ChatResult(content="", finish_reason="length",
                              usage=Usage(llm_calls=1, cost_usd=self.cost))
        return ChatResult(content=answer, usage=Usage(llm_calls=1, cost_usd=self.cost))


class FakeGitHub:
    def __init__(self, body: str = "parse() drops the last item. See `parse` in core.py") -> None:
        self.body = body

    async def fetch_issue(self, url: str) -> Issue:
        return Issue(url=url, owner="acme", repo="widgets", number=42,
                     title="parse() drops the last item", body=self.body, default_branch="main")

    async def aclose(self) -> None:
        return None


def assessment(*, verdict: Verdict = Verdict.OPEN_BUG, code: str | None = TEST_CODE,
               issue_url: str = ISSUE) -> Assessment:
    return Assessment(
        issue_url=issue_url, title="parse() drops the last item", verdict=verdict,
        confidence=Confidence.HIGH,
        reproduction=Reproduction(
            repro_test_path="tests/test_fixedyet_repro.py", repro_test_code=code,
            reported_side=SideRun(ref="1.2.0", outcomes=[Outcome.FAIL, Outcome.FAIL]),
            fix_side=SideRun(ref="main", outcomes=[Outcome.FAIL, Outcome.FAIL],
                             log_excerpt="widgets/core.py:2: in parse\nE  AssertionError"),
        ),
    )


async def lister(repo: str) -> dict[str, str]:
    assert repo == "acme/widgets"
    return {"main": HEAD, "1.2.x": "b" * 40}


async def run_fix(
    answers: list[str | BaseException], world: World | None = None,
    a: Assessment | None = None, **kwargs: Any,
) -> tuple[fm.FixResult, ScriptedSandbox, FakeLLM]:
    sandbox = ScriptedSandbox(world)
    llm = FakeLLM(answers)
    events: list[ProgressEvent] = kwargs.pop("events", [])
    result = await fm.attempt_fix(
        a or assessment(), sandbox=sandbox, llm=llm, github=FakeGitHub(), list_branches=lister,
        settings=Settings(nebius_api_key="k"), progress=events.append, **kwargs,
    )
    return result, sandbox, llm


# -- the attempt loop --------------------------------------------------------------------------


async def test_a_good_first_answer_is_verified() -> None:
    events: list[ProgressEvent] = []
    result, sandbox, llm = await run_fix([GOOD], events=events)
    assert result.status is FixStatus.VERIFIED, result.reason
    assert result.repo == "acme/widgets" and result.base_branch == "main"
    assert result.base_sha == HEAD
    assert result.files_changed == ["widgets/core.py"]
    assert (result.lines_added, result.lines_removed) == (1, 1)
    assert "-    return text.split(',')[:-1]" in result.diff
    assert result.summary == "Keep the last item in parse"
    assert [o.value for o in result.repro_before.outcomes] == ["fail", "fail"]
    assert [o.value for o in result.repro_after.outcomes] == ["pass", "pass"]
    assert (result.suite_before.passed, result.suite_after.passed) == (120, 121)
    assert result.regression_test_path == "tests/test_issue_42.py"
    assert result.repro_test_path == "tests/test_issue_42.py"  # verified where the PR puts it
    assert result.context_files == ["widgets/core.py"]  # __init__.py is far behind: left out
    assert [a.outcome for a in result.attempts] == ["verified"]
    assert result.usage.llm_calls == 1 and result.usage.cost_usd == pytest.approx(0.01)
    assert result.usage.sandbox_runs == len(sandbox.calls) and result.usage.wall_seconds >= 0
    assert "attempt 1 of 3" in result.reason and "no newly failing" in result.reason
    assert sandbox.kinds() == [
        "build", "survey", "repro-before", "repro-before", "read", "callers", "apply",
        "repro-after", "suite-before", "suite-after", "callers",
    ]
    # first where the assessment ran it, then where the pull request adds it
    assert [c["path"] for c in sandbox.calls if c["kind"] == "repro-before"] == [
        "tests/test_fixedyet_repro.py", "tests/test_issue_42.py",
    ]
    assert {e.stage for e in events} == {"fix", "environment"}


async def test_patched_code_runs_only_in_the_sandbox_and_without_network() -> None:
    _, sandbox, _ = await run_fix([GOOD])
    by_kind = {c["kind"]: c for c in sandbox.calls}
    for kind in ("survey", "read", "callers", "apply", "repro-before", "repro-after",
                 "suite-before", "suite-after"):
        assert by_kind[kind]["network"] is False, kind
    assert by_kind["apply"]["keep"] is True and by_kind["apply"]["image"] == "img-base"
    assert by_kind["apply"]["files"] == {"/work/repo/widgets/core.py": CORE.replace("[:-1]", "")}
    assert by_kind["repro-after"]["image"] == "img-patched-1"
    assert by_kind["suite-after"]["image"] == "img-patched-1"
    assert HEAD in by_kind["build"]["command"]  # pinned to the commit, not the branch name
    assert by_kind["suite-before"]["timeout_s"] >= 900


async def test_the_prompt_carries_the_issue_the_test_the_failure_and_the_code() -> None:
    _, _, llm = await run_fix([GOOD])
    system, user = llm.prompts[0][0]["content"], llm.prompts[0][1]["content"]
    assert "SEARCH" in system and "CANNOT_FIX" in system and str(fm.MAX_CHANGED_LINES) in system
    assert "untrusted data" in system
    assert "parse() drops the last item" in user and TEST_CODE.strip() in user
    assert '<data kind="file" path="widgets/core.py" lines="whole">' in user
    assert "widgets/core.py" in user and "tests/test_core.py" not in user.split("source files")[1]
    assert f"main at {HEAD[:12]}" in user


async def test_rejected_edits_go_back_to_the_model_and_the_next_answer_is_checked() -> None:
    bad = GOOD.replace("FILE: widgets/core.py", "FILE: tests/test_core.py")
    result, sandbox, llm = await run_fix([bad, GOOD])
    assert result.status is FixStatus.VERIFIED
    assert [a.outcome for a in result.attempts] == ["invalid_edits", "verified"]
    assert "test file" in result.attempts[0].reason
    feedback = llm.prompts[1][-1]["content"]
    assert "rejected and nothing was run" in feedback and "test file" in feedback
    assert sandbox.kinds().count("apply") == 1  # the rejected answer never reached the sandbox


async def test_a_change_that_does_not_fix_the_test_gets_its_output_as_feedback() -> None:
    result, sandbox, llm = await run_fix([WRONG, GOOD])
    assert result.status is FixStatus.VERIFIED
    first = result.attempts[0]
    assert first.outcome == "repro_still_fails" and first.reason == "runs: fail, fail"
    assert "text.split(';')" in first.diff
    feedback = llm.prompts[1][-1]["content"]
    assert "does not pass" in feedback and "text.split(';')" in feedback
    assert "back to the original code" in feedback
    assert sandbox.kinds().count("suite-before") == 1  # the suite only runs for a passing test


async def test_a_regression_names_the_newly_failing_tests_to_the_model() -> None:
    world = World(good_core=CORE.replace("[:-1]", ""),
                  suite_with_good=suite_output(passed=119, failed=("tests/test_core.py::test_ok",)))
    other = GOOD.replace("    return text.split(',')\n", "    return list(text.split(','))\n")
    world_good = CORE.replace("    return text.split(',')[:-1]", "    return list(text.split(','))")

    class TwoFixes(ScriptedSandbox):
        async def run(self, image: str, command: str, **kwargs: Any) -> ExecResult:
            if "__RUN_START__" in command and image != "img-base":
                self._record("repro-after", image, network=kwargs.get("network"))
                return repro_output([0, 0], "1 passed")
            if "--maxfail=0" in command and image != "img-base":
                self._record("suite-after", image, network=kwargs.get("network"))
                core = self.patched[image]["widgets/core.py"]
                return (suite_output(passed=121) if core == world_good else
                        suite_output(passed=119, failed=("tests/test_core.py::test_ok",)))
            return await super().run(image, command, **kwargs)

    sandbox = TwoFixes(world)
    llm = FakeLLM([GOOD, other])
    result = await fm.attempt_fix(
        assessment(), sandbox=sandbox, llm=llm, github=FakeGitHub(), list_branches=lister,
        settings=Settings(nebius_api_key="k"),
    )
    assert result.status is FixStatus.VERIFIED
    first = result.attempts[0]
    assert first.outcome == "regression" and first.newly_failing == ["tests/test_core.py::test_ok"]
    assert "tests/test_core.py::test_ok" in llm.prompts[1][-1]["content"]
    assert sandbox.kinds().count("suite-before") == 1  # the baseline suite is run once
    assert "list(text.split(','))" in result.diff


async def test_three_failed_attempts_end_not_fixed_with_every_attempt_recorded() -> None:
    result, sandbox, _ = await run_fix([WRONG, WRONG, "no edits here"])
    assert result.status is FixStatus.NOT_FIXED
    assert [a.outcome for a in result.attempts] == [
        "repro_still_fails", "repro_still_fails", "no_edits"]
    assert "no verified fix in 3 attempt(s)" in result.reason
    assert result.diff == "" and result.summary == ""
    assert sandbox.kinds().count("suite-before") == 0


async def test_the_model_may_ask_once_for_more_code() -> None:
    world = World(files={**FILES, "widgets/extra.py": "def helper():\n    return 1\n"})
    need = "NEED: widgets/extra.py helper\nNEED: tests/test_core.py\n"
    result, sandbox, llm = await run_fix([need, GOOD], world)
    assert result.status is FixStatus.VERIFIED
    assert len(result.attempts) == 1 and result.attempts[0].cost_usd == pytest.approx(0.02)
    answer = llm.prompts[1][-1]["content"]
    assert '<data kind="file" path="widgets/extra.py" lines="whole">' in answer
    assert "tests/test_core.py: tests/test_core.py is a test file" in answer
    assert "widgets/extra.py" in result.context_files
    assert sandbox.kinds().count("read") == 2


async def test_a_second_need_counts_as_an_attempt_without_edits() -> None:
    need = "NEED: widgets/__init__.py\n"
    result, _, _ = await run_fix([need, need, GOOD])
    assert [a.outcome for a in result.attempts] == ["no_edits", "verified"]


async def test_an_answer_cut_off_by_the_token_limit_is_named_and_the_next_call_is_brief() -> None:
    result, _, llm = await run_fix([CUT_OFF, GOOD])
    assert result.status is FixStatus.VERIFIED
    first = result.attempts[0]
    assert first.outcome == "no_edits" and "ran out of tokens" in first.reason
    assert "cut off" in llm.prompts[1][-1]["content"]
    assert llm.thinking == [None, False]


async def test_cannot_fix_stops_at_once_with_the_models_reason() -> None:
    result, sandbox, _ = await run_fix(["CANNOT_FIX: the test compares ParseResults to a list"])
    assert result.status is FixStatus.NOT_FIXED
    assert result.attempts[0].outcome == "gave_up"
    assert "ParseResults" in result.reason
    assert "apply" not in sandbox.kinds()


async def test_the_budget_caps_the_model_spend() -> None:
    result, _, llm = await run_fix([WRONG, WRONG, GOOD], budget_usd=0.025)
    assert result.status is FixStatus.NOT_FIXED
    assert "fix budget of $0.03" in result.reason or "fix budget of $0.02" in result.reason
    assert llm.usage.cost_usd <= 0.025 + 1e-9
    assert result.budget_usd == pytest.approx(0.025)


async def test_a_model_api_failure_is_an_error_not_a_crash() -> None:
    result, _, _ = await run_fix([LLMError("HTTP 500")])
    assert result.status is FixStatus.ERROR and "model API failed" in result.reason


async def test_a_test_that_already_passes_on_main_needs_no_fix() -> None:
    result, sandbox, llm = await run_fix([GOOD], World(before=repro_output([0, 0], "1 passed")))
    assert result.status is FixStatus.ALREADY_FIXED and "passes on main" in result.reason
    assert "(its commit was not recorded)" in result.reason
    assert llm.prompts == [] and "apply" not in sandbox.kinds()
    assert [c["path"] for c in sandbox.calls if c["kind"] == "repro-before"] == [
        "tests/test_fixedyet_repro.py",  # the way the assessment ran it
    ]


def assessed_at(commit: str, outcomes: list[Outcome] | None = None) -> Assessment:
    a = assessment()
    a.reproduction.fix_side.commit = commit
    if outcomes is not None:
        a.reproduction.fix_side.outcomes = outcomes
    return a


async def test_a_pass_at_the_commit_the_assessment_saw_fail_is_an_error_not_already_fixed() -> None:
    # RDFLib/pySHACL#332: the assessment failed on master; fix mode, at the same commit, saw it
    # pass. Two runs that contradict each other are never "already fixed".
    world = World(before_at={"tests/test_fixedyet_repro.py": repro_output([0, 0], "1 passed")})
    a = assessed_at(HEAD, [Outcome.FAIL])  # one run only: not reused, so fix mode runs it
    result, _, llm = await run_fix([GOOD], world, a=a)
    assert result.status is FixStatus.ERROR
    assert "contradict each other" in result.reason and HEAD[:12] in result.reason
    assert llm.prompts == []


async def test_a_pass_after_the_branch_moved_is_already_fixed() -> None:
    world = World(before=repro_output([0, 0], "1 passed"))
    result, _, _ = await run_fix([GOOD], world, a=assessed_at("c" * 40))
    assert result.status is FixStatus.ALREADY_FIXED
    assert f"saw it fail on main at {'c' * 12}" in result.reason


async def test_the_assessments_runs_at_the_same_commit_are_reused() -> None:
    result, sandbox, _ = await run_fix([GOOD], a=assessed_at(HEAD))
    assert result.status is FixStatus.VERIFIED
    befores = [c["path"] for c in sandbox.calls if c["kind"] == "repro-before"]
    assert befores == ["tests/test_issue_42.py"]  # only where the pull request adds it


async def test_a_test_that_only_fails_where_the_assessment_ran_it_is_not_shipped() -> None:
    # In test/, the package's __init__.py imports the project before the test can set what the
    # project reads at import time, so the test passes there trivially. It must not be the
    # regression test, and the fix is verified where the test does fail.
    world = World(before_at={"tests/test_issue_42.py": repro_output([0, 0], "1 passed")})
    result, sandbox, _ = await run_fix([GOOD], world)
    assert result.status is FixStatus.VERIFIED, result.reason
    assert result.regression_test_path is None
    assert result.repro_test_path == "tests/test_fixedyet_repro.py"
    assert "does not fail at tests/test_issue_42.py" in result.regression_test_note
    after = [c for c in sandbox.calls if c["kind"] == "repro-after"]
    assert after and all(c["path"] == "tests/test_fixedyet_repro.py" for c in after)
    assert [o.value for o in result.repro_before.outcomes] == ["fail", "fail"]


async def test_a_flaky_or_erroring_test_before_the_change_cannot_judge_a_fix() -> None:
    result, _, llm = await run_fix([GOOD], World(before=repro_output([1, 0])))
    assert result.status is FixStatus.ERROR and "clean failure" in result.reason
    assert llm.prompts == []


async def test_a_suite_that_cannot_run_on_main_stops_with_the_patch_unverified() -> None:
    result, _, llm = await run_fix(
        [GOOD, GOOD], World(suite_before=suite_output(rc=5, summary="no tests ran in 0.01s")))
    assert result.status is FixStatus.ERROR and "unverified" in result.reason
    assert result.diff and len(llm.prompts) == 1
    assert result.attempts[0].outcome == "suite_not_run"


async def test_a_suite_that_breaks_with_the_change_is_a_failed_attempt() -> None:
    world = World(suite_with_good=suite_output(rc=2, summary="interrupted: 1 error in 0.2s"))
    result, _, llm = await run_fix([GOOD, GOOD, GOOD], world)
    assert result.status is FixStatus.NOT_FIXED
    assert [a.outcome for a in result.attempts] == ["suite_not_run"] * 3
    assert "could not finish" in llm.prompts[1][-1]["content"]


@pytest.mark.parametrize(
    ("a", "fragment"),
    [
        (assessment(verdict=Verdict.FIXED_ON_MAIN), "needs an open bug"),
        (assessment(code=None), "no reproduction test"),
        (assessment(issue_url="swe-bench:acme__widgets-42"), "not about a GitHub issue"),
    ],
)
async def test_only_an_open_bug_with_a_test_on_github_can_be_fixed(
    a: Assessment, fragment: str
) -> None:
    result, sandbox, llm = await run_fix([GOOD], a=a)
    assert result.status is FixStatus.NOT_APPLICABLE and fragment in result.reason
    assert sandbox.calls == [] and llm.prompts == []
    assert fm.fix_ready(a) is False
    assert fm.fix_ready(assessment()) is True


async def test_a_sandbox_failure_is_an_error_result() -> None:
    result, _, _ = await run_fix([GOOD], World(build=SandboxError("docker is not running")))
    assert result.status is FixStatus.ERROR and "docker is not running" in result.reason


async def test_a_missing_default_branch_is_an_error() -> None:
    async def nothing(repo: str) -> dict[str, str]:
        return {"develop": HEAD}

    result = await fm.attempt_fix(
        assessment(), sandbox=ScriptedSandbox(), llm=FakeLLM([GOOD]), github=FakeGitHub(),
        list_branches=nothing, settings=Settings(nebius_api_key="k"),
    )
    assert result.status is FixStatus.ERROR and "no branch named 'main'" in result.reason


async def test_nothing_to_tie_the_failure_to_is_not_fixed_without_a_model_call() -> None:
    world = World(files={"README.md": "x", "tests/test_a.py": "", "lib/other.py": "y = 1\n"})
    a = assessment()
    a.reproduction.fix_side = SideRun(ref="main", outcomes=[Outcome.FAIL, Outcome.FAIL])
    result, _, llm = await run_fix([GOOD], world, a=a)
    assert result.status is FixStatus.NOT_FIXED and "could be tied" in result.reason
    assert llm.prompts == []


# -- the markdown summary and the command line --------------------------------------------------


async def test_the_markdown_summary_lists_every_attempt_and_the_verified_diff() -> None:

    result, _, _ = await run_fix([WRONG, GOOD])
    text = render_fix_markdown(result)
    assert text.startswith("# Verified fix: the test fails on main without it")
    assert "| 1 | `repro_still_fails` |" in text and "| 2 | `verified` |" in text
    assert "Verified diff:" in text and "+    return text.split(',')" in text
    assert "| Without the change | FAIL | FAIL |" in text
    assert "**Regression test in the pull request:** `tests/test_issue_42.py`" in text
    failed, _, _ = await run_fix([WRONG, WRONG, WRONG])
    text = render_fix_markdown(failed)
    assert text.startswith("# No verified fix") and "Diff (not verified)" not in text


def test_the_fix_command_checks_its_arguments_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from fixedyet import cli

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(nebius_api_key="k"))
    monkeypatch.setattr(cli, "_run_fix", lambda *a, **k: pytest.fail("must not run"))
    assert cli.main(["fix"]) == 2
    assert "--from-assessment" in capsys.readouterr().err
    assert cli.main(["fix", ISSUE, "--pr-target", "upstream"]) == 2
    assert cli.main(["fix", ISSUE, "--yes-upstream"]) == 2
    assert cli.main(["fix", ISSUE, "--budget", "0"]) == 2
    fixed = tmp_path / "a.json"
    fixed.write_text(assessment(verdict=Verdict.FIXED_ON_MAIN).model_dump_json())
    assert cli.main(["fix", "--from-assessment", str(fixed)]) == 2
    assert "needs open_bug" in capsys.readouterr().err
    other = tmp_path / "b.json"
    other.write_text(assessment().model_dump_json())
    assert cli.main(["fix", "https://github.com/acme/widgets/issues/7",
                     "--from-assessment", str(other)]) == 2
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(nebius_api_key=""))
    assert cli.main(["fix", "--from-assessment", str(other)]) == 2
    assert "NEBIUS_API_KEY" in capsys.readouterr().err


def test_the_fix_command_writes_the_result_and_the_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from fixedyet import cli

    saved = tmp_path / "a.json"
    saved.write_text(assessment().model_dump_json())
    seen: dict[str, Any] = {}
    real = fm.attempt_fix

    async def fake_attempt(a: Assessment, **kwargs: Any) -> fm.FixResult:
        seen.update(kwargs)
        return await real(
            a, sandbox=ScriptedSandbox(), llm=FakeLLM([GOOD]), github=FakeGitHub(),
            list_branches=lister, settings=Settings(nebius_api_key="k"),
        )

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(nebius_api_key="k"))
    monkeypatch.setattr(fm, "attempt_fix", fake_attempt)
    out_json, preview = tmp_path / "r.json", tmp_path / "p.md"
    code = cli.main(["fix", "--from-assessment", str(saved), "--json", str(out_json),
                     "--pr-preview", str(preview), "--pr-login", "alice", "--budget", "0.1"])
    assert code == 0 and seen["budget_usd"] == 0.1
    assert fm.FixResult.model_validate_json(out_json.read_text()).status is FixStatus.VERIFIED
    text = preview.read_text()
    assert "head: alice:fixedyet/fix-42" in text and text.count("reviewed and submitted by") == 1
    assert "# Verified fix" in capsys.readouterr().out


async def test_an_assessment_with_a_fix_gains_a_fix_section() -> None:
    from fixedyet.report import render_markdown

    result, _, _ = await run_fix([GOOD])
    a = assessment()
    a.fix = result
    text = render_markdown(a)
    assert "## Fix" in text and "**Status:** `verified`" in text
    assert text.index("## Fix") < text.index("## Usage")


def test_fix_mode_gives_a_long_reasoning_answer_time_to_arrive() -> None:
    from fixedyet.llm import LLM

    client = LLM(Settings(nebius_api_key="k"), timeout_s=fm.FIX_REQUEST_TIMEOUT_S)._get_client()
    assert client.timeout == fm.FIX_REQUEST_TIMEOUT_S > 180
    assert LLM(Settings(nebius_api_key="k"))._get_client().timeout == 180.0


# -- symbol-based localization --------------------------------------------------------------

ISSUE_2244 = (
    "ChainableUndefined causes `is escaped` test to pass for `undefined` child elements\n"
    "When using `ChainableUndefined` the `is escaped` test incorrectly passes.\n"
    "```jinja\n{{ parent.child | safe if parent.child is escaped }}\n```\n"
    "We render with a FileSystemLoader and the nhs_frontend package."
)
TEST_2244 = (
    "from jinja2 import Environment, ChainableUndefined\n\n"
    "def test_it():\n    template_string = '{{ x if x is mapping else y }}'\n"
    "    env = Environment(undefined=ChainableUndefined)\n"
    "    assert env.from_string(template_string).render() == ''\n"
)


def test_distinctive_terms_come_from_templates_code_spans_names_and_quotes() -> None:
    terms = fm.distinctive_terms(ISSUE_2244, TEST_2244)
    assert terms["escaped"] == terms["safe"] == terms["mapping"] == 3.0  # after is / |
    assert terms["undefined"] == 2.0 and terms["ChainableUndefined"] == 2.0
    assert terms["FileSystemLoader"] == 2.0 and terms["nhs_frontend"] == 2.0
    assert "template_string" not in terms and "env" not in terms  # the test's own names
    assert not {"is", "if", "else", "When", "using", "test"} & set(terms)
    assert list(terms)[:4] == ["safe", "escaped", "mapping", "undefined"] or \
        list(terms)[0] in ("safe", "escaped")
    quoted = fm.distinctive_terms("It should be 'escaped' here", "")
    assert quoted == {"escaped": 1.0}


def test_symbol_scores_favour_rare_terms_breadth_and_registries() -> None:
    sources = [f"pkg/m{i}.py" for i in range(8)]
    counts = {
        "escaped": {"pkg/m0.py": 2, "pkg/m1.py": 1},
        "mapping": {"pkg/m0.py": 2},
        "undefined": {p: 40 for p in sources[1:7]},  # everywhere: worth little
        "elsewhere": {"tests/test_x.py": 9},  # only in tests: not counted
    }
    scores, weights = fm.symbol_scores(counts, sources, {"escaped": 3.0, "mapping": 3.0},
                                       quoted={"escaped": {"pkg/m0.py": 1}})
    assert max(scores, key=scores.get) == "pkg/m0.py"
    assert weights["escaped"] > weights["undefined"] and "elsewhere" not in weights
    plain, _ = fm.symbol_scores(counts, sources, {"escaped": 3.0, "mapping": 3.0})
    assert scores["pkg/m0.py"] > plain["pkg/m0.py"]  # the registry bonus
    assert scores["pkg/m1.py"] < scores["pkg/m0.py"] / 2


def test_a_template_registry_and_the_undefined_class_rank_above_incidental_api() -> None:
    """The shape of pallets/jinja#2244: the test imports Environment and ChainableUndefined, the
    issue is about `is escaped`. tests.py registers "escaped"; runtime.py defines the class."""
    sources = ["src/jinja2/tests.py", "src/jinja2/runtime.py", "src/jinja2/environment.py",
               "src/jinja2/filters.py", "src/jinja2/utils.py", "src/jinja2/compiler.py",
               "src/jinja2/nodes.py", "src/jinja2/ext.py", "tests/test_api.py"]
    terms = fm.distinctive_terms(ISSUE_2244, TEST_2244)
    j = "src/jinja2/"
    counts = {  # the real counts at 5ef70112 (git grep -c -w), for the files listed
        "escaped": {j + "filters.py": 2, j + "tests.py": 2, j + "utils.py": 1},
        "safe": {j + "compiler.py": 1, j + "ext.py": 1, j + "filters.py": 6, j + "nodes.py": 3,
                 j + "runtime.py": 3, j + "utils.py": 4},
        "mapping": {j + "environment.py": 1, j + "filters.py": 4, j + "runtime.py": 2,
                    j + "tests.py": 2},
        "undefined": {j + "compiler.py": 10, j + "environment.py": 13, j + "ext.py": 1,
                      j + "filters.py": 11, j + "nodes.py": 1, j + "runtime.py": 24,
                      j + "tests.py": 2, j + "utils.py": 2},
        "ChainableUndefined": {j + "filters.py": 1, j + "runtime.py": 4},
    }
    quoted = {"escaped": {j + "tests.py": 1}, "mapping": {j + "tests.py": 1},
              "safe": {j + "filters.py": 1}}
    ranked = fm.rank_files(
        tracked=sources, frames=[], issue_files=[], names=fm.test_names(TEST_2244),
        issue_names=[t for t, w in terms.items() if w >= 2],
        definitions=[("src/jinja2/environment.py", 300, "class Environment:"),
                     ("src/jinja2/runtime.py", 970, "class ChainableUndefined(Undefined):")],
        counts=counts, term_weights=terms, quoted=quoted,
    )
    top_two = {c.path for c in ranked[:2]}
    assert top_two == {"src/jinja2/tests.py", "src/jinja2/runtime.py"}
    tests_py = next(c for c in ranked if c.path == "src/jinja2/tests.py")
    assert "escaped" in tests_py.terms and "mentions the issue's names" in tests_py.reasons


def test_a_module_named_tests_inside_the_package_is_source() -> None:
    tracked = {"src/jinja2/tests.py", "src/jinja2/runtime.py", "tests/test_api.py"}
    assert fm.path_problem("src/jinja2/tests.py", tracked) is None
    assert fm.is_test_path("src/jinja2/tests.py") is False
    assert fm.source_files(sorted(tracked)) == ["src/jinja2/runtime.py", "src/jinja2/tests.py"]
    files = {"src/jinja2/tests.py": 'def test_escaped(value):\n    return hasattr(value, "x")\n'}
    out = fm.apply_edits([FileEdit(path="src/jinja2/tests.py",
                                   old='    return hasattr(value, "x")\n',
                                   new='    return hasattr(value, "y")\n')], files, tracked)
    assert out.ok
    assert fm.regression_test_path(sorted(tracked), 2244) == "tests/test_issue_2244.py"


def test_the_survey_counts_terms_and_their_quoted_uses() -> None:
    script = fm.survey_script([], ["escaped", "bad name"], ["escaped"])
    assert "grep -c -w -I -F -e escaped -- '*.py'" in script
    assert "__CNT__=escaped" in script and "bad name" not in script
    assert "grep -c -I -E '[\"'\"'\"'\"']escaped[\"'\"'\"'\"']'" in script or \
        "__QCNT__=escaped" in script
    out = ExecResult(exit_code=0, stdout=(
        "__LS__=src/jinja2/tests.py\n__CNT__=escaped\tsrc/jinja2/tests.py:2\n"
        "__CNT__=escaped\tsrc/a:b.py:1\n__QCNT__=escaped\tsrc/jinja2/tests.py:1\n"
        "__CNT__=bad\tgarbage\n"
    ))
    survey = fm.parse_survey(out)
    assert survey.counts == {"escaped": {"src/jinja2/tests.py": 2, "src/a:b.py": 1}}
    assert survey.quoted == {"escaped": {"src/jinja2/tests.py": 1}}


def test_term_anchors_point_at_the_rarest_terms_first() -> None:
    content = "a = 1\nTESTS = {'escaped': test_escaped}\nb = undefined\nescaped = 2\n"
    anchors = fm.term_anchors(content, {"escaped": 3.0, "undefined": 1.0})
    assert list(anchors)[:2] == [2, 4] and anchors[3] < anchors[2]


# -- what the model is shown after a miss, and the format reminder ---------------------------

EXTRA = "def helper(x):\n    return x + 1\n\n\ndef other():\n    return 2\n"


async def test_a_file_the_model_edited_without_seeing_it_is_shown_next_time() -> None:
    world = World(files={**FILES, "widgets/extra.py": EXTRA})
    guess = GOOD.replace("FILE: widgets/core.py", "FILE: widgets/extra.py").replace(
        "    return text.split(',')[:-1]\n=", "    return x - 1\n=")
    result, _, llm = await run_fix([guess, GOOD], world)
    assert result.status is FixStatus.VERIFIED
    assert result.attempts[0].outcome == "invalid_edits"
    feedback = llm.prompts[1][-1]["content"]
    assert '<data kind="file" path="widgets/extra.py" lines="whole">' in feedback
    assert "as it really is" in feedback and "    return x + 1" in feedback
    assert "widgets/extra.py" in result.context_files


async def test_a_missed_block_in_a_big_file_shows_the_lines_it_aimed_at() -> None:
    body = "".join(f"def f{i}(x):\n    y = x + {i}\n    return y\n\n\n" for i in range(900))
    world = World(files={**FILES, "widgets/big.py": body})
    guess = (
        "SUMMARY: x\nFILE: widgets/big.py\n<<<<<<< SEARCH\ndef f700(x):\n    y = x + 701\n"
        "=======\ndef f700(x):\n    y = x + 0\n>>>>>>> REPLACE\n"
    )
    result, _, llm = await run_fix([guess, GOOD], world)
    feedback = llm.prompts[1][-1]["content"]
    shown = feedback.split('<data kind="file" path="widgets/big.py"')[1]
    assert "def f700(x):" in shown and "    y = x + 700" in shown
    assert "not shown" in shown  # windows, not the whole file
    assert result.status is FixStatus.VERIFIED


@pytest.mark.parametrize(
    "answer",
    [
        "".join(f"FILE: widgets/core.py\n<<<<<<< SEARCH\nx{i}\n=======\ny\n>>>>>>> REPLACE\n"
                for i in range(fm.MAX_EDITS + 5)),
        "I think the bug is in parse, it should keep the last item.",
    ],
    ids=["too-many-edits", "no-edit-block"],
)
async def test_too_many_edits_or_none_get_the_format_the_cap_and_the_smallest_change(
    answer: str,
) -> None:
    result, _, llm = await run_fix([answer, GOOD])
    assert result.status is FixStatus.VERIFIED
    feedback = llm.prompts[1][-1]["content"]
    assert f"at most {fm.MAX_EDITS} edits and {fm.MAX_CHANGED_LINES} changed lines" in feedback
    assert "the smallest change that fixes the root cause" in feedback
    assert "<<<<<<< SEARCH" in feedback and ">>>>>>> REPLACE" in feedback
    assert result.attempts[0].answer.startswith(answer[:40])


async def test_the_one_request_for_code_may_come_after_a_failed_attempt() -> None:
    world = World(files={**FILES, "widgets/extra.py": EXTRA})
    result, _, llm = await run_fix([WRONG, "NEED: widgets/extra.py helper", GOOD], world)
    assert [a.outcome for a in result.attempts] == ["repro_still_fails", "verified"]
    assert '<data kind="file" path="widgets/extra.py"' in llm.prompts[2][-1]["content"]


async def test_an_open_pull_request_for_the_issue_is_a_warning_up_front() -> None:
    from fixedyet.models import IssueStatus, LinkedPullRequest

    a = assessment().model_copy(update={"issue_status": IssueStatus(linked_prs=[
        LinkedPullRequest(number=77, url="https://github.com/acme/widgets/pull/77", state="open",
                          draft=True, closes=True)])})
    events: list[ProgressEvent] = []
    await run_fix([GOOD], a=a, events=events)
    assert events[0].stage == "fix"
    assert events[0].message.startswith(
        "warning: open pull request(s) already linked to this issue: #77 (draft)")
    quiet: list[ProgressEvent] = []
    await run_fix([GOOD], events=quiet)
    assert not any("pull request" in e.message for e in quiet[:1])
