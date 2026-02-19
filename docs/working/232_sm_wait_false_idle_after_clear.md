# sm#232 + sm#240: sm wait False Idle After Clear / Stale Running State

## Issues

- **sm#232**: `sm wait` fires a false idle notification 2s after every `sm clear` + `sm send` dispatch.
- **sm#240**: `sm children` reports an idle agent as "running" for 10+ minutes; `sm wait` times out.

Both are symptoms of Stop hook delivery timing interacting incorrectly with in-memory idle state.

---

## sm#232: False Idle at 2s After sm clear + sm send

### Observed Symptom

Every `sm clear` + `sm send --urgent` + `sm wait` cycle produces a spurious idle notification:

```
sm clear cc4d95a7
sm send cc4d95a7 "As engineer, ..." --urgent
sm wait cc4d95a7 600
→ [sm wait] engineer-sm224 is now idle (waited 2s)   ← FALSE
sm children | grep cc4d95a7
→ running, last tool 1s ago                           ← agent is actually running
```

### Root Cause

**The `/clear` Stop hook arrives at the server AFTER `sm send` has already re-dispatched the agent, and `mark_session_idle()` unconditionally sets `state.is_idle = True` at line 320 — before the skip_count guard can prevent it.**

#### Full Timeline

```
CLI                              Server (asyncio event loop)
───                              ─────────────────────────
sm clear X:
  invalidate_cache(X)         →  skip_count = 1, skip_count_armed_at = now()
  send /clear to tmux
  Agent runs /clear
  Agent fires Stop hook        →  (curl background, takes ~0.5-1.5s)
  CLI detects '>' prompt
  sm clear returns

sm send X "task" --urgent:
  queue_message(urgent)       →  mark_session_active(X):
                                   state.is_idle = False  ← correct
                                   session.status = RUNNING ← correct
                                 _deliver_urgent task scheduled

sm wait X 600:
  watch_session(X)            →  _watch_for_idle task scheduled
                                 Poll 1 (t=0): is_idle=False → no fire ✓
                                 (2s sleep)

                                 Late /clear Stop hook curl arrives:
                                 mark_session_idle(from_stop_hook=True):
                                   line 320: state.is_idle = True  ← WRONG
                                   skip_count=1>0 → skip_count=0
                                   → early return (notification skipped)
                                   → BUT is_idle=True remains!
                                 server.py:1521: session.status = IDLE ← WRONG

                                 Poll 2 (t=2s):
                                   Phase 1: state.is_idle=True → mem_idle=True
                                   Phase 2: SKIPPED (only runs if not mem_idle)
                                   Phase 4: no pending msgs → SKIPPED
                                   → is_idle = True → FIRES "[sm wait] idle (waited 2s)"
```

#### Code Location

`src/message_queue.py`, `mark_session_idle()`:

```python
# CURRENT (lines 319-347):
state.is_idle = True          # ← line 320: set BEFORE skip check
state.last_idle_at = datetime.now()
...
if from_stop_hook and state.stop_notify_skip_count > 0:
    state.stop_notify_skip_count -= 1
    asyncio.create_task(self._try_deliver_messages(session_id))
    return                    # ← returns early, but is_idle=True already set
```

`src/server.py`, Stop hook handler (lines 1518-1523):

```python
# CURRENT: runs unconditionally for every Stop hook
app.state.session_manager.update_session_status(session_manager_id, SessionStatus.IDLE)
# ← sets session.status=IDLE even when skip_count absorbed the stop hook
```

#### Why the Existing Defenses Fail

1. **`#182` suppression** (30s window, checks `stop_notify_sender_id == last_outgoing_sm_send_target`): Doesn't help because `mark_session_idle()` already set `is_idle = True` at line 320, before any notification logic runs. The `_watch_for_idle` Phase 1 fires on `is_idle`, not on notification state.

2. **`#191` fix** (`mark_session_active` sets `session.status = RUNNING`): Sets `session.status` correctly at dispatch time, but the late stop hook OVERWRITES it afterwards at server.py:1521.

3. **Phase 2 tmux check**: Only runs `if not mem_idle`. Since Phase 1 (`state.is_idle`) is True, Phase 2 is skipped entirely. The tmux prompt is never consulted to override the stale idle.

### Proposed Fix

Two-part fix. Both parts are required.

#### Part 1: Time-bounded skip fence + move `is_idle = True` after skip check

**The problem with moving `is_idle = True` alone:**

If the `/clear` Stop hook curl is *lost* (OS kills background process before completion), `skip_count` stays armed at 1 indefinitely. The next *legitimate* Stop hook from the new task would then be absorbed by skip_count without setting `is_idle = True`, leaving the session permanently stuck as RUNNING.

