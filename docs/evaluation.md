# Evaluation

This page says how FixedYet is measured on SWE-bench, what counts as a success, what the numbers cannot tell you, and how to rerun them. The results section at the end is filled in from `eval/results/` after a run.

## What is measured

Given a bug report, FixedYet writes a pytest test that should fail while the bug is present. It runs that test at the reported version and again on the fix side, and a rule-based verdict decides the outcome. SWE-bench supplies both sides for free, so the labels cost nothing.

| SWE-bench field | Role here |
|---|---|
| `problem_statement` | the issue text the agent reads. Nothing else about the issue is loaded: no hints, no comments |
| `repo`, `base_commit` | the reported version. The sandbox clones `repo` and checks out `base_commit` |
| `patch` (the gold fix) | the fix side. `base_commit` plus `patch` plays "main". Used only by the sandbox, to build the fix side after the agent has finished its test |
| `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS` | not used at all. The agent never sees them, and neither does the grading |
| `version` | picks the Python version, through `python_version_for` in `src/fixedyet/eval/swebench.py` |

The agent never sees the gold patch or SWE-bench's own tests. It sees the problem statement, the repository at the base commit, and whatever it reads from there.

## Success definition

An instance counts as **reproduced** when the verdict is `fixed_on_main`: the agent's test fails at `base_commit` and passes with the gold patch applied, in every run on both sides (two runs per side). The grading is the agent's own test, not SWE-bench's.

| Metric | Verdict | Meaning |
|---|---|---|
| Reproduction rate | `fixed_on_main` | the test fails at the base commit and passes with the patch |
| of which high confidence | `fixed_on_main`, confidence high | both runs agree on both sides |
| Wrong open-bug rate | `open_bug` | the test fails on both sides, so the tool would wrongly call the bug still open |
| Not reproduced rate | `not_reproduced` | the test passes at the base commit |
| Inconsistent rate | `inconsistent` | the test passes at base but fails with the patch, or runs disagree |
| Setup failure rate | `unsupported` | the environment could not be built, or the test errored |
| Needs-info rate | `needs_info` | the agent found no runnable reproduction in the issue |
| Harness error rate | none | the run crashed or timed out before it returned an Assessment |

All rates divide by the number of instances that ran. They add up to 100%. Cost per issue is `Assessment.usage.cost_usd` (Token Factory tokens at list price); Tavily credits are reported as a separate count because the pipeline records them as credits, not dollars. Wall time is measured by the runner around the whole `reproduce_instance` call.

