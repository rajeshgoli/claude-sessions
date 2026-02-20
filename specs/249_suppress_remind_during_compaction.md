# sm#249: Suppress remind delivery during active compaction

**Issue**: https://github.com/rajeshgoli/session-manager/issues/249
**Classification**: Single ticket

---

## Problem

When an agent is mid-compaction, `sm remind` delivers its overdue message anyway, interrupting the compaction cycle and triggering a second compaction.

**Observed sequence:**
1. Agent context approaches limit → `PreCompact` hook fires
2. While context is being compacted, a periodic remind threshold expires
3. `_run_remind_task` queues a remind message (urgent/important)
4. Compaction completes, agent wakes and resumes processing
5. Agent sees the queued remind, responds to it — consuming fresh context
6. Response pushes context over the limit again → second compaction fires

This is reproducible whenever `sm#225-A` auto-remind is active (default for dispatched agents) and the elapsed time since last remind reset exceeds the soft threshold before compaction completes.

---

## Root Cause

**No compaction state is tracked server-side.** The `Session` model has no flag indicating that a session is currently compacting. The remind delivery paths (`_run_remind_task`, `_fire_reminder`) have no guard against this.

### Compaction lifecycle (current)

| Event | Hook | Server effect |
|---|---|---|
| Compaction starts | `PreCompact` → `precompact_notify.sh` → POST `/hooks/context-usage` with `event: "compaction"` | Resets `_context_warning_sent`, `_context_critical_sent`, notifies parent |
| Compaction ends | `SessionStart(compact)` → `post_compact_recovery.sh` | GET `/sessions/{id}` (read-only, **no server notification**) |

**Gap**: The server knows when compaction starts but not when it ends. There is no `_is_compacting` flag, so remind delivery has nothing to check.

### Remind delivery paths

Both paths in `message_queue.py` are blind to compaction state:

1. **`_run_remind_task`** (lines 1448–1500): Polls every 5s, queues soft/hard remind messages when elapsed time exceeds thresholds. `elapsed` is computed as `now - reg.last_reset_at` (line 1464). No session state check.
2. **`_fire_reminder`** (lines 1282–1306): One-shot; sleeps `delay_seconds`, then calls `queue_message("urgent")`. No session state check.

`MessageQueueManager` already holds `self.session_manager`, so calling `self.session_manager.get_session(target_session_id)` is the natural access pattern.

---

## Proposed Solution

### 1. Track compaction state on `Session` (runtime-only flag)

Add `_is_compacting: bool` to `Session` in `models.py`, following the same pattern as `_context_warning_sent` / `_context_critical_sent` (runtime-only, not persisted, not included in `to_dict`/`from_dict`):

```python
# In Session dataclass (models.py), alongside existing runtime flags
_is_compacting: bool = field(default=False, init=False, repr=False)
```

### 2. Set flag on PreCompact event (server.py)

In the existing compaction event handler in `/hooks/context-usage`, set the flag — no remind timer reset here:

```python
if data.get("event") == "compaction":
    session._is_compacting = True          # NEW
    # ... (existing flag resets and parent notification unchanged)
    return {"status": "compaction_logged"}
```

### 3. Clear flag on compaction-complete + reset remind timer

This is the critical step. When `_is_compacting` clears, the agent wakes fresh. The remind timer is reset **here** (not at PreCompact start) to give the agent the full soft-threshold window before the next remind fires.

**Why reset here and not at PreCompact?**
`elapsed` in `_run_remind_task` is measured from `reg.last_reset_at`. If compaction lasts longer than `soft_threshold` (e.g., 200s vs 180s), resetting at PreCompact start still leaves `elapsed > soft_threshold` when the guard lifts — the remind fires immediately. Resetting at `compaction_complete` ensures `elapsed = 0` exactly when the agent resumes.

**`~/.claude/hooks/post_compact_recovery.sh`** (canonical template in `scripts/install_context_hooks.sh` lines 95–118):

Add a POST to notify compaction is done, after the existing handoff-path block:

```bash
# After the existing handoff-path injection block:
# Notify sm that compaction has completed — clears _is_compacting flag and resets remind timer
curl -s --max-time 0.5 -X POST http://localhost:8420/hooks/context-usage \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg sid "$SM_SESSION_ID" '{session_id: $sid, event: "compaction_complete"}')" \
  >/dev/null 2>&1
```

**`server.py`** — add handler before the existing `context_reset` handler:

```python
if data.get("event") == "compaction_complete":
    session._is_compacting = False
    if queue_mgr:
        queue_mgr.reset_remind(session_id)   # gives fresh window post-compaction
    return {"status": "compaction_complete_logged"}
```

### 4. Rollout for existing installs

Re-run the installer to push the updated template to `~/.claude/hooks/`:

```bash
bash scripts/install_context_hooks.sh
```