**The fix: add `skip_count_armed_at` timestamp. Only absorb if the arm happened within the curl timeout window (8s default, configurable). If stale, reset the entire fence (both `skip_count` and `skip_count_armed_at`) atomically and fall through to normal processing.**

Note: the TTL applies to the whole fence, not per-slot. When stale, both fields are cleared together. The fence is not designed to track individual clears when multiple clears fire in rapid succession — in that case, the arm time is overwritten by the most recent clear. This is acceptable because back-to-back `sm clear` calls without an intervening `sm send` are unusual in practice.

**Residual risk:** A fast task that completes within the TTL window after a lost `/clear` hook will still have its Stop hook absorbed. Example: lost hook at T=0, new task completes at T=7s (within 8s window) — Stop hook absorbed, session stuck. This is an inherent limit of the TTL approach and is documented as a known edge case (see regression test below).

`src/models.py`, `SessionDeliveryState`:
```python
stop_notify_skip_count: int = 0
skip_count_armed_at: Optional[datetime] = None  # When skip fence was last armed (sm#232)
```

**Two arm locations** — both must set `skip_count_armed_at`:

`src/server.py`, `_invalidate_session_cache()`:
```python
if arm_skip:
    state.stop_notify_skip_count += 1
    state.skip_count_armed_at = datetime.now()  # sm#232
```

`src/message_queue.py`, `_execute_handoff()` (line ~1582):
```python
# 1. Arm skip fence for /clear Stop hook + clear stale notification state
state.stop_notify_skip_count += 1
state.skip_count_armed_at = datetime.now()  # sm#232
```

`src/message_queue.py`, `mark_session_idle()` — restructured:
```python
def mark_session_idle(self, session_id, ...):
    state = self._get_or_create_state(session_id)
    logger.info(f"Session {session_id} marked idle")

    # Cancel periodic remind on stop hook
    if from_stop_hook:
        self.cancel_remind(session_id)

    # Handoff check (must precede skip check — sets is_idle=False before returning)
    if from_stop_hook and getattr(state, "pending_handoff_path", None):
        file_path = state.pending_handoff_path
        state.pending_handoff_path = None
        state.is_idle = False
        asyncio.create_task(self._execute_handoff(session_id, file_path))
        return

    # Absorb /clear stop hooks — time-bounded to prevent stale-armed skip (sm#232)
    # notify_server.sh uses --max-time 5; default 8s gives reasonable buffer.
    # Configurable via message_queue_timeouts.skip_fence_window_seconds in config.
    SKIP_FENCE_WINDOW_SECONDS = self.skip_fence_window_seconds  # default 8
    if from_stop_hook and state.stop_notify_skip_count > 0:
        armed_at = state.skip_count_armed_at
        if armed_at and (datetime.now() - armed_at).total_seconds() < SKIP_FENCE_WINDOW_SECONDS:
            # Within window: absorb this /clear stop hook
            state.stop_notify_skip_count -= 1
            if state.stop_notify_skip_count == 0:
                state.skip_count_armed_at = None  # state hygiene: clear when fence fully consumed
            # Do NOT set is_idle here — agent may already be processing new task.
            # Preserves is_idle=False if mark_session_active already ran.
            asyncio.create_task(self._try_deliver_messages(session_id))
            return
        else:
            # Stale arm (hook was lost): reset ENTIRE fence atomically (sm#232)
            # TTL applies to the whole fence; reset both fields together.
            state.stop_notify_skip_count = 0
            state.skip_count_armed_at = None
            logger.warning(
                f"Session {session_id}: skip fence was stale "
                f"(armed >{SKIP_FENCE_WINDOW_SECONDS}s ago), resetting"
            )
            # Fall through to normal processing (sets is_idle=True)

    # Now safe to mark idle
    state.is_idle = True
    state.last_idle_at = datetime.now()
    ...  # rest of method unchanged
```

The `skip_fence_window_seconds` value is loaded from config in `MessageQueueManager.__init__`:
```python
self.skip_fence_window_seconds = mq_timeouts.get("skip_fence_window_seconds", 8)
```

#### Part 2: Gate `session.status = IDLE` on `state.is_idle`

