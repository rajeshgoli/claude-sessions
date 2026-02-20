"""Unit tests for sm#241: message_category column + cancel stale context notifications.

Tests cover:
1. Compaction message cancelled on clear (queue_message called with category, then deleted by cancel)
2. Warning/critical cancelled on clear
3. Same-sender sm send messages (category=NULL) preserved on clear
4. Other-sender messages unaffected by clear
5. cancel_context_monitor_messages_from returns correct count
6. Legitimate compaction after clear is NOT cancelled (cancel ran before new message queued)
7. context_reset belt-and-suspenders for unregistered sessions
8. context_reset belt-and-suspenders for registered sessions
"""

import pytest
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.message_queue import MessageQueueManager
from src.models import Session, SessionStatus
from src.server import create_app


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def noop_create_task(coro):
    """Silently close coroutine without running it."""
    coro.close()
    return MagicMock()


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_mq_241.db")


@pytest.fixture
def mock_session_manager_mq():
    mock = MagicMock()
    mock.sessions = {}
    mock.get_session = MagicMock(return_value=None)
    mock.tmux = MagicMock()
    mock._save_state = MagicMock()
    return mock


@pytest.fixture
def message_queue(mock_session_manager_mq, temp_db_path):
    return MessageQueueManager(
        session_manager=mock_session_manager_mq,
        db_path=temp_db_path,
        config={},
        notifier=None,
    )


def _make_session(session_id: str = "abc12345", enabled: bool = True) -> Session:
    s = Session(
        id=session_id,
        name=f"claude-{session_id}",
        working_dir="/tmp/test",
        tmux_session=f"claude-{session_id}",
        provider="claude",
        log_file="/tmp/test.log",
        status=SessionStatus.RUNNING,
    )
    s.context_monitor_enabled = enabled
    s.context_monitor_notify = session_id
    return s


# ---------------------------------------------------------------------------
# 1. MessageQueueManager: cancel_context_monitor_messages_from
# ---------------------------------------------------------------------------


class TestCancelContextMonitorMessagesFrom:
    """Direct unit tests for the new cancel method."""

    def _queue_msg(self, mq, sender, category=None, target="em-session"):
        with patch("asyncio.create_task", noop_create_task):
            return mq.queue_message(
                target_session_id=target,
                text="test message",
                sender_session_id=sender,
                message_category=category,
            )

    def test_cancels_context_monitor_messages_from_sender(self, message_queue):
        """Undelivered context_monitor messages from sender are deleted."""
        self._queue_msg(message_queue, sender="agent-A", category="context_monitor")
        count = message_queue.cancel_context_monitor_messages_from("agent-A")
        assert count == 1

        # Verify actually deleted from DB
        rows = message_queue._execute_query(
            "SELECT COUNT(*) FROM message_queue WHERE sender_session_id = 'agent-A' AND delivered_at IS NULL"
        )
        assert rows[0][0] == 0

    def test_preserves_sm_send_from_same_sender(self, message_queue):
        """sm send messages (category=NULL) from sender are NOT cancelled."""
        self._queue_msg(message_queue, sender="agent-A", category=None)  # sm send
        count = message_queue.cancel_context_monitor_messages_from("agent-A")
        assert count == 0

        rows = message_queue._execute_query(
            "SELECT COUNT(*) FROM message_queue WHERE sender_session_id = 'agent-A' AND delivered_at IS NULL"
        )
        assert rows[0][0] == 1

    def test_returns_correct_count_mixed(self, message_queue):
        """3 context_monitor + 2 sm send from agent-A → cancel returns 3, 2 remain."""
        for _ in range(3):
            self._queue_msg(message_queue, sender="agent-A", category="context_monitor")
        for _ in range(2):
            self._queue_msg(message_queue, sender="agent-A", category=None)

        count = message_queue.cancel_context_monitor_messages_from("agent-A")
        assert count == 3

        remaining = message_queue._execute_query(
            "SELECT COUNT(*) FROM message_queue WHERE sender_session_id = 'agent-A' AND delivered_at IS NULL"
        )
        assert remaining[0][0] == 2

    def test_other_sender_messages_unaffected(self, message_queue):
        """Cancelling agent-A does not touch agent-B's context-monitor messages."""
        self._queue_msg(message_queue, sender="agent-B", category="context_monitor")
        count = message_queue.cancel_context_monitor_messages_from("agent-A")
        assert count == 0

        rows = message_queue._execute_query(
            "SELECT COUNT(*) FROM message_queue WHERE sender_session_id = 'agent-B' AND delivered_at IS NULL"
        )
        assert rows[0][0] == 1

    def test_no_messages_returns_zero(self, message_queue):
        """Nothing queued → returns 0 without error."""
        count = message_queue.cancel_context_monitor_messages_from("agent-X")
        assert count == 0

    def test_delivered_messages_not_cancelled(self, message_queue):
        """Already-delivered context_monitor messages are not touched."""
        msg = self._queue_msg(message_queue, sender="agent-A", category="context_monitor")
        # Mark delivered
        message_queue._execute(
            "UPDATE message_queue SET delivered_at = ? WHERE id = ?",
            (datetime.now().isoformat(), msg.id),
        )
        count = message_queue.cancel_context_monitor_messages_from("agent-A")
        assert count == 0