Existing installs that do not re-run the installer retain the old hook and will not receive `event: "compaction_complete"`. In that case, `_is_compacting` stays `True` after compaction — remind suppression continues until next server restart (flag resets to `False` on restart since it's runtime-only). This is the safe failure direction.

### 5. Guard remind delivery in message_queue.py

**In `_run_remind_task`** — add a compaction check before either soft or hard delivery:

```python
# After checking reg is active, before threshold checks:
session = self.session_manager.get_session(target_session_id)
if session and session._is_compacting:
    continue  # Skip this iteration; timer will be reset at compaction_complete
```

**In `_fire_reminder`** — add a bounded wait loop after the initial sleep:

```python
await asyncio.sleep(delay_seconds)

# Wait for compaction to complete, bounded at MAX_COMPACT_WAIT_SECONDS
MAX_COMPACT_WAIT_SECONDS = 300
compact_waited = 0
COMPACT_POLL_INTERVAL = 5
while compact_waited < MAX_COMPACT_WAIT_SECONDS:
    session = self.session_manager.get_session(session_id)
    if not session or not session._is_compacting:
        break
    await asyncio.sleep(COMPACT_POLL_INTERVAL)
    compact_waited += COMPACT_POLL_INTERVAL
else:
    logger.warning(
        f"Reminder {reminder_id}: compaction wait exceeded {MAX_COMPACT_WAIT_SECONDS}s, delivering anyway"
    )

# ... proceed with queue_message(...)
```

The 5-minute bound ensures reminders are not permanently stuck if `post_compact_recovery.sh` fails to POST within the same server lifecycle. The `not session` break handles session-stopped cases. Delivery after timeout is the safe direction — dropping the reminder entirely would violate the one-shot guarantee.

---

## What the fix does NOT change

- Remind registration, cancellation, and reset mechanisms are unchanged
- The PreCompact → parent notification flow is unchanged
- `_context_warning_sent` / `_context_critical_sent` flags are unchanged

---

## Edge cases

| Case | Behavior |
|---|---|
| Compaction takes longer than `soft_threshold` | `_is_compacting = True` suppresses delivery during the window. `reset_remind` at `compaction_complete` gives agent a fresh soft_threshold window after waking. |
| Server restart mid-compaction | `_is_compacting` defaults to `False` on restart (runtime-only). Possible one stale remind fires post-restart. Same as today's behavior; acceptable. |
| `post_compact_recovery.sh` POST fails (server down) | `_is_compacting` stays `True`. No reminds fire until server restart resets flag. Safe direction — suppression beats false delivery. |
| `post_compact_recovery.sh` not updated (old install) | Same as above — hook sends no `compaction_complete`, flag stays True. Rollout instructions mitigate this. |
| `_fire_reminder` waits > 300s | Delivers anyway, logs a warning. One-shot guarantee preserved. |
| Agent has no remind registered | `reset_remind` and skip guard both no-op for unregistered sessions. No effect. |

---

## Implementation approach

All changes are in existing files — no new files needed.

| File | Change |
|---|---|
| `src/models.py` | Add `_is_compacting: bool = field(default=False, init=False, repr=False)` to `Session` |
| `src/server.py` | (a) Set `session._is_compacting = True` on `event: "compaction"`. (b) Add handler for `event: "compaction_complete"`: set `session._is_compacting = False` + call `queue_mgr.reset_remind()` |
| `src/message_queue.py` | (a) Add compaction skip in `_run_remind_task` before soft/hard delivery. (b) Add bounded compaction wait loop in `_fire_reminder` after `asyncio.sleep` |
| `scripts/install_context_hooks.sh` | Update `post_compact_recovery.sh` template (lines 95–118) to add `compaction_complete` curl POST after existing handoff-path block |

---

## Test plan

Tests split between two existing test files based on what is being tested.

### `tests/unit/test_context_monitor.py` — new test class: `TestCompactionStateTracking`

These test the server-side event handler behavior (compaction flag lifecycle):

**Test 1 — `event: "compaction"` sets `_is_compacting = True`**
- Call server handler with `event: "compaction"` for a known session
- Assert: `session._is_compacting == True`

**Test 2 — `event: "compaction_complete"` clears `_is_compacting` and resets remind timer**
- Set `session._is_compacting = True`, register periodic remind
- Call server handler with `event: "compaction_complete"`
- Assert: `session._is_compacting == False`; `reg.last_reset_at` updated to approx-now; `reg.soft_fired == False`

**Test 3 — `_is_compacting` defaults to `False` on new `Session`**
- `assert Session()._is_compacting == False`

### `tests/unit/test_remind.py` — new test class: `TestCompactionSuppression`

These test the delivery guard in the remind task and fire paths:

**Test 4 — Periodic remind skipped when `session._is_compacting = True`**
- Register periodic remind, set `reg.last_reset_at` to past-threshold (5s ago, soft_threshold=1)
- `mock_session_manager.get_session.return_value` = a mock `Session` with `_is_compacting = True`
- Run one `_run_remind_task` iteration
- Assert: no message queued

**Test 5 — Periodic remind fires when `session._is_compacting = False`**
- Same setup as Test 4 but `_is_compacting = False` (baseline: guard doesn't over-suppress)
- Assert: remind message queued

**Test 6 — One-shot reminder waits when compacting, fires when clear**
- Mock `asyncio.sleep` to fast-forward
- `get_session` returns `_is_compacting = True` for first 2 calls, then `False`
- Assert: `_fire_reminder` delivers the message once `_is_compacting` clears

**Test 7 — One-shot reminder delivers after max wait even if still compacting**
- Mock `asyncio.sleep` to fast-forward; `get_session` always returns `_is_compacting = True`
- Assert: message is delivered after 300s bound, warning logged

### Regression: existing remind tests pass unchanged

All existing tests (scenarios 1–16 in `test_remind.py`) continue to pass. Existing mocks return `None` from `get_session`, which the compaction guard treats as `not session → not compacting`, so all delivery proceeds as before.
