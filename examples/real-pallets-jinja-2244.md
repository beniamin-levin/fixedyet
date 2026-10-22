# Open bug: fails at the reported version and still fails on the fix side

**Issue:** [ChainableUndefined causes `is escaped` test to pass for `undefined` child elements](https://github.com/pallets/jinja/issues/2244)  
**Verdict:** `open_bug`  
**Confidence:** high

Basis:
- both runs agree: fail on the reported side
- both runs agree: fail on the fix side
- the same kind of failure on both sides (AssertionError)
- no run errored

## Results

| Side | Run 1 | Run 2 |
|---|---|---|
| Reported version (`3.1.6`) | FAIL | FAIL |
| Fix side (`main`) | FAIL | FAIL |

## Reproduction test (`tests/test_fixedyet_repro.py`)

```python
import inspect
import pytest
from jinja2 import Environment, ChainableUndefined

@pytest.fixture
def environment():
    return Environment(
        undefined=ChainableUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=True,
    )

def test_compatibility_escaped(environment):
    template_string = inspect.cleandoc("""
        {% macro card(params = {}) %}
            {#- Support description as string (with deprecated options) #}
            {%- set description = params.description if params.description is mapping else {
                "text": params.description if params.description is string else undefined,
                "html": params.description if params.description is escaped else params.descriptionHtml
            } -%}

          {{- description.html | safe if description.html else description.text }}
        {% endmacro %}
    """)

    text = inspect.cleandoc("""
        {{ card({
            "description": "Example description"
        }) }}
    """)

    text_safe = inspect.cleandoc("""
        {{ card({
            "description": "<p>Example description</p>" | safe
        }) }}
    """)

    text_nested = inspect.cleandoc("""
        {{ card({
            "description": {
                "text": "Example description"
            }
        }) }}
    """)

    html_nested = inspect.cleandoc("""
        {{ card({
            "description": {
                "html": "<p>Example description</p>"
            }
        }) }}
    """)

    html_deprecated = inspect.cleandoc("""
        {{ card({
          "descriptionHtml": "<p>Example description</p>"
        }) }}
    """)

    assert (
        environment.from_string(f"{template_string}\n{text}").render()
        == "Example description\n"
    )

    assert (
        environment.from_string(f"{template_string}\n{text_safe}").render()
        == "<p>Example description</p>\n"
    )

    assert (
        environment.from_string(f"{template_string}\n{text_nested}").render()
        == "Example description\n"
    )

    assert (
        environment.from_string(f"{template_string}\n{html_nested}").render()
        == "<p>Example description</p>\n"
    )

    assert (
        environment.from_string(f"{template_string}\n{html_deprecated}").render()
        == "<p>Example description</p>\n"
    )
```

## Output at the reported version (end of the log)

```
            }) }}
        """)
    
        html_deprecated = inspect.cleandoc("""
            {{ card({
              "descriptionHtml": "<p>Example description</p>"
            }) }}
        """)
    
        assert (
            environment.from_string(f"{template_string}\n{text}").render()
            == "Example description\n"
        )
    
        assert (
            environment.from_string(f"{template_string}\n{text_safe}").render()
            == "<p>Example description</p>\n"
        )
    
        assert (
            environment.from_string(f"{template_string}\n{text_nested}").render()
            == "Example description\n"
        )
    
        assert (
            environment.from_string(f"{template_string}\n{html_nested}").render()
            == "<p>Example description</p>\n"
        )
    
>       assert (
            environment.from_string(f"{template_string}\n{html_deprecated}").render()
            == "<p>Example description</p>\n"
        )
E       AssertionError: assert '\n' == '<p>Example description</p>\n'
E         
E         Strings contain only whitespace, escaping them using repr()
E         - '<p>Example description</p>\n'
E         + '\n'

tests/test_fixedyet_repro.py:81: AssertionError
=========================== short test summary info ============================
FAILED tests/test_fixedyet_repro.py::test_compatibility_escaped - AssertionEr...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.20s
```

## Output on the fix side (end of the log)

```
            }) }}
        """)
    
        html_deprecated = inspect.cleandoc("""
            {{ card({
              "descriptionHtml": "<p>Example description</p>"
            }) }}
        """)
    
        assert (
            environment.from_string(f"{template_string}\n{text}").render()
            == "Example description\n"
        )
    
        assert (
            environment.from_string(f"{template_string}\n{text_safe}").render()
            == "<p>Example description</p>\n"
        )
    
        assert (
            environment.from_string(f"{template_string}\n{text_nested}").render()
            == "Example description\n"
        )
    
        assert (
            environment.from_string(f"{template_string}\n{html_nested}").render()
            == "<p>Example description</p>\n"
        )
    
>       assert (
            environment.from_string(f"{template_string}\n{html_deprecated}").render()
            == "<p>Example description</p>\n"
        )
E       AssertionError: assert '\n' == '<p>Example description</p>\n'
E         
E         Strings contain only whitespace, escaping them using repr()
E         - '<p>Example description</p>\n'
E         + '\n'

tests/test_fixedyet_repro.py:81: AssertionError
=========================== short test summary info ============================
FAILED tests/test_fixedyet_repro.py::test_compatibility_escaped - AssertionEr...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.10s
```

## Evidence

- **log**: Key error at the reported version - `AssertionError: assert '\n' == '<p>Example description</p>\n'`
- **upstream**: [Issues · pallets/jinja · GitHub](https://github.com/pallets/jinja/issues) - github.com: Clear filter Search ## Issues Open 80 (80) Closed Comfortable display density Compact display density ### i18n: Allow extracting strings from trans environment using new-style formatting pallets/ji...
- **upstream**: [API — Jinja Documentation (3.1.x)](https://jinja.palletsprojects.com/en/stable/api) - jinja.palletsprojects.com: Undefined._fail_with_undefined_error() - ChainableUndefined - DebugUndefined - StrictUndefined - make_logging_undefined() + The Context - Context Context.parent Context.vars Context.environment Con...
- **upstream**: [NVD-CVE-2024-56326](https://nvd.nist.gov/vuln/detail/CVE-2024-56326) - nvd.nist.gov: Denotes Vulnerable Software Are we missing a CPE here? Please let us know. ### Affected Products Source: GitHub, Inc.(hide) | Vendor | Product | Versions | --- | pallets | jinja | Affected 3.1.5 | ...
- **log**: How the test was written - `turn 1: list_dir / | turn 2: list_dir /work/repo | turn 3: list_dir /work/repo/src | turn 4: read_file /work/repo/tests/conftest.py:1-200 | turn 5: read_file /work/repo/tests/conftest.py:1-50 | turn 6: read_file /work/repo/src/jinja2/__init__.py:1-20 | turn 7: read_file /work/repo/src/jinja2/environment.py:1-30 | turn 8: run_test -> FAILED | turn 9: run_test -> FAILED | turn 10: submit | submitted: The test reproduces the bug where ChainableUndefined causes the 'is escaped' test to return True for undefined values. In the html_deprecated case, params.description is undefined, but params.description is escaped evaluates to True, leading to the wrong branch being taken and an empty string being rendered instead of the expected HTML. The first four assertions pass; the last fails, demonstrating the incorrect behavior.`

## Root cause

The test fails on the final assertion where `html_deprecated` expects rendered HTML but gets only a newline. This occurs because `params.description is escaped` evaluates to False for an undefined value when using ChainableUndefined, causing the template to fall back to `params.descriptionHtml` which is not provided, leaving `description.html` undefined and thus the conditional `description.html | safe if description.html else description.text` renders nothing. The issue stems from ChainableUndefined not properly overriding the `__ironic__` or `__bool__` behavior for the `is escaped` test, leading to incorrect truthiness in conditional expressions.

_Written by the model from the issue, the test and the logs. The sandbox runs did not check it._

## Next actions

1. Inspect the Jinja2 source code for how ChainableUndefined handles the `is escaped` operator and comparison with undefined values.
2. Add a test case that directly checks the behavior of `undefined is escaped` under ChainableUndefined to confirm it returns False when it should return True.
3. Consider modifying ChainableUndefined to ensure that `is escaped` returns True for undefined values, aligning with the expected behavior for safe filtering in templates.
4. Verify if the issue is specific to certain Jinja2 versions and check whether a patch or workaround exists in upstream releases.

## Fix

**Status:** `verified`  
**Result:** attempt 1 of 3: the reproduction test fails on main before the change and passes after it, twice each; the project's suite has no newly failing tests (911 passed before, 911 passed after)  
**Verified on:** `main` at `5ef70112a1ff`  
**Code shown to the model:** `src/jinja2/runtime.py`, `src/jinja2/tests.py`, `src/jinja2/environment.py`, `src/jinja2/filters.py`  
**Regression test in the pull request:** `tests/test_issue_2244.py`

**Change:** Fix test_escaped to return False for undefined values

**The model's account** (not checked by the sandbox): The test_escaped function incorrectly returns True for ChainableUndefined instances because they have an __html__ method. By adding a check for undefined values, we ensure that undefined values are not considered escaped, which aligns with the expected behavior.

| Reproduction test on `main` | Run 1 | Run 2 |
|---|---|---|
| Without the change | FAIL | FAIL |
| With the change | PASS | PASS |

| Project test suite | Without the change | With the change |
|---|---|---|
| Passed | 911 | 911 |
| Failed | 0 | 0 |
| Errors | 0 | 0 |
| Skipped | 0 | 0 |
| Newly failing tests | - | none |

| Attempt | Outcome | Changed lines | Model cost | Detail |
|---|---|---|---|---|
| 1 | `verified` | 2 | $0.0063 | - |

Verified diff:

```diff
diff --git a/src/jinja2/tests.py b/src/jinja2/tests.py
index 1a59e37..b334372 100644
--- a/src/jinja2/tests.py
+++ b/src/jinja2/tests.py
@@ -202,7 +202,7 @@ def test_iterable(value: t.Any) -> bool:
 
 def test_escaped(value: t.Any) -> bool:
     """Check if the value is escaped."""
-    return hasattr(value, "__html__")
+    return not isinstance(value, Undefined) and hasattr(value, "__html__")
 
 
 def test_in(value: t.Any, seq: t.Container[t.Any]) -> bool:
```

_1 attempt(s), 1 model calls, 8 sandbox runs, 134 s wall time, $0.0063 model cost (cap $0.25)._

## Usage

12 LLM calls, 63,873 tokens (56,410 in, 7,463 out), $0.0228, 12 sandbox runs, 1 search credits, 135 s wall time, model nvidia/nemotron-3-super-120b-a12b

## Rerun this yourself

Each side is a fresh clone at the commit it ran on, installed in editable mode with pytest, with the reproduction test above saved as `tests/test_fixedyet_repro.py`.

Reported version (`3.1.6`): The commit was not recorded, so this is the ref as it is now.

```sh
git clone https://github.com/pallets/jinja.git fixedyet-rerun && cd fixedyet-rerun
git checkout 3.1.6
python -m pip install -e '.[test]' || python -m pip install -e .
python -m pip install pytest
# save the reproduction test as tests/test_fixedyet_repro.py, then:
python -m pytest -x -q -p no:cacheprovider tests/test_fixedyet_repro.py
```

Fix side (`main`): The commit was not recorded, so this is the ref as it is now.

```sh
git clone https://github.com/pallets/jinja.git fixedyet-rerun && cd fixedyet-rerun
git checkout main
python -m pip install -e '.[test]' || python -m pip install -e .
python -m pip install pytest
# save the reproduction test as tests/test_fixedyet_repro.py, then:
python -m pytest -x -q -p no:cacheprovider tests/test_fixedyet_repro.py
```

Or repeat the whole assessment: `fixedyet run https://github.com/pallets/jinja/issues/2244`
