"""The agent's pytest plugin, the notes the agent adds to a failed run, and the symptom check."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from fixedyet import repro_plugin
from fixedyet.agent import (
    PLUGIN_MODULE,
    TEST_PATH,
    api_crash_note,
    failure_notes,
    missing_symptoms,
    plugin_files,
    type_mismatch_notes,
    write_repro_test,
)
from fixedyet.models import ReproContract


class Results:
    """A list-like container that, like pyparsing's ParseResults, never equals a list."""

    def __init__(self, items):
        self._items = list(items)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __repr__(self) -> str:
        return f"Results({self._items!r})"


def explain(left, right, op: str = "==") -> list[str] | None:
    return repro_plugin.pytest_assertrepr_compare(None, op, left, right)


# -- the plugin's hook -----------------------------------------------------------------------------


def test_unrelated_types_with_equal_values_say_so() -> None:
    lines = explain(Results([Results(["x"])]), [["x"]])
    assert lines is not None
    assert lines[0] == "Results([Results(['x'])]) == [['x']]"
    assert f"{repro_plugin.MARKER}: == compares a " in lines[1]
    assert ".Results with a list" in lines[1]
    assert "ARE equal" in lines[2]


def test_unrelated_types_with_other_values_suggest_what_to_check() -> None:
    lines = explain(Results(["x", "/* */"]), ["x"])
    assert lines is not None and "convert first" in lines[2]
    assert explain((1, 2), [1, 2]) is not None  # a tuple is not a list either


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ([1], [2]),  # same type: pytest's own diff is enough
        (1, 1.5),  # numbers compare across types
        (None, [1]),  # comparing with None is a real check
        (True, 2),  # bool is an int
        ({"a": 1}, {"a": 2}),
    ],
)
def test_related_types_get_no_note(left, right) -> None:
    assert explain(left, right) is None


def test_other_operators_and_odd_values_never_break() -> None:
    assert explain(Results([1]), [1], op="!=") is None

    class Exploding:
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

        def __len__(self) -> int:
            raise RuntimeError("no len")

        def __iter__(self):
            raise RuntimeError("no iter")

    lines = explain(Exploding(), [1])
    assert lines is not None and lines[0].startswith("<Exploding> == [1]")


def test_the_plugin_runs_inside_pytest(tmp_path: Path) -> None:
    """The real hook through a real pytest run, loaded the way the agent's command loads it."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    for dest, source in plugin_files().items():
        (plugins / Path(dest).name).write_text(source, encoding="utf-8")
    (tmp_path / "test_it.py").write_text(
        "class R:\n"
        "    def __init__(self, items): self.items = list(items)\n"
        "    def __len__(self): return len(self.items)\n"
        "    def __iter__(self): return iter(self.items)\n"
        "    def __getitem__(self, i): return self.items[i]\n"
        "    def __repr__(self): return f'R({self.items!r})'\n"
        "def test_it():\n"
        "    assert R([R(['x'])]) == [['x']]\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(plugins)}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "--tb=short", "-p", "no:cacheprovider",
         "-p", PLUGIN_MODULE, "test_it.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    notes = type_mismatch_notes(run.stdout)
    assert notes and "test_it.R with a list" in notes[0]
    assert any("ARE equal" in note for note in notes)


# -- notes for the model ---------------------------------------------------------------------------

OWN_LINE_CRASH = """\
F
=================================== FAILURES ===================================
____________________________________ test_x ____________________________________
tests/test_fixedyet_repro.py:7: in test_x
    run = Run(["--min-similarity-lines=0", "a.py"], reporter=CollectingReporter())
E   TypeError: __init__() got an unexpected keyword argument 'reporter'
=========================== short test summary info ============================
FAILED tests/test_fixedyet_repro.py::test_x - TypeError: __init__() got an unexpected k...
1 failed in 0.21s
"""
DEEP_CRASH = """\
tests/test_fixedyet_repro.py:7: in test_x
    widgets.parse(None)
src/widgets/core.py:12: in parse
    return text.items
E   AttributeError: 'NoneType' object has no attribute 'items'
1 failed in 0.1s
"""
OPTION_CRASH = """\
tests/test_fixedyet_repro.py:5: in test_x
    Run(["--similarities.min-similarity-lines=0"])
pylint/config/option_manager_mixin.py:250: in load_command_line_configuration
    raise optparse.OptionError("no such option: " + opt, opt)
