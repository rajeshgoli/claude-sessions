# 168: sm wait times out for Codex CLI sessions (no idle detection)

## Problem

`sm wait <codex-session>` always times out, even after the Codex agent has finished responding. The watcher receives repeated `[sm wait] Timeout: <name> still active after Ns` messages.

### Reproduction

1. Have a Codex CLI session (`provider: "codex"`) running in tmux
2. Send it a task: `sm send <codex-session> "do something"`
3. Watch it: `sm wait <codex-session> 300`
4. Codex agent completes the task
5. **Bug:** `sm wait` reports timeout instead of idle

Observed with `doc-reviewer-167` (`provider: "codex"`, session `c1d607d3`) — repeated 300s timeout notifications even though the agent had finished and responded via `sm send`.

## Root Cause

`sm wait` polls `delivery_states[session_id].is_idle` via `_watch_for_idle()` (message_queue.py:1102-1151). The polling loop checks `state.is_idle` every `watch_poll_interval` seconds until either idle is detected or timeout is reached (message_queue.py:1115-1151).

For Claude sessions, `mark_session_idle()` is called when the Stop hook fires (server.py:1321). This sets `state.is_idle = True` and triggers stop notifications.

**Codex CLI sessions (`provider: "codex"`) have no hooks.** No Stop or Notification hooks fire when Codex finishes. The codebase explicitly acknowledges this at message_queue.py:405: `"Codex CLI sessions have no hooks so idle detection never triggers."`

For message delivery, this is worked around by setting `state.is_idle = True` directly when a message is queued for a Codex session (message_queue.py:415-421):

```python
elif is_codex:
    # Codex: set idle flag and deliver immediately, but skip the
    # stop-notification side effects of mark_session_idle() since
    # this isn't a real stop event.
    state = self._get_or_create_state(target_session_id)
    state.is_idle = True
    asyncio.create_task(self._try_deliver_messages(target_session_id))
```

But `sm wait`'s `_watch_for_idle` polling loop has no equivalent workaround. After a Codex session finishes responding, nothing sets `is_idle = True`, so the watch loop runs until timeout.

### Why `is_idle` is False

When a message is delivered to a Codex session, `_try_deliver_messages` calls `mark_session_active(target_session_id)` (message_queue.py:413), setting `is_idle = False`. After Codex processes the message, no hook fires to set it back to `True`. The transient `True` set at line 420 during `queue_message` is only for that specific delivery check.

## Proposed Fix

In `_watch_for_idle`, when the target is a Codex CLI session, use `session.status` as the idle signal instead of `delivery_states[session_id].is_idle`.

1. **In `_watch_for_idle()`** (message_queue.py:1102-1151):
   - After checking `state.is_idle`, add a fallback for Codex sessions:
     - Look up the session via `self.session_manager.get_session(target_session_id)`
     - If `session.provider == "codex"` and `session.status == SessionStatus.IDLE`, treat it as idle
   - This leverages the existing `session.status` field, which is updated by the OutputMonitor's tmux pane monitoring independently of hooks

2. **Verify OutputMonitor sets status for Codex sessions:**
   - OutputMonitor watches tmux pane output via `pipe-pane` log files for all tmux-based sessions (both Claude and Codex)
   - Confirm that it updates `session.status` to `IDLE` when Codex finishes — if not, this is a prerequisite fix

## Scope

- `src/message_queue.py` — `_watch_for_idle()`: add Codex session status fallback
- Possibly `src/output_monitor.py` — verify Codex idle detection via tmux output

## Edge Cases

1. **OutputMonitor not running**: If OutputMonitor is not monitoring the Codex session (e.g., session created without monitoring), the status fallback won't help. The watch would still timeout, which is the same behavior as today — no regression.

2. **Race between status update and watch check**: `session.status` may lag behind the actual Codex state by one poll interval. This is acceptable — `sm wait` is already polling on an interval.

3. **Codex-app sessions**: `provider: "codex-app"` sessions use a different mechanism (RPC-based turn completion via `_handle_codex_turn_complete`), which already calls `mark_session_idle()` (session_manager.py:860). This fix only targets `provider: "codex"` (tmux-based Codex CLI).

## Ticket Classification

**Single ticket.** The fix is a small conditional in `_watch_for_idle` plus verification of OutputMonitor behavior. One agent can complete this without compacting context.
