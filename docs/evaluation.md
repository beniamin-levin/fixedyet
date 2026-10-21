# Evaluation

This page says how FixedYet is measured on SWE-bench, what counts as a success, what the numbers cannot tell you, and how to rerun them. The results section at the end is the measured run, copied from `eval/results/`.

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

SWE-bench's own environments pin dependency versions. The sandbox builds a plain editable install plus the project's own test requirements instead, so an instance can fail to build where SWE-bench's image would have worked. That shows up as a setup failure, which is the point of reporting it as its own metric. Two instances hit this in the first development runs, and both were harness bugs that are now fixed (details under Results): flask-5014, where a file of minimum versions (`requirements/tests-pallets-min.txt`) was installed as if it were the test requirements, and requests-5414, where the Flask<2 pins in requests' test requirements met a new MarkupSafe and pytest could not start. All 15 instances build in the final runs.

## Running it

```sh
# Pilot: three instances, a few cents
.venv/bin/python -m fixedyet.eval.run --limit 3 --budget-usd 2

# The full set of 15 on a local Docker daemon, two at a time, stopping at $8 cumulative
FIXEDYET_SANDBOX=docker .venv/bin/fixedyet eval --dataset verified \
    --ids-file eval/instances.txt --concurrency 2 --instance-timeout-s 1800 \
    --budget-usd 8 --out eval/results

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
- **Environment gap.** See above: plain editable installs against SWE-bench's pinned images. The fix side reinstalls the project after the gold patch is applied, and a reinstall can change dependency versions, so the two sides are only guaranteed to share one environment when the baseline already satisfies what the project declares. That is how the flask-5014 bug below showed up: the base side errored while the fix side ran.
- **Local Docker, not the demo's sandbox.** The numbers were measured on a local Docker daemon. The live demo runs on Nebius microVMs, with the same scripts but a different base image and different wall times.

## Results

Measured on 2026-10-21. Two complete runs of the 15 instances on the same code (A and B), preceded by three development runs that found harness bugs and are listed at the end. The tables below put the two final runs side by side. Every number comes from the files the runner wrote (`eval/results/20261021T114507Z` for A, `eval/results/20261021T115553Z` for B); nothing was edited by hand.

### Run

| | |
|---|---|
| Date | 2026-10-21. Run A started 11:45 UTC, run B 11:55 UTC |
| FixedYet commit | `348a63f` |
| Model | `nvidia/nemotron-3-super-120b-a12b` writes the test; `NVIDIA-Nemotron-3-Nano-30B-A3B` extracts the issue contract. Both through Nebius Token Factory |
| Sandbox backend | **Local Docker** (`FIXEDYET_SANDBOX=docker`; Colima on an Apple-silicon Mac, linux/arm64 `python:3.9` and `python:3.11` images). Not the Nebius microVM sandbox the live demo uses |
| Dataset and ids | SWE-bench Verified, the 15 ids of `eval/instances.txt` |
| Settings | concurrency 2, 1800 s per-instance timeout, $8 budget (never close: each run cost under $0.50) |
| Results files | `eval/results/20261021T114507Z.{md,json}` (A), `eval/results/20261021T115553Z.{md,json}` (B) |
| Runs completed | 15 of 15 in both, no harness errors, no timeouts |

### Metrics

| Metric | Run A | Run B |
|---|---|---|
| Reproduction rate (`fixed_on_main`) | **12/15 (80.0%)** | **10/15 (66.7%)** |
| of which high confidence | 12/15 (80.0%) | 10/15 (66.7%) |
| Wrong open-bug rate (`open_bug`) | 3/15 (20.0%) | 4/15 (26.7%) |
| Not reproduced rate | 0/15 (0.0%) | 0/15 (0.0%) |
| Inconsistent rate | 0/15 (0.0%) | 0/15 (0.0%) |
| Setup failure rate (`unsupported`) | 0/15 (0.0%) | 1/15 (6.7%) |
| Needs-info rate | 0/15 (0.0%) | 0/15 (0.0%) |
| Harness error rate | 0/15 (0.0%) | 0/15 (0.0%) |

With 15 instances the 95% Wilson interval is 55% to 93% around run A and 42% to 85% around run B. The two runs differ by two instances, which is the model's run-to-run noise: 10 instances were reproduced in both runs, 2 in one run only (pylint-4604 in A, requests-2317 in A), and 3 in neither.

The one setup failure in run B is not an environment problem: the model wrote a test that names a class that does not exist, so pytest errored at collection on both sides (pylint-4604, see the table below). In the final runs no environment failed to build.

| Measure | Run A | Run B |
|---|---|---|
| Total cost | $0.4708 | $0.4603 |
| Mean cost per issue | $0.0314 | $0.0307 |
| Median cost per issue | $0.0245 | $0.0216 |
| Mean wall time per issue | 1m24s | 1m33s |
| Median wall time per issue | 1m10s | 1m25s |
| Whole run, concurrency 2 | 10m46s | 12m09s |
| Tavily credits | 0 | 0 |

Pooled over the 30 instance runs: mean $0.031, median $0.024, most expensive $0.076; mean wall time 1m29s, slowest 4m42s. Cost is Token Factory tokens at list price (on average about 90,000 input and 5,000 output tokens per issue, in 15 model calls). Wall time includes building the environment from scratch every time, on a laptop, two instances at a time.

### Per instance

Verdicts per run, cost and time as A / B. A reproduced instance (`fixed_on_main`) has no failure cause.

| Instance | Run A | Run B | Cost A / B | Time A / B | Failure cause |
|---|---|---|---|---|---|
| psf__requests-1921 | fixed_on_main | fixed_on_main | $0.023 / $0.034 | 58s / 1m11s | |
| pytest-dev__pytest-6202 | fixed_on_main | fixed_on_main | $0.016 / $0.028 | 56s / 1m32s | |
| pylint-dev__pylint-4604 | fixed_on_main | unsupported | $0.050 / $0.069 | 1m49s / 1m33s | B: the test names `variables.UnusedImportChecker`, which does not exist, so pytest errors at collection on both sides |
| pallets__flask-5014 | fixed_on_main | fixed_on_main | $0.016 / $0.009 | 36s / 33s | |
| pytest-dev__pytest-7432 | open_bug | open_bug | $0.016 / $0.011 | 43s / 43s | A and B: the test expects the skip line to name `test_skip_location.py`, but `testdir` names the file after the test function (`test_skip_location_with_runxfail.py`), so the match fails with the gold patch too. The base side did show the bug (`skipping.py` in the report), the test just could not tell the sides apart |
| psf__requests-2317 | fixed_on_main | open_bug | $0.037 / $0.024 | 2m08s / 1m28s | B: tests `requests.compat.builtin_str` directly; the gold patch fixes `Session.request`, so the test fails on both sides |
| pylint-dev__pylint-4970 | open_bug | open_bug | $0.062 / $0.069 | 1m20s / 2m33s | A: passes a `CollectingReporter` to `Run`, which raises `TypeError: 'NoneType' object is not callable` on both sides. B: sets `similarities.min-similarity-lines`, an option that does not exist (`optparse.OptionError`). Neither test reached the bug |
| pytest-dev__pytest-7571 | fixed_on_main | fixed_on_main | $0.010 / $0.022 | 31s / 59s | |
| psf__requests-2931 | fixed_on_main | fixed_on_main | $0.059 / $0.015 | 1m40s / 1m21s | |
| pylint-dev__pylint-6386 | fixed_on_main | fixed_on_main | $0.046 / $0.076 | 1m24s / 2m25s | |
| pytest-dev__pytest-7982 | fixed_on_main | fixed_on_main | $0.025 / $0.043 | 1m16s / 3m36s | |
| pylint-dev__pylint-6528 | open_bug | open_bug | $0.055 / $0.013 | 4m42s / 1m39s | A and B: the test also demands that `.a/foo.py` is skipped with no ignore option at all, as the issue says. The issue is wrong about the default (`ignore-patterns` is `^\.#`, not `^\.`) and the gold patch only fixes the three explicit options, so the test fails with the patch too |
| psf__requests-5414 | fixed_on_main | fixed_on_main | $0.022 / $0.019 | 1m10s / 1m18s | |
| pytest-dev__pytest-10051 | fixed_on_main | fixed_on_main | $0.025 / $0.020 | 47s / 56s | |
| pylint-dev__pylint-7277 | fixed_on_main | fixed_on_main | $0.008 / $0.008 | 1m08s / 1m25s | |

