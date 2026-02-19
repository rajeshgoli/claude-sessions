# sm#241: Stale compaction notification delivered after sm clear

## Problem

After `sm clear <agent>`, a compaction notification from the agent's previous session was delivered to the EM. The agent had restarted at 34% context (fresh after clear), but the notification read "Compaction fired — agent is still running," causing the EM to incorrectly believe the cleared agent had just compacted.

## Root Cause

Three gaps combine:

### Gap 1: Compaction `queue_message` calls are untagged

When compaction fires (`src/server.py:2394–2419`), the notification is queued via:

```python
queue_mgr.queue_message(
    target_session_id=notify_target,   # EM's session ID
    text=msg,
    delivery_mode="sequential",
    # sender_session_id NOT passed — defaults to NULL
    # message_category NOT set — no way to distinguish from sm send traffic
)
```

The same applies to warning and critical notifications (`src/server.py:2462–2485`).

### Gap 2: `_invalidate_session_cache` cannot cancel queued context-monitor messages

When `sm clear` runs, `_invalidate_session_cache` (`src/server.py:249–277`) resets stop-hook state but does not touch the message queue. Because the queued compaction message targets the EM (not the cleared agent), no existing cleanup path removes it:
- `_cleanup_messages_for_session(session_id)` (`src/message_queue.py:635`) deletes messages where `target_session_id = session_id` — wrong target
- `cancel_remind(session_id)` handles the periodic remind registration only

**Delivery sequence:**
```
t=0  PreCompact fires for agent → queue_message(target=EM, "Compaction fired...")
t=1  sm clear runs → _invalidate_session_cache → stop-hook state cleared, queue untouched
t=2  Agent restarts at 34% context
t=3  EM goes idle → message queue delivers stale "Compaction fired" notification
```

### Gap 3: `context_reset` handler is unreachable for unregistered sessions

When the cleared agent's new session starts, the SessionStart hook sends `event=context_reset`, which is handled at `src/server.py:2426–2431`. This resets `_context_warning_sent` / `_context_critical_sent` flags. However, the `context_reset` branch is **after** the registration gate at line 2422:

```python
if not session.context_monitor_enabled:
    return {"status": "not_registered"}   # <-- early return

if data.get("event") == "context_reset":  # never reached for unregistered sessions
    ...
```

Unregistered sessions can still receive compaction notifications via the parent fallback (`src/server.py:2408`: `notify_target = session.context_monitor_notify or session.parent_session_id`). But `context_reset` never fires for them, so any belt-and-suspenders cancellation in that branch also never runs.

## Proposed Fix

### Part 1: Add `message_category` column to the message queue schema

A narrow cancellation that avoids touching `sm send` traffic requires identifying context-monitor messages in the queue. Add a `message_category` column via the existing ALTER TABLE migration pattern in `src/message_queue.py`:

**Schema (`src/message_queue.py`, CREATE TABLE block):**
```python
CREATE TABLE IF NOT EXISTS message_queue (
    ...
    delivered_at TIMESTAMP,
    message_category TEXT DEFAULT NULL   # <-- add: e.g. 'context_monitor'
)
```

**Migration (alongside existing ALTER TABLE checks):**
```python
if "message_category" not in columns:
    cursor.execute("ALTER TABLE message_queue ADD COLUMN message_category TEXT DEFAULT NULL")
    logger.info("Migrated message_queue: added message_category column")
```

**`queue_message` signature — add optional parameter:**
```python
def queue_message(
    self,
    ...
    message_category: Optional[str] = None,   # <-- add
) -> QueuedMessage:
```

Persist it alongside the other fields in the INSERT.

### Part 2: Tag all context monitor outbound `queue_message` calls

In `src/server.py`, pass `message_category="context_monitor"` to the three `queue_message` calls in the context monitor handler:

- **Compaction** (`src/server.py:2414`): add `sender_session_id=session_id, message_category="context_monitor"`
- **Critical warning** (`src/server.py:2462`): add `sender_session_id=session_id, message_category="context_monitor"`
- **Warning** (`src/server.py:2482`): add `sender_session_id=session_id, message_category="context_monitor"`

`sender_session_id` is also added so the category filter can be combined with source — both fields narrow the DELETE to exactly the right rows.

### Part 3: Add `cancel_context_monitor_messages_from` to `MessageQueueManager`

In `src/message_queue.py`, add a targeted cancellation method:

