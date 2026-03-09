from __future__ import annotations

import json
from unittest.mock import Mock

from fastapi.testclient import TestClient

from src.cli.commands import cmd_maintainer, resolve_session_id
from src.models import Session, SessionStatus
from src.server import create_app
from src.session_manager import SessionManager


def _manager(tmp_path) -> SessionManager:
    return SessionManager(
        log_dir=str(tmp_path / "logs"),
        state_file=str(tmp_path / "sessions.json"),
        config={},
    )


def _session(session_id: str, tmp_path) -> Session:
    return Session(
        id=session_id,
        name=f"claude-{session_id}",
        working_dir=str(tmp_path),
        tmux_session=f"claude-{session_id}",
        provider="claude",
        log_file=str(tmp_path / f"{session_id}.log"),
        status=SessionStatus.RUNNING,
    )


def test_set_maintainer_persists_and_roundtrips(tmp_path):
    manager = _manager(tmp_path)
    session = _session("maint123", tmp_path)
    manager.sessions[session.id] = session

    assert manager.set_maintainer_session(session.id) is True
    assert manager.get_session_aliases(session.id) == ["maintainer"]
    state_data = json.loads((tmp_path / "sessions.json").read_text())
    assert state_data["maintainer_session_id"] == session.id


def test_list_sessions_exposes_maintainer_alias(tmp_path):
    manager = _manager(tmp_path)
    session = _session("maint123", tmp_path)
    manager.sessions[session.id] = session
    manager.set_maintainer_session(session.id)

    client = TestClient(create_app(session_manager=manager))
    response = client.get("/sessions")

    assert response.status_code == 200
    payload = response.json()["sessions"][0]
    assert payload["aliases"] == ["maintainer"]
    assert payload["is_maintainer"] is True


def test_put_maintainer_requires_self_auth(tmp_path):
    manager = _manager(tmp_path)
    session = _session("maint123", tmp_path)
    manager.sessions[session.id] = session
    client = TestClient(create_app(session_manager=manager))

    response = client.put(f"/sessions/{session.id}/maintainer", json={"requester_session_id": "other"})

    assert response.status_code == 400
    assert "self-directed" in response.json()["detail"]


def test_resolve_session_id_matches_alias():
    client = Mock()
    client.get_session.return_value = None
    client.list_sessions.return_value = [
        {"id": "maint123", "friendly_name": "codex-ops", "aliases": ["maintainer"]},
    ]

    resolved_id, resolved_session = resolve_session_id(client, "maintainer")

    assert resolved_id == "maint123"
    assert resolved_session["friendly_name"] == "codex-ops"


def test_cmd_maintainer_registers_alias(capsys):
    client = Mock()
    client.set_maintainer.return_value = (True, False)

    rc = cmd_maintainer(client, "maint123")

    assert rc == 0
    client.set_maintainer.assert_called_once_with("maint123")
    assert "maintainer -> maint123" in capsys.readouterr().out


def test_cmd_maintainer_clear(capsys):
    client = Mock()
    client.clear_maintainer.return_value = (True, False)

    rc = cmd_maintainer(client, "maint123", clear=True)

    assert rc == 0
    client.clear_maintainer.assert_called_once_with("maint123")
    assert "cleared" in capsys.readouterr().out
