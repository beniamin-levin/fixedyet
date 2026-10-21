"""SWE-bench loader tests. No network: the HTTP layer is an httpx MockTransport."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fixedyet.eval import swebench
from fixedyet.eval.swebench import (
    DatasetError,
    fetch_dataset,
    load_instances,
    parse_rows_page,
    python_version_for,
)
from fixedyet.models import SweBenchInstance

FIXTURE = Path(__file__).with_name("test_eval_rows_fixture.json")


def fixture_payload() -> dict[str, Any]:
    """A real datasets-server /rows response: SWE-bench Verified rows 289 and 290."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_instance(repo: str, version: str) -> SweBenchInstance:
    return SweBenchInstance(
        instance_id="a__b-1", repo=repo, base_commit="abc", patch="", test_patch="",
        problem_statement="", version=version,
    )


def paged_transport(total: int, calls: list[dict[str, str]]) -> httpx.MockTransport:
    """A fake datasets-server serving `total` rows built from the recorded fixture row."""
    template = fixture_payload()["rows"][0]

    def row(index: int) -> dict[str, Any]:
        entry = copy.deepcopy(template)
        entry["row_idx"] = index
        entry["row"]["instance_id"] = f"owner__repo-{index}"
        return entry

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        offset, length = int(params["offset"]), int(params["length"])
        rows = [row(i) for i in range(offset, min(offset + length, total))]
        return httpx.Response(200, json={"rows": rows, "num_rows_total": total})

    return httpx.MockTransport(handler)


# --- parsing -----------------------------------------------------------------------------


def test_parse_rows_page_reads_the_recorded_response() -> None:
    instances, total = parse_rows_page(fixture_payload())
    assert total == 500
    assert [i.instance_id for i in instances] == ["pallets__flask-5014", "psf__requests-1142"]
    flask = instances[0]
    assert flask.repo == "pallets/flask"
    assert flask.version == "2.3"
    assert flask.base_commit == "7ee9ceb71e868944a46e1ff00b506772a53a4f1d"
    assert flask.patch.startswith("diff --git")
    assert flask.test_patch.startswith("diff --git")
    assert "Blueprint" in flask.problem_statement
    assert flask.created_at.startswith("2023-03-04")
    assert flask.environment_setup_commit


def test_parse_rows_page_rejects_truncated_required_cell() -> None:
    payload = fixture_payload()
    payload["rows"][1]["truncated_cells"] = ["patch"]
    with pytest.raises(DatasetError, match="truncated"):
        parse_rows_page(payload)


def test_parse_rows_page_ignores_truncated_unused_cell() -> None:
    payload = fixture_payload()
    payload["rows"][1]["truncated_cells"] = ["hints_text"]
    instances, _ = parse_rows_page(payload)
    assert len(instances) == 2


def test_parse_rows_page_rejects_missing_column() -> None:
    payload = fixture_payload()
    del payload["rows"][0]["row"]["base_commit"]
    with pytest.raises(DatasetError, match="base_commit"):
        parse_rows_page(payload)


def test_parse_rows_page_rejects_error_payload() -> None:
    with pytest.raises(DatasetError, match="unexpected"):
        parse_rows_page({"error": "rate limited"})


# --- python version table ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "version", "expected"),
    [
        ("psf/requests", "2.3", "3.9"),
        ("psf/requests", "0.14", "3.9"),
        ("pytest-dev/pytest", "7.2", "3.9"),
        ("pylint-dev/pylint", "2.15", "3.9"),
        ("pallets/flask", "2.0", "3.9"),
        ("pallets/flask", "2.1", "3.10"),
        ("pallets/flask", "2.3", "3.11"),
        ("pydicom/pydicom", "2.3", "3.10"),
        ("pallets/flask", "9.9", None),  # release not in the table
        ("django/django", "4.2", None),  # repository not in the table
    ],
)
def test_python_version_for(repo: str, version: str, expected: str | None) -> None:
    assert python_version_for(make_instance(repo, version)) == expected


# --- fetching, paging and cache -----------------------------------------------------------


def test_fetch_dataset_pages_until_total() -> None:
    calls: list[dict[str, str]] = []
    client = httpx.Client(transport=paged_transport(250, calls))
    instances = fetch_dataset("verified", client=client)
    assert len(instances) == 250
    assert [c["offset"] for c in calls] == ["0", "100", "200"]
    assert {c["length"] for c in calls} == {"100"}
    assert {c["dataset"] for c in calls} == {"princeton-nlp/SWE-bench_Verified"}
    assert {c["config"] for c in calls} == {"default"}
    assert {c["split"] for c in calls} == {"test"}
    assert instances[249].instance_id == "owner__repo-249"


def test_load_instances_caches_and_selects_ids(tmp_path: Path) -> None:
    calls: list[dict[str, str]] = []
    client = httpx.Client(transport=paged_transport(150, calls))
    first = load_instances("lite", cache_dir=tmp_path, client=client)
    assert len(first) == 150
    assert (tmp_path / "swebench_lite.json").exists()
    assert len(calls) == 2

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the cache should have been used")

    offline = httpx.Client(transport=httpx.MockTransport(forbidden))
    picked = load_instances(
        "lite", ["owner__repo-7", "owner__repo-3"], cache_dir=tmp_path, client=offline
    )
    assert [i.instance_id for i in picked] == ["owner__repo-7", "owner__repo-3"]


def test_load_instances_names_missing_ids(tmp_path: Path) -> None:
    client = httpx.Client(transport=paged_transport(5, []))
    with pytest.raises(DatasetError, match="owner__repo-99"):
        load_instances("verified", ["owner__repo-1", "owner__repo-99"], cache_dir=tmp_path,
                       client=client)


def test_load_instances_refetches_a_corrupt_cache(tmp_path: Path) -> None:
    (tmp_path / "swebench_verified.json").write_text("{not json", encoding="utf-8")
    client = httpx.Client(transport=paged_transport(3, []))
    assert len(load_instances("verified", cache_dir=tmp_path, client=client)) == 3


def test_load_instances_refresh_ignores_the_cache(tmp_path: Path) -> None:
    calls: list[dict[str, str]] = []
    client = httpx.Client(transport=paged_transport(3, calls))
    load_instances("verified", cache_dir=tmp_path, client=client)
    load_instances("verified", cache_dir=tmp_path, client=client, refresh=True)
    assert len(calls) == 2


def test_unknown_dataset_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="unknown dataset"):
        load_instances("full", cache_dir=tmp_path)


def test_rate_limit_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(swebench.time, "sleep", lambda s: None)
    payload = fixture_payload()
    payload["num_rows_total"] = len(payload["rows"])  # one page is the whole split
    responses = [
        httpx.Response(429, headers={"retry-after": "1"}),
        httpx.Response(503),
        httpx.Response(200, json=payload),
    ]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: responses.pop(0)))
    instances = fetch_dataset("verified", client=client)
    assert [i.instance_id for i in instances] == ["pallets__flask-5014", "psf__requests-1142"]
    assert responses == []  # all three responses were consumed: two retries, then success


def test_persistent_server_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(swebench.time, "sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(DatasetError, match="after 5 attempts"):
        fetch_dataset("verified", client=client)


def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(swebench.time, "sleep", lambda s: None)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(404, text="no such dataset")

    with pytest.raises(DatasetError, match="404"):
        fetch_dataset("lite", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(seen) == 1