### Failures by cause

All 8 non-reproduced results (3 in run A, 5 in run B) have the same root: **the test the model wrote is wrong, or asks for more than the fix delivers**. None is an environment failure. The `open_bug` verdicts here are the "wrong open-bug" cases: the tool would have told a maintainer that a bug is still open when it is fixed.

Which one to fix first: the second and third rows. There the test dies on an exception raised by its own API use (`TypeError`, `OptionError`, a missing class), which is not the symptom the issue reports, so a rule can spot it and send the failure back to the agent for another attempt. The first row cannot be told apart from a real open bug by any rule; it is the limit of the method, and only a person reading the test can catch it.

| Cause | Instances | Note |
|---|---|---|
| Test asks for more than the fix does, or has a wrong expectation | pytest-7432 (A, B), pylint-6528 (A, B), requests-2317 (B) | The test fails on both sides, so the verdict is `open_bug`. This is the "wrong open-bug" rate. |
| Test misuses a project API | pylint-4970 (A, B) | A wrong reporter and a wrong option name. The test never reaches the bug. |
| Test uses an API that does not exist | pylint-4604 (B) | Reported as `unsupported`, which is why the setup failure count is 1 in run B. |

Over the five runs, pylint-4970 was never reproduced, and pytest-7432 and pylint-6528 were reproduced in some runs and not in others.

