"""The spend ledger: how much model money this installation has used in its whole life.

Nebius Token Factory draws from a balance and charges the owner's card when the balance goes
negative; it has no spending-limit setting. So the cap on total spend lives here. One JSON file
holds the cumulative USD spent on chat calls across every run (CLI, web demo, MCP, eval) plus a
per-day breakdown:

    {"total_usd": 1.2345, "days": {"2026-10-21": 0.8, "2026-10-22": 0.4345}}

Where it lives
    ``FIXEDYET_LEDGER_PATH`` when it is set; otherwise
    ``$XDG_STATE_HOME/fixedyet/spend-ledger.json`` (``~/.local/state/fixedyet/...``), one file
    per user, whatever the runs directory. Before this setting existed the ledger was
    ``<runs_dir>/spend-ledger.json``, so a new ``FIXEDYET_RUNS_DIR`` started again at $0. When
    the ledger file does not exist yet, the total is read from that old file of the current runs
    directory, and the first spend writes it to the new place: moving the ledger never resets
    the total.

How it stays correct
    * Writes are atomic: a temporary file in the same directory, fsync, then ``os.replace``. A
      crash leaves the old file or the new one, never half of one.
    * ``record`` is a locked read-modify-write: a thread lock inside the process and an ``flock``
      on ``spend-ledger.lock`` between processes (POSIX; elsewhere the thread lock only), so
      concurrent runs never lose an update.
    * ``reserve`` / ``release`` set money aside for calls in flight (in-process), so two
      concurrent calls cannot both pass the check when only one of them fits under the cap.
    * It fails closed. A ledger that exists but cannot be parsed raises ``LedgerError`` instead of
      counting as zero, which would silently reset the cap. Spend that could not be written to
      disk is kept in memory and still counts for the rest of the process.

The caller is `fixedyet.llm.LLM`: it reserves the worst-case cost of a call before sending it,
refuses with ``TotalBudgetExceeded`` if that does not fit, and records the real cost afterwards.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fixedyet.config import DEFAULT_TOTAL_BUDGET_USD

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]

LEDGER_NAME = "spend-ledger.json"
LOCK_NAME = "spend-ledger.lock"
_EPSILON = 1e-9  # spending exactly the budget is allowed; this absorbs float rounding only

# Guards the two dicts below and serialises writers inside this process.
_thread_lock = threading.RLock()
_reserved: dict[Path, float] = {}  # worst-case cost of calls in flight, per ledger
_unsaved: dict[Path, float] = {}  # spent, but not written to disk (write failed), per ledger


class LedgerError(RuntimeError):
    """The ledger cannot be read or written. Callers must treat this as 'do not spend'."""


# -- paths and settings --------------------------------------------------------------------------


def default_ledger_path() -> Path | None:
    """``$XDG_STATE_HOME/fixedyet/spend-ledger.json``, else under ``~/.local/state``; None when
    there is no home directory to put it in."""
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    try:
        base = Path(state) if state.startswith("/") else Path.home() / ".local" / "state"
    except RuntimeError:  # no HOME and no passwd entry
        return None
    return base / "fixedyet" / LEDGER_NAME


def legacy_ledger_path(settings: Any) -> Path:
    """Where the ledger was kept before ``FIXEDYET_LEDGER_PATH``: in the runs directory."""
    return Path(settings.runs_dir) / LEDGER_NAME


def ledger_path(settings: Any) -> Path:
    """``settings.ledger_path`` (``FIXEDYET_LEDGER_PATH``), else the user-level default."""
    configured = getattr(settings, "ledger_path", None)
    if configured:
        return Path(configured).expanduser()
    return default_ledger_path() or legacy_ledger_path(settings)


def _lock_path(path: Path) -> Path:
    return path.with_name(LOCK_NAME) if path.name == LEDGER_NAME else path.with_suffix(".lock")


def _key(settings: Any) -> Path:
    return ledger_path(settings).resolve()


def total_budget(settings: Any) -> float:
    """The cap in USD. A value that is not a number counts as 0, so a bad setting spends nothing."""
    value = float(getattr(settings, "total_budget_usd", DEFAULT_TOTAL_BUDGET_USD))
    return 0.0 if math.isnan(value) else value


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


# -- reading and writing the file ----------------------------------------------------------------


def _read(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"total_usd": 0.0, "days": {}}
    except OSError as exc:
        raise LedgerError(f"cannot read the spend ledger {path}: {exc}") from exc
    try:
        data = json.loads(text)
        total = float(data["total_usd"])
        days = data.get("days", {})
        if not isinstance(days, dict) or not math.isfinite(total) or total < 0:
            raise ValueError("unexpected content")
        return {"total_usd": total, "days": {str(k): float(v) for k, v in days.items()}}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LedgerError(
            f"the spend ledger {path} is corrupt ({exc}); fix or delete it by hand after "
            "checking the real balance at tokenfactory.nebius.com"
        ) from exc


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Hold the writer lock: the thread lock, plus an flock so other processes wait too."""
    with _thread_lock:
        if fcntl is None:  # pragma: no cover - not POSIX
            yield
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(_lock_path(path), "a")  # noqa: SIM115 - closed below
        except OSError as exc:
            raise LedgerError(f"cannot lock the spend ledger {path}: {exc}") from exc
        with lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _load(settings: Any, path: Path) -> dict[str, Any]:
    """The ledger at ``path``. While that file does not exist, the old one in the runs directory
    (see "Where it lives"), so that a new ledger location starts from the total spent so far."""
    if path.exists():
        return _read(path)
    legacy = legacy_ledger_path(settings)
    try:
        if legacy.exists() and legacy.resolve() != path.resolve():
            return _read(legacy)
    except OSError:
        pass
    return _read(path)


