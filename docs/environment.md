# The environment builder

How FixedYet turns an old release of a pure-Python project into a sandbox where its tests can run: test dependencies, optional extras, a conftest that cannot load, and the disk the Docker backend uses. The [README](../README.md) has the short version.

## Test dependencies

Projects often keep what their tests need outside the package's own extras, and a missing `hypothesis` or `xmlschema` turns a whole test file into a collection error. After the editable install the baseline step therefore reads these from the checkout and installs them, once, into the checkpoint:

- PEP 735 `[dependency-groups]` named `test`, `tests` or `testing` (and the same names in `[tool.pdm.dev-dependencies]`), with `include-group` resolved;
- requirements files: `requirements/test*.txt`, `test-requirements.txt`, `requirements-test.txt`, `tests/requirements.txt` and a few spellings of those;
- a `dev` group or `requirements-dev.txt` only when the project has no test group or file at all, since dev groups tend to hold linters and documentation tools.

A requirements file whose name carries another Python's version (`requirements/tests37.txt`, `test-py38.txt`) is skipped: it is the pin set of a second environment, and installed over the right pins it downgrades them to versions this interpreter cannot import (pallets/jinja 3.1.6 ships `tests.txt` and `tests37.txt`; the second brought trio 0.22.2 to Python 3.13, which broke the `import trio` in the conftest of the main branch).

A group is installed with `pip install --group <name>` when the pip in the image supports it (pip 25.1 and newer; the `python:3.12` image ships an older one), otherwise its entries are read with `tomllib` and installed by name, in one command first and one at a time if that fails. A dependency that cannot be installed is reported in the progress log and never fails the build. Only plain requirements (`name[extras] version-pins ; marker`) are installed: URLs, VCS references, local paths, pip options, an `-r` include or a symlink that leaves the repository, the project's own name and anything past three sources or 120 requirements are refused and counted. The code is `src/fixedyet/testdeps.py`; it runs inside the sandbox with the standard library only and reads the files as text.

## Optional extras, and a conftest that cannot load

Two more steps run in the sandbox with the same helper, after the dependencies and again after the fix side's checkout:

- **Extras of the project itself.** `pip install "python-dotenv[cli]"`, or "with `python-dotenv[cli]` installed", in the thread installs that extra (`pip install -e .[cli]`), and so does a console script of the project that cannot start and tells the reader which extra it wants (`dotenv --help` prints `Run pip install "python-dotenv[cli]"`). Only an extra the project declares is installed, and the agent is told which ones are in.
- **Repair of the conftest.** Pytest collects the test file once before anything runs. If that dies while loading the project's conftest or plugins, the one fixable cause is fixed from the project's own declarations: a missing module (`No module named 'click'`) is installed from the requirement that an extra, group or requirements file of the project lists for it, never guessed from the package index; a package that crashes on import (a stale pin left over from the reported version) is upgraded. At most three rounds, never fatal.

When a run still errors, the report's basis says why (`pytest could not load the project's tests/conftest.py (ModuleNotFoundError: No module named 'click')`), and the test-writing agent is told in the same words that a setup error is not a failure of its test; two in a row end its loop instead of burning the remaining turns.

## Disk use of the Docker backend

Every checkpoint is a local Docker image of about 1.5 GB. After each run FixedYet keeps the newest `FIXEDYET_KEEP_CHECKPOINTS` images of the `fixedyet-ckpt` repository (default 6, so roughly the last three runs) and removes the rest. It never touches an image of another repository, skips an image a container still uses, and when several runs are in flight at once (an evaluation) it waits for the last one. To prune right now:

```bash
fixedyet gc               # keep the newest FIXEDYET_KEEP_CHECKPOINTS images
fixedyet gc --keep 2      # keep two
fixedyet gc --dry-run     # list what would be removed, remove nothing
```

A backport makes two checkpoints (the branch as it was, and with the fix applied) and prunes the same way when it ends, and so does a fix. Checkpoints are working storage: with runs in parallel an image can be gone within minutes, so reports never point to one; their rerun commands start from a clean clone at the recorded commit. Raise `FIXEDYET_KEEP_CHECKPOINTS` if you want to keep more images around.