```python
def cancel_context_monitor_messages_from(self, sender_session_id: str) -> int:
    """Cancel undelivered context-monitor notifications from sender_session_id.

    Called on sm clear to discard stale compaction/warning/critical alerts
    before they reach the parent EM (#241). Does NOT affect sm send traffic
    from the same sender (those have message_category=NULL).

    Returns:
        Number of messages cancelled.
    """
    rows = self._execute_query(
        "SELECT COUNT(*) FROM message_queue "
        "WHERE sender_session_id = ? AND message_category = 'context_monitor' AND delivered_at IS NULL",
        (sender_session_id,)
    )
    count = rows[0][0] if rows else 0
    if count:
        self._execute(
            "DELETE FROM message_queue "
            "WHERE sender_session_id = ? AND message_category = 'context_monitor' AND delivered_at IS NULL",
            (sender_session_id,)
        )
        logger.info(
            f"Cancelled {count} stale context-monitor message(s) from cleared session {sender_session_id}"
        )
    return count
```

### Part 4: Call `cancel_context_monitor_messages_from` in `_invalidate_session_cache`

In `src/server.py:_invalidate_session_cache`, add after the existing state resets:

```python
        # Cancel stale context-monitor notifications from this session (#241)
        queue_mgr.cancel_context_monitor_messages_from(session_id)
```

This runs on all clear paths (tmux invalidate-cache and Codex-app clear).

### Part 5 (belt-and-suspenders): Move `context_reset` above the registration gate

Move the `context_reset` branch in `src/server.py` to before the `context_monitor_enabled` gate so it executes for unregistered sessions (which can still receive compaction notifications via parent fallback):

```python
# Handle manual /clear event (from SessionStart clear hook)
# Must be before the registration gate — unregistered sessions still receive
# compaction notifications and need cancellation on context reset (#241).
if data.get("event") == "context_reset":
    session._context_warning_sent = False
    session._context_critical_sent = False
    if queue_mgr:
        queue_mgr.cancel_context_monitor_messages_from(session_id)
    return {"status": "flags_reset"}

# Gate: skip unregistered sessions for usage/warning/critical events (#206)
if not session.context_monitor_enabled:
    return {"status": "not_registered"}
```

## What the category filter protects

| Message type | `message_category` | Cancelled on clear? |
|---|---|---|
| Compaction notification | `"context_monitor"` (after fix) | ✓ |
| Critical warning | `"context_monitor"` (after fix) | ✓ |
| Warning | `"context_monitor"` (after fix) | ✓ |
| `sm send` from same agent | `NULL` (never set) | ✗ — preserved |
| `sm send` from other agents | `NULL` | ✗ — preserved |

`sm send` messages queued from `session_manager.py:673,697` do not set `message_category`, so they are never matched by the category-scoped DELETE.

## Test Plan

1. **Compaction message cancelled on clear:** Mock compaction event → verify `queue_message` called with `sender_session_id=session_id, message_category="context_monitor"` → call `_invalidate_session_cache` → verify message deleted from SQLite → verify EM never receives it.

2. **Warning/critical cancelled on clear:** Same for warning and critical events.

3. **Same-sender `sm send` messages preserved on clear:** Queue a `sm send` message from agent A to EM (category=NULL). Clear agent A via `_invalidate_session_cache`. Verify the `sm send` message is still in the queue — `cancel_context_monitor_messages_from` must NOT delete it.

4. **Other-sender messages unaffected:** Queue context-monitor messages from sender B; clear agent A → B's messages intact.

5. **`cancel_context_monitor_messages_from` returns correct count:** Queue 3 context-monitor messages from sender A, 2 `sm send` from sender A; call cancel → count=3, the 2 `sm send` messages remain.

6. **Legitimate compaction after clear still delivered:** New compaction fires AFTER clear → new queued message (new `queued_at`) → NOT cancelled (cancel ran at clear time, before this message was queued) → delivered correctly.

7. **context_reset belt-and-suspenders (unregistered session):** Session with `context_monitor_enabled=False` receives compaction (parent fallback) → message queued → `context_reset` event fires → verify message cancelled and `{"status": "flags_reset"}` returned (not `"not_registered"`).

8. **context_reset belt-and-suspenders (registered session):** Same as 7 but with `context_monitor_enabled=True` → same cancellation occurs.

## Ticket Classification

**Single ticket.** Changes:
- `src/message_queue.py`: schema ALTER TABLE migration (~4 lines), `queue_message` signature (~2 lines + INSERT), `cancel_context_monitor_messages_from` method (~15 lines)
- `src/server.py`: 3 `queue_message` call sites (~2 lines each), `_invalidate_session_cache` (~2 lines), `context_reset` handler repositioned and extended (~3 lines)

One agent can complete without compacting context.