A run that dies with an exception carrying a `spent` attribute (the pipeline's `BudgetExceeded` does) is charged that amount. Any other crash counts as zero cost, so cost per issue can be understated by the price of crashed runs.

## Instances

The shipped selection is `eval/instances.txt`: 15 instances of SWE-bench Verified. Verified is used because human reviewers screened its problem statements for under-specification and its tests for validity. `--dataset lite` works for any id that is in Lite.

Selection rule, in full at the top of the file:

1. Light, pure-Python repositories only: psf/requests, pallets/flask, pytest-dev/pytest, pylint-dev/pylint. No services, no compiled extension built from source.
2. The base commit installs with a plain `pip install -e .` on the Python version SWE-bench uses for that release (all within 3.9 to 3.12), with no pre-install command, apt package or pre-release.
3. The problem statement has a snippet or clear steps and a stated expected behaviour, and the bug can be shown without the network.
4. Older and newer mixed. The fixing pull requests run from 2014 to 2023.

| Instance | Repository | Release | Fix PR date | Python |
|---|---|---|---|---|
| psf__requests-1921 | psf/requests | 2.3 | 2014-02-14 | 3.9 |
| pytest-dev__pytest-6202 | pytest-dev/pytest | 5.2 | 2019-11-16 | 3.9 |
| pylint-dev__pylint-4604 | pylint-dev/pylint | 2.9 | 2021-06-22 | 3.9 |
| pallets__flask-5014 | pallets/flask | 2.3 | 2023-03-04 | 3.11 |
| pytest-dev__pytest-7432 | pytest-dev/pytest | 5.4 | 2020-06-29 | 3.9 |
| psf__requests-2317 | psf/requests | 2.4 | 2014-11-01 | 3.9 |
| pylint-dev__pylint-4970 | pylint-dev/pylint | 2.10 | 2021-09-05 | 3.9 |
| pytest-dev__pytest-7571 | pytest-dev/pytest | 6.0 | 2020-07-29 | 3.9 |
| psf__requests-2931 | psf/requests | 2.9 | 2015-12-16 | 3.9 |
| pylint-dev__pylint-6386 | pylint-dev/pylint | 2.14 | 2022-04-19 | 3.9 |
| pytest-dev__pytest-7982 | pytest-dev/pytest | 6.2 | 2020-10-31 | 3.9 |
| pylint-dev__pylint-6528 | pylint-dev/pylint | 2.14 | 2022-05-06 | 3.9 |
| psf__requests-5414 | psf/requests | 2.26 | 2020-04-05 | 3.9 |
| pytest-dev__pytest-10051 | pytest-dev/pytest | 7.2 | 2022-06-15 | 3.9 |
| pylint-dev__pylint-7277 | pylint-dev/pylint | 2.15 | 2022-08-08 | 3.9 |

The Python versions come from SWE-bench's harness constants (`MAP_REPO_VERSION_TO_SPECS_PY`, tag v4.1.0, the last release that kept the table in one file). Old requests releases need Python 3.9 because they import ABCs from `collections`, which Python 3.10 removed.

The ids are interleaved by repository and age, so `--limit N` on any prefix is still a mix.

SWE-bench's own environments pin dependency versions. The sandbox builds a plain editable install instead, so an instance can fail to build where SWE-bench's image would have worked. That shows up as a setup failure, which is the point of reporting it as its own metric. The instances most likely to hit it are the oldest requests releases (a bare `setup.py` that imports the package), pytest 5.x on Python 3.9, and flask-5014, whose `pyproject.toml` turns warnings into errors.

## Running it

```sh
# Pilot: three instances, about a dollar at most
.venv/bin/python -m fixedyet.eval.run --limit 3 --budget-usd 2

# The full set of 15, one at a time, stopping at $10 cumulative
.venv/bin/python -m fixedyet.eval.run --dataset verified \
    --ids-file eval/instances.txt --concurrency 1 --budget-usd 10 --out eval/results

# Any instances by id
.venv/bin/python -m fixedyet.eval.run --ids psf__requests-2931 pytest-dev__pytest-7571
```

`main(argv)` in `src/fixedyet/eval/run.py` is the same entry point behind `fixedyet eval`.

Needs: a filled-in `.env` (Token Factory key and sandbox access; see `.env.example`), or `FIXEDYET_SANDBOX=docker` with a local Docker daemon. The first run downloads the dataset from the Hugging Face datasets-server (free, no key) and caches it as JSON in `eval/cache/`, which is not committed.

| Flag | Default | Effect |
|---|---|---|
| `--dataset` | `verified` | `lite` or `verified` |
| `--ids-file` | `eval/instances.txt` | one id per line, `#` comments allowed |
| `--ids` | | ids on the command line, replaces `--ids-file` |
| `--limit N` | all | first N ids only |
| `--concurrency` | 1 | instances at a time, 1 or 2 |
| `--budget-usd` | 10 | no new instance starts once cumulative cost reaches this |
| `--instance-timeout-s` | 1800 | give up on one instance after this long |
| `--out` | `eval/results` | results directory |

Behaviour worth knowing:

- **Budget.** The check runs before each instance starts. Instances already running finish, so with concurrency 2 the total can pass the budget by up to two instances' cost. The summary says so when a run stopped early, and rates then cover only the instances that ran. The pipeline has its own per-run limit, `FIXEDYET_RUN_BUDGET_USD`.
- **Output.** `eval/results/<UTC timestamp>.json` holds every full Assessment, the wall time and the failure cause; `<UTC timestamp>.md` holds the metrics table and the per-instance table. Both are rewritten after each finished instance, so an interrupted run keeps its partial results (status `interrupted`, exit code 130).
- **Exit code.** 0 when at least one instance returned an Assessment, 1 when none did, 2 for bad input.
- **Variance.** The model is not deterministic. For a number to quote, run the full set at least twice and report both.

## Reading the numbers

- **Small sample.** With 15 instances, one instance is 6.7 points. The 95% Wilson interval around 8 of 15 (53%) runs from 30% to 75%; around 10 of 15 (67%) from 42% to 85%. Quote the count, not just the percentage.
- **`fixed_on_main` is necessary, not sufficient.** A test can fail at the base commit for the wrong reason and still pass once an unrelated line of the patch lands. The two-sided rule makes that unlikely, not impossible. Each reproduced test should be read by a person before the headline number is quoted, and the audit result recorded below.
- **The fix side is the gold patch.** In live mode the fix side is the current main branch, which has other changes too. Eval mode isolates the one fix.
- **Contamination.** Nemotron may have seen SWE-bench issues and their fixes during training. A high score on these instances partly measures recall. The live runs on open issues, which have no public fix yet, are the check against this, and the results below keep the two apart.
- **Environment gap.** See above: plain editable installs against SWE-bench's pinned images.

## Results

Fill this in from `eval/results/<stamp>.md` after the real run. Do not edit the numbers by hand; paste the table the runner wrote.

### Run

| | |
|---|---|
| Date | TBD |
| FixedYet commit | TBD |
| Model | TBD |
| Sandbox backend | TBD |
| Dataset and ids | TBD (verified, `eval/instances.txt`) |
| Results file | TBD (`eval/results/<stamp>.json`) |
| Runs completed | TBD |

### Metrics

| Metric | Count | Rate |
|---|---|---|
| Reproduction rate | TBD | TBD |
| of which high confidence | TBD | TBD |
| Wrong open-bug rate | TBD | TBD |
| Not reproduced rate | TBD | TBD |
| Inconsistent rate | TBD | TBD |
| Setup failure rate | TBD | TBD |
| Needs-info rate | TBD | TBD |
| Harness error rate | TBD | TBD |

| Measure | Value |
|---|---|
| Total cost | TBD |
| Mean cost per issue | TBD |
| Median cost per issue | TBD |
| Mean wall time per issue | TBD |
| Median wall time per issue | TBD |
| Tavily credits | TBD |

### Per instance

| Instance | Verdict | Confidence | Cost | Time | Failure cause |
|---|---|---|---|---|---|
| TBD | | | | | |

### Failures by cause

Group the failure causes from the table above and say which one to fix first.

| Cause | Instances | Note |
|---|---|---|
| TBD | | |

### Audit of reproduced tests

For each `fixed_on_main` instance: did a person read the test and agree it exercises the reported bug? TBD.

### Live check on open issues

Three to five open issues, reports kept local. This is the check against contamination. TBD.