# ---------------------------------------------------------------------------
# 2. schema: message_category column persisted in DB
# ---------------------------------------------------------------------------


class TestMessageCategorySchema:
    """message_category is stored in and retrievable from the database."""

    def test_message_category_persisted(self, message_queue):
        with patch("asyncio.create_task", noop_create_task):
            msg = message_queue.queue_message(
                target_session_id="em-session",
                text="compaction notice",
                sender_session_id="agent-A",
                message_category="context_monitor",
            )

        rows = message_queue._execute_query(
            "SELECT message_category FROM message_queue WHERE id = ?", (msg.id,)
        )
        assert rows[0][0] == "context_monitor"

    def test_message_category_null_by_default(self, message_queue):
        with patch("asyncio.create_task", noop_create_task):
            msg = message_queue.queue_message(
                target_session_id="em-session",
                text="regular sm send",
                sender_session_id="agent-A",
            )

        rows = message_queue._execute_query(
            "SELECT message_category FROM message_queue WHERE id = ?", (msg.id,)
        )
        assert rows[0][0] is None

    def test_queued_message_dataclass_has_category(self, message_queue):
        with patch("asyncio.create_task", noop_create_task):
            msg = message_queue.queue_message(
                target_session_id="em-session",
                text="test",
                message_category="context_monitor",
            )
        assert msg.message_category == "context_monitor"


# ---------------------------------------------------------------------------
# 3. server.py: queue_message calls tagged with category + sender
# ---------------------------------------------------------------------------


def _make_mock_sm(session):
    mock = MagicMock()
    mock.sessions = {session.id: session}
    mock.get_session = MagicMock(return_value=session)
    mock._save_state = MagicMock()
    mock.message_queue_manager = MagicMock()
    return mock


def _post_event(client, session_id, event, **extra):
    payload = {"session_id": session_id, "event": event, **extra}
    return client.post("/hooks/context-usage", json=payload)


def _post_context(client, session_id, used_pct, **extra):
    payload = {"session_id": session_id, "used_percentage": used_pct, **extra}
    return client.post("/hooks/context-usage", json=payload)


class TestQueueMessageTagging:
    """queue_message calls in context monitor handler pass category + sender."""

    def test_compaction_queue_call_tagged(self):
        session = _make_session("agent-1")
        session.context_monitor_notify = "em-session"
        mock_sm = _make_mock_sm(session)
        client = TestClient(create_app(session_manager=mock_sm))

        _post_event(client, session.id, event="compaction", trigger="auto")

        call_kwargs = mock_sm.message_queue_manager.queue_message.call_args[1]
        assert call_kwargs.get("message_category") == "context_monitor"
        assert call_kwargs.get("sender_session_id") == session.id

    def test_warning_queue_call_tagged(self):
        session = _make_session("agent-1")
        session.context_monitor_notify = "em-session"
        mock_sm = _make_mock_sm(session)
        client = TestClient(create_app(session_manager=mock_sm))

        _post_context(client, session.id, used_pct=55)

        call_kwargs = mock_sm.message_queue_manager.queue_message.call_args[1]
        assert call_kwargs.get("message_category") == "context_monitor"
        assert call_kwargs.get("sender_session_id") == session.id

    def test_critical_queue_call_tagged(self):
        session = _make_session("agent-1")
        session.context_monitor_notify = "em-session"
        mock_sm = _make_mock_sm(session)
        client = TestClient(create_app(session_manager=mock_sm))

        _post_context(client, session.id, used_pct=65)

        call_kwargs = mock_sm.message_queue_manager.queue_message.call_args[1]
        assert call_kwargs.get("message_category") == "context_monitor"
        assert call_kwargs.get("sender_session_id") == session.id


# ---------------------------------------------------------------------------
# 4. context_reset cancels queued messages + works for unregistered sessions
# ---------------------------------------------------------------------------


