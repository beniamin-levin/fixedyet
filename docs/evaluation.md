# Evaluation

This page says how FixedYet is measured on SWE-bench, what counts as a success, what the numbers cannot tell you, and how to rerun them. The results sections at the end are the measured runs, copied from `eval/results/`: the final agent of 22 October 2026 first, then rules v2 and the first measurement (rules v1), kept as history.

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
| Wrong open bug at high confidence | `open_bug`, confidence high | the wrong open bugs no rule flagged: the ones a maintainer would have trusted |
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

### Re-scoring saved results

`fixedyet rescore` runs the verdict rules again over results that are already on disk, with no model call and no sandbox run: the stored side runs go through `verdict.decide` again, with the issue thread (the cached SWE-bench problem statement, or the issue from GitHub) and the issue's GitHub status. It is how a change to the rules is measured before any money is spent, and how old reports on a server catch up with new rules.

```sh
# The saved eval runs and the saved live reports, with the labels of the live issues
fixedyet rescore eval/results/20261021T114507Z.json eval/results/20261021T115553Z.json \
    eval/live/20261021 --labels eval/live-labels.txt --md eval/results/rescore.md

# A server's runs directory: read only first, then in place
fixedyet rescore /var/lib/fixedyet/runs
fixedyet rescore /var/lib/fixedyet/runs --write
```

| Flag | Effect |
|---|---|
| `--write` | update each changed `assessment.json` and its `report.md`, atomically, keeping the file's mode and owner. The first verdict is kept in `Assessment.rescored`, and the basis ends with "re-scored by rules v2". A second run changes nothing. Eval results and saved web reports are never written |
| `--offline` | no GitHub and no dataset download. The rule about an exception the issue never names then keeps what the assessment says, and the issue status is the stored one |
| `--labels FILE` | `<issue URL or instance id> <open\|fixed\|bad_test>` per line: the truth for live issues, for the "wrong open bug" and "real open bugs kept" counts. Eval instances are `fixed` by construction |
| `--md FILE` | also write the summary |

The labels of the live issues are in `eval/live-labels.txt`; the reports they label, as the demo saved them on 2026-10-21, are in `eval/live/20261021/`.

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
- **A laptop's Docker, not the demo's server.** The SWE-bench numbers were measured on a local Docker daemon (Colima on an Apple-silicon Mac). The live demo uses the same Docker backend and the same scripts on a 1 GB Oracle Cloud VM, so its wall times differ. The Nebius Sandboxes backend is implemented but has never run against the live service: no number on this page comes from it.

## Results, the agent of 22 October 2026

**Three full runs of the 15 instances on the final agent (commit `e6f2f25`): 13, 12 and 11 reproduced** (87%, 80% and 73%; mean 12 of 15). 14 of the 15 instances were reproduced in at least one of the three runs; pylint-4970 never was. Same setup as before: SWE-bench Verified, local Docker (Colima on an Apple-silicon Mac), Nemotron 3 Super with Nemotron 3 Nano for intake, concurrency 2.

| Run | Results file | `fixed_on_main` | Wrong `open_bug` (high confidence) | `not_reproduced` | `inconsistent` | `unsupported` | Mean / median cost | Mean wall time | Total |
|---|---|---|---|---|---|---|---|---|---|
| F1 | `20261022T080111Z` | **13** | 0 (0) | 1 | 0 | 1 | $0.032 / $0.025 | 1m43s | $0.48 |
| F2 | `20261022T081434Z` | **12** | 1 (1) | 2 | 0 | 0 | $0.041 / $0.043 | 1m11s | $0.62 |
| F3 | `20261022T082425Z` | **11** | 2 (2) | 2 | 0 | 0 | $0.026 / $0.021 | 1m01s | $0.38 |

| Instance | F1 | F2 | F3 |
|---|---|---|---|
| requests-1921, pytest-6202, flask-5014, pytest-7432, pytest-7571, requests-2931, pylint-6386, requests-5414, pytest-10051 | reproduced | reproduced | reproduced |
| pylint-4604 | `unsupported`: the test imports `ImportChecker`, which does not exist | reproduced | reproduced |
| requests-2317 | reproduced | `open_bug`: the test asserts that a `bytes` method becomes `"GET"` on the prepared request, which the fix does not do | reproduced |
| pylint-4970 | `not_reproduced` | `not_reproduced` | `not_reproduced` |
| pytest-7982 | reproduced | reproduced | `not_reproduced`: the symlink layout of the test is collected at the base commit too |
| pylint-6528 | reproduced | `not_reproduced` | `open_bug`: the test asks for the default ignore of `.a` that the issue wrongly assumes |
| pylint-7277 | reproduced | reproduced | `open_bug`: parametrized over three placeholders, one of which the fix does not handle |

