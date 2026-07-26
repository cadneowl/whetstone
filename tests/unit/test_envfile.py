from __future__ import annotations

import os
from pathlib import Path

import pytest

from whetstone.config import load_config
from whetstone.envfile import ENV_FILE_VAR, find_env_file, load_env_file

pytestmark = pytest.mark.uses_dotenv


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clears the *starting* state; `tests/conftest.py` restores the environment afterwards.
    for name in (ENV_FILE_VAR, "GITLAB_TOKEN", "JIRA_TOKEN", "ANTHROPIC_API_KEY", "HOST",
                 "WHETSTONE_UI_PORT", "WHETSTONE_READ_ONLY", "WHETSTONE_LLM_MODEL", "QUOTED",
                 "WHETSTONE_LLM_BASE_URL", "SPACED", "HASHED", "EXPORTED", "BARE", "EMPTY"):
        monkeypatch.delenv(name, raising=False)


def _env(directory: Path, body: str) -> Path:
    path = directory / ".env"
    path.write_text(body, encoding="utf-8")
    return path


# --- discovery ------------------------------------------------------------------


def test_env_file_is_found_and_loaded(tmp_path: Path) -> None:
    _env(tmp_path, "GITLAB_TOKEN=glpat-from-file\n")
    assert load_env_file(start=tmp_path) == tmp_path / ".env"
    assert os.environ["GITLAB_TOKEN"] == "glpat-from-file"