`src/server.py`, Stop hook handler:
```python
# After calling queue_mgr.mark_session_idle(...):
state = queue_mgr.delivery_states.get(session_manager_id)
if app.state.session_manager:
    target_session = app.state.session_manager.get_session(session_manager_id)
    if target_session and target_session.status != SessionStatus.STOPPED:
        # Only sync to IDLE if message_queue also considers session idle (sm#232)
        # When skip_count absorbed the Stop hook, state.is_idle was NOT set to True,
        # so session.status correctly remains RUNNING.
        if not state or state.is_idle:
            app.state.session_manager.update_session_status(
                session_manager_id, SessionStatus.IDLE
            )
```

### Case Analysis

| Scenario | skip_count | armed_at | is_idle before | Result |
|----------|-----------|----------|----------------|--------|
| Normal Stop hook (no /clear) | 0 | — | any | Falls through → `is_idle = True` ✓ |
| Late /clear hook, re-dispatch ran | 1 | <8s | False | Absorbed, `is_idle` stays False ✓ |
| Early /clear hook, before dispatch | 1 | <8s | True or False | Absorbed, `is_idle` preserved ✓ |
| Lost /clear hook, then task Stop (>8s) | 1 | >8s (stale) | False | Fence reset → falls through → `is_idle = True` ✓ |
| Lost /clear hook, fast task Stop (<8s) | 1 | <8s (still live) | False | Absorbed ← residual risk, task stuck ⚠ |
| Handoff Stop hook | any | — | any | Handoff branch fires before skip check ✓ |

**Fresh state note (no prior delivery_state):** When `_get_or_create_state()` creates a new state for a session that has never had a Stop hook, `is_idle = False` (default). If skip absorbs in this state, `is_idle` remains False. This is correct: `sm send` always follows `sm clear`, and `mark_session_active()` will set `is_idle = False` regardless. The session won't appear idle until it genuinely completes a task. The `_try_deliver_messages` call in the absorbed path handles any pre-queued messages.

---

## sm#240: Stale Running State (Stop Hook Lost)

### Observed Symptom

```
Scout (5399edcb) completed work and went idle.
sm children → "running | thinking 15s | last tool: Bash: echo test (15s ago)"
             (stays frozen for 10+ minutes)
sm wait 5399edcb 600 → "[sm wait] Timeout: still active after 600s"
User visually confirmed agent was idle in UI.
Context: agent had been compacted earlier in the session.
```

### Root Cause A (Confirmed): Async Stop Hook Delivery Failure

`~/.claude/hooks/notify_server.sh` runs the Stop hook curl call in a **background process** (fire-and-forget):

```bash
( curl -s --max-time 5 ... http://localhost:8420/hooks/claude ... ) </dev/null >/dev/null 2>&1 &
disown 2>/dev/null
exit 0
```

If the background `curl` process is killed before completing (OS cleanup, SIGTERM, timeout), the server never receives the Stop notification:

- `mark_session_idle()` is never called
- `state.is_idle` remains `False`
- `session.status` remains `RUNNING`
- `sm children` shows "running" forever

### Root Cause B (Unconfirmed): Phase 2 Tmux Fallback Failure

`_watch_for_idle` Phase 2 (`_check_idle_prompt`) should detect idle via tmux even when the Stop hook is lost. For Phase 2 to have failed for the entire 600s watch (300 polls at 2s intervals), `_check_idle_prompt` must have returned `False` consistently even while the agent was at the `>` prompt.

The current check:
```python
output = stdout.decode().rstrip()   # strips trailing whitespace
last_line = output.split('\n')[-1]
return last_line.rstrip() == '>' or last_line.startswith('> ') and not last_line[2:].strip()
```

Note: `rstrip()` already handles blank lines after the prompt, so that is not a viable failure mode. The actual cause of Phase 2 failure in this incident is **not confirmed** and should not be assumed. Possible candidates:
- Post-compaction UI elements ("Razzle-dazzling…", context summary) rendering as the last visible line
- The `>` prompt scrolled off the terminal's visible 24-line window after a long final response
- `tmux_session` field mismatch or tmux pane in an unexpected state

### Required: Investigation Ticket with Telemetry

Before implementing any Phase 2 fix, instrument `_check_idle_prompt` to log the actual captured output when idle is NOT detected:

```python
# _check_idle_prompt: add logging when not detected
logger.debug(f"check_idle_prompt({tmux_session}): last_line={repr(last_line)}, full_output_tail={repr(output[-200:])}")
```

This captures the actual pane content at failure time and confirms which failure mode occurred. A Phase 2 fix (if any) should be designed based on observed evidence, not speculation. Fix may include expanding to check last N lines, or may reveal a different root cause requiring a different approach.

### Connection to sm#232

Both bugs share the same mechanism: the **Stop hook is the primary state transition trigger** for RUNNING→IDLE. When it's late (sm#232) or lost (sm#240), the system makes incorrect decisions.