### Audit of reproduced tests

I read the 22 reproduced tests (12 in A, 10 in B) against their issues and the stored logs. **This was done by the evaluating agent, Claude, an AI. No person has audited them yet**, so treat it as a first pass. All 22 exercise the behaviour the issue describes. For 21 of them the stored base-side log shows the symptom the issue reports; for pylint-7277 (B) the log excerpt holds only pylint's help text, so I could not confirm from it which line failed. Caveats found:

- requests-2931 (A) calls the live `httpbin.org`. It passes with the patch because nothing raises, which needs network access, so it would be fragile outside the sandbox. B mocks the transport and is sound.
- requests-5414 (A) would pass if `requests.get` raised nothing at all. A request to `.example.com` cannot succeed, so it still discriminates in practice. B uses a close variant of the URL (`http://.例子.com`) that reaches the same code.
- pylint-7277 (A) calls `modify_sys_path()` directly instead of going through `runpy` as the issue does. It exercises the line the issue points at. B follows the issue.
- pytest-10051 (A, B), pytest-7571 (A, B) and requests-2931 (A) are the issue's own snippet with an assertion added, so for those the agent mostly had to transcribe.

### Live check on real issues

On 21 October 2026, ten real issues from ten pure-Python projects were run on the live demo (Docker backend on the 1 GB Oracle VM, Nemotron 3 Super). All ten were opened between July and October 2026. They were picked at random from recent bug reports, not from SWE-bench, and nobody had marked a fix for FixedYet to find. Each run took 1.5 to 5 minutes, and the ten together cost about $0.19 in model spend. The "Correct?" column is a judgement made afterwards against the issue thread and the project's history, by an AI (Claude), not by a person.

