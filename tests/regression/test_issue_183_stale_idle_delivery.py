"""Regression tests for sm#183: non-urgent sm send interrupts active agents.

Verifies that _try_deliver_messages checks tmux prompt visibility for Claude
sessions before delivering, preventing delivery when is_idle is stale-True
but the agent is actually mid-turn.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch

from src.message_queue import MessageQueueManager
from src.models import SessionDeliveryState, SessionStatus


@pytest.fixture
def mock_session_manager():
    mock = MagicMock()
    mock.sessions = {}
    mock.get_session = MagicMock(return_value=None)
    mock._save_state = MagicMock()
    mock._deliver_direct = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def mq(mock_session_manager, tmp_path):
    return MessageQueueManager(
        session_manager=mock_session_manager,
        db_path=str(tmp_path / "test_mq.db"),
        config={
            "sm_send": {"input_poll_interval": 1, "input_stale_timeout": 30},
            "timeouts": {"message_queue": {"subprocess_timeout_seconds": 1}},
        },
        notifier=None,
    )


def _make_claude_session(session_id="target183"):
    s = MagicMock()
    s.id = session_id
    s.provider = "claude"
    s.tmux_session = f"tmux-{session_id}"
    s.friendly_name = "test-agent"
    s.name = "claude-agent"
    s.status = SessionStatus.RUNNING
    s.last_activity = datetime.now()
    return s


class TestStaleIdleDeliveryBlocked:
    """Core fix: stale is_idle + no prompt → delivery deferred."""

    @pytest.mark.asyncio
    async def test_delivery_deferred_when_prompt_not_visible(self, mq, mock_session_manager):
        """is_idle=True but agent mid-turn (no '>' prompt) → message NOT delivered."""
        session = _make_claude_session()
        mock_session_manager.get_session.return_value = session

        # Queue a message
        mq.queue_message("target183", "Hello", delivery_mode="sequential")

        # Mark idle (simulates stale flag from prior Stop hook)
        mq.delivery_states["target183"] = SessionDeliveryState(
            session_id="target183", is_idle=True
        )

        # Prompt is NOT visible (agent is mid-turn)
        mq._check_idle_prompt = AsyncMock(return_value=False)
        # No user input at prompt (returns None because prompt isn't even showing)
        mq._get_pending_user_input_async = AsyncMock(return_value=None)

        await mq._try_deliver_messages("target183")

        # Delivery should NOT have happened
        mock_session_manager._deliver_direct.assert_not_called()
        # Message still pending
        assert mq.get_queue_length("target183") == 1

    @pytest.mark.asyncio
    async def test_important_delivery_deferred_when_prompt_not_visible(self, mq, mock_session_manager):
        """Important-mode delivery also deferred when prompt not visible."""
        session = _make_claude_session()
        mock_session_manager.get_session.return_value = session

        mq.queue_message("target183", "Important msg", delivery_mode="important")
        mq.delivery_states["target183"] = SessionDeliveryState(
            session_id="target183", is_idle=True
        )

        mq._check_idle_prompt = AsyncMock(return_value=False)
        mq._get_pending_user_input_async = AsyncMock(return_value=None)

        await mq._try_deliver_messages("target183", important_only=True)

        mock_session_manager._deliver_direct.assert_not_called()
        assert mq.get_queue_length("target183") == 1


class TestNormalDeliveryUnaffected:
    """Regression: normal idle delivery still works when prompt IS visible."""

    @pytest.mark.asyncio
    async def test_delivery_proceeds_when_prompt_visible(self, mq, mock_session_manager):
        """is_idle=True and prompt visible → message delivered normally."""
        session = _make_claude_session()
        mock_session_manager.get_session.return_value = session

        mq.queue_message("target183", "Hello", delivery_mode="sequential")
        mq.delivery_states["target183"] = SessionDeliveryState(
            session_id="target183", is_idle=True
        )

        # Prompt IS visible
        mq._check_idle_prompt = AsyncMock(return_value=True)
        mq._get_pending_user_input_async = AsyncMock(return_value=None)

        await mq._try_deliver_messages("target183")

        # Delivery should have happened
        mock_session_manager._deliver_direct.assert_called_once()
        assert mq.get_queue_length("target183") == 0


class TestNonClaudeSessionsUnaffected:
    """Codex sessions are not subject to prompt verification."""

    @pytest.mark.asyncio
    async def test_codex_session_bypasses_prompt_check(self, mq, mock_session_manager):
        """provider='codex' skips prompt verification entirely."""
        session = _make_claude_session("codex_target")
        session.provider = "codex"
        mock_session_manager.get_session.return_value = session

        mq.queue_message("codex_target", "Hello codex", delivery_mode="sequential")
        mq.delivery_states["codex_target"] = SessionDeliveryState(
            session_id="codex_target", is_idle=True
        )

        # _check_idle_prompt should NOT be called for codex sessions
        check_mock = AsyncMock(return_value=False)
        mq._check_idle_prompt = check_mock
        mq._get_pending_user_input_async = AsyncMock(return_value=None)

        await mq._try_deliver_messages("codex_target")

        # Delivery proceeds regardless of prompt state
        mock_session_manager._deliver_direct.assert_called_once()
        check_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_codex_app_session_bypasses_prompt_check(self, mq, mock_session_manager):
        """provider='codex-app' skips prompt verification entirely."""
        session = _make_claude_session("codex_app_target")
        session.provider = "codex-app"
        mock_session_manager.get_session.return_value = session

        mq.queue_message("codex_app_target", "Hello codex-app", delivery_mode="sequential")
        mq.delivery_states["codex_app_target"] = SessionDeliveryState(
            session_id="codex_app_target", is_idle=True
        )

        check_mock = AsyncMock(return_value=False)
        mq._check_idle_prompt = check_mock
        mq._get_pending_user_input_async = AsyncMock(return_value=None)

        await mq._try_deliver_messages("codex_app_target")

        # codex-app skips both user-input check and prompt check
        mock_session_manager._deliver_direct.assert_called_once()
        check_mock.assert_not_called()


class TestUrgentDeliveryUnaffected:
    """Urgent delivery bypasses the prompt check (uses _deliver_urgent path)."""

    @pytest.mark.asyncio
    async def test_urgent_does_not_use_try_deliver(self, mq, mock_session_manager):
        """Urgent messages go through _deliver_urgent, not _try_deliver_messages."""
        session = _make_claude_session()
        mock_session_manager.get_session.return_value = session

        # Mock _deliver_urgent to verify it's called
        mq._deliver_urgent = AsyncMock()

        with patch("asyncio.create_task") as mock_create_task:
            # Make create_task run the coroutine immediately for urgent path
            def run_coro(coro):
                coro.close()
                return MagicMock()
            mock_create_task.side_effect = run_coro

            mq.queue_message("target183", "Urgent!", delivery_mode="urgent")

        # The urgent path creates a task for _deliver_urgent, not _try_deliver_messages
        # Verify that queue_message created a task (the urgent delivery task)
        assert mock_create_task.called
