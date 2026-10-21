# Fixed on main: fails at the reported version, passes on the fix side

**Issue:** [Click 8.3.3 breaks `pytest` when stream `fd` is duplicated](https://github.com/pallets/click/issues/3384)  
**Verdict:** `fixed_on_main`  
**Confidence:** high

Basis:
- both runs agree: fail on the reported side
- both runs agree: pass on the fix side
- no run errored

## Results

| Side | Run 1 | Run 2 |
|---|---|---|
| Reported version (`8.3.3`) | FAIL | FAIL |
| Fix side (`main`) | PASS | PASS |

- Fixing commit: `d959898db264aaf07e70ad4eafa254286f9a5185`
- Fixed in release: `8.4.0`

## Reproduction test (`tests/test_issue_repro.py`)

```python
import io
import os
import sys

import click
from click.testing import CliRunner

@click.command()
def cmd():
    try:
        stdout_fd = sys.stdout.fileno()
    except io.UnsupportedOperation:
        click.echo("fileno() not available (click <=8.3.2)")
        return

    r, w = os.pipe()
    os.dup2(w, stdout_fd)
    os.close(w)
    os.close(r)
    click.echo("hello")

def test_invoke():
    runner = CliRunner()
    result = runner.invoke(cmd)
    assert result.exit_code == 0
```

## Output at the reported version (end of the log)

```
  File "/usr/local/lib/python3.12/site-packages/_pytest/capture.py", line 591, in snap
    self.tmpfile.seek(0)
OSError: [Errno 29] Illegal seek

==================================== ERRORS ====================================
_______________________ ERROR at teardown of test_invoke _______________________

self = <contextlib._GeneratorContextManager object at 0xf75a2521ed80>
typ = None, value = None, traceback = None

    def __exit__(self, typ, value, traceback):
        if typ is None:
            try:
>               next(self.gen)
E               OSError: [Errno 29] Illegal seek

/usr/local/lib/python3.12/contextlib.py:144: OSError
=================================== FAILURES ===================================
_________________________________ test_invoke __________________________________

self = <contextlib._GeneratorContextManager object at 0xf75a2521e120>
typ = None, value = None, traceback = None

    def __exit__(self, typ, value, traceback):
        if typ is None:
            try:
>               next(self.gen)
E               OSError: [Errno 29] Illegal seek

/usr/local/lib/python3.12/contextlib.py:144: OSError
=========================== short test summary info ============================
FAILED tests/test_issue_repro.py::test_invoke - OSError: [Errno 29] Illegal seek
ERROR tests/test_issue_repro.py::test_invoke - OSError: [Errno 29] Illegal seek
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 2 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 1 error in 0.01s
```

## Evidence

- **log**: Key error at the reported version - OSError: [Errno 29] Illegal seek
- **upstream**: [Changes — Click Documentation (8.5.x)](https://click.palletsprojects.com/en/stable/changes) - click.palletsprojects.com: Revert the 8.3.3 change that exposed the original file descriptor via fileno() on the redirected CliRunner streams in the default capture mode. os.dup2(w,sys.stdout.fileno()) calls inside a CLI no ...
- **upstream**: [Merged pull request that references this issue](https://github.com/pallets/click/pull/3391) - Merge commit d959898db2.
- **changelog**: [Released in 8.4.0](https://github.com/pallets/click/releases) - The changelog entry for 8.4.0 mentions #3384 / #3391.
- **log**: How the test was written - turn 1: list_dir . | turn 2: read_file src:1-200 | turn 3: list_dir src | turn 5: run_test -> FAILED | turn 6: submit | submitted: The test reproduces the issue: under Click 8.3.3, when sys.stdout.fileno() is called inside a CliRunner invocation and the file descriptor is duplicated via os.dup2, pytest's capture mechanism fails during teardown with an OSError: [Errno 29] Illegal seek when trying to seek on a temporary file used for capturing output. This matches the reported traceback.

## Root cause

In Click 8.3.3, PR #3244 changed how CliRunner handles sys.stdout by preserving the original file descriptor instead of raising UnsupportedOperation when fileno() is called. When the test duplicates stdout via os.dup2 and later attempts to seek on the captured stream (likely during pytest's teardown or output capture cleanup), the underlying file descriptor refers to a pipe (from os.pipe) which does not support seeking, causing an OSError: [Errno 29] Illegal seek.

_Written by the model from the issue, the test and the logs. The sandbox runs did not check it._

## Next actions

1. Revert or modify the file descriptor handling in CliRunner to avoid returning a valid fileno when the stream has been redirected at the FD level
2. Ensure that sys.stdout.fileno() raises UnsupportedOperation in CliRunner context when the standard stream has been replaced via os.dup2, to match pre-8.3.3 behavior
3. Add a test case that duplicates stdout with os.dup2 before invoking a Click command via CliRunner to prevent regression

## Usage

8 LLM calls, 33,755 tokens (32,005 in, 1,750 out), $0.0104, 7 sandbox runs, 2 search credits, 34 s wall time, model nvidia/nemotron-3-super-120b-a12b

## Rerun this yourself

Checkpoint images (runs fork from them):

- Reported version (`8.3.3`): `issue-repro-ckpt:31175117ec92433d`
- Fix side (`main`): `issue-repro-ckpt:31175117ec92433d`

Start a sandbox from either image and run:

```sh
cd /work/repo && python -m pytest -x -q tests/test_issue_repro.py
```

Or repeat the whole assessment: `fixedyet run https://github.com/pallets/click/issues/3384`
