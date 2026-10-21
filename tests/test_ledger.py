"""The spend ledger: persistence, atomic writes, concurrency, failing closed, reservations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fixedyet import ledger
from fixedyet.config import Settings


def make_settings(tmp_path: Path, budget: float = 20.0) -> Settings:
    return Settings(runs_dir=tmp_path / "runs", total_budget_usd=budget)


def ledger_file(settings: Settings) -> Path:
    return settings.runs_dir / "spend-ledger.json"


# -- the empty ledger and persistence ----------------------------------------------------------


def test_a_missing_ledger_means_nothing_spent_and_reading_creates_nothing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=5.0)
    assert ledger.spent_total(settings) == 0.0
    assert ledger.remaining(settings) == 5.0
    assert not settings.runs_dir.exists()


def test_the_ledger_lives_in_runs_dir(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    ledger.record(settings, 0.25)
    assert ledger_file(settings).is_file()
    assert ledger.ledger_path(settings) == ledger_file(settings)


def test_record_accumulates_and_survives_a_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=10.0)
    ledger.record(settings, 1.5)
    ledger.record(settings, 0.25)
    assert ledger.spent_total(settings) == pytest.approx(1.75)
    assert ledger.remaining(settings) == pytest.approx(8.25)

    # A new Settings object and a cold in-memory state: only the file carries the total.
    again = make_settings(tmp_path, budget=10.0)
    ledger._unsaved.clear()
    ledger._reserved.clear()
    assert ledger.spent_total(again) == pytest.approx(1.75)
    ledger.record(again, 0.25)
    assert ledger.spent_total(settings) == pytest.approx(2.0)


def test_the_file_holds_the_total_and_per_day_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(ledger, "_today", lambda: "2026-10-21")
    ledger.record(settings, 0.5)
    ledger.record(settings, 0.25)
    monkeypatch.setattr(ledger, "_today", lambda: "2026-10-22")
    ledger.record(settings, 1.0)
    data = json.loads(ledger_file(settings).read_text(encoding="utf-8"))
    assert data["total_usd"] == pytest.approx(1.75)
    assert data["days"] == pytest.approx({"2026-10-21": 0.75, "2026-10-22": 1.0})


def test_remaining_never_goes_below_zero(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=1.0)
    ledger.record(settings, 1.4)  # the real bill can pass the estimate: remaining stops at 0
    assert ledger.remaining(settings) == 0.0
    assert ledger.spent_total(settings) == pytest.approx(1.4)


def test_a_bad_amount_is_rejected_and_zero_changes_nothing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    for bad in (-0.01, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            ledger.record(settings, bad)
    ledger.record(settings, 0.0)
    assert ledger.spent_total(settings) == 0.0


# -- atomic writes -----------------------------------------------------------------------------


def test_a_write_that_fails_leaves_the_old_file_intact_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    ledger.record(settings, 1.0)
    before = ledger_file(settings).read_text(encoding="utf-8")

    def broken_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", broken_replace)
        with pytest.raises(ledger.LedgerError, match="cannot write"):
            ledger.record(settings, 0.5)

    assert ledger_file(settings).read_text(encoding="utf-8") == before  # old file untouched
    assert [p.name for p in settings.runs_dir.iterdir() if p.name.endswith(".tmp")] == []
    # The 0.5 that could not be saved still counts in this process, and the next write flushes it.
    assert ledger.spent_total(settings) == pytest.approx(1.5)
    ledger.record(settings, 0.25)
    assert json.loads(ledger_file(settings).read_text(encoding="utf-8"))["total_usd"] == (
        pytest.approx(1.75)
    )
    assert ledger.spent_total(settings) == pytest.approx(1.75)


def test_the_file_is_replaced_never_written_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    ledger.record(settings, 1.0)
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        replaced.append((Path(src).name, Path(dst).name))
        assert json.loads(Path(src).read_text(encoding="utf-8"))["total_usd"] == pytest.approx(1.5)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    ledger.record(settings, 0.5)
    assert len(replaced) == 1
    temp_name, final_name = replaced[0]
    assert final_name == "spend-ledger.json" and temp_name != final_name


# -- concurrency -------------------------------------------------------------------------------


def test_concurrent_threads_lose_no_update(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=1_000.0)
    threads, per_thread = 8, 40

    def work() -> None:
        for _ in range(per_thread):
            ledger.record(settings, 0.01)

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert ledger.spent_total(settings) == pytest.approx(threads * per_thread * 0.01)
    data = json.loads(ledger_file(settings).read_text(encoding="utf-8"))
    assert data["total_usd"] == pytest.approx(3.2)
    assert sum(data["days"].values()) == pytest.approx(3.2)


def test_concurrent_processes_lose_no_update(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=1_000.0)
    script = (
        "import sys\n"
        "from fixedyet import ledger\n"
        "from fixedyet.config import Settings\n"
        "s = Settings(runs_dir=sys.argv[1])\n"
        "for _ in range(30):\n"
        "    ledger.record(s, 0.01)\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(settings.runs_dir)])
        for _ in range(4)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0, 0]
    assert ledger.spent_total(settings) == pytest.approx(4 * 30 * 0.01)


# -- fail closed -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", '{"days": {}}', '{"total_usd": "lots"}', '{"total_usd": -1}',
     '{"total_usd": 1, "days": []}'],
)
def test_a_corrupt_ledger_raises_instead_of_counting_as_zero(tmp_path: Path, content: str) -> None:
    settings = make_settings(tmp_path)
    settings.runs_dir.mkdir()
    ledger_file(settings).write_text(content, encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="corrupt"):
        ledger.spent_total(settings)
    with pytest.raises(ledger.LedgerError):
        ledger.remaining(settings)
    with pytest.raises(ledger.LedgerError):
        ledger.reserve(settings, 0.01)
    assert ledger_file(settings).read_text(encoding="utf-8") == content  # never overwritten


def test_a_budget_that_is_not_a_number_spends_nothing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=float("nan"))
    assert ledger.total_budget(settings) == 0.0
    assert ledger.remaining(settings) == 0.0
    assert ledger.reserve(settings, 0.0001) is False


# -- refusal at the cap (reserve / release) ------------------------------------------------------


def test_reserve_refuses_what_would_pass_the_cap(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=1.0)
    ledger.record(settings, 0.90)
    assert ledger.reserve(settings, 0.20) is False
    assert ledger.reserved_total(settings) == 0.0  # a refusal sets nothing aside
    assert ledger.reserve(settings, 0.05) is True


def test_spending_exactly_up_to_the_cap_is_allowed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=1.0)
    ledger.record(settings, 0.7)
    assert ledger.reserve(settings, 0.3) is True  # 0.7 + 0.3 hits the cap, does not pass it
    assert ledger.reserve(settings, 0.0001) is False


def test_reservations_count_against_the_cap_until_released(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=1.0)
    assert ledger.reserve(settings, 0.6) is True
    assert ledger.reserve(settings, 0.6) is False  # the first is still in flight
    assert ledger.reserved_total(settings) == pytest.approx(0.6)
    ledger.record(settings, 0.2)  # it really cost less
    ledger.release(settings, 0.6)
    assert ledger.reserved_total(settings) == 0.0
    assert ledger.reserve(settings, 0.6) is True  # 0.2 spent + 0.6 fits


def test_only_one_of_many_concurrent_reservations_fits(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=1.0)
    granted: list[bool] = []

    def grab() -> None:
        granted.append(ledger.reserve(settings, 0.6))

    pool = [threading.Thread(target=grab) for _ in range(12)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert granted.count(True) == 1


def test_a_zero_budget_refuses_everything(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=0.0)
    assert ledger.reserve(settings, 0.000001) is False
    assert ledger.remaining(settings) == 0.0


def test_ledgers_in_different_runs_dirs_do_not_share_reservations(tmp_path: Path) -> None:
    one = Settings(runs_dir=tmp_path / "one", total_budget_usd=1.0)
    two = Settings(runs_dir=tmp_path / "two", total_budget_usd=1.0)
    assert ledger.reserve(one, 0.9) is True
    assert ledger.reserve(two, 0.9) is True
