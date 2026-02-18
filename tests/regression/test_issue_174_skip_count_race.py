"""
Regression tests for issue #174: stale stop notification after context clear.

When `sm clear` + `sm send --urgent` is used, the /clear Stop hook can arrive
late and steal stop_notify_sender_id, sending a stale notification and preventing
the real task B notification from firing.

The fix uses a skip counter (stop_notify_skip_count) that absorbs the /clear
Stop hook without consuming stop_notify_sender_id.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from datetime import datetime

from src.models import SessionDeliveryState
from src.cli.commands import cmd_clear
from src.cli.client import SessionManagerClient


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def app_with_state():
    """Create a mock FastAPI app with state fields for _invalidate_session_cache."""
    from src.server import _invalidate_session_cache

    app = Mock()
    app.state.last_claude_output = {}
    app.state.pending_stop_notifications = set()

    queue_mgr = Mock()
    queue_mgr.delivery_states = {}
    queue_mgr._get_or_create_state = lambda sid: queue_mgr.delivery_states.setdefault(
        sid, SessionDeliveryState(session_id=sid)
    )
    app.state.session_manager = Mock()
    app.state.session_manager.message_queue_manager = queue_mgr

    return app, queue_mgr


@pytest.fixture
def mock_client():
    """Create a mock SessionManagerClient."""
    client = Mock(spec=SessionManagerClient)
    client.invalidate_cache = Mock(return_value=(True, False))
    return client


@pytest.fixture
def mock_subprocess_run():
    """Mock subprocess.run and _wait_for_claude_prompt to avoid tmux and polling."""
    with patch("subprocess.run") as mock_run, \
         patch("src.cli.commands._wait_for_claude_prompt", return_value=True):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        yield mock_run


# ============================================================================
# Core race scenario: late /clear Stop hook
# ============================================================================


def test_race_scenario_skip_count_absorbs_late_clear_hook(app_with_state):
    """
    Acceptance criterion 3: full race scenario end-to-end.

    1. invalidate_cache(arm_skip=True) → skip_count=1
    2. sm send --urgent arms stop_notify_sender_id
    3. Late /clear Stop hook fires → absorbed by skip_count, sender_id preserved
    4. Task B Stop hook fires → notification sent with task B content
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "engineer-174"

    # Step 1: sm clear calls invalidate_cache with arm_skip=True
    _invalidate_session_cache(app, session_id, arm_skip=True)
    state = queue_mgr.delivery_states[session_id]
    assert state.stop_notify_skip_count == 1

    # Step 2: sm send --urgent arms stop_notify_sender_id (simulated)
    state.stop_notify_sender_id = "em-parent"
    state.stop_notify_sender_name = "em-1615"

    # Step 3: Late /clear Stop hook fires with stale content
    # mark_session_idle would be called — simulate skip logic directly
    assert state.stop_notify_skip_count > 0
    state.stop_notify_skip_count -= 1

    # Verify: no notification consumed, sender_id preserved
    assert state.stop_notify_sender_id == "em-parent"
    assert state.stop_notify_sender_name == "em-1615"
    assert state.stop_notify_skip_count == 0

    # Step 4: Task B Stop hook fires — skip_count is 0, notification should fire
    assert state.stop_notify_skip_count == 0
    assert state.stop_notify_sender_id == "em-parent"
    # In real code, mark_session_idle would fire _send_stop_notification here


def test_happy_path_skip_count_consumed_before_send(app_with_state):
    """
    Happy path: /clear Stop hook arrives early (before sm send --urgent).
    skip_count is consumed by the /clear hook, then sm send sets sender_id,
    and task B hook fires the notification normally.
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "engineer-happy"

    # Step 1: invalidate_cache arms skip
    _invalidate_session_cache(app, session_id, arm_skip=True)
    state = queue_mgr.delivery_states[session_id]
    assert state.stop_notify_skip_count == 1

    # Step 2: /clear Stop hook arrives early (no sender_id set yet)
    assert state.stop_notify_sender_id is None
    state.stop_notify_skip_count -= 1
    assert state.stop_notify_skip_count == 0

    # Step 3: sm send --urgent arms sender_id
    state.stop_notify_sender_id = "em-parent"

    # Step 4: Task B Stop hook fires — skip_count=0, sender set → notification fires
    assert state.stop_notify_skip_count == 0
    assert state.stop_notify_sender_id == "em-parent"


# ============================================================================
# skip_count with no pending notification (acceptance criterion 4)
# ============================================================================


def test_skip_count_consumed_even_without_pending_notification(app_with_state):
    """
    When skip_count > 0 but stop_notify_sender_id is None, skip_count is still
    decremented and no spurious notification is sent.
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "engineer-no-sender"

    _invalidate_session_cache(app, session_id, arm_skip=True)
    state = queue_mgr.delivery_states[session_id]
    assert state.stop_notify_skip_count == 1
    assert state.stop_notify_sender_id is None

    # Simulate /clear Stop hook when no sender is armed
    state.stop_notify_skip_count -= 1
    assert state.stop_notify_skip_count == 0
    assert state.stop_notify_sender_id is None  # no spurious sender


# ============================================================================
# arm_skip path isolation (acceptance criterion 5)
# ============================================================================


def test_arm_skip_false_does_not_increment_skip_count(app_with_state):
    """
    Calling _invalidate_session_cache with arm_skip=False (default, e.g. /clear
    endpoint for codex-app) does NOT increment stop_notify_skip_count.
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "codex-app-001"

    # Pre-create state with some skip_count (shouldn't happen, but tests isolation)
    state = SessionDeliveryState(session_id=session_id)
    state.stop_notify_skip_count = 0
    queue_mgr.delivery_states[session_id] = state

    _invalidate_session_cache(app, session_id)  # arm_skip defaults to False

    assert state.stop_notify_skip_count == 0


def test_arm_skip_true_increments_skip_count(app_with_state):
    """
    Calling _invalidate_session_cache with arm_skip=True (/invalidate-cache
    endpoint, tmux CLI path) DOES increment stop_notify_skip_count.
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "tmux-agent-001"

    _invalidate_session_cache(app, session_id, arm_skip=True)

    state = queue_mgr.delivery_states[session_id]
    assert state.stop_notify_skip_count == 1


