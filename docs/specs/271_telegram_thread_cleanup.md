# sm#271 — Telegram Thread Clutter: Investigation & Fix Spec

## Problem

The Telegram forum is accumulating too many open threads, making it impossible
for the user to locate the active EM thread. EM→user paging is degraded because
the user has to hunt for the right topic every time.

## Investigation Findings

### 1. Thread Creation: One Per Session, Always

Every call to `_create_session_common()` (`session_manager.py:270`) triggers
`_ensure_telegram_topic()` (`session_manager.py:388-389`), which creates a
Telegram forum topic for the new session.

This includes:
- EM sessions (`sm new`, `/new` from Telegram)
- Child agent sessions (`sm spawn`, `sm dispatch`)
- Review sessions (`sm review`, `spawn_review_session`)
- Any session spawned from the CLI

Child sessions receive `telegram_chat_id = default_forum_chat_id` even when
spawned programmatically (not via Telegram). Result: **every single agent
session — engineer, scout, architect, reviewer — gets its own Telegram topic.**

### 2. Thread Closure: Only Three Paths Trigger Cleanup

`output_monitor.cleanup_session()` is the only function that closes a forum
topic. It runs on exactly three triggers:

| Trigger | Path | Closes topic? |
|---------|------|---------------|
| `sm kill <id>` | `server.py` → `cleanup_session` | ✅ Yes |
| Explicit API kill | `server.py` → `cleanup_session` | ✅ Yes |
| Tmux session dies (detected by monitor) | `output_monitor._handle_session_died` → `cleanup_session` | ✅ Yes (~30s lag) |
| `sm clear` | `server.py` | ❌ No (sends "Context cleared" message only) |
| Claude Code process exits (Stop hook fires) | None | ❌ No |
| ChildMonitor detects completion | `child_monitor._notify_parent_completion` | ❌ No |

**Critical gap**: When Claude Code finishes work and exits naturally, the
_tmux session stays alive_ at a bash prompt. The output_monitor polls for tmux
session existence (~every 30 seconds), but since tmux is alive, it never calls
`cleanup_session`. The topic stays open indefinitely.

**Note on cleanup_session scope**: `cleanup_session` also removes the session
from the `session_manager.sessions` dict (`output_monitor.py:539-545`). This
makes it unsuitable for closing topics after normal task completion — it would
break `sm children --status completed` (`server.py:2043-2044`) and `sm clear`.
The proposed fixes use a narrower `close_session_topic()` helper instead.

### 3. Observed Stale State (2026-02-19)

Current sessions.json contains 13 sessions, all with open Telegram topics,
all with live tmux:

```
thread=12319  engineer-1614          idle  tmux=alive  created=2026-02-18
thread=12326  (unnamed)              idle  tmux=alive  created=2026-02-18
thread=13175  scout-1685-enddate     idle  tmux=alive  created=2026-02-19
thread=13289  spec-reviewer          idle  tmux=alive  created=2026-02-19
thread=13447  em-fractal             idle  tmux=alive  created=2026-02-19
thread=13451  engineer-1702          idle  tmux=alive  created=2026-02-19
thread=13480  architect-pr1706       idle  tmux=alive  created=2026-02-19
thread=13824  em                     idle  tmux=alive  created=2026-02-19
thread=13827  engineer-256           running
thread=13845  scout-269              running
thread=14205  scout-warmup-bugs      running
thread=14434  reviewer-240           running
thread=15325  scout-271              running (this session)
```

All 8 idle sessions have `completion_status=None` — ChildMonitor did not fire
for them. They are parent/EM sessions, or older sessions spawned before
ChildMonitor was in place. Their tmux sessions are alive (Claude Code may have
exited but tmux keeps running), so output_monitor never triggers cleanup.

**Thread accumulation rate**: Thread IDs advanced from 12319 to 15325 in ~2
days. Visible accumulation of 13 open topics confirms the rate.

### 4. The EM Thread Continuity Problem

Two EM sessions exist with open topics: `em` (13824) and `em-fractal` (13447).
Each `sm em` call on a new session creates a new Telegram topic. When the EM
runs `sm handoff`, the old EM's Claude Code exits (tmux stays alive), and a new
EM session is created with a new topic.

