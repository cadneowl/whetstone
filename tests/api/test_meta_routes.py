from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whetstone.config import Config, LLMConfig, SkillsConfig, UIConfig
from whetstone.runs import RunStore
from whetstone.ui.app import create_app


def test_config_reports_identity_and_capabilities(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert body["read_only"] is False
    assert body["practice_mode"] is False
    assert body["principal"]["mode"] == "local"
    assert body["principal"]["name"] == "Tester"  # from the repo's git config
    assert any(b["name"] == "ollama" for b in body["backends"])


def test_read_only_is_advertised(config: Config, store: RunStore) -> None:
    config.ui.read_only = True
    with TestClient(create_app(config, store=store)) as client:
        assert client.get("/api/config").json()["read_only"] is True


def test_proxy_identity_is_ignored_unless_trusted(config: Config, store: RunStore) -> None:
    # Without opt-in, a forged header must not become an identity.
    with TestClient(create_app(config, store=store)) as client:
        body = client.get("/api/config", headers={"X-Forwarded-User": "attacker"}).json()
    assert body["principal"]["name"] == "Tester"
    assert body["principal"]["mode"] == "local"


def test_proxy_identity_is_used_when_trusted(config: Config, store: RunStore) -> None:
    config.ui.trust_proxy_headers = True
    with TestClient(create_app(config, store=store)) as client:
        body = client.get(
            "/api/config",
            headers={"X-Forwarded-User": "dana", "X-Forwarded-Email": "dana@example.com"},
        ).json()
    assert body["principal"] == {"name": "dana", "email": "dana@example.com", "mode": "proxy"}


def test_trusted_proxy_without_headers_is_anonymous(config: Config, store: RunStore) -> None:
    config.ui.trust_proxy_headers = True
    with TestClient(create_app(config, store=store)) as client:
        body = client.get("/api/config").json()
    assert body["principal"]["mode"] == "anonymous"


def test_model_defaults_to_anthropic_with_no_override(client: TestClient) -> None:
    body = client.get("/api/config/model").json()
    # No `[llm]` set: the override is empty and resolution falls to the built-in default.
    assert body["provider"] == ""
    assert body["resolved_backend"] == "anthropic"
    assert body["resolved_model"]  # the DEFAULT_MODEL
    assert body["billing"] == "billed"
    assert any(b["name"] == "ollama" for b in body["available"])


def test_config_seeds_the_model_override(
    config: Config, store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHETSTONE_LLM_MODEL", "should-be-overridden")
    config.llm = LLMConfig(provider="anthropic", model="claude-from-config")
    with TestClient(create_app(config, store=store)) as client:
        body = client.get("/api/config/model").json()
    # The file's `[llm]` wins over the environment — it is the deployment's explicit default.
    assert body["provider"] == "anthropic"
    assert body["resolved_model"] == "claude-from-config"


def test_setting_the_model_changes_what_a_run_resolves_to(client: TestClient) -> None:
    put = client.put("/api/config/model", json={"provider": "anthropic", "model": "claude-picked"})
    assert put.status_code == 200
    assert put.json()["resolved_model"] == "claude-picked"

    # It sticks for the next reader…
    assert client.get("/api/config/model").json()["resolved_model"] == "claude-picked"
    # …and every launch the console plans now quotes it, which is the whole point.
    plan = client.post("/api/jobs/review/plan", json={"skill_id": "rust-errors"}).json()
    assert plan["model"] == "claude-picked"


def test_clearing_the_provider_returns_to_the_default(client: TestClient) -> None:
    client.put("/api/config/model", json={"provider": "anthropic", "model": "claude-picked"})
    body = client.put("/api/config/model", json={"provider": "", "model": ""}).json()
    assert body["provider"] == ""
    assert body["resolved_backend"] == "anthropic"
    assert body["resolved_model"] != "claude-picked"


def test_unknown_provider_is_refused(client: TestClient) -> None:
    response = client.put("/api/config/model", json={"provider": "gpt5-local", "model": "m"})
    assert response.status_code == 422
    assert "unknown provider" in response.json()["message"]


def test_provider_needing_a_model_without_one_is_refused(client: TestClient) -> None:
    # `openai` has no default model, so choosing it with an empty model must fail at the click.
    response = client.put("/api/config/model", json={"provider": "openai", "model": ""})
    assert response.status_code == 422
    assert "needs a model" in response.json()["message"]


def test_base_url_is_never_taken_from_the_browser(client: TestClient) -> None:
    # `custom` needs a base URL, and one cannot be supplied over the wire — so it is refused rather
    # than silently letting the browser point model traffic somewhere.
    response = client.put("/api/config/model", json={"provider": "custom", "model": "m"})
    assert response.status_code == 422
    assert "unknown provider" in response.json()["message"] or "base URL" in response.json()[
        "message"
    ]


def test_setting_the_model_is_blocked_read_only(config: Config, store: RunStore) -> None:
    config.ui.read_only = True
    with TestClient(create_app(config, store=store)) as client:
        response = client.put("/api/config/model", json={"provider": "anthropic", "model": "x"})
    assert response.status_code == 403


def test_git_status_reports_the_repo(client: TestClient) -> None:
    body = client.get("/api/git/status").json()
    assert body["available"] is True
    assert body["status"]["branch"] == "main"
    assert body["status"]["clean"] is True


def test_git_status_degrades_outside_a_repo(tmp_path: Path, store: RunStore) -> None:
    loose = Config(skills=SkillsConfig(root=tmp_path / "skills", repo=tmp_path / "not-a-repo"))
    with TestClient(create_app(loose, store=store)) as client:
        body = client.get("/api/git/status").json()
    # Read-only browsing still works without git; only proposing would be unavailable.
    assert body["available"] is False
    assert body["message"]


def test_openapi_schema_is_served(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/skills" in schema["paths"]
    assert "/api/runs/{run_id}" in schema["paths"]


def test_placeholder_page_when_assets_are_not_built(
    config: Config, store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("whetstone.ui.app.STATIC_DIR", tmp_path / "unbuilt")
    with TestClient(create_app(config, store=store)) as client:
        body = client.get("/").text
    # The API still works without a frontend build; say so instead of failing opaquely.
    assert "Console assets not built" in body
    assert "npm run build" in body


def test_unmatched_api_path_is_json_404_not_the_spa(client: TestClient) -> None:
    response = client.get("/api/nonsense")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_ui_config_defaults_are_safe() -> None:
    ui = UIConfig()
    assert ui.host == "127.0.0.1"
    assert ui.trust_proxy_headers is False


def test_dev_mode_serves_no_console(config: Config, store: RunStore) -> None:
    # `whetstone ui --dev` proxies through Vite, so the API port must not answer with a stale build
    # that looks live.
    with TestClient(create_app(config, store=store, serve_console=False)) as client:
        assert client.get("/api/skills").status_code == 200
        assert client.get("/").status_code == 404
        assert client.get("/triage").status_code == 404
