# Open bug: fails at the reported version and still fails on the fix side

**Issue:** [No stdout/stderr is captured with `invoke` during tests in combination with structlog](https://github.com/pallets/click/issues/2827)  
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
| Reported version (`8.1.7`) | FAIL | FAIL |
| Fix side (`main`) | FAIL | FAIL |

## Reproduction test (`tests/test_issue_repro.py`)

```python
import structlog
import click
from click.testing import CliRunner

logger = structlog.get_logger()
structlog.configure(logger_factory=structlog.stdlib.LoggerFactory())

@click.command()
def hello() -> None:
    logger.info("Hello!")

def test_log() -> None:
    runner = CliRunner()
    result = runner.invoke(hello)
    print(result.stdout)
    assert "Hello" in result.stdout
```

## Output at the reported version (end of the log)

```
--- last of 2 runs, every run exited 1 ---
F
=================================== FAILURES ===================================
___________________________________ test_log ___________________________________

    def test_log() -> None:
        runner = CliRunner()
        result = runner.invoke(hello)
        print(result.stdout)
>       assert "Hello" in result.stdout
E       AssertionError: assert 'Hello' in ''
E        +  where '' = <Result okay>.stdout

tests/test_issue_repro.py:16: AssertionError
----------------------------- Captured stdout call -----------------------------

=========================== short test summary info ============================
FAILED tests/test_issue_repro.py::test_log - AssertionError: assert 'Hello' i...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.02s
```

## Output on the fix side (end of the log)

```
--- last of 2 runs, every run exited 1 ---
F
=================================== FAILURES ===================================
___________________________________ test_log ___________________________________

    def test_log() -> None:
        runner = CliRunner()
        result = runner.invoke(hello)
        print(result.stdout)
>       assert "Hello" in result.stdout
E       AssertionError: assert 'Hello' in ''
E        +  where '' = <Result okay>.stdout

tests/test_issue_repro.py:16: AssertionError
----------------------------- Captured stdout call -----------------------------

=========================== short test summary info ============================
FAILED tests/test_issue_repro.py::test_log - AssertionError: assert 'Hello' i...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.02s
```

## Evidence

- **log**: Key error at the reported version - AssertionError: assert 'Hello' in ''
- **duplicate**: [#2647 Unflushed `stderr` doesn't get captured via `CliRunner`](https://github.com/pallets/click/issues/2647) - Similar issue in the same repository (closed, 7 comments).
- **duplicate**: [CliRunner.invoke doesn't capture stdout from logging · Issue #2497 · pallets/click · GitHub](https://github.com/pallets/click/issues/2497) - github.com: @Kamforka Hi @davidism, thanks for the heads up. Can you please link me to the related issue so I can follow the conversation? ### kdeldycke commented on Apr 29, 2026 @kdeldycke This is going to be...
- **upstream**: [Testing Click Applications — Click Documentation (8.5.x)](https://click.palletsprojects.com/en/stable/testing) - click.palletsprojects.com: Redirects OS file descriptors 1 and 2 to a temporary file via os.dup2()"), inspired by Pytest’s capfd. This catches output that bypasses sys.stdout, including: Stale references to the original sys....
- **log**: How the test was written - turn 1: list_dir . | turn 2: list_dir src | turn 3: run_test -> FAILED | turn 4: submit | submitted: This test reproduces the issue: when using structlog with stdlib LoggerFactory inside a Click command, invoking via CliRunner results in empty stdout, causing the assertion 'Hello' in result.stdout to fail. The test matches the reporter's snippet and expected failure.

## Root cause

The structlog output is directed to the standard library logger, whose StreamHandler writes to sys.stderr; however, Click's CliRunner isolation context replaces sys.stdout and sys.stderr with StringIO objects that are not flushed before capture, causing the logged message to remain in the buffer and not appear in result.stdout or result.stderr.

_Written by the model from the issue, the test and the logs. The sandbox runs did not check it._

## Next actions

1. Add logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)]) before invoking the command to ensure logs go to stdout.
2. Alternatively, configure structlog to use a PrintLoggerFactory or similar that writes directly to stdout.
3. Upgrade to Click >= 8.4.0 and use CliRunner(capture='fd') to capture file-descriptor level output, which bypasses Python stream buffering issues.

## Usage

6 LLM calls, 18,203 tokens (17,118 in, 1,085 out), $0.0056, 6 sandbox runs, 1 search credits, 29 s wall time, model nvidia/nemotron-3-super-120b-a12b

## Rerun this yourself

Checkpoint images (runs fork from them):

- Reported version (`8.1.7`): `issue-repro-ckpt:702386342e8f40ae`
- Fix side (`main`): `issue-repro-ckpt:702386342e8f40ae`

Start a sandbox from either image and run:

```sh
cd /work/repo && python -m pytest -x -q tests/test_issue_repro.py
```

Or repeat the whole assessment: `fixedyet run https://github.com/pallets/click/issues/2827`