# -- public API ----------------------------------------------------------------------------------


def spent_total(settings: Any) -> float:
    """Cumulative USD spent on chat calls, including any spend that could not be saved yet."""
    with _thread_lock:
        path = ledger_path(settings)
        return _load(settings, path)["total_usd"] + _unsaved.get(_key(settings), 0.0)


def remaining(settings: Any) -> float:
    """USD left under the cap, never below zero."""
    return max(total_budget(settings) - spent_total(settings), 0.0)


def record(settings: Any, usd: float) -> None:
    """Add `usd` to the lifetime total and to today's total. Raises `LedgerError` if it cannot be
    saved; the amount then still counts in `spent_total` until the process ends or a later
    `record` manages to write it."""
    usd = float(usd)
    if not math.isfinite(usd) or usd < 0:
        raise ValueError(f"cannot record a spend of {usd!r}")
    path, key = ledger_path(settings), _key(settings)
    with _thread_lock:
        _unsaved[key] = _unsaved.get(key, 0.0) + usd  # counted from now on, whatever happens next
    with _exclusive(path):
        owed = _unsaved.get(key, 0.0)  # includes what other threads queued while we waited
        if owed <= 0:
            return
        try:
            data = _load(settings, path)
            today = _today()
            data["days"][today] = data["days"].get(today, 0.0) + owed
            data["total_usd"] += owed
            data["total_usd"] = round(data["total_usd"], 10)
            data["days"] = {day: round(amount, 10) for day, amount in data["days"].items()}
            _write(path, data)
        except OSError as exc:
            raise LedgerError(f"cannot write the spend ledger {path}: {exc}") from exc
        _unsaved.pop(key, None)


def reserve(settings: Any, usd: float) -> bool:
    """Set `usd` aside for a call about to be sent. False (and nothing set aside) when the
    lifetime total plus every reservation in flight plus `usd` would pass the cap."""
    key = _key(settings)
    with _thread_lock:
        committed = spent_total(settings) + _reserved.get(key, 0.0) + usd
        if not committed <= total_budget(settings) + _EPSILON:  # written so NaN refuses
            return False
        _reserved[key] = _reserved.get(key, 0.0) + usd
        return True


def release(settings: Any, usd: float) -> None:
    """Give back what `reserve` set aside, once the real cost has been recorded."""
    key = _key(settings)
    with _thread_lock:
        left = _reserved.get(key, 0.0) - usd
        if left > _EPSILON:
            _reserved[key] = left
        else:
            _reserved.pop(key, None)


def reserved_total(settings: Any) -> float:
    """USD currently set aside for calls in flight (this process only)."""
    with _thread_lock:
        return _reserved.get(_key(settings), 0.0)