def test_discovery_walks_upward(tmp_path: Path) -> None:
    # Same rule as `whetstone.toml`: running from a subdirectory behaves like the repo root.
    _env(tmp_path, "JIRA_TOKEN=from-the-root\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_env_file(nested) == tmp_path / ".env"
    load_env_file(start=nested)
    assert os.environ["JIRA_TOKEN"] == "from-the-root"


def test_the_nearest_file_wins(tmp_path: Path) -> None:
    _env(tmp_path, "JIRA_TOKEN=outer\n")
    nested = tmp_path / "inner"
    nested.mkdir()
    _env(nested, "JIRA_TOKEN=inner\n")
    load_env_file(start=nested)
    assert os.environ["JIRA_TOKEN"] == "inner"


def test_no_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_env_file(start=tmp_path) is None


# --- precedence -----------------------------------------------------------------


def test_a_real_environment_variable_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`GITLAB_TOKEN=x whetstone corpus pull` must do what it looks like it does.

    This is also what lets CI inject a secret without editing a file that is not committed.
    """
    monkeypatch.setenv("GITLAB_TOKEN", "from-the-shell")
    _env(tmp_path, "GITLAB_TOKEN=from-the-file\n")
    load_env_file(start=tmp_path)
    assert os.environ["GITLAB_TOKEN"] == "from-the-shell"


def test_loading_twice_changes_nothing(tmp_path: Path) -> None:
    # `load_config` is called several times per command; the second pass must be a no-op.
    _env(tmp_path, "JIRA_TOKEN=first\n")
    load_env_file(start=tmp_path)
    _env(tmp_path, "JIRA_TOKEN=second\n")
    load_env_file(start=tmp_path)
    assert os.environ["JIRA_TOKEN"] == "first"


# --- an explicitly named file ---------------------------------------------------


def test_an_explicit_path_beats_discovery(tmp_path: Path) -> None:
    _env(tmp_path, "JIRA_TOKEN=discovered\n")
    staging = tmp_path / "staging.env"
    staging.write_text("JIRA_TOKEN=staging\n", encoding="utf-8")
    assert load_env_file(staging, start=tmp_path) == staging
    assert os.environ["JIRA_TOKEN"] == "staging"


def test_the_env_var_names_the_file_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # How `--env-file` reaches the `load_config` calls that happen later in the same command.
    staging = tmp_path / "staging.env"
    staging.write_text("JIRA_TOKEN=staging\n", encoding="utf-8")
    _env(tmp_path, "JIRA_TOKEN=discovered\n")
    monkeypatch.setenv(ENV_FILE_VAR, str(staging))
    load_env_file(start=tmp_path)
    assert os.environ["JIRA_TOKEN"] == "staging"


def test_naming_a_file_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    """Silence here sends an operator looking for the bug in their credentials instead."""
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_env_file(tmp_path / "nope.env")


def test_an_empty_env_file_variable_falls_back_to_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty means unset, as everywhere else in config resolution — not "a file named ''".
    _env(tmp_path, "JIRA_TOKEN=discovered\n")
    monkeypatch.setenv(ENV_FILE_VAR, "")
    assert load_env_file(start=tmp_path) == tmp_path / ".env"
    assert os.environ["JIRA_TOKEN"] == "discovered"


# --- values are literal ---------------------------------------------------------


def test_a_token_containing_a_substitution_survives_intact(tmp_path: Path) -> None:
    """dotenv's `${NAME}` expansion has no escape — not backslashes, not quotes of either kind.

    Left on, a credential containing `${` is silently rewritten with no way for its owner to stop
    it. Off, the worst case is a value that visibly reads `${HOST}` because that is what was typed.
    For a file whose whole purpose is secrets, that is the right way round.
    """
    _env(tmp_path, "HOST=example.com\nJIRA_TOKEN=abc${HOST}-xyz\n")
    load_env_file(start=tmp_path)
    assert os.environ["JIRA_TOKEN"] == "abc${HOST}-xyz"


@pytest.mark.parametrize("literal", ["abc$HOST-x", "abc${HOST}-x", r"abc\${HOST}-x"])
def test_no_form_of_dollar_is_rewritten(tmp_path: Path, literal: str) -> None:
    _env(tmp_path, f"HOST=example.com\nJIRA_TOKEN={literal}\n")
    load_env_file(start=tmp_path)
    assert os.environ["JIRA_TOKEN"] == literal


# --- parsing --------------------------------------------------------------------


def test_the_shapes_people_actually_write(tmp_path: Path) -> None:
    _env(
        tmp_path,
        "# a comment\n"
        "\n"
        'QUOTED="quoted value"\n'
        "SPACED = spaced out\n"
        'HASHED="has # inside"\n'
        "export EXPORTED=exported\n",
    )
    load_env_file(start=tmp_path)
    assert os.environ["QUOTED"] == "quoted value"
    assert os.environ["SPACED"] == "spaced out"
    # A token containing '#' must survive; truncating it produces an auth failure nobody can read.
    assert os.environ["HASHED"] == "has # inside"
    assert os.environ["EXPORTED"] == "exported"


def test_a_byte_order_mark_does_not_eat_the_first_variable(tmp_path: Path) -> None:
    """Notepad, VS Code on Windows, and `Set-Content -Encoding utf8` all write a BOM.

    Decoded as plain utf-8 the first key becomes "﻿GITLAB_TOKEN" and vanishes — silently, and
    only ever the first one, which is about the least debuggable failure this file could have.
    """
    (tmp_path / ".env").write_text(
        "GITLAB_TOKEN=first\nJIRA_TOKEN=second\n", encoding="utf-8-sig"
    )
    load_env_file(start=tmp_path)
    assert os.environ["GITLAB_TOKEN"] == "first"
    assert os.environ["JIRA_TOKEN"] == "second"


def test_a_key_with_no_value_is_left_unset(tmp_path: Path) -> None:
    # Setting it to "" would look deliberate, and some settings read an empty string as meaningful.
    _env(tmp_path, "BARE\nEMPTY=\n")
    load_env_file(start=tmp_path)
    assert "BARE" not in os.environ
    assert os.environ["EMPTY"] == ""


# --- reaching the configuration -------------------------------------------------


def test_whetstone_settings_from_env_file_reach_the_config(tmp_path: Path) -> None:
    _env(tmp_path, "WHETSTONE_UI_PORT=9123\nWHETSTONE_READ_ONLY=true\n")
    config = load_config(start=tmp_path)
    assert config.ui.port == 9123
    assert config.ui.read_only is True


def test_the_real_environment_still_beats_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "whetstone.toml").write_text("[ui]\nport = 1111\n", encoding="utf-8")
    _env(tmp_path, "WHETSTONE_UI_PORT=2222\n")
    monkeypatch.setenv("WHETSTONE_UI_PORT", "3333")
    assert load_config(start=tmp_path).ui.port == 3333


def test_env_file_beats_the_toml(tmp_path: Path) -> None:
    # `.env` sits at the environment tier, because its contents *are* environment variables.
    (tmp_path / "whetstone.toml").write_text("[ui]\nport = 1111\n", encoding="utf-8")
    _env(tmp_path, "WHETSTONE_UI_PORT=2222\n")
    assert load_config(start=tmp_path).ui.port == 2222