E   optparse.OptionError: no such option: --similarities.min-similarity-lines
1 failed in 0.3s
"""


def test_a_crash_on_the_tests_own_line_is_flagged() -> None:
    note = api_crash_note(OWN_LINE_CRASH, thread="Setting min-similarity-lines to 0 fails")
    assert note is not None
    assert note.startswith(
        "NOTE: your test crashed on its own line (tests/test_fixedyet_repro.py:7)")
    assert "unexpected keyword argument 'reporter'" in note


def test_a_crash_inside_the_project_is_not_the_tests_fault() -> None:
    assert api_crash_note(DEEP_CRASH, thread="parse(None) crashes") is None


def test_an_unknown_option_is_flagged_wherever_it_is_raised() -> None:
    note = api_crash_note(OPTION_CRASH, thread="min-similarity-lines=0 should disable the check")
    assert note is not None and "An option your test passes may not exist" in note


def test_a_crash_the_issue_reports_is_left_alone() -> None:
    thread = "Run() got an unexpected keyword 'reporter'"
    assert api_crash_note(OWN_LINE_CRASH, thread=thread) is None
    thread = "pylint says no such option: --similarities.min-similarity-lines"
    assert api_crash_note(OPTION_CRASH, thread=thread) is None


def test_failure_notes_only_for_a_failed_run_with_a_summary() -> None:
    assert failure_notes(1, OWN_LINE_CRASH, "") is not None
    assert failure_notes(2, OWN_LINE_CRASH, "") is None
    assert failure_notes(1, "Traceback (most recent call last):\nE   boom", "") is None
    plain = OWN_LINE_CRASH.replace("TypeError: __init__() got an unexpected keyword argument "
                                   "'reporter'", "AssertionError: assert 1 == 2")
    assert failure_notes(1, plain, "") is None


def test_type_notes_are_collected_once_each() -> None:
    note = f"E       {repro_plugin.MARKER}: == compares a R with a list."
    output = f"{note}\n{note}\n1 failed in 0.1s\n"
    assert type_mismatch_notes(output) == [f"{repro_plugin.MARKER}: == compares a R with a list."]
    assert "unrelated types" in (failure_notes(1, output, "") or "")


@pytest.mark.parametrize(
    ("observed", "output", "missing"),
    [
        ("Raises TypeError: 'NoneType' object is not callable", OWN_LINE_CRASH,
         ['"NoneType object is not callable"']),
        ("Raises ValueError when the list is empty", OWN_LINE_CRASH, ["ValueError"]),
        ("TypeError: __init__() got an unexpected keyword argument", OWN_LINE_CRASH, []),
        ("the last item is missing from the result", OWN_LINE_CRASH, []),
        ("", OWN_LINE_CRASH, []),
        # a plain assert is printed without the word AssertionError (pytest-10051 in run C)
        ("AssertionError: [<LogRecord: ...>] == []", "E       assert [<LogRecord: x>] == []\n",
         []),
        ("raises AssertionError", "E       assert [1] == []\n1 failed in 0.1s\n", []),
    ],
)
def test_missing_symptoms(observed: str, output: str, missing: list[str]) -> None:
    assert missing_symptoms(observed, output) == missing


# -- the loop: a submit is sent back once ----------------------------------------------------------

FAILING_RUN = """\
F
tests/test_fixedyet_repro.py:4: in test_x
    assert parse("a,b,c") == ["a", "b", "c"]
E   AssertionError: assert ['a', 'b'] == ['a', 'b', 'c']
1 failed in 0.01s
"""


async def test_a_submit_whose_failure_lacks_the_reported_exception_is_sent_back_once(
    fake_llm, fake_sandbox, sample_issue
) -> None:
    from fixedyet.models import ExecResult

    fake_sandbox.on("pytest", ExecResult(exit_code=1, stdout=FAILING_RUN))
    contract = ReproContract(runnable=True, observed_behaviour="parse raises KeyError: 'c'")
    code = (
        "from widgets import parse\n\ndef test_x():\n    assert parse('a,b,c') == ['a', 'b', 'c']\n"
    )
    fake_llm.queue(
        fake_llm.tool("run_test", code=code),
        fake_llm.tool("submit", code=code, rationale="first"),
        fake_llm.tool("submit", code=code, rationale="the KeyError is caught inside parse"),
    )
    path, got, summary = await write_repro_test(
        fake_llm, fake_sandbox, "img", sample_issue, contract
    )
    assert (path, got) == (TEST_PATH, code)
    assert "submit returned once: the failure lacks KeyError" in summary
    assert "submitted: the KeyError is caught inside parse" in summary
    returned = [m for m in fake_llm.calls[-1]["messages"] if m.get("role") == "tool"][-1]
    assert returned["content"].startswith("submit returned (once): the issue reports KeyError")
    assert "AssertionError: assert ['a', 'b'] == ['a', 'b', 'c']" in returned["content"]


async def test_a_failure_with_the_reported_exception_is_accepted_at_once(
    fake_llm, fake_sandbox, sample_issue
) -> None:
    from fixedyet.models import ExecResult

    fake_sandbox.on("pytest", ExecResult(exit_code=1, stdout=FAILING_RUN))
    contract = ReproContract(runnable=True, observed_behaviour="AssertionError on the last item")
    code = (
        "from widgets import parse\n\ndef test_x():\n    assert parse('a,b,c') == ['a', 'b', 'c']\n"
    )
    fake_llm.queue(fake_llm.tool("run_test", code=code),
                   fake_llm.tool("submit", code=code, rationale="ok"))
    _, _, summary = await write_repro_test(fake_llm, fake_sandbox, "img", sample_issue, contract)
    assert "submit returned" not in summary and "submitted: ok" in summary
