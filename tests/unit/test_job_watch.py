"""Unit tests for durable external job watches (#377)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.cli.commands import cmd_watch_job_add, cmd_watch_job_cancel, cmd_watch_job_list
from src.message_queue import MessageQueueManager
from src.models import JobWatchRegistration, Session, SessionStatus
from src.server import create_app
from src.session_manager import SessionManager


def noop_create_task(coro):
    """Silently close coroutine without running it."""
    coro.close()
    return MagicMock()


@pytest.fixture
def mock_session_manager():
    """Mock SessionManager."""
    mock = MagicMock()
    mock.sessions = {}
    mock.get_session = MagicMock(return_value=None)
    mock.tmux = MagicMock()
    mock._save_state = MagicMock()
    mock._deliver_direct = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_job_watch.db")


@pytest.fixture
def mq(mock_session_manager, temp_db_path):
    return MessageQueueManager(
        session_manager=mock_session_manager,
        db_path=temp_db_path,
        config={},
        notifier=None,
    )


def _make_session(session_id: str, tmp_path, provider: str = "claude") -> Session:
    return Session(
        id=session_id,
        name=f"{provider}-{session_id}",
        working_dir=str(tmp_path),
        tmux_session=f"{provider}-{session_id}",
        provider=provider,
        log_file=str(tmp_path / f"{session_id}.log"),
        status=SessionStatus.RUNNING,
    )


def test_register_job_watch_persists_and_lists(mq, mock_session_manager, temp_db_path, tmp_path):
    session = _make_session("agent001", tmp_path)
    mock_session_manager.get_session.return_value = session

    with patch("asyncio.create_task", noop_create_task):
        reg = mq.register_job_watch(
            target_session_id=session.id,
            label="checkpoint-build",
            pid=12345,
            file_path=str(tmp_path / "checkpoint.log"),
            progress_regex="bars processed",
            interval_seconds=60,
        )

    assert reg.id in mq._job_watches
    assert mq.list_job_watches(target_session_id=session.id)[0].label == "checkpoint-build"

    conn = sqlite3.connect(temp_db_path)
    row = conn.execute(
        "SELECT target_session_id, label, pid, progress_regex, is_active FROM job_watch_registrations WHERE id = ?",
        (reg.id,),
    ).fetchone()
    conn.close()
    assert row == (session.id, "checkpoint-build", 12345, "bars processed", 1)


def test_register_job_watch_rejects_ruleless_file_watch(mq, mock_session_manager, tmp_path):
    session = _make_session("agent002", tmp_path)
    mock_session_manager.get_session.return_value = session

    with pytest.raises(ValueError, match="pid or at least one regex/exit-code rule"):
        mq.register_job_watch(
            target_session_id=session.id,
            label="bad-watch",
            file_path=str(tmp_path / "only-file.log"),
        )


def test_evaluate_job_watch_notifies_only_on_progress_change(mq, mock_session_manager, tmp_path):
    session = _make_session("agent003", tmp_path)
    mock_session_manager.get_session.return_value = session
    log_path = tmp_path / "progress.log"
    log_path.write_text("bars processed: 10\n")

    reg = JobWatchRegistration(
        id="watch001",
        target_session_id=session.id,
        label="checkpoint-build",
        pid=None,
        file_path=str(log_path),
        progress_regex="bars processed",
        done_regex=None,
        error_regex=None,
        exit_code_file=None,
        interval_seconds=60,
        tail_lines=200,
        tail_on_error=10,
        notify_on_change=True,
        created_at=session.created_at,
    )

    first = mq._evaluate_job_watch(reg)
    assert first["event"] == "progress"
    assert "bars processed: 10" in first["message"]

    second = mq._evaluate_job_watch(reg)
    assert second["event"] is None

    log_path.write_text("bars processed: 10\nbars processed: 20\n")
    third = mq._evaluate_job_watch(reg)
    assert third["event"] == "progress"
    assert "bars processed: 20" in third["message"]


def test_evaluate_job_watch_uses_exit_code_file_after_pid_exit(mq, mock_session_manager, tmp_path):
    session = _make_session("agent004", tmp_path)
    mock_session_manager.get_session.return_value = session
    exit_code_path = tmp_path / "job.exit"
    exit_code_path.write_text("0\n")

    reg = JobWatchRegistration(
        id="watch002",
        target_session_id=session.id,
        label="checkpoint-build",
        pid=99999,
        file_path=None,
        progress_regex=None,
        done_regex=None,
        error_regex=None,
        exit_code_file=str(exit_code_path),
        interval_seconds=60,
        tail_lines=200,
        tail_on_error=10,
        notify_on_change=True,
        created_at=session.created_at,
    )

    with patch.object(mq, "_pid_exists", return_value=False):
        result = mq._evaluate_job_watch(reg)

    assert result["event"] == "completed"
    assert "exited with code 0" in result["message"]
    assert result["deactivate"] is True


def test_recover_job_watches_restores_active_records(mock_session_manager, temp_db_path, tmp_path):
    session = _make_session("agent005", tmp_path)
    mock_session_manager.get_session.return_value = session

    mq1 = MessageQueueManager(mock_session_manager, db_path=temp_db_path, config={}, notifier=None)
    with patch("asyncio.create_task", noop_create_task):
        reg = mq1.register_job_watch(
            target_session_id=session.id,
            label="checkpoint-build",
            pid=23456,
            progress_regex="bars processed",
            interval_seconds=120,
        )

    mq2 = MessageQueueManager(mock_session_manager, db_path=temp_db_path, config={}, notifier=None)
    with patch("asyncio.create_task", noop_create_task):
        asyncio.run(mq2._recover_job_watches())

    assert reg.id in mq2._job_watches
    assert mq2._job_watches[reg.id].label == "checkpoint-build"


def test_job_watch_endpoints_roundtrip(tmp_path):
    session_manager = SessionManager(
        log_dir=str(tmp_path / "logs"),
        state_file=str(tmp_path / "sessions.json"),
        config={},
    )
    session = _make_session("agent006", tmp_path)
    session_manager.sessions[session.id] = session
    queue_mgr = MessageQueueManager(session_manager, db_path=str(tmp_path / "mq.db"), config={}, notifier=None)
    session_manager.message_queue_manager = queue_mgr

    client = TestClient(create_app(session_manager=session_manager))

    with patch("asyncio.create_task", noop_create_task):
        create_response = client.post(
            "/job-watches",
            json={
                "target_session_id": session.id,
                "label": "checkpoint-build",
                "pid": 34567,
                "progress_regex": "bars processed",
                "interval_seconds": 90,
            },
        )

    assert create_response.status_code == 200
    payload = create_response.json()
    watch_id = payload["id"]
    assert payload["target_name"] == session.id or payload["target_name"] == session.name
    assert payload["label"] == "checkpoint-build"

    list_response = client.get(f"/job-watches?target_session_id={session.id}")
    assert list_response.status_code == 200
    assert list_response.json()["watches"][0]["id"] == watch_id

    cancel_response = client.delete(f"/job-watches/{watch_id}")
    assert cancel_response.status_code == 200
    assert cancel_response.json()["is_active"] is False


def test_cmd_watch_job_add_list_and_cancel(capsys, tmp_path):
    client = MagicMock()
    client.create_job_watch.return_value = {
        "ok": True,
        "unavailable": False,
        "data": {
            "id": "watch123",
            "target_session_id": "sess1234",
            "target_name": "maintainer",
            "label": "checkpoint-build",
            "pid": 45678,
            "file_path": str(tmp_path / "job.log"),
            "interval_seconds": 120,
            "notify_on_change": True,
        },
    }
    client.list_job_watches.return_value = [
        {
            "id": "watch123",
            "target_session_id": "sess1234",
            "target_name": "maintainer",
            "label": "checkpoint-build",
            "pid": 45678,
            "interval_seconds": 120,
            "last_event": "progress",
            "is_active": True,
        }
    ]
    client.cancel_job_watch.return_value = {
        "ok": True,
        "unavailable": False,
        "data": {"id": "watch123", "label": "checkpoint-build"},
    }

    rc = cmd_watch_job_add(
        client,
        current_session_id="sess1234",
        target_identifier=None,
        label="checkpoint-build",
        pid=45678,
        file_path=str(tmp_path / "job.log"),
        progress_regex="bars processed",
        done_regex=None,
        error_regex=None,
        exit_code_file=None,
        interval_seconds=120,
        tail_lines=200,
        tail_on_error=10,
        notify_on_change=True,
    )
    assert rc == 0
    client.create_job_watch.assert_called_once()
    assert "Job watch registered: checkpoint-build (watch123)" in capsys.readouterr().out

    rc = cmd_watch_job_list(
        client,
        current_session_id="sess1234",
        target_identifier=None,
        list_all=False,
        include_inactive=False,
        json_output=False,
    )
    assert rc == 0
    client.list_job_watches.assert_called_with(target_session_id="sess1234", include_inactive=False)
    output = capsys.readouterr().out
    assert "checkpoint-build" in output
    assert "watch123" in output

    rc = cmd_watch_job_cancel(client, "watch123")
    assert rc == 0
    client.cancel_job_watch.assert_called_once_with("watch123")
    assert "Cancelled job watch: checkpoint-build (watch123)" in capsys.readouterr().out