The `is_em` flag (added in sm#256) is never set on any current session. Once
sm#256 lands, `sm em` will set `is_em=True` on the calling session. But even
then, each new EM session gets a new Telegram topic via `_ensure_telegram_topic`
during session creation — **before** `sm em` can set `is_em=True`.

Result: the user cannot reliably locate the EM thread because a new one appears
after every `sm handoff`.

### 5. Telegram API Limitation

Telegram Bot API has **no `getForumTopics` endpoint**. We cannot enumerate open
topics programmatically. We can only manage topics by known ID (from
sessions.json). Topics from sessions already removed from sessions.json cannot
be discovered or closed automatically.

### 6. What sm#200 Fixed

sm#200 (PR #268) added `close_forum_topic` to the kill and natural-death paths.
The gap it did not address: Claude Code process exit without tmux death.

---

## Root Cause

Two distinct causes:

**A. Completed sessions keep topics open** — When Claude Code finishes and
exits naturally, the tmux session stays alive. `cleanup_session` only fires on
tmux death or explicit kill, so no topic closure happens. Idle/completed
sessions accumulate open topics until someone runs `sm kill`.

**B. EM has no thread continuity** — Each EM session (created by `sm em` on
a new session) gets a new Telegram topic created during `_create_session_common`
before `sm em` can signal it's an EM session. The user must hunt for the new EM
thread after every `sm handoff`.

---

## Proposed Solution

The fix has three parts. Together they address the full problem:

- Fix A: child sessions via ChildMonitor (sessions spawned with `--wait`)
- Fix B: EM sessions specifically (thread continuity)
- Fix C: all other idle/completed sessions (backlog and non-ChildMonitor cases)

### Fix A: New `close_session_topic()` helper — call from ChildMonitor on completion

**Problem with using `cleanup_session`**: `cleanup_session` removes the session
from `session_manager.sessions`, breaking `sm children --status completed` and
`sm clear`. It also does not close codex-app server sessions (`kill_session`
does that, `session_manager.py:1134-1141`), so calling it from ChildMonitor
would leak codex-app resources.

**The fix**: Add a new `close_session_topic(session)` method on
`OutputMonitor` (or as a standalone helper) that:

1. Sends "Session completed [id]: {message}" to the Telegram topic using
   `send_with_fallback`.
2. Calls `close_forum_topic(chat_id, thread_id)` if in forum mode.
3. Removes the topic from in-memory mappings (`_topic_sessions`, `_session_threads`).
4. Does NOT remove the session from `session_manager.sessions`.
5. Does NOT affect codex-app session state.

**Where to call it**: In `child_monitor._notify_parent_completion()`, after
setting `completion_status = CompletionStatus.COMPLETED`. Add an
`output_monitor` reference to `ChildMonitor` via a setter (analogous to
`set_session_manager`). Call:

```python
if self.output_monitor:
    await self.output_monitor.close_session_topic(
        child_session,
        message=completion_msg or "Completed"
    )
```

**Scope**: Only sessions registered with ChildMonitor (spawned with `--wait`
via `sm dispatch`, `sm spawn --wait`, etc.). Sessions not registered
with ChildMonitor rely on explicit `sm kill` or Fix C for topic cleanup.
Fixes A + C together cover the full range.

### Fix B: EM thread continuity — inherit previous EM topic at `sm em` time

**The lifecycle**: Session is created via `_create_session_common` →
`_ensure_telegram_topic` creates a new topic → user runs `sm em` which sets
`is_em=True`. By the time `is_em=True` is set, the new EM session already has
a freshly-created Telegram topic.

**The fix**: When the endpoint that sets `is_em=True` fires:

1. Read `last_em_topic: {chat_id, thread_id}` from persistent storage (see
   schema below).
2. If a previous EM topic is found with the same `chat_id`:
   a. Call `delete_forum_topic(new_session.telegram_chat_id, new_session.telegram_thread_id)` to remove the newly-created topic.
   b. Set `new_session.telegram_thread_id = last_em_topic["thread_id"]`.
   c. Call `reopenForumTopic(chat_id, thread_id)` (in case old topic was closed).
   d. Post "EM session [new_id] continuing" to the thread.
   e. Update in-memory `_topic_sessions` and `_session_threads` mappings.
3. If no previous EM topic found, keep the newly-created topic (existing
   behavior). Persist `last_em_topic` for future sessions.
4. Write `last_em_topic: {chat_id, thread_id}` to persistent storage.
5. On the OLD EM session(s): set `telegram_thread_id = None` and
   `telegram_chat_id = None` (or leave chat_id if desired, but thread_id must
   be nulled). Remove the old session's entry from `_topic_sessions` and
   `_session_threads` in-memory mappings. This prevents `cleanup_session` from
   closing the shared thread when the old EM session is later killed or dies
   (cleanup_session fires on any session with `telegram_thread_id` set,
   `output_monitor.py:506`).
6. Clear `is_em=True` from any OTHER session in the sessions dict.

**Persistent storage for `last_em_topic`**: Add a top-level field to
sessions.json alongside the `sessions` array:

```json
{
  "sessions": [...],
  "em_topic": {"chat_id": -1003506774897, "thread_id": 13824}
}
```

`_load_state()` reads this field. `_save_state()` writes it whenever it changes.
Schema is backward-compatible (missing field treated as None). No per-session
schema change.

**Fix B failure handling** — fail open, preserve mapping consistency:

Rows are mutually exclusive, keyed on which step failed first:

| Failing step | Precondition | Behavior |
|--------------|--------------|----------|
| `delete_forum_topic(new_topic)` fails | delete not yet run | Log warning. Abort inheritance entirely. Newly-created topic still exists — keep it. Update `last_em_topic` to the new (kept) topic. EM session proceeds with new topic. |
| `reopenForumTopic(old_topic)` fails | delete SUCCEEDED (new topic is gone) | Log warning. Newly-created topic no longer exists. Create a brand-new topic via `create_forum_topic`. Update `last_em_topic` to the brand-new topic. EM session proceeds with brand-new topic. |
| Post-continuation message fails | delete + reopen both succeeded | Log warning only. Continue with inherited topic. Non-critical, no fallback needed. |

**Invariant**: After Fix B runs (success or failure), the session ALWAYS has
a valid `telegram_thread_id` pointing to an open topic. The `last_em_topic` in
sessions.json always points to the most recent successful EM topic.

**Files**: `src/session_manager.py` (add `em_topic` field, persist/load),
`src/server.py` (set `em_topic` when `is_em=True` set; trigger inheritance logic),
`src/telegram_bot.py` (add `delete_forum_topic` call path for the replaced topic).

### Fix C: Backlog cleanup — `close_session_topic()` for stale sessions

Fix C operates in two modes to handle both future completions and the existing
backlog.

**Mode 1 — automated (safe)**: `POST /admin/cleanup-idle-topics` with no body:
1. Iterates all sessions where `completion_status == CompletionStatus.COMPLETED`.
2. Calls `close_session_topic(session, message="Completed")` for each.
3. Returns `{closed: N, skipped: M}`.
4. Does NOT touch sessions with `completion_status=None`.

**Mode 2 — explicit (for backlog)**: `POST /admin/cleanup-idle-topics` with
body `{"session_ids": ["id1", "id2", ...]}`:
1. Closes topics for the exact session IDs listed.
2. Rejects the request if any ID corresponds to a session with
   `is_em=True` or `status=running` (safety guard).
3. Returns `{closed: N, rejected: [{id, reason}]}`.

**Existing backlog** (8 idle sessions with `completion_status=None`): These
are not automatically touched by Mode 1. The user closes them via Mode 2 by
listing specific session IDs once they confirm those sessions are done, OR via
`sm kill` (which already closes topics via sm#200).

**CLI wrapper** (`sm clean [--session-id ID ...]`):
- No args: calls Mode 1 (safe automated cleanup).
- With `--session-id`: calls Mode 2 with the specified IDs.

**What Fix C does NOT do**: Does not remove sessions from the sessions dict.
Does not close topics based on idle time alone (too risky for EM sessions
waiting for user input).

---

## Files to Modify

| File | Change |
|------|--------|
| `src/output_monitor.py` | Add `close_session_topic(session, message)` method |
| `src/child_monitor.py` | Add `output_monitor` setter; call `close_session_topic` on completion |
| `src/session_manager.py` | Add `em_topic` field; persist/load from sessions.json |
| `src/server.py` | Handle `is_em=True`: inherit old EM topic, delete new, persist `em_topic` |
| `src/telegram_bot.py` | Expose `delete_forum_topic` path used by Fix B |

Fix C optionally adds:
| `src/server.py` | `POST /admin/cleanup-idle-topics` endpoint |
| `src/cli/commands.py` | `sm clean` subcommand wrapper |

---

## Test Plan

### Automated (unit/integration)

**Fix A**:
- Unit test: `close_session_topic()` called on a child session with a forum
  topic → `close_forum_topic` called, session remains in `session_manager.sessions`
  dict, `_topic_sessions` mapping cleared.
- Unit test: `close_session_topic()` on a codex-app session → only Telegram
  mappings affected; `codex_sessions` dict unchanged (no resource leak).
- Integration test: Spawn child with `--wait`, simulate completion via
  ChildMonitor → Telegram topic closed, session still visible via
  `sm children --status completed`, `sm clear` still works.

**Fix B**:
- Unit test: `is_em=True` set on session → `last_em_topic` persisted to
  sessions.json.
- Unit test: `is_em=True` set on second session when `last_em_topic` exists
  → newly-created topic deleted, `telegram_thread_id` set to inherited value,
  `reopenForumTopic` called.
- Unit test: No `last_em_topic` in sessions.json → new topic kept (no
  regression).
- Unit test: `_load_state()` reads `em_topic` field correctly; missing field
  → `em_topic=None` (backward compat).

**Fix C**:
- Unit test: `cleanup-idle-topics` with mix of COMPLETED and non-COMPLETED
  sessions → only COMPLETED sessions get `close_session_topic` called.
- Unit test: Running/idle sessions with `completion_status=None` are NOT
  touched.

### Manual smoke test

1. Kill a session — verify topic closed (sm#200 regression check).
2. Clear a session — verify "Context cleared" sent, topic stays open.
3. Run `sm handoff` on EM, create new session, run `sm em` → verify new EM
   session uses same Telegram thread as before, "EM session [id] continuing"
   posted.
4. Spawn child with `--wait 30`, wait for completion → verify topic closed,
   child still visible in `sm children --status completed`.

---

## Classification

**Single ticket**. All three fixes can be delivered by one engineer in a
single PR. Fix C (cleanup endpoint) is optional and can be deferred if schedule
is tight.
