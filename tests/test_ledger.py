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
    return Settings(runs_dir=tmp_path / "runs", total_budget_usd=budget,
                    ledger_path=tmp_path / "ledger" / "spend-ledger.json")


def ledger_file(settings: Settings) -> Path:
    assert settings.ledger_path is not None
    return settings.ledger_path


# -- the empty ledger and persistence ----------------------------------------------------------


def test_a_missing_ledger_means_nothing_spent_and_reading_creates_nothing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, budget=5.0)
    assert ledger.spent_total(settings) == 0.0
    assert ledger.remaining(settings) == 5.0
    assert not settings.runs_dir.exists() and not ledger_file(settings).parent.exists()


def test_the_ledger_lives_where_fixedyet_ledger_path_says(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    ledger.record(settings, 0.25)
    assert ledger_file(settings).is_file()
    assert ledger.ledger_path(settings) == ledger_file(settings)
    assert not (settings.runs_dir / "spend-ledger.json").exists()
    loaded = Settings.load({"FIXEDYET_LEDGER_PATH": str(tmp_path / "x.json")})
    assert loaded.ledger_path == tmp_path / "x.json"
    assert ledger.ledger_path(loaded) == tmp_path / "x.json"


def test_without_a_setting_the_ledger_is_a_user_level_state_file(tmp_path: Path) -> None:
    # conftest points XDG_STATE_HOME at tmp_path/state
    settings = Settings(runs_dir=tmp_path / "runs")
    assert ledger.ledger_path(settings) == tmp_path / "state" / "fixedyet" / "spend-ledger.json"


def test_a_new_runs_dir_does_not_start_a_new_total(tmp_path: Path) -> None:
    # FIXEDYET_RUNS_DIR used to carry the ledger, so every new runs directory began at $0
    first = Settings(runs_dir=tmp_path / "one", total_budget_usd=1.0)
    ledger.record(first, 0.75)
    second = Settings(runs_dir=tmp_path / "two", total_budget_usd=1.0)
    assert ledger.spent_total(second) == pytest.approx(0.75)
    assert ledger.reserve(second, 0.5) is False


def test_the_old_runs_dir_ledger_carries_over_to_the_new_place(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "spend-ledger.json").write_text(
        json.dumps({"total_usd": 0.45, "days": {"2026-10-20": 0.45}}), encoding="utf-8")
    settings = Settings(runs_dir=runs, total_budget_usd=20.0)
    new = ledger.ledger_path(settings)
    assert not new.exists()
    assert ledger.spent_total(settings) == pytest.approx(0.45)  # read from the old file
    ledger.record(settings, 0.05)  # the first spend writes the new file from the old total
    data = json.loads(new.read_text(encoding="utf-8"))
    assert data["total_usd"] == pytest.approx(0.5) and data["days"]["2026-10-20"] == 0.45
    # from now on the new file is the ledger, whatever the runs directory holds
    (runs / "spend-ledger.json").write_text('{"total_usd": 99}', encoding="utf-8")
    assert ledger.spent_total(settings) == pytest.approx(0.5)


def test_a_ledger_path_that_is_the_old_file_keeps_using_it(tmp_path: Path) -> None:
    # the deployed server sets FIXEDYET_LEDGER_PATH to <runs_dir>/spend-ledger.json
    runs = tmp_path / "runs"
    settings = Settings(runs_dir=runs, ledger_path=runs / "spend-ledger.json")
    ledger.record(settings, 0.45)
    assert json.loads((runs / "spend-ledger.json").read_text())["total_usd"] == 0.45
    assert (runs / "spend-ledger.lock").exists()


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
    folder = ledger_file(settings).parent
    assert [p.name for p in folder.iterdir() if p.name.endswith(".tmp")] == []
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
        "s = Settings(runs_dir=sys.argv[1], ledger_path=sys.argv[2])\n"
        "for _ in range(30):\n"
        "    ledger.record(s, 0.01)\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(settings.runs_dir),
                          str(ledger_file(settings))])
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
    ledger_file(settings).parent.mkdir(parents=True)
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


def test_ledgers_at_different_paths_do_not_share_reservations(tmp_path: Path) -> None:
    one = Settings(runs_dir=tmp_path / "r", ledger_path=tmp_path / "one.json", total_budget_usd=1.0)
    two = Settings(runs_dir=tmp_path / "r", ledger_path=tmp_path / "two.json", total_budget_usd=1.0)
    assert ledger.reserve(one, 0.9) is True
    assert ledger.reserve(two, 0.9) is True
