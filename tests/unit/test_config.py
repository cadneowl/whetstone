from pathlib import Path

import pytest

from whetstone.config import find_config, load_config

TOML = """
[skills]
root = "registry"
repo = "../company-skills"

[git]
default_base = "trunk"
author = "principal"

[ui]
port = 9000
read_only = true

[runs]
max_llm_calls_per_run = 50
"""


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WHETSTONE_SKILLS_ROOT",
        "WHETSTONE_SKILLS_REPO",
        "WHETSTONE_RUNS_DIR",
        "WHETSTONE_UI_HOST",
        "WHETSTONE_UI_PORT",
        "WHETSTONE_READ_ONLY",
        "WHETSTONE_PRACTICE_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_without_a_file(tmp_path: Path) -> None:
    cfg = load_config(start=tmp_path)
    assert cfg.skills.root == Path("skills")
    assert cfg.ui.host == "127.0.0.1"
    assert cfg.ui.port == 8787
    assert cfg.ui.read_only is False
    assert cfg.ui.trust_proxy_headers is False  # never trust identity headers by default


def test_reads_every_section(tmp_path: Path) -> None:
    (tmp_path / "whetstone.toml").write_text(TOML, encoding="utf-8")
    cfg = load_config(start=tmp_path)
    assert cfg.skills.root == Path("registry")
    assert cfg.git.default_base == "trunk"
    assert cfg.git.author == "principal"
    assert cfg.ui.port == 9000
    assert cfg.ui.read_only is True
    assert cfg.runs.max_llm_calls_per_run == 50


def test_partial_file_keeps_defaults(tmp_path: Path) -> None:
    (tmp_path / "whetstone.toml").write_text("[ui]\nport = 1234\n", encoding="utf-8")
    cfg = load_config(start=tmp_path)
    assert cfg.ui.port == 1234
    assert cfg.git.default_base == "main"
    assert cfg.skills.root == Path("skills")


def test_relative_paths_resolve_against_the_config_file(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / "whetstone.toml").write_text(TOML, encoding="utf-8")
    cfg = load_config(start=nested)
    # Discovered two levels up, so "registry" means <tmp_path>/registry, not <nested>/registry.
    assert cfg.skills_root == (tmp_path / "registry").resolve()
    assert cfg.skills_repo == (tmp_path.parent / "company-skills").resolve()


def test_absolute_paths_pass_through(tmp_path: Path) -> None:
    absolute = (tmp_path / "elsewhere").resolve()
    (tmp_path / "whetstone.toml").write_text(
        f'[skills]\nroot = "{absolute.as_posix()}"\n', encoding="utf-8"
    )
    assert load_config(start=tmp_path).skills_root == absolute


def test_find_config_walks_upward(tmp_path: Path) -> None:
    nested = tmp_path / "x" / "y" / "z"
    nested.mkdir(parents=True)
    (tmp_path / "whetstone.toml").write_text("", encoding="utf-8")
    assert find_config(nested) == tmp_path / "whetstone.toml"


def test_find_config_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_config(tmp_path) is None


def test_explicit_path_wins_over_discovery(tmp_path: Path) -> None:
    (tmp_path / "whetstone.toml").write_text("[ui]\nport = 1\n", encoding="utf-8")
    other = tmp_path / "other.toml"
    other.write_text("[ui]\nport = 2\n", encoding="utf-8")
    assert load_config(other, start=tmp_path).ui.port == 2


def test_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "whetstone.toml").write_text(TOML, encoding="utf-8")
    monkeypatch.setenv("WHETSTONE_UI_PORT", "7777")
    monkeypatch.setenv("WHETSTONE_READ_ONLY", "false")
    cfg = load_config(start=tmp_path)
    assert cfg.ui.port == 7777
    assert cfg.ui.read_only is False  # env can turn a file setting off, not just on


def test_env_path_resolves_against_cwd_not_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "whetstone.toml").write_text(TOML, encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("WHETSTONE_SKILLS_ROOT", "from-env")
    assert load_config(start=tmp_path).skills_root == (workdir / "from-env").resolve()


@pytest.mark.parametrize("value,expected", [("1", True), ("yes", True), ("TRUE", True),
                                            ("0", False), ("no", False)])
def test_bool_env_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("WHETSTONE_PRACTICE_MODE", value)
    assert load_config(start=tmp_path).ui.practice_mode is expected


def test_empty_env_does_not_disable_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty variable is unset, not false.

    `WHETSTONE_READ_ONLY=` is a shell-quoting accident, and reading it as false would silently
    re-enable every mutating route on a console the config file deliberately locked down.
    """
    (tmp_path / "whetstone.toml").write_text("[ui]\nread_only = true\n", encoding="utf-8")
    monkeypatch.setenv("WHETSTONE_READ_ONLY", "")
    assert load_config(start=tmp_path).ui.read_only is True


def test_gate_tolerances_are_read_from_the_file(tmp_path: Path) -> None:
    (tmp_path / "whetstone.toml").write_text(
        "[gate]\nrecall_tol = 0.05\nfp_tol = 0.02\n", encoding="utf-8"
    )
    cfg = load_config(start=tmp_path)
    assert (cfg.gate.recall_tol, cfg.gate.fp_tol) == (0.05, 0.02)


def test_gates_dir_resolves_against_the_config_file(tmp_path: Path) -> None:
    (tmp_path / "whetstone.toml").write_text("[gate]\ndir = 'evidence'\n", encoding="utf-8")
    assert load_config(start=tmp_path).gates_dir == (tmp_path / "evidence").resolve()


def test_gates_dir_defaults_next_to_the_runs(tmp_path: Path) -> None:
    assert load_config(start=tmp_path).gate.dir == Path(".whetstone/gates")


def test_the_environment_can_move_the_gate_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHETSTONE_GATES_DIR", str(tmp_path / "elsewhere"))
    assert load_config(start=tmp_path).gates_dir == (tmp_path / "elsewhere").resolve()


# --- the output cap ---------------------------------------------------------------


def test_the_output_cap_is_read_from_the_llm_block(tmp_path: Path) -> None:
    """`[llm] max_tokens` — the knob a truncated improve draft sends an operator to."""
    (tmp_path / "whetstone.toml").write_text(
        "[llm]\nprovider = 'anthropic'\nmax_tokens = 32000\n", encoding="utf-8"
    )
    assert load_config(start=tmp_path).llm.max_tokens == 32000


def test_an_unset_cap_leaves_the_client_default(tmp_path: Path) -> None:
    """None, not 0: every client keeps its own default rather than being capped at nothing."""
    assert load_config(start=tmp_path).llm.max_tokens is None


def test_a_cap_below_one_is_refused_by_the_config(tmp_path: Path) -> None:
    (tmp_path / "whetstone.toml").write_text("[llm]\nmax_tokens = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_tokens"):
        load_config(start=tmp_path)