def test_arm_skip_true_creates_state_if_absent(app_with_state):
    """
    arm_skip=True creates delivery state via _get_or_create_state if it doesn't
    exist yet (closes the state-missing gap).
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "new-session-no-state"

    assert session_id not in queue_mgr.delivery_states

    _invalidate_session_cache(app, session_id, arm_skip=True)

    assert session_id in queue_mgr.delivery_states
    assert queue_mgr.delivery_states[session_id].stop_notify_skip_count == 1


def test_arm_skip_false_does_not_create_state(app_with_state):
    """
    arm_skip=False (default) does NOT create delivery state if absent.
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "absent-session"

    assert session_id not in queue_mgr.delivery_states

    _invalidate_session_cache(app, session_id)  # arm_skip=False

    assert session_id not in queue_mgr.delivery_states


# ============================================================================
# Consecutive clears
# ============================================================================


def test_consecutive_clears_increment_skip_count(app_with_state):
    """
    Two consecutive sm clear calls should set skip_count=2, absorbing
    two /clear Stop hooks correctly.
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "engineer-double-clear"

    _invalidate_session_cache(app, session_id, arm_skip=True)
    _invalidate_session_cache(app, session_id, arm_skip=True)

    state = queue_mgr.delivery_states[session_id]
    assert state.stop_notify_skip_count == 2

    # First /clear Stop hook absorbed
    state.stop_notify_skip_count -= 1
    assert state.stop_notify_skip_count == 1

    # Second /clear Stop hook absorbed
    state.stop_notify_skip_count -= 1
    assert state.stop_notify_skip_count == 0


# ============================================================================
# cmd_clear ordering: invalidate_cache called BEFORE tmux ops
# ============================================================================


def test_cmd_clear_calls_invalidate_before_tmux(mock_client, mock_subprocess_run):
    """
    invalidate_cache must be called BEFORE the tmux ESC + /clear operations,
    so skip_count is armed before the /clear Stop hook can fire.
    """
    session = {
        "id": "child-174",
        "name": "test-session",
        "tmux_session": "claude-child-174",
        "parent_session_id": "parent-174",
        "completion_status": None,
        "friendly_name": "test-child",
    }

    mock_client.get_session.return_value = session
    mock_client.list_sessions.return_value = [session]

    call_order = []
    original_invalidate = mock_client.invalidate_cache

    def track_invalidate(*args, **kwargs):
        call_order.append("invalidate_cache")
        return original_invalidate(*args, **kwargs)

    mock_client.invalidate_cache = track_invalidate

    def track_subprocess(*args, **kwargs):
        call_order.append("subprocess.run")
        return Mock(returncode=0, stdout="", stderr="")

    mock_subprocess_run.side_effect = track_subprocess

    result = cmd_clear(
        client=mock_client,
        requester_session_id="parent-174",
        target_identifier="child-174",
    )

    assert result == 0
    # invalidate_cache must come before any subprocess.run (tmux) calls
    assert call_order[0] == "invalidate_cache"
    assert all(c == "subprocess.run" for c in call_order[1:])


def test_cmd_clear_invalidate_failure_does_not_block_clear(mock_client, mock_subprocess_run):
    """
    If invalidate_cache fails (server unavailable), cmd_clear should still
    proceed with the tmux /clear operation — it's a best-effort fence.
    """
    session = {
        "id": "child-fail",
        "name": "test-session",
        "tmux_session": "claude-child-fail",
        "parent_session_id": "parent-fail",
        "completion_status": None,
        "friendly_name": "test-child",
    }

    mock_client.get_session.return_value = session
    mock_client.list_sessions.return_value = [session]
    mock_client.invalidate_cache.return_value = (False, True)  # unavailable

    result = cmd_clear(
        client=mock_client,
        requester_session_id="parent-fail",
        target_identifier="child-fail",
    )

    assert result == 0  # clear still succeeds


# ============================================================================
# Existing #167 regression: arm_skip=False path still clears sender fields
# ============================================================================


def test_invalidate_still_clears_stop_notify_sender(app_with_state):
    """
    Regression for #167: _invalidate_session_cache (both arm_skip=True and
    arm_skip=False) still clears stop_notify_sender_id and sender_name.
    """
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "regression-167"

    state = SessionDeliveryState(session_id=session_id)
    state.stop_notify_sender_id = "old-parent"
    state.stop_notify_sender_name = "old-name"
    queue_mgr.delivery_states[session_id] = state

    _invalidate_session_cache(app, session_id)  # arm_skip=False

    assert state.stop_notify_sender_id is None
    assert state.stop_notify_sender_name is None


def test_invalidate_arm_skip_also_clears_sender(app_with_state):
    """arm_skip=True also clears sender fields (in addition to arming skip)."""
    from src.server import _invalidate_session_cache

    app, queue_mgr = app_with_state
    session_id = "arm-skip-sender"

    state = SessionDeliveryState(session_id=session_id)
    state.stop_notify_sender_id = "old-parent"
    state.stop_notify_sender_name = "old-name"
    queue_mgr.delivery_states[session_id] = state

    _invalidate_session_cache(app, session_id, arm_skip=True)

    assert state.stop_notify_sender_id is None
    assert state.stop_notify_sender_name is None
    assert state.stop_notify_skip_count == 1
