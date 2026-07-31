"""The Anthropic path over real HTTP, through the real SDK, at a gateway base URL.

Claude is reached either directly or through a gateway that speaks the Anthropic API, and the two
differ only by base URL. `test_tool_clients.py` verifies the *translation* against a fake SDK
object; this verifies the whole path — the real `anthropic` client, a real socket, real request
bodies — because the failure this guards against is not a translation bug. It is billed traffic
going to the public endpoint while the operator believes it is going to their gateway, which no
in-process fake can catch.

A local server plays the gateway, so this needs no credentials and no network.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from whetstone.agent.loop import run_agent
from whetstone.llm.factory import build_llm_client, resolve_backend
from whetstone.llm.tools import ToolCall, ToolResult, ToolSpec

TOOLS = [
    ToolSpec(
        name="read_skill_file",
        description="Read a page.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
    ToolSpec(name="submit_findings", description="Finish.", input_schema={"type": "object"}),
]


class _Gateway(HTTPServer):
    """Records every request body and the credential it arrived with."""

    seen: list[dict]
    auth: list[str]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        assert self.path.endswith("/v1/messages"), self.path
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server: _Gateway = self.server  # type: ignore[assignment]
        server.seen.append(body)
        server.auth.append(self.headers.get("x-api-key") or "")

        turns = len([m for m in body["messages"] if m["role"] == "user"])
        if turns == 1:
            content = [
                {"type": "text", "text": "Reading the principles."},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "read_skill_file",
                    "input": {"path": "references/principles.md"},
                },
            ]
        else:
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_02",
                    "name": "submit_findings",
                    "input": {"findings": [{"path": "a.java", "line": 15, "message": "unbounded"}]},
                }
            ]
        raw = json.dumps(
            {
                "id": "msg_stub",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "claude-stub"),
                "content": content,
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def gateway() -> Iterator[_Gateway]:
    server = _Gateway(("127.0.0.1", 0), _Handler)
    server.seen, server.auth = [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server: _Gateway) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_a_gateway_base_url_survives_resolution(gateway: _Gateway) -> None:
    """It used to be dropped: `resolve_backend` returned `base_url=None` for Anthropic, so a
    `--base-url` at a gateway silently sent billed traffic to the public endpoint instead."""
    backend = resolve_backend("anthropic", model="claude-sonnet-5", base_url=_url(gateway))
    assert backend.kind == "anthropic"
    assert backend.base_url == _url(gateway)


def test_the_whole_agent_loop_runs_through_an_anthropic_gateway(gateway: _Gateway) -> None:
    client = build_llm_client(
        "anthropic", model="claude-sonnet-5", base_url=_url(gateway), api_key="gw-token"
    )

    def dispatch(call: ToolCall) -> ToolResult:
        return ToolResult(call.id, "P2: result sets must be paginated.")

    answer, trace = run_agent(
        client,
        system="You review code.",
        task="Review this diff.",
        tools=TOOLS,
        dispatch=dispatch,
        terminal_tool="submit_findings",
        max_steps=5,
    )

    assert answer["findings"][0]["line"] == 15
    assert trace.calls == ["read_skill_file(references/principles.md)"]
    assert trace.forced is False

    # The credential reached the gateway, or a working URL would still fail to authenticate.
    assert gateway.auth[0] == "gw-token"

    first, second = gateway.seen
    assert [t["name"] for t in first["tools"]] == ["read_skill_file", "submit_findings"]
    assert first["system"] == "You review code."
    # The result came back as a *user* turn carrying `tool_result` — Anthropic's shape, and the
    # thing most likely to be got wrong, since OpenAI uses a dedicated `tool` role instead.
    last = second["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "toolu_01"
    assert "paginated" in last["content"][0]["content"]
    # ...and the assistant's own turn was replayed with its tool_use block, or it addresses nothing.
    assert second["messages"][-2]["content"][-1]["type"] == "tool_use"


def test_a_forced_final_turn_reaches_the_gateway(gateway: _Gateway) -> None:
    """The anti-hang guard has to work over the wire too, not only against a fake."""
    client = build_llm_client("anthropic", model="claude-sonnet-5", base_url=_url(gateway))
    client.converse("s", [], TOOLS, force_tool="submit_findings")
    assert gateway.seen[0]["tool_choice"] == {"type": "tool", "name": "submit_findings"}
