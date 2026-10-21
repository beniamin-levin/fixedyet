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
        ({"pkg/a.py": "", "pkg/tests/test_a.py": ""}, None),  # only top-level directories count
        ({"tests/test_issue_42.py": "", "pkg/a.py": ""}, "tests/test_issue_42_2.py"),
    ],
)
def test_the_regression_test_goes_into_the_existing_test_directory(
    files: dict[str, str], expected: str | None
) -> None:
    assert fm.regression_test_path(files, 42) == expected


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
        if "__DIFF_B64__" in command:
            self._record("apply", image, **meta)
            edited = {k.removeprefix("/work/repo/"): v for k, v in (files or {}).items()}
            diff = "".join(git_diff(p, w.files[p], t) for p, t in sorted(edited.items()))
            new_image = f"img-patched-{len(self.patched) + 1}"
            self.patched[new_image] = edited
            return ExecResult(exit_code=0, stdout=f"__DIFF_B64__={b64(diff)}\n", image=new_image)
        if "__RUN_START__" in command:
            if image == "img-base":
                self._record("repro-before", image, **meta)
                return w.before
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


class FakeLLM:
    """Answers in turn; each answer costs ``cost`` dollars."""

    def __init__(self, answers: list[str | BaseException], cost: float = 0.01) -> None:
        self.answers = list(answers)
        self.usage = Usage()
        self.cost = cost
        self.prompts: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], **_: Any) -> ChatResult:
        self.prompts.append([dict(m) for m in messages])
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        self.usage.llm_calls += 1
        self.usage.cost_usd += self.cost
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
    assert result.context_files == ["widgets/core.py", "widgets/__init__.py"]  # best first
    assert [a.outcome for a in result.attempts] == ["verified"]
    assert result.usage.llm_calls == 1 and result.usage.cost_usd == pytest.approx(0.01)
    assert result.usage.sandbox_runs == len(sandbox.calls) and result.usage.wall_seconds >= 0
    assert "attempt 1 of 3" in result.reason and "no newly failing" in result.reason
    assert sandbox.kinds() == [
        "build", "survey", "repro-before", "read", "apply", "repro-after", "suite-before",
        "suite-after",
    ]
    assert {e.stage for e in events} == {"fix", "environment"}


async def test_patched_code_runs_only_in_the_sandbox_and_without_network() -> None:
    _, sandbox, _ = await run_fix([GOOD])
    by_kind = {c["kind"]: c for c in sandbox.calls}
    for kind in ("survey", "read", "apply", "repro-before", "repro-after", "suite-before",
                 "suite-after"):
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
    assert llm.prompts == [] and "apply" not in sandbox.kinds()


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
    from fixedyet.report import render_fix_markdown

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