What changed in the agent since run C, each from a miss in these runs or in a showcase (the commits are on `main`, 22 October 2026):

| Change | The miss behind it |
|---|---|
| A submit whose last run passed, or stopped before its assertions, is sent back once; a test that imports nothing from the repository is sent back once | pylint-4604 (a test that could not be collected), requests-5414 (a test of the `idna` package, not of requests) |
| A first test is due by two fifths of the turns; a `run_python` tool to look at real values before asserting, refused until a first test has run | pylint-4970 (its only test ran on turn 20 of 20); one `run_test` call in nine was a plain script, refused |
| The main model checks the small model's "not runnable" | requests-2317 (`needs_info` in run C) |
| At the turn limit, the last test that failed is used when the last run errored or passed | requests-2317 (turn 19 failed, turn 20 could not be collected) |
| Any request to a non-local host gets a note; the test goes through the public call when the issue quotes internal code | requests-2931 (a PUT to a real host), requests-2317 (a test of the `builtin_str` helper alone) |
| The test asserts every failing input the issue names | psf/black#5468 (see [fix mode](fix-mode.md)) |
| An `inconsistent` result gets one more, independent test, and that test's verdict is reported, whatever it is | most `inconsistent` results were tests with a wrong expected value (a log level of 30 where the issue says 0) |

Read the numbers with these caveats:

- **These 15 instances are also where the changes came from.** Each change is a general rule, not a special case for an instance, but the same 15 issues both motivated and measured them, so this is not a held-out score. The live check on ten recent issues (below) and the psf/black fix are the out-of-sample evidence.
- **The spread between runs is large.** On the same agent, runs differ by two instances (11 to 13); on the intermediate agents of the same day they ranged from 9 to 11. One instance is 6.7 points.
- **Wrong open bugs remain.** Three of the 45 instance runs ended in `open_bug` at high confidence (requests-2317 once, pylint-6528 once, pylint-7277 once): a test that asks for more than the fix delivers fails the same way on both sides, which no rule over two logs can tell from a real open bug.
- **The second test costs more.** An `inconsistent` first test adds a second agent run; the mean cost per issue stayed at about $0.03.

### Development runs of 22 October 2026

Every run is kept in `eval/results/`. These ran on intermediate commits that a later change replaced, and are not in the numbers above.

| Run | Commit | Reproduced | Why it was replaced |
|---|---|---|---|
| `20261022T053836Z` | `bac67f8` | 10 of 15 | first agent changes; the misses showed the scripts sent as tests (requests-5414: six refusals) |
| `20261022T055757Z` | `198102a` (`run_python`) | 7 of the first 10, stopped | stopped to add the import check, the network note and the turn-limit fallback |
| `20261022T061444Z` | `1bdd64f` | 11 of 15, 3 `unsupported` | a regression of `run_python`: three instances ran scripts for all 20 turns and wrote no test (fixed in `5fd17d9`) |
| `20261022T062307Z` | `1bdd64f` | 6 of the first 8, stopped | the same regression |
| `20261022T062753Z` | `5fd17d9` | 11 of the first 14, stopped | stopped when psf/black#5468 showed the one-input test |
| `20261022T065113Z`, `070406Z`, `071715Z` | `ff21320` | 10, 9, 11 | requests-2317 tested the quoted helper, requests-2931 sent a request to example.com |
| `20261022T073005Z`, `073855Z`, `074708Z` | `703f4b1` | 10, 10, 10 | most misses were `inconsistent` tests with a wrong expected value |

Total model spend of the day's evaluation, development runs included: about $6.55 at list price.

### Model choice, 22 October 2026

A check of the model choice: two variants of the final agent, two full runs each on the same 15 instances, against F1 to F3 above. In the first, Super runs the agent with its reasoning off (`FIXEDYET_AGENT_THINKING=off`, commit `a6a9edf`; the root cause analysis keeps its reasoning). In the second, Nano is the main model (`FIXEDYET_MODEL=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`), so it runs the agent, the analysis and the check of a "not runnable" contract. The results files are in `eval/results/ablations/`.