class TestContextResetCancellation:
    """context_reset event triggers cancel_context_monitor_messages_from."""

    def test_context_reset_calls_cancel(self):
        """context_reset event calls cancel_context_monitor_messages_from on queue_mgr."""
        session = _make_session("agent-1")
        mock_sm = _make_mock_sm(session)
        client = TestClient(create_app(session_manager=mock_sm))

        _post_event(client, session.id, event="context_reset")

        mock_sm.message_queue_manager.cancel_context_monitor_messages_from.assert_called_once_with(
            session.id
        )

    def test_context_reset_unregistered_session_calls_cancel(self):
        """Unregistered session context_reset also cancels and returns flags_reset (not not_registered)."""
        session = _make_session("agent-1", enabled=False)
        mock_sm = _make_mock_sm(session)
        client = TestClient(create_app(session_manager=mock_sm))

        resp = _post_event(client, session.id, event="context_reset")

        assert resp.status_code == 200
        assert resp.json()["status"] == "flags_reset"
        mock_sm.message_queue_manager.cancel_context_monitor_messages_from.assert_called_once_with(
            session.id
        )

    def test_context_reset_returns_flags_reset_not_not_registered(self):
        """Unregistered session context_reset returns flags_reset, not not_registered."""
        session = _make_session("agent-1", enabled=False)
        mock_sm = _make_mock_sm(session)
        client = TestClient(create_app(session_manager=mock_sm))

        resp = _post_event(client, session.id, event="context_reset")
        assert resp.json()["status"] == "flags_reset"

    def test_context_reset_registered_session_also_cancels(self):
        """Registered session context_reset also cancels."""
        session = _make_session("agent-1", enabled=True)
        mock_sm = _make_mock_sm(session)
        client = TestClient(create_app(session_manager=mock_sm))

        resp = _post_event(client, session.id, event="context_reset")

        assert resp.json()["status"] == "flags_reset"
        mock_sm.message_queue_manager.cancel_context_monitor_messages_from.assert_called_once_with(
            session.id
        )

    def test_context_reset_no_queue_mgr_no_crash(self):
        """context_reset without queue_mgr does not crash."""
        session = _make_session("agent-1")
        mock_sm = _make_mock_sm(session)
        mock_sm.message_queue_manager = None
        client = TestClient(create_app(session_manager=mock_sm))

        resp = _post_event(client, session.id, event="context_reset")
        assert resp.status_code == 200
        assert resp.json()["status"] == "flags_reset"


# ---------------------------------------------------------------------------
# 5. _invalidate_session_cache cancels context_monitor messages
# ---------------------------------------------------------------------------


class TestInvalidateSessionCacheCancel:
    """_invalidate_session_cache calls cancel_context_monitor_messages_from."""

    def test_invalidate_cache_endpoint_calls_cancel(self):
        """POST /sessions/{id}/invalidate-cache triggers cancel_context_monitor_messages_from."""
        session = _make_session("agent-1")
        mock_sm = _make_mock_sm(session)
        client = TestClient(create_app(session_manager=mock_sm))

        resp = client.post(f"/sessions/{session.id}/invalidate-cache")
        assert resp.status_code == 200

        mock_sm.message_queue_manager.cancel_context_monitor_messages_from.assert_called_with(
            session.id
        )


# ---------------------------------------------------------------------------
# 6. Legitimate compaction after clear is delivered (not retroactively cancelled)
# ---------------------------------------------------------------------------


class TestLegitimateCompactionAfterClear:
    """New compaction after clear is not cancelled (cancel ran before new message)."""

    def test_cancel_does_not_affect_messages_queued_after_cancel(self, message_queue):
        """Messages queued AFTER cancel() are not retroactively deleted."""
        # Step 1: queue a compaction message, then cancel it
        with patch("asyncio.create_task", noop_create_task):
            message_queue.queue_message(
                target_session_id="em-session",
                text="old compaction",
                sender_session_id="agent-A",
                message_category="context_monitor",
            )
        cancelled = message_queue.cancel_context_monitor_messages_from("agent-A")
        assert cancelled == 1

        # Step 2: new compaction fires AFTER the clear
        with patch("asyncio.create_task", noop_create_task):
            message_queue.queue_message(
                target_session_id="em-session",
                text="new compaction",
                sender_session_id="agent-A",
                message_category="context_monitor",
            )

        # The new message should still be in the queue
        rows = message_queue._execute_query(
            "SELECT COUNT(*) FROM message_queue WHERE sender_session_id = 'agent-A' AND delivered_at IS NULL"
        )
        assert rows[0][0] == 1
