"""Unit tests for cmd_codex_tui command wiring."""

from unittest.mock import MagicMock, patch

from src.cli.commands import cmd_codex_tui


def test_cmd_codex_tui_unavailable_returns_2(capsys):
    client = MagicMock()
    client.get_session.return_value = None
    client.list_sessions.return_value = None
    client.get_rollout_flags.return_value = {}

    rc = cmd_codex_tui(client, "missing")

    assert rc == 2
    assert "unavailable" in capsys.readouterr().err.lower()


def test_cmd_codex_tui_rejects_non_codex_app(capsys):
    client = MagicMock()
    client.get_session.return_value = {
        "id": "abc123",
        "provider": "claude",
        "name": "claude-abc123",
    }
    client.get_rollout_flags.return_value = {}

    rc = cmd_codex_tui(client, "abc123")

    assert rc == 1
    assert "provider=codex-app" in capsys.readouterr().err


def test_cmd_codex_tui_calls_runner():
    client = MagicMock()
    client.get_session.return_value = {
        "id": "abc123",
        "provider": "codex-app",
        "name": "codex-app-abc123",
    }
    client.get_rollout_flags.return_value = {"enable_codex_tui": True}
    with patch("src.cli.codex_tui.run_codex_tui", return_value=0) as runner:
        rc = cmd_codex_tui(client, "abc123", poll_interval=0.5, event_limit=50)

    assert rc == 0
    runner.assert_called_once_with(
        client=client,
        session_id="abc123",
        poll_interval=0.5,
        event_limit=50,
    )


def test_cmd_codex_tui_rollout_disabled(capsys):
    client = MagicMock()
    client.get_session.return_value = {
        "id": "abc123",
        "provider": "codex-app",
        "name": "codex-app-abc123",
    }
    client.get_rollout_flags.return_value = {"enable_codex_tui": False}

    rc = cmd_codex_tui(client, "abc123")

    assert rc == 1
    assert "rollout" in capsys.readouterr().err.lower()
