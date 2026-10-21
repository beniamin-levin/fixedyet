"""A pytest plugin that `backport.suite_script` ships into the sandbox: it writes every outcome of
the project's test suite to a JSON file, whatever the verbosity.

pytest drops its "N passed in Xs" line at ``-qq`` (a project's ``addopts = "-q"`` plus our own
``-q`` is enough), and plugins such as pytest-sugar rewrite it. This file does not depend on what
the terminal shows: it counts the reports the way pytest's own summary does and keeps the ids of
the tests that failed or errored, so two runs can be compared test by test.

It runs inside the sandbox with the project's Python, so it uses the standard library and the
pytest hooks only, and it is never imported by FixedYet itself. The output path comes from the
``FIXEDYET_SUITE_REPORT`` environment variable; without it the plugin does nothing.
"""

import json
import os
import time

MAX_FAILING = 1000  # ids kept; the counts stay exact
MAX_ID_CHARS = 500

_counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}
_failing = []
_seen = set()
_started = time.time()


def _failed(nodeid):
    nodeid = (nodeid or "<collection>")[:MAX_ID_CHARS]
    if nodeid not in _seen and len(_failing) < MAX_FAILING:
        _seen.add(nodeid)
        _failing.append(nodeid)


def pytest_sessionstart(session):
    global _started
    _started = time.time()


def pytest_collectreport(report):
    if report.failed:
        _counts["error"] += 1
        _failed(report.nodeid)


def pytest_runtest_logreport(report):
    xfail = hasattr(report, "wasxfail")
    if report.when == "call":
        if report.passed:
            _counts["xpassed" if xfail else "passed"] += 1
        elif report.failed:
            _counts["failed"] += 1
            _failed(report.nodeid)
        else:
            _counts["xfailed" if xfail else "skipped"] += 1
    elif report.failed:  # setup or teardown
        _counts["error"] += 1
        _failed(report.nodeid)
    elif report.skipped:
        _counts["xfailed" if xfail else "skipped"] += 1


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("FIXEDYET_SUITE_REPORT")
    if not path or hasattr(session.config, "workerinput"):  # an xdist worker: the controller writes
        return
    data = {
        "exitstatus": int(exitstatus),
        "counts": _counts,
        "failing": _failing,
        "duration": round(time.time() - _started, 2),
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        pass
