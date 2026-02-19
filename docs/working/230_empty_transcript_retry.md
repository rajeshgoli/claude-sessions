# sm#230: Empty transcript retry in Stop hook handler

## Problem

When the Stop hook fires, `read_transcript()` occasionally returns `None` because Claude hasn't flushed the transcript JSONL yet. The current code defers the notification to the next `idle_prompt` Notification hook — which can fire minutes later if the agent is actively composing a long response — producing a silent notification gap.

**Observed (from sm#224 investigation):**
```
2026-02-19 08:12:48 - Stop hook for c3bbc6b9 had empty transcript, deferring notification
2026-02-19 08:13:00 - Stop hook for c3bbc6b9 had empty transcript, deferring notification
2026-02-19 08:28:59 - Sending deferred response notification for c3bbc6b9
```
Two Stop hooks deferred; notification delivered 16 minutes later when the next `idle_prompt` fired.

## Root Cause

The race: Claude calls the Stop hook synchronously before the transcript JSONL is fully flushed to disk. `read_transcript()` reads the file and either finds no assistant entry yet, or finds an entry with whitespace-only text — both return `(True, None)`. The code at `src/server.py:1604–1610` detects `not last_message` and adds the session to `pending_stop_notifications`, deferring to the next `idle_prompt` hook.

```python
# src/server.py:1604
if hook_event == "Stop" and not last_message and session_manager_id:
    app.state.pending_stop_notifications.add(session_manager_id)
    logger.info(f"Stop hook for {session_manager_id} had empty transcript, deferring notification")
```

The transcript flush typically completes within a few hundred milliseconds. A single bounded retry eliminates most deferrals without meaningfully delaying notifications.

## Relationship to sm#184 (stale transcript retry)

The existing stale-transcript retry at `src/server.py:1473–1490` handles a different case: `read_transcript()` returns content, but it matches the previously stored output (stale entry not yet overwritten). It fires only when `last_message` is non-None:

```python
if hook_event == "Stop" and session_manager_id and last_message:  # <-- last_message set
    if stored_output and last_message == stored_output:
        ...retry after 300ms...
```

The empty-transcript case (`last_message is None`) is explicitly excluded by this guard. The two retries are **mutually exclusive** on entry — they handle non-overlapping failure modes.

## Proposed Fix

### New constant (alongside `TRANSCRIPT_RETRY_DELAY_SECONDS` at `src/server.py:21`)

```python
# Delay before retrying an empty transcript read in the Stop hook handler (#230).
EMPTY_TRANSCRIPT_RETRY_DELAY_SECONDS = 0.5
```

### Insertion point: `src/server.py`, between lines 1471 and 1473

After the initial `read_transcript()` call and before the existing stale-transcript retry block. The retry must be inside the `if transcript_path:` block (lines 1419–1491) where the `read_transcript` closure is in scope.

```python
            try:
                success, last_message = await asyncio.to_thread(read_transcript)
                if not success:
                    logger.warning(f"Failed to read transcript for hook event: {hook_event}")
            except Exception as e:
                logger.error(f"CRITICAL: Error reading transcript in thread: {e}")
                last_message = None

            # Fix #230: Bounded retry for empty transcript reads on Stop hooks.
            # The Stop hook can fire before Claude flushes the current response to
            # the transcript JSONL, returning None. Retry once after 500ms before
            # deferring to the idle_prompt hook.
            # Note: this retry is inside the if-transcript_path block and executes
            # before Stop-hook side effects (queue idle, lock cleanup). The 500ms
            # delay applies only in the empty-transcript edge case.
            if hook_event == "Stop" and not last_message:
                logger.info(
                    f"Empty transcript for {session_manager_id or 'unknown'}, "
                    f"retrying after {EMPTY_TRANSCRIPT_RETRY_DELAY_SECONDS}s"
                )
                await asyncio.sleep(EMPTY_TRANSCRIPT_RETRY_DELAY_SECONDS)
                try:
                    success, last_message = await asyncio.to_thread(read_transcript)
                    if not success:
                        logger.warning(f"Empty transcript retry: failed for {session_manager_id or 'unknown'}")
                        last_message = None
                except Exception as e:
                    logger.error(f"Empty transcript retry: error for {session_manager_id or 'unknown'}: {e}")
                    last_message = None

            # Fix #184: Bounded retry for stale transcript reads on Stop hooks.
            ...
```

**Guard change vs. original proposal:** The guard is `hook_event == "Stop" and not last_message` — no `session_manager_id` check. The immediate notification path (line 1612) already works without `session_manager_id` by falling back to `transcript_path` and `claude_session_id` matching. Keeping `session_manager_id` in the guard would silently skip the retry for sessions where the env var isn't set, leaving the race-condition drop intact.

### Latency impact on Stop-hook side effects

**Accepted.** The `read_transcript` closure is scoped inside `if transcript_path:` (lines 1419–1491). All Stop-hook side effects — queue idle transition (`src/server.py:1505`), restore scheduling (`src/server.py:1516`), lock cleanup (`src/server.py:1546`) — execute after this block. The 500ms retry therefore delays all of them in the empty-transcript case.

This is acceptable because:
1. The empty-transcript case is the edge case (transcript flush normally completes well before Stop hook fires)
2. A 500ms delay on queue idle and lock release is negligible for the workflows these side effects serve
3. The alternative — hoisting `read_transcript` out of the `if transcript_path:` block so the retry can be deferred past line 1491 — adds structural complexity for no practical benefit

### Code flow after fix

| State after initial read | After empty retry | Outcome |
|---|---|---|
| `last_message = None` (not flushed yet) | content available | immediate notification (no deferral); side effects delayed 500ms |
| `last_message = None` (genuinely slow write) | still `None` | deferred to `idle_prompt` (unchanged); side effects delayed 500ms |
| `last_message = text` (non-stale) | n/a — skip empty retry | immediate notification (unchanged); no delay |
| `last_message = stale text` | n/a — skip empty retry | stale retry (#184) fires → updated content; no additional delay |

### Interaction between retries

When the empty retry (sm#230) succeeds and returns content, the stale retry (#184) then evaluates `last_message == stored_output`. If content happens to match stored output (repeated identical response), the stale retry adds a further 300ms wait. This is an accepted edge case — the combined delay (500ms + 300ms = 800ms) is still well under the seconds-to-minutes delay of the deferred path.

## Test Plan

1. **Fast-response agent — retry succeeds:** Dispatch a task with a very short, deterministic response. Confirm notification arrives within ~1s of Stop hook and no `deferring notification` log line appears.

2. **Slow transcript write — retry fails, deferred path works:** Simulate a genuinely slow flush using `unittest.mock.patch("pathlib.Path.read_text", side_effect=...)` or by writing a temp transcript file after a delay in an async integration test (`asyncio.create_task` that writes the file 1s later). Verify:
   - `pending_stop_notifications.add(...)` fires (deferred log line appears)
   - Next `idle_prompt` hook delivers the notification

3. **Log output:** Confirm `"Empty transcript for ... retrying after 0.5s"` appears on retry, and `"Stop hook for ... had empty transcript, deferring notification"` only appears when the retry also fails.

4. **Stale retry unaffected:** Verify sm#184 stale-transcript retry still fires correctly when a stale (repeated) last message is read.

5. **No `session_manager_id` — retry still runs:** Confirm the empty-transcript retry fires even when `session_manager_id` is absent (guard is `hook_event == "Stop" and not last_message` only). If the retry succeeds, verify the immediate notification path resolves the session via `transcript_path` or `claude_session_id` fallback. If the retry fails, verify `pending_stop_notifications` is NOT updated (the deferral at `src/server.py:1604` still guards on `session_manager_id`, so no deferred path — consistent with pre-existing behavior).

## Ticket Classification

**Single ticket.** Changes: `src/server.py` — one new module-level constant (~2 lines) and one new retry block (~12 lines) inserted at a well-defined location. No other files. One agent can complete without compacting context.
