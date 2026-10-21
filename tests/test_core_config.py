from __future__ import annotations

from pathlib import Path

import pytest

from fixedyet import config
from fixedyet.config import Settings, get_settings


def test_defaults_match_the_spec() -> None:
    s = Settings()
    assert s.nebius_base_url == "https://api.tokenfactory.nebius.com/v1/"
    assert s.model == "nvidia/nemotron-3-super-120b-a12b"
    assert s.small_model == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    assert s.sandbox == "nebius"
    assert s.run_budget_usd == 0.60
    assert s.total_budget_usd == 20.0
    assert s.max_turns == 20
    assert s.demo_daily_limit == 5
    assert s.runs_dir == Path("runs")
    assert s.keep_checkpoints == 6
    assert s.root_path == ""


def test_load_reads_the_env_names_of_dot_env_example() -> None:
    s = Settings.load(
        env={
            "NEBIUS_API_KEY": "nk",
            "NEBIUS_BASE_URL": "https://example.test/v1/",
            "FIXEDYET_MODEL": "m-big",
            "FIXEDYET_SMALL_MODEL": "m-small",
            "CONTREE_TOKEN": "ct",
            "TAVILY_API_KEY": "tk",
            "GITHUB_TOKEN": "gt",
            "FIXEDYET_SANDBOX": "docker",
            "FIXEDYET_RUN_BUDGET_USD": "1.25",
            "FIXEDYET_TOTAL_BUDGET_USD": "12.5",
            "FIXEDYET_MAX_TURNS": "7",
            "FIXEDYET_DEMO_DAILY_LIMIT": "9",
        }
    )
    assert (s.nebius_api_key, s.contree_token, s.tavily_api_key, s.github_token) == (
        "nk", "ct", "tk", "gt",
    )
    assert (s.model, s.small_model, s.sandbox) == ("m-big", "m-small", "docker")
    assert (s.run_budget_usd, s.max_turns, s.demo_daily_limit) == (1.25, 7, 9)
    assert s.total_budget_usd == 12.5
    assert s.nebius_base_url == "https://example.test/v1/"


def test_keep_checkpoints_and_root_path_come_from_the_env() -> None:
    s = Settings.load(
        env={"FIXEDYET_KEEP_CHECKPOINTS": "3", "FIXEDYET_ROOT_PATH": "fixedyet/"}
    )
    assert (s.keep_checkpoints, s.root_path) == (3, "/fixedyet")
    assert Settings.load(env={"FIXEDYET_KEEP_CHECKPOINTS": "0"}).keep_checkpoints == 0
    assert Settings.load(env={"FIXEDYET_ROOT_PATH": "  "}).root_path == ""


def test_a_negative_keep_checkpoints_is_refused() -> None:
    with pytest.raises(ValueError):
        Settings.load(env={"FIXEDYET_KEEP_CHECKPOINTS": "-1"})


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (None, ""), ("", ""), ("/", ""), ("fixedyet", "/fixedyet"), ("/fixedyet/", "/fixedyet"),
        ("/tools/fixedyet", "/tools/fixedyet"), ("//fixedyet//", "/fixedyet"),
    ],
)
def test_normalize_root_path(raw: str | None, clean: str) -> None:
    assert config.normalize_root_path(raw) == clean


@pytest.mark.parametrize(
    "raw", ["/a b", "/a//b", "/a?x=1", "/a#b", "https://evil.example", "/a\\b"]
)
def test_a_root_path_that_is_not_plain_path_segments_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        config.normalize_root_path(raw)
    with pytest.raises(ValueError):
        Settings.load(env={"FIXEDYET_ROOT_PATH": raw})


def test_dot_env_example_lists_the_new_variables() -> None:
    example = (config.PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FIXEDYET_KEEP_CHECKPOINTS=6" in example
    assert "FIXEDYET_ROOT_PATH=" in example


def test_empty_values_mean_unset() -> None:
    """`GITHUB_TOKEN=` in a .env file must not become an empty override of a default."""
    s = Settings.load(env={"NEBIUS_BASE_URL": "  ", "FIXEDYET_MODEL": "", "GITHUB_TOKEN": ""})
    assert s.nebius_base_url == "https://api.tokenfactory.nebius.com/v1/"
    assert s.model == "nvidia/nemotron-3-super-120b-a12b"
    assert s.github_token == ""


def test_contree_token_falls_back_to_the_nebius_key() -> None:
    assert Settings.load(env={"NEBIUS_API_KEY": "nk"}).contree_token == "nk"
    assert Settings.load(env={"NEBIUS_API_KEY": "nk", "CONTREE_TOKEN": "ct"}).contree_token == "ct"


def test_dot_env_file_does_not_override_real_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("FIXEDYET_MAX_TURNS=3\nFIXEDYET_RUN_BUDGET_USD=0.10  # inline\n")
    monkeypatch.setenv("FIXEDYET_MAX_TURNS", "11")  # the real environment wins
    # load_dotenv writes into os.environ; register an undo so the value cannot leak to other tests.
    monkeypatch.setenv("FIXEDYET_RUN_BUDGET_USD", "placeholder")
    monkeypatch.delenv("FIXEDYET_RUN_BUDGET_USD")
    s = Settings.load(dotenv_path=dotenv)
    assert s.max_turns == 11
    assert s.run_budget_usd == 0.10


def test_repr_never_shows_secret_values() -> None:
    s = Settings(
        nebius_api_key="SECRET-A", contree_token="SECRET-B",
        tavily_api_key="SECRET-C", github_token="SECRET-D",
    )
    for text in (repr(s), str(s), f"{s!r}", f"{s}"):
        assert "SECRET" not in text
    assert "nebius_api_key='<set>'" in repr(s)
    assert "<unset>" in repr(Settings())


def test_has_reports_presence_without_the_value() -> None:
    s = Settings(tavily_api_key="x")
    assert s.has("tavily_api_key") and not s.has("github_token")


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)  # no real .env is read in tests
    get_settings.cache_clear()
    monkeypatch.setenv("FIXEDYET_MAX_TURNS", "4")
    try:
        first = get_settings()
        assert first is get_settings()
        assert first.max_turns == 4
    finally:
        get_settings.cache_clear()


def test_total_budget_comes_from_the_env_and_dot_env_example_sets_20() -> None:
    assert Settings.load(env={}).total_budget_usd == 20.0
    assert Settings.load(env={"FIXEDYET_TOTAL_BUDGET_USD": "0"}).total_budget_usd == 0.0
    example = (config.PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    env = dict(
        line.split("=", 1)[0:2] for line in example.splitlines() if "=" in line and line[0] != "#"
    )
    assert env["FIXEDYET_TOTAL_BUDGET_USD"].split("#")[0].strip() == "20"