| | sm#232 | sm#240 |
|---|---|---|
| Stop hook status | Late (arrives post-dispatch) | Lost (curl dropped) |
| `state.is_idle` effect | Set to True when it should be False | Never set to True |
| `_watch_for_idle` result | False positive (fired at 2s) | False negative (timeout) |
| Phase 2 role | Bypassed (Phase 1 already True) | Should have caught it, didn't |

---

## Implementation Approach

### sm#232 (immediate implementation ticket)

| File | Change |
|------|--------|
| `src/models.py` | `SessionDeliveryState`: add `skip_count_armed_at: Optional[datetime] = None` |
| `src/server.py` | `_invalidate_session_cache()`: set `state.skip_count_armed_at = datetime.now()` when arming |
| `src/message_queue.py` | `_execute_handoff()`: set `state.skip_count_armed_at = datetime.now()` when arming |
| `src/message_queue.py` | `mark_session_idle()`: time-bounded skip check (8s default, configurable); move `is_idle = True` after skip check; reset entire fence on stale |
| `src/server.py` | Stop hook handler: gate `update_session_status(IDLE)` on `state.is_idle` |
| `config.yaml` | Add `message_queue_timeouts.skip_fence_window_seconds: 8` |

### sm#240 (investigation ticket first)

1. Add telemetry to `_check_idle_prompt`: log last 200 chars of captured pane output when not detected
2. Reproduce: run a session with compaction, let it complete, observe what `capture-pane` shows
3. Based on confirmed evidence, file implementation sub-ticket for Phase 2 fix or hook reliability hardening

---

## Test Plan

### sm#232 Tests

| Test | Setup | Assertion |
|------|-------|-----------|
| `test_late_clear_stop_hook_does_not_set_idle` | `mark_session_active()` ran (is_idle=False); skip_count=1, armed <8s | Call `mark_session_idle(from_stop_hook=True)`. Assert: `state.is_idle = False`, skip_count=0 |
| `test_normal_stop_hook_sets_idle` | skip_count=0, no pending dispatch | Call `mark_session_idle(from_stop_hook=True)`. Assert: `state.is_idle = True` |
| `test_clear_stop_hook_before_dispatch` | skip_count=1, armed <8s; **two sub-cases**: (a) is_idle=True (previously idle session), (b) is_idle=False (fresh session default) | Call `mark_session_idle(from_stop_hook=True)`. Assert: is_idle unchanged from before, skip_count=0 |
| `test_stale_skip_count_does_not_absorb` | skip_count=1, armed >8s ago; is_idle=False | Call `mark_session_idle(from_stop_hook=True)`. Assert: `state.is_idle = True`, skip_count=0, skip_count_armed_at=None (entire fence reset) |
| `test_watch_no_false_idle_after_clear_dispatch` | Register watch; call `mark_session_active()` (is_idle=False, skip_count=1, armed <8s); then call `mark_session_idle(from_stop_hook=True)` | Assert: watch does NOT fire within 10s |
| `test_fast_task_within_ttl_residual_risk` | skip_count=1, armed 6s ago (within 8s window); is_idle=False | Call `mark_session_idle(from_stop_hook=True)`. Assert: **absorbed** (is_idle stays False). Documents residual risk: fast tasks completing within TTL are still affected if /clear hook was lost |
| `test_handoff_path_arms_both_fields` | Simulate `_execute_handoff`: set skip_count += 1, skip_count_armed_at = now(). Then call `mark_session_idle(from_stop_hook=True)` within window. Assert: absorbed (is_idle unchanged), skip_count=0, skip_count_armed_at=None (cleared on full consumption) |

### sm#240 Tests (post-investigation)

Tests to be defined after telemetry confirms Phase 2 failure mode. At minimum:

| Test | Setup | Assertion |
|------|-------|-----------|
| `test_check_idle_prompt_logs_on_failure` | tmux capture returns output with `>` NOT as last line | Assert: debug log contains captured output tail |

---

## Ticket Classification

**sm#232: Single implementation ticket.** Four-file change (models, server ×2, message_queue ×2, config.yaml), seven unit tests. Engineer can complete without context compaction.

**sm#240: Investigation ticket first**, then one or more implementation sub-tickets based on confirmed telemetry. Do not implement Phase 2 heuristic changes before the root cause of the prompt-detection failure is confirmed.

**Suggested order:**
1. Ship sm#232 fix + regression tests (unblocks EM dispatch workflow immediately)
2. Run sm#240 investigation with telemetry (short ticket, observational)
3. File sm#240 implementation ticket(s) based on confirmed evidence