| Main model | Runs | Reproduced | Instance runs reproduced (95% interval) | Instances reproduced in either of the first two runs | Wrong `open_bug` (high confidence) | `unsupported` | Mean cost per issue | Mean wall time | Output tokens per issue |
|---|---|---|---|---|---|---|---|---|---|
| Super, reasoning on (F1 to F3) | 3 | **13, 12, 11** | **36 of 45, 80%** (66-89%) | 14 | 3 (3) | 1 | $0.033 | 1m18s | 5,600 |
| Super, reasoning off (`20261022T102353Z`, `103148Z`) | 2 | 12, 9 | 21 of 30, 70% (52-83%) | 12 | 6 (4) | 0 | $0.034 | 1m05s | 3,100 |
| Nano (`20261022T102358Z`, `104226Z`) | 2 | 7, 7 | 14 of 30, 47% (30-64%) | 8 | 3 (0) | 8 | $0.011 | 3m01s | 19,300 |

- **Reasoning stays on.** Without it the agent wrote about half the output tokens, but the cost did not fall: input tokens are most of the bill (about 85 percent with reasoning, 92 without, since every call sends the conversation again), and the runs made a few more calls (252 a run against 237). The tests got worse: twice as many wrong `open_bug` verdicts (6 in 30 runs against 3 in 45), four of them at high confidence, all well-formed tests that asserted something the fix does not do. It saved about 13 seconds an issue.
- **Nano costs a third as much and reproduces about half as many.** 8 of its 30 runs ended `unsupported`, 7 of them with no test written in 20 turns: the agent kept searching the code despite the nudge to run a first test early. It wrote about 19,000 output tokens per issue, most of them reasoning, and took over twice as long; its second run, alone on the Docker VM, was the slower of the two. Its wrong tests were broken ones (an `ImportError` or `NameError` on both sides), which rules v2 flags, so none of its `open_bug` verdicts was at high confidence. With Nano also checking the contract, requests-2317 was `needs_info` again in one run. It reproduced pylint-6528 in both runs, which Super did in one run of three.
- **So Nano stays on intake**, a structured extraction, and Super with reasoning runs the agent.

Two runs per variant is a small sample. The spread between runs of one agent is two instances, and the intervals for Super with and without reasoning overlap, so that gap is not conclusive on its own; the Nano gap is outside the spread. The reasoning-off runs and Nano's first run ran at the same time on one Docker VM (four sandboxes at once), and F1 to F3 one at a time, so wall times compare only roughly. Model spend for the four runs: $1.34.

## Results, rules v2

Measured on 2026-10-21 (UTC, evening), after the first measurement further down. Every wrong verdict of that first measurement, on SWE-bench and on the live issues, was an `open_bug` from a model-written test that failed on both sides for a reason other than the bug. Rules v2 (`verdict.RULES_VERSION`, commits `1aed881` to `8b6b8b1`) changes two things.