| Issue | Opened | Verdict | Correct? |
|---|---|---|---|
| [pallets/werkzeug#3312](https://github.com/pallets/werkzeug/issues/3312) | 2026-09-29 | `fixed_on_main` | yes, closed as fixed |
| [pypa/packaging#1315](https://github.com/pypa/packaging/issues/1315) | 2026-07-06 | `fixed_on_main` | yes, fixing commit `0a85b41` found |
| [PyCQA/isort#2723](https://github.com/PyCQA/isort/issues/2723) | 2026-10-13 | `fixed_on_main` | yes, fixing commit `986b53f` found |
| [r1chardj0n3s/parse#262](https://github.com/r1chardj0n3s/parse/issues/262) | 2026-10-20 | `fixed_on_main` | yes, fixing commit `4f76a3e` found |
| [marshmallow-code/marshmallow#3072](https://github.com/marshmallow-code/marshmallow/issues/3072) | 2026-10-11 | `open_bug` | yes, still open (an unmerged PR exists) |
| [dateutil/dateutil#1588](https://github.com/dateutil/dateutil/issues/1588) | 2026-09-30 | `open_bug` | yes, still open (an unmerged PR exists) |
| [pytest-dev/pytest-mock#639](https://github.com/pytest-dev/pytest-mock/issues/639) | 2026-10-17 | `open_bug` | **no**: fixed the day it was opened; the model's test failed on both sides |
| [pyparsing/pyparsing#693](https://github.com/pyparsing/pyparsing/issues/693) | 2026-10-13 | `open_bug` | **no as evidence**: the issue is open, but the test compares a `ParseResults` with a list, which is never equal, so the verdict rests on a broken test |
| [theskumar/python-dotenv#697](https://github.com/theskumar/python-dotenv/issues/697) | 2026-09-08 | `unsupported` at first; `fixed_on_main` after the fix below | yes after the fix: fixing commit `e0310e5`, released in 1.2.4 |
| [pallets/jinja#2244](https://github.com/pallets/jinja/issues/2244) | 2026-08-27 | `unsupported` at first; `open_bug` after the fix below | yes after the fix: still open (an unmerged PR exists) |

**First pass: 6 correct, 2 wrong, 2 without a verdict.** The two `unsupported` runs were environment failures, not model failures. python-dotenv's tests need its `cli` extra (click), which was never installed. jinja ships a requirements file for Python 3.7 that downgraded `trio` below what Python 3.13 can import. Both were fixed in general terms (`6a5d38d`, `a952ec1`: a project's own extras, other Pythons' requirements files skipped, a conftest that cannot load repaired from the project's own declared requirements), and both issues were then rerun on the demo. **After those fixes: 8 correct and 2 wrong out of 10.** Both wrong verdicts are `open_bug` from a wrong model-written test, the same failure mode as the SWE-bench misses.

Two of the open bugs went on to [fix mode](../README.md#fix-mode-open-bugs-to-verified-draft-prs), and jinja#2244 was verified on the demo server. One fixed-on-main report went on to a backport: pydantic#13991 was backported to `v2.14-fixes` and opened upstream as [pydantic/pydantic#14043](https://github.com/pydantic/pydantic/pull/14043).

### Contamination caveat

Nemotron may have seen these public issues, their fixing pull requests and SWE-bench itself during training. Several of the reproduced tests are the issue's own snippet (pytest-10051, pytest-7571), which any model can transcribe whether or not it remembers the fix. These 22 reproductions therefore partly measure recall and snippet-copying, not the ability to reproduce a bug nobody has fixed yet. The live check on real issues above is the counterweight: ten recent issues, none from SWE-bench, gave 8 correct verdicts out of 10 after the environment fixes, with the same failure mode (a wrong test, read as an open bug).

### Development runs

One bug found by reading the runner and three earlier full runs found harness bugs, each fixed in a commit before the final runs. The three full runs are kept in `eval/results/` and are not the published numbers.

| Run | Reproduced | What it found | Fixed in |
|---|---|---|---|
| Before the dry run (found by reading the runner) | | The eval runner computed SWE-bench's Python version for each release but never passed it to the build, so the environment used the model's guess. The dry run on requests-1921 then reproduced the bug in 70 s for $0.013 | `4d6d9e7` |
| `20261021T110000Z` | 9/15 | A pytest that dies on a plugin import exits with code 1, like a failed test, so requests-5414 was counted as `open_bug`. Underneath, the Flask<2 pins in requests' test requirements met MarkupSafe 3 (`soft_unicode` import error) | `4d6d9e7`: exit 1 without a pytest summary line is an error; `markupsafe<2.1` is pinned when pytest cannot start for that reason |
| `20261021T111954Z`, `20261021T113010Z` | 11/15, 11/15 | flask-5014 was a setup failure in both. The test-requirements glob installed `requirements/tests-pallets-min.txt` (Werkzeug==2.0.0), so the base side could not import Flask, while the fix side's reinstall upgraded Werkzeug again | `348a63f`: requirements files named min, minimum, lowest or oldest are not installed |

Total model spend for the whole exercise (dry run, five full runs, two single-instance probes): about $2.50 at list price, against the $8 cap.
