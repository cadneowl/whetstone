"""The `whetstone ui` command's guard rails. Serving itself is covered by tests/api."""

from __future__ import annotations

from typer.testing import CliRunner

from whetstone.cli import _is_loopback, app

runner = CliRunner()


def test_loopback_detection() -> None:
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("localhost")
    assert _is_loopback("::1")
    assert not _is_loopback("0.0.0.0")
    assert not _is_loopback("192.168.1.10")
    assert not _is_loopback("example.com")


def test_public_bind_is_refused_without_acknowledgement() -> None:
    result = runner.invoke(app, ["ui", "--host", "0.0.0.0", "--no-open"])
    assert result.exit_code != 0
    # The console has no auth of its own, so exposing it must be a deliberate act.
    assert "--insecure-bind" in result.output
    assert "no" in result.output and "authentication" in result.output


def test_ui_is_listed_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ui" in result.stdout