The verdict rules (no model, see [the decision matrix](architecture.md#decision-matrix-verdictpy)):

- **The failure text must match.** A fail/fail whose `E` lines differ between the sides is `inconsistent`, low. The lines come from the last run's whole log (`SideRun.failure_detail`), with memory addresses, durations, line numbers, `/tmp` and `pytest-of` paths and the `.dYYYYMMDD` local version suffix normalised; the basis quotes the first line that differs on each side.
- **An exception the issue never names** (not AssertionError, not pytest's `Failed`) keeps `open_bug` but at low confidence: it is more often the test's own misuse of the API than the bug.
- **The issue's state on GitHub.** An open bug whose issue was closed as completed, or that a merged pull request of the same repository closes or mentions, gets low confidence and a basis line naming the date and the pull request (GraphQL `closedByPullRequestsReferences` and the closing event when there is a token, the REST timeline without). Open linked pull requests are stored in the report and listed in it, and fix mode warns about them before it starts.

The test-writing agent is also told when its test fails for the wrong reason: a pytest plugin in its own runs flags `==` between unrelated types, a crash on the test's own line with "has no attribute" or "unexpected keyword argument" (or "no such option" anywhere) is flagged as an API that may not exist, and a first submit whose failure lacks the exception or message the issue reports is sent back once.

### Offline re-score of the saved runs

No model call: `fixedyet rescore` ran the v2 rules over the stored sandbox runs of the five SWE-bench runs and the fifteen reports the demo saved on 2026-10-21 (`eval/live/20261021/`, labelled in `eval/live-labels.txt`). The full output is `eval/results/rescore-20261021.md`.

| Saved results | Instances | `fixed_on_main` | `open_bug` | Wrong open bug at high confidence | Real open bugs kept |
|---|---|---|---|---|---|
| Run A (`20261021T114507Z`) | 15 | 12 (unchanged) | 3 → 2 | **3 → 1** | - |
| Run B (`20261021T115553Z`) | 15 | 10 (unchanged) | 4 → 3 | **4 → 2** | - |
| Development run `110000Z` | 15 | 9 | 4 → 3 | 4 → 2 | - |
| Development run `111954Z` | 15 | 11 | 2 | 2 | - |
| Development run `113010Z` | 15 | 11 | 2 | 2 → 1 | - |
| Live reports from the demo | 15 reports of 13 issues | 8 (unchanged) | 6 → 4 | **2 → 0** | 4 of 4 (3 at high confidence) |

What moved:

- pytest-7432 (A, B and a development run): `open_bug` → `inconsistent`. At the base commit the skip line names `src/_pytest/skipping.py`, the bug; with the gold patch it names the test file, under another name than the test expects. The failure changed, so the fix side does not show the same bug.
- pylint-4970 (A, B and a development run) and pylint-4604 (a development run): `open_bug` stays, at low confidence: the tests died on `TypeError`, `OptionError` and `AttributeError`, which the issues never name.
- pytest-mock#639: `open_bug` → `inconsistent`. The introspection the issue is about is gone on main; the test failed on both sides only because its own call can never pass.
- pyparsing#693: `open_bug` → `inconsistent`. On main the comment is no longer in the result (`ParseResults(['x'])`), and the test still fails because it compares a `ParseResults` with a list.
- click#2827: `open_bug` stays, at low confidence: the issue was closed as completed on 2026-05-16 by pull request #3391. That pull request added an opt-in capture mode; the default behaviour the issue reports is unchanged on main, so `open_bug` is right and the closing is worth the warning.
- marshmallow#3072, dateutil#1588 and jinja#2244, the real open bugs, keep `open_bug` at high confidence. Each now lists its open pull request.
- Unchanged: every `fixed_on_main`. The rules only read fail/fail rows.

What did not move, and cannot by rule: pylint-6528 (four of the five saved runs), requests-2317 (B and a development run) and pytest-6202 (a development run). Each fails the same way on both sides with an AssertionError. In pylint-6528 the issue itself is wrong about pylint's default ignore pattern and the test asks for what the issue asks; in requests-2317 the test checks `compat.builtin_str` directly while the gold patch fixes `Session.request`; in pytest-6202 the test expects a node id without its file path. Only a reader of the test can see these. The fourth miss of development run `110000Z`, requests-5414, was a harness bug fixed before run A (a pytest crash read as a failure).

### New live runs

Same setup as the first measurement: SWE-bench Verified, local Docker (Colima on an Apple-silicon Mac), Nemotron 3 Super with Nemotron 3 Nano for intake, concurrency 2. The seven-id runs used commit `bcf670e`, run C `b931b20` (the same code plus a re-score input). Three runs, in this order: the seven instances that ever came out `open_bug` (in any of the five saved runs), twice, and then the full fifteen once, to check that the new rules and notes did not cost reproductions.

| Run | Results file | Instances | `fixed_on_main` | Wrong `open_bug` | of which high confidence | `inconsistent` | Other | Cost |
|---|---|---|---|---|---|---|---|---|
| **C, the full set** | `20261021T210757Z` | 15 | **11 (73.3%)** | **0** | **0** | 1 | 1 `not_reproduced`, 1 `unsupported`, 1 `needs_info` | $0.41 |
| Seven ids, run 1 | `20261021T205338Z` | 7 | 2 | 3 | 2 | 1 | 1 `not_reproduced` | $0.37 |
| Seven ids, run 2 | `20261021T205948Z` | 7 | 4 | 1 | 1 | 1 | 1 `not_reproduced` | $0.20 |

Run C against the first measurement: 11 of 15 reproduced, between run A's 12 and run B's 10, so the reproduction rate did not drop (95% Wilson interval 48% to 89%). The wrong open bugs went from 3 and 4 to none. Mean cost per issue $0.027 (median $0.021), mean wall time 1m17s.

The seven instances across all five runs (verdict, confidence):

| Instance | v1, run A | v1, run B | v2, ids run 1 | v2, ids run 2 | v2, run C |
|---|---|---|---|---|---|
| pytest-7432 | **open_bug, high** | **open_bug, high** | inconsistent, low | inconsistent, low | inconsistent, low |
| pylint-4970 | **open_bug, high** | **open_bug, high** | open_bug, low | not_reproduced | not_reproduced |
| pylint-6528 | **open_bug, high** | **open_bug, high** | **open_bug, high** | **open_bug, high** | fixed_on_main |
| requests-5414 | fixed_on_main | fixed_on_main | fixed_on_main | fixed_on_main | fixed_on_main |
| pytest-6202 | fixed_on_main | fixed_on_main | fixed_on_main | fixed_on_main | fixed_on_main |
| pylint-4604 | fixed_on_main | unsupported | not_reproduced | fixed_on_main | unsupported |
| requests-2317 | fixed_on_main | **open_bug, high** | **open_bug, high** | fixed_on_main | needs_info |
| **Wrong open bug at high confidence** | 3 of 7 | 4 of 7 | 2 of 7 | 1 of 7 | 0 of 7 |
| Reproduced | 4 of 7 | 2 of 7 | 2 of 7 | 4 of 7 | 4 of 7 |

Read per instance:

- pytest-7432 is `inconsistent` in all three new runs, by the failure-text rule, every time for the reason above.
- pylint-4970 was caught once by the exception rule (`TypeError`, low confidence); in the other two runs the model wrote a test that passes at the base commit, so `not_reproduced`. It has not been reproduced in any of the eight runs.
- pylint-6528 and requests-2317 are the two wrong open bugs at high confidence that remain. They fail the same way on both sides with an assertion, which no rule can tell from a real open bug (see above). pylint-6528 was reproduced in run C, as in one of the development runs.
- pylint-4604 in run C is the same test as run B: it names `variables.UnusedImportChecker`, which does not exist, and pytest cannot collect it on either side. The agent was told so after its run (an error before any assertion) and submitted it anyway. In the other runs the model kept sending files without a test function (rejected before a run), which is not new.
- requests-2317 in run C is `needs_info`: Nemotron Nano read the issue as having no runnable reproduction, which it did not do in the other seven runs.

The agent's notes, as the transcripts show them: the type-mismatch or API note appeared in 2 of the 29 new instance runs (requests-2317 run 1, pylint-7277 run C), and the symptom check sent a test back 4 times (requests-5414 three times, pytest-10051 once). For requests-5414 the issue reports a `UnicodeError` that this environment does not raise (urllib3 raises `LocationParseError`); the model said so and resubmitted, and the instance was reproduced each time. For pytest-10051 the bounce was a false alarm: a failed plain `assert` does not print the word AssertionError. That was fixed after run C (`8b6b8b1`); the runs above were made before it.

Total model spend of the three runs and the four controls: $1.05 at list price, counted in the spend ledger.

### Controls on real open bugs

Run with `fixedyet run` on the local Docker backend at commit `bcf670e`, with a GitHub token (for the linked pull requests).

| Issue | What is true | Before (demo, rules v1) | Now (rules v2) | Cost |
|---|---|---|---|---|
| [pallets/jinja#2244](https://github.com/pallets/jinja/issues/2244) | open; open pull request #2259 | `open_bug`, high | `open_bug`, high; lists #2259 | $0.027 |
| [dateutil/dateutil#1588](https://github.com/dateutil/dateutil/issues/1588) | open; open pull requests #1589 (closes it) and #1575 (mentions it) | `open_bug`, high | `open_bug`, high; lists both | $0.008 |
| [marshmallow-code/marshmallow#3072](https://github.com/marshmallow-code/marshmallow/issues/3072) | open; open pull request #3073 | `open_bug`, high | `open_bug`, high; lists #3073 | $0.023 |
| [pallets/click#2827](https://github.com/pallets/click/issues/2827) | closed as completed by #3391, an opt-in; the default behaviour is unchanged | `open_bug`, high | `open_bug`, **low**: "the issue was closed as completed on 2026-05-16 by pull request #3391" | $0.005 |

No real open bug lost its verdict. The four controls are few, and three of them were picked because FixedYet had already called them correctly, so they show that the new rules do not break known-good results, not how often they would on new issues.

### Limits of rules v2

- **Same failure, wrong test.** A test that fails the same way on both sides for its own reason (pylint-6528, requests-2317) is still an `open_bug` at high confidence. No rule over two pytest logs can tell it from a real open bug.
- **Different text, same bug.** A fix side whose message changed for an unrelated reason (a reworded error, a newer pytest after the fix side's reinstall) now reads `inconsistent` although the bug is still there. Nothing like it happened in these runs; it would cost a real open bug its verdict, not create a wrong one.
- **The exception rule reads only the issue.** An exception the reporter did not name but the bug really raises lowers a real open bug to low confidence. It needs the thread: `fixedyet rescore --offline` keeps whatever an assessment already says.
- **GitHub's state is one more signal, not proof.** "Closed as completed" can mean an opt-in (click#2827), a fix in another repository, or a wrong closing. A merged pull request that only mentions the issue may be unrelated. Both only lower the confidence. Without a token the Development sidebar is not read.
- **The agent's notes do not decide anything.** The type note, the API note and the symptom check change what the model reads; the verdict still comes from the two side runs, which do not load the plugin. The symptom check matches names and the first words of a quoted message, so it can send back a correct test once, at the price of one more model turn (it did for pytest-10051 before the fix for plain asserts).
- **Small numbers.** Seven instances twice, one full run of fifteen, four controls. One instance is 6.7 points of a full run.
- **Labels by an AI.** The truth of the live issues in `eval/live-labels.txt` was judged by Claude, not by a person.
- **No second test.** The optional second test (a different angle, Nemotron 3 Ultra, on fail/fail only) was not built: Ultra's price is not in FixedYet's price table, so the spend ledger would have counted it at a guessed rate.

## Results, rules v1 (first measurement)

Kept as history: the numbers before rules v2, measured earlier on 2026-10-21. Two complete runs of the 15 instances on the same code (A and B), preceded by three development runs that found harness bugs and are listed at the end. The tables below put the two final runs side by side. Every number comes from the files the runner wrote (`eval/results/20261021T114507Z` for A, `eval/results/20261021T115553Z` for B); nothing was edited by hand.

### Run

| | |
|---|---|
| Date | 2026-10-21. Run A started 11:45 UTC, run B 11:55 UTC |
| FixedYet commit | `348a63f` |
| Model | `nvidia/nemotron-3-super-120b-a12b` writes the test; `NVIDIA-Nemotron-3-Nano-30B-A3B` extracts the issue contract. Both through Nebius Token Factory |
| Sandbox backend | **Local Docker** (`FIXEDYET_SANDBOX=docker`; Colima on an Apple-silicon Mac, linux/arm64 `python:3.9` and `python:3.11` images). The live demo uses the same Docker backend on its own VM; the Nebius Sandboxes backend has not run live |
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

### Audit of reproduced tests, run C (`20261021T210757Z`)

I read the 11 reproduced tests of run C (`fixed_on_main`, Nemotron 3 Super) against their issues and the stored logs. **This was done by Claude, an AI. No person has audited them yet**, so treat it as a first pass.

How the issues were found. `assessment.issue_url` holds `swe-bench:<instance id>`, not a GitHub URL, and the instance number is the fixing pull request, not the issue. I took the issue number from "Fixes #N" in each pull request body and read the issue with `gh api repos/OWNER/REPO/issues/N` (read-only). The GitHub text is identical to the SWE-bench `problem_statement` the agent was given (compared after whitespace normalisation). Issue numbers: requests-1921 -> #1920, requests-2931 -> #2930, requests-5414 -> #5367, pytest-6202 -> #6189, pytest-7571 -> #7569, pytest-7982 -> #7981, pytest-10051 -> #9877, flask-5014 -> #5010, pylint-6386 -> #6385, pylint-6528 -> #6471, pylint-7277 -> #7231.

On every instance the test failed on the base commit in both runs and passed with the gold patch in both runs, and none of the 11 needs the network.

| Instance | Exercises the issue? | Base log shows the symptom? | Caveat |
|---|---|---|---|
| psf__requests-1921 | yes: session header set to `None`, then a request | yes: `'Accept-Encoding': None` in the prepared headers | mocks `session.send`, so it checks the prepared request, not the wire; the issue's snippet plus a mock |
| pytest-dev__pytest-6202 | yes: `..[` parameter id, headline of the failure report | yes: captured stdout shows `test_boo[.[]` | the issue's snippet run through `testdir`, plus a substring assertion |
| pallets__flask-5014 | yes: `Blueprint('', __name__)` | yes: `DID NOT RAISE <class 'ValueError'>` | the issue asks for a `ValueError`; it reports no crash, so the "symptom" is the missing error |
| pytest-dev__pytest-7571 | yes: `set_level` in one test, handler level in the next | yes: `Expected level 0, got 42` | depends on test order; the issue's own snippet with an assertion added |
| psf__requests-2931 | yes, up to the point of sending: `Request('PUT', ..., data=bytes).prepare()` | yes: `UnicodeDecodeError` raised from `to_native_string` | no assertion; passes if nothing raises |
| pylint-dev__pylint-6386 | yes: `pylint <file> -v` | yes: `argument --verbose/-v: expected one argument`, the issue's own line | negative assertion only; the agent hit its turn limit without submitting |
| pytest-dev__pytest-7982 | yes: symlink to a directory inside a test directory | yes: exit code 5, `no tests ran` | runs `pytest.main` inside the outer pytest |
| pylint-dev__pylint-6528 | yes: `--recursive=y --ignore=.a .` | yes: `.a/foo.py:1:0: ...` is still reported | covers `--ignore` only; hard-coded sandbox path |
| psf__requests-5414 | yes: `requests.get("http://.example.com")` | different exception class: `LocationParseError`, the issue says `UnicodeError` | exception class differs from the issue's; no network needed (first caveat below) |
| pytest-dev__pytest-10051 | yes: `get_records("call")` against `records` after `clear()` | yes: `assert [<LogRecord: ...y, 8, "test">] == []`, the issue's own line | the issue's snippet copied, comment included |
| pylint-dev__pylint-7277 | yes, the line the issue points at: calls `modify_sys_path()` directly, not through `runpy` | yes: `sys.path[0]` is `/work/repo/tests`, `"something"` is gone | direct call instead of `runpy`, as in run A |

Caveats found:

- requests-5414: the base-side log ends in `urllib3.exceptions.LocationParseError: Failed to parse: '.example.com', label empty or too long`, raised in urllib3's `create_connection`. The issue reports `UnicodeError: encoding with 'idna' codec failed`; the installed urllib3 wraps that error. The cause is the same (the empty label) and the expected result is the same (`InvalidURL`), but the exception class in the log is not the one the issue quotes. The harness itself bounced one submit at turn 5 with "the failure lacks UnicodeError", and the stored log still shows `LocationParseError`. The idna check runs before any DNS lookup, so no network is needed. The test is the issue's snippet in `pytest.raises(InvalidURL)` with the message taken from the issue's "Expected Result" (`URL has an invalid label.`), checked by equality, so it is not vacuous.
- requests-2931: unlike run A, it does not call the live `httpbin.org`; it only prepares the request, so it needs no network. It has no assertion. It passes with the patch because `prepare()` raises nothing, and a fix that returned a mangled body would pass too. The base log shows `UnicodeDecodeError: 'ascii' codec can't decode byte 0xc3` inside `to_native_string`, which matches the issue's title; the issue itself gives no traceback.
- requests-1921: the test replaces `session.send`, so the adapter and the wire are never reached. The base value in the prepared headers is the Python `None`, which `http.client` later turns into the string "None" as the issue says. The second assertion (`val != 'None'`) cannot fail on the base, because the value is `None`, not the string; the first assertion does the work.
- pytest-7571: `test_bar` fails only if `test_foo` ran before it in the same process. Run alone, deselected, or reordered by a plugin, it passes. That order dependence is the bug itself (a leak across tests), and the issue's snippet has it too, but the test would be vacuous without it.
- pytest-10051, pytest-7571, pytest-6202, requests-1921 and requests-5414 are the issue's own snippet with an assertion (and a mock, `pytest.raises` or `testdir`) added, so the agent mostly had to transcribe. pytest-10051 is copied line for line, including the issue's trailing comment `# fails: assert [<LogRecord: ...y, 8, "test">] == []`, and the log reproduces that same text. pylint-6386 and pylint-6528 run the issue's command line. flask-5014 has no snippet in the issue.
- pylint-6386: the only assertion is that `"expected one argument"` is absent from stderr. With the patch it would also pass if pylint died for some other reason, and the exit code and the verbose output are not checked. The agent used all 20 turns without submitting (two submits rejected as "code differs from the last run"), and the harness took the last test run, so this test was not a deliberate submission.
- pytest-7982: `pytest.main` is run in-process inside the outer pytest run, so it shares `sys.modules` and plugins with it. It still discriminates (exit 5, `no tests ran`, on the base; exit 0 with the patch) and the rootdir is the temp directory, so no repo config leaks in. The agent's first three versions passed on the base (turns 4, 5 and 8) before it found one that failed. The failure is the symlink being skipped, as the issue says.
- pylint-6528: it tests only `--ignore=.a`, one of the issue's four commands. It does not demand that `.a/foo.py` is skipped with no ignore option, the wrong expectation that made A and B `open_bug`, and the gold patch fixes the three explicit options through one helper, so test and patch agree. The `'bar.py' in out` check stops it passing if pylint prints nothing. It hard-codes `PYTHONPATH=/work/repo`, which only exists in the sandbox. It also writes `'# pass\\n'` (a literal backslash-n), so each file is one comment line with no final newline (the log shows `C0304 missing-final-newline`) instead of the issue's `import re`; this does not change the result.
- pylint-7277: it calls `pylint.modify_sys_path()` directly instead of `runpy.run_module('pylint', run_name="__main__", alter_sys=True)` as the issue does (the same choice as run A). It exercises the line the issue points at. The base log is good evidence this time: `sys.path[0]` is `/work/repo/tests`, so the inserted `"something"` was popped. Run B's log held only help text.
- pallets__flask-5014: the issue is a request for validation, not a crash report. The test is the one-line call `Blueprint('', __name__)` inside `pytest.raises(ValueError)` with no `match`, so any `ValueError` would satisfy it. The base builds the Blueprint without raising, so it does discriminate.

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

**First pass: 6 correct, 2 wrong, 2 without a verdict.** The two `unsupported` runs were environment failures, not model failures. python-dotenv's tests need its `cli` extra (click), which was never installed. jinja ships a requirements file for Python 3.7 that downgraded `trio` below what Python 3.13 can import. Both were fixed in general terms (`6a5d38d`, `a952ec1`: a project's own extras, other Pythons' requirements files skipped, a conftest that cannot load repaired from the project's own declared requirements), and both issues were then rerun on the demo. **After those fixes: 8 correct and 2 wrong out of 10.** Both wrong verdicts are `open_bug` from a wrong model-written test, the same failure mode as the SWE-bench misses. Re-scored by rules v2 (above, no model call), both become `inconsistent`, so none of the ten is wrong any more, and none of the eight correct verdicts changed.

**Rerun on the final agent, 22 October 2026** (commit `e6f2f25`, deployed on the demo; all ten again, one after another, 29 minutes and $0.15 in all):

| Issue | Verdict | Correct? |
|---|---|---|
| werkzeug#3312, packaging#1315, isort#2723, parse#262, python-dotenv#697 | `fixed_on_main`, high; the fixing commit found for each (`7ad32e8`, `0a85b41`, `986b53f`, `4f76a3e`, `e0310e5`) | yes |
| marshmallow#3072, dateutil#1588, jinja#2244 | `open_bug`, high | yes: all three still open, each with an unmerged pull request |
| pyparsing#693 | `fixed_on_main`, **low**: "the reported version is not in the history of master" | the test is right this time (the issue's snippet: the comment leaks into the result at 3.3.3 and not on master), but 3.3.3 was released from its own branch, `3.3.3_work`, which master never merged, so nothing was fixed on master. That is what the new history check (`e62601d`) says; the issue is still open |
| pytest-mock#639 | `inconsistent`, low, after a second test | no: fixed the day it was opened. The bug is in the wording of the failure message, and both tests asserted only that the call fails, not what the message says |

**8 of 10 correct, the other two flagged at low confidence with the reason, none wrong at high confidence.** The ten assessments are saved in `eval/live/20261022/`. pyparsing#693 is the run that led to the history check: before it, the same result said `fixed_on_main` at high confidence.

Two of the open bugs went on to [fix mode](../README.md#fix-mode-open-bugs-to-verified-draft-prs), and jinja#2244 was verified on the demo server. One fixed-on-main report went on to a backport: pydantic#13991 was backported to `v2.14-fixes` and opened upstream as [pydantic/pydantic#14043](https://github.com/pydantic/pydantic/pull/14043), which the maintainer closed unmerged on 22 October, as they do the backports themselves.

### Contamination caveat

Nemotron may have seen these public issues, their fixing pull requests and SWE-bench itself during training. Several of the reproduced tests are the issue's own snippet (pytest-10051, pytest-7571), which any model can transcribe whether or not it remembers the fix. These 22 reproductions therefore partly measure recall and snippet-copying, not the ability to reproduce a bug nobody has fixed yet. The live check on real issues above is the counterweight: ten recent issues, none from SWE-bench, gave 8 correct verdicts out of 10 after the environment fixes, and on the final agent 8 of 10 with the other two flagged at low confidence.

### Development runs

One bug found by reading the runner and three earlier full runs found harness bugs, each fixed in a commit before the final runs. The three full runs are kept in `eval/results/` and are not the published numbers.

| Run | Reproduced | What it found | Fixed in |
|---|---|---|---|
| Before the dry run (found by reading the runner) | | The eval runner computed SWE-bench's Python version for each release but never passed it to the build, so the environment used the model's guess. The dry run on requests-1921 then reproduced the bug in 70 s for $0.013 | `4d6d9e7` |
| `20261021T110000Z` | 9/15 | A pytest that dies on a plugin import exits with code 1, like a failed test, so requests-5414 was counted as `open_bug`. Underneath, the Flask<2 pins in requests' test requirements met MarkupSafe 3 (`soft_unicode` import error) | `4d6d9e7`: exit 1 without a pytest summary line is an error; `markupsafe<2.1` is pinned when pytest cannot start for that reason |
| `20261021T111954Z`, `20261021T113010Z` | 11/15, 11/15 | flask-5014 was a setup failure in both. The test-requirements glob installed `requirements/tests-pallets-min.txt` (Werkzeug==2.0.0), so the base side could not import Flask, while the fix side's reinstall upgraded Werkzeug again | `348a63f`: requirements files named min, minimum, lowest or oldest are not installed |

Total model spend for the whole exercise (dry run, five full runs, two single-instance probes): about $2.50 at list price, against the $8 cap.
