"""SWE-bench Lite and Verified loader.

Rows come from the free Hugging Face datasets-server rows API, one page of 100 at a
time, and are cached as JSON under ``eval/cache/`` so a rerun never touches the network.
Only the fields the harness needs are kept (see ``SweBenchInstance``); the gold test
lists and the maintainers' hints are dropped.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from fixedyet.models import SweBenchInstance

DATASET_NAMES: dict[str, str] = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
}
ROWS_URL = "https://datasets-server.huggingface.co/rows"
PAGE_SIZE = 100  # the API's maximum rows per request
DEFAULT_CACHE_DIR = Path("eval/cache")  # relative to the working directory, like eval/results

# Columns that SweBenchInstance needs. If the API truncated any of them the row is unusable.
_REQUIRED_COLUMNS = (
    "instance_id", "repo", "base_commit", "patch", "test_patch", "problem_statement",
)
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 5


class DatasetError(RuntimeError):
    """The dataset could not be fetched, parsed or searched."""


# ---------------------------------------------------------------------------
# Python version per repository and version
# ---------------------------------------------------------------------------

# Source: SWE-bench harness constants, MAP_REPO_VERSION_TO_SPECS_PY in
# https://github.com/SWE-bench/SWE-bench/blob/v4.1.0/swebench/harness/constants/python.py
# (tag v4.1.0, commit 726c5461e2ef52d83cf1ea2107870a8bb3328d57; later releases moved the
# table out of that file). Only the repositories this harness can run are copied:
# light, pure-Python projects. Each entry is the "python" value of that repo's spec.
# A repo maps to either one version for all its releases or a per-release table.
_PYTHON_BY_REPO: dict[str, str | dict[str, str]] = {
    "psf/requests": "3.9",
    "pytest-dev/pytest": "3.9",
    "pylint-dev/pylint": "3.9",
    "pylint-dev/astroid": "3.9",
    "marshmallow-code/marshmallow": "3.9",
    "sqlfluff/sqlfluff": "3.9",
    # Flask: 2.0 -> 3.9, 2.1 -> 3.10, everything newer -> 3.11 (SPECS_FLASK).
    "pallets/flask": {"2.0": "3.9", "2.1": "3.10", "2.2": "3.11", "2.3": "3.11",
                      "3.0": "3.11", "3.1": "3.11"},
    # pydicom: per-release table (SPECS_PYDICOM).
    "pydicom/pydicom": {"1.0": "3.6", "1.1": "3.6", "1.2": "3.6", "1.3": "3.6",
                        "1.4": "3.8", "2.0": "3.8", "2.1": "3.9", "2.2": "3.9",
                        "2.3": "3.10", "2.4": "3.11", "3.0": "3.11"},
}


def python_version_for(instance: SweBenchInstance) -> str | None:
    """The Python version SWE-bench's harness builds this instance with.

    Returns None for a repository or release the table does not cover, which tells the
    sandbox to use its default image.
    """
    entry = _PYTHON_BY_REPO.get(instance.repo)
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    return entry.get(instance.version)


# ---------------------------------------------------------------------------
# datasets-server parsing
# ---------------------------------------------------------------------------


def parse_rows_page(payload: dict[str, Any]) -> tuple[list[SweBenchInstance], int]:
    """Turn one datasets-server ``/rows`` response into instances.

    Returns the instances on this page and ``num_rows_total`` for the whole split.
    Raises DatasetError if the payload has no rows or a needed cell was truncated.
    """
    if "rows" not in payload:
        raise DatasetError(f"unexpected datasets-server response: keys {sorted(payload)}")
    instances: list[SweBenchInstance] = []
    for entry in payload["rows"]:
        row = entry.get("row") or {}
        truncated = [c for c in entry.get("truncated_cells") or [] if c in _REQUIRED_COLUMNS]
        if truncated:
            raise DatasetError(
                f"row {entry.get('row_idx')} ({row.get('instance_id')}) has truncated "
                f"cells {truncated}; the rows API cut the data"
            )
        missing = [c for c in _REQUIRED_COLUMNS if c not in row]
        if missing:
            raise DatasetError(f"row {entry.get('row_idx')} is missing columns {missing}")
        instances.append(
            SweBenchInstance(
                instance_id=row["instance_id"],
                repo=row["repo"],
                base_commit=row["base_commit"],
                patch=row["patch"],
                test_patch=row["test_patch"],
                problem_statement=row["problem_statement"],
                version=str(row.get("version") or ""),
                created_at=str(row.get("created_at") or ""),
                environment_setup_commit=str(row.get("environment_setup_commit") or ""),
            )
        )
    total = int(payload.get("num_rows_total", len(instances)))
    return instances, total


def _get_json(client: httpx.Client, params: dict[str, Any]) -> dict[str, Any]:
    """GET the rows API with retry and backoff on rate limits and server errors."""
    last: str = ""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = client.get(ROWS_URL, params=params)
        except httpx.TransportError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200:
                return response.json()
            last = f"HTTP {response.status_code}"
            if response.status_code not in _RETRY_STATUS:
                raise DatasetError(f"datasets-server returned {last}: {response.text[:200]}")
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                time.sleep(min(int(retry_after), 60))
                continue
        time.sleep(2.0 * 2**attempt)
    raise DatasetError(f"datasets-server failed after {_MAX_ATTEMPTS} attempts: {last}")


def fetch_dataset(dataset: str, *, client: httpx.Client | None = None) -> list[SweBenchInstance]:
    """Download every row of ``lite`` or ``verified``, paging 100 at a time."""
    name = _dataset_name(dataset)
    own_client = client is None
    http = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        instances: list[SweBenchInstance] = []
        offset = 0
        total = 1  # replaced by the first response
        while offset < total:
            payload = _get_json(
                http,
                {"dataset": name, "config": "default", "split": "test",
                 "offset": offset, "length": PAGE_SIZE},
            )
            page, total = parse_rows_page(payload)
            if not page:
                break
            instances.extend(page)
            offset += len(page)
        return instances
    finally:
        if own_client:
            http.close()


# ---------------------------------------------------------------------------
# Public loader with cache
# ---------------------------------------------------------------------------


def _dataset_name(dataset: str) -> str:
    try:
        return DATASET_NAMES[dataset]
    except KeyError:
        raise DatasetError(
            f"unknown dataset {dataset!r}; choose one of {sorted(DATASET_NAMES)}"
        ) from None


def cache_path(dataset: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    _dataset_name(dataset)
    return cache_dir / f"swebench_{dataset}.json"


def _read_cache(path: Path) -> list[SweBenchInstance] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [SweBenchInstance.model_validate(item) for item in raw]
    except (OSError, ValueError):
        return None  # unreadable or stale cache: fetch again


def _write_cache(path: Path, instances: list[SweBenchInstance]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps([i.model_dump() for i in instances], ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(path)


def load_instances(
    dataset: str,
    ids: Iterable[str] | None = None,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    client: httpx.Client | None = None,
) -> list[SweBenchInstance]:
    """Load SWE-bench Lite (``"lite"``) or Verified (``"verified"``).

    With ``ids``, returns exactly those instances in the order given and raises
    DatasetError naming any id the dataset does not contain. Without ``ids``, returns the
    whole split. The split is downloaded once and cached under ``cache_dir``.
    """
    path = cache_path(dataset, cache_dir)
    instances = None if refresh else _read_cache(path)
    if instances is None:
        instances = fetch_dataset(dataset, client=client)
        _write_cache(path, instances)
    if ids is None:
        return instances
    by_id = {i.instance_id: i for i in instances}
    wanted = list(ids)
    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise DatasetError(f"not in SWE-bench {dataset}: {', '.join(missing)}")
    return [by_id[i] for i in wanted]
