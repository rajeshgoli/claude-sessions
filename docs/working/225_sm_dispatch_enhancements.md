# sm#225: sm dispatch enhancements — auto-remind, parent wake-ups, built-in safety nets

**Issue:** https://github.com/rajeshgoli/session-manager/issues/225
**Status:** Draft v1
**Classification:** Epic — see bottom for sub-ticket breakdown

---

## Problem

`sm dispatch` (sm#187, PR #202) sends template-expanded prompts to child agents. `sm remind` (sm#188) provides periodic status reminders when manually armed with `sm send --remind`. `sm wait` fires once when the child goes idle or times out.

EM still manually orchestrates every dispatch:

```bash
sm send <id> "As engineer, implement #1668..." --urgent --remind 180
sm wait <id> 600
```

This is fragile:
- **No auto-remind**: EM must remember to pass `--remind` — forgetting leaves child silent for hours
- **No periodic parent wake-ups**: `sm wait` fires once. If child is stuck mid-task, EM doesn't find out until the watch times out. EM hung 6.5 hours in the past because no watch was re-armed.
- **No progress escalation**: after a silent child gets ESC-interrupted, EM doesn't see this and doesn't change frequency
- **Boilerplate `sm send --remind` separate from dispatch**: coupling EM intent to `sm send` flags instead of dispatch config

---

## Scope of Investigation

Both sm#187 (dispatch) and sm#188 (remind) are fully implemented. This spec covers enhancements to `sm dispatch` that integrate both into a unified EM orchestration command.

### Key Existing Infrastructure

**`sm dispatch`** (`src/cli/dispatch.py`, `src/cli/commands.py:722`):
- Loads YAML templates, expands variables, calls `cmd_send`
- No automatic remind or parent wake-up today
- `notify_on_stop=True` is passed through to `cmd_send`

**`sm send --remind <seconds>`** (`src/cli/commands.py:858`, `src/message_queue.py`):
- Arms periodic remind registration: soft (important, non-interrupting) at N sec, hard (urgent, ESC interrupt) at N+hard_gap sec
- `--remind` fires only if explicitly passed by EM; `sm dispatch` does not pass it today
- Default config: `soft_threshold_seconds: 180`, `hard_gap_seconds: 120` → hard at 300s (5 min)

**`sm wait`** (`src/cli/commands.py:1761`, `src/message_queue.py:_watch_for_idle`):
- One-shot: notifies watcher when target goes idle or when timeout fires
- No periodic notification; no content summary; no escalation logic

**`sm status "<text>"`** (sm#188):
- Agent self-reports status; resets `last_reset_at` on active remind registration
- `session.agent_status_text` and `session.agent_status_at` persisted to `sessions.json`

**`sm tail <id>`** (`src/cli/commands.py:1628`):
- Structured mode: last N `PreToolUse` events from `tool_usage.db`
- Returns tool name, target file, bash command, timestamp

---

## Design

### A. Auto-Remind Integration

`sm dispatch` should automatically arm periodic reminders for the child, replacing the need for `sm send --remind`.

**New thresholds (separate from sm#188 defaults):**
- Soft: 210s (3.5 min) — important, non-interrupting. Deliver at next turn boundary.
- Hard: 420s (7 min) from last status update — urgent, ESC interrupt. Child must respond.

**Why 7 min for hard (not 5 min as in sm#188 defaults):**
- 7 min gives child enough time to background a long operation after the ESC, then call `sm status`
- 5 min (current default) overlaps with child's typical tool call cycle
- Design intent: teach agents that after an ESC, they should not restart the long task inline — background it, call `sm status`, then let the task run

**Config change:**
```yaml
dispatch:
  auto_remind:
    soft_threshold_seconds: 210    # 3.5 min, fires 2× before parent wakes at 10 min
    hard_threshold_seconds: 420    # 7 min — ESC interrupt
```

`sm dispatch` calls `cmd_send` with `remind_soft_threshold=210, remind_hard_threshold=420` on every dispatch. No EM flag needed.

**`--remind` flag behavior on `sm dispatch`:**
Not supported. Remind is always active when using `sm dispatch`. EM can override thresholds via a future `--no-remind` flag if needed, but that is out of scope here.

**`sm send --remind` removal:**
Remove `--remind` from `sm send`. Agent-to-agent sends are fire-and-forget. `sm dispatch` is the only path that arms remind behavior. This is a breaking change: any existing EM workflow using `sm send --remind` must migrate to `sm dispatch`.

Migration: `sm dispatch <id> --role <role> [...]` replaces `sm send <id> "..." --remind 180`.

---

### B. Parent Wake-Ups

`sm dispatch` should periodically wake the parent EM so they can decide without polling.

**Registration:**
On dispatch, register a `ParentWakeRegistration` for the dispatching EM (identified by `CLAUDE_SESSION_MANAGER_ID`). The registration is co-managed with the remind registration — both are cancelled when the child goes idle (stop hook fires).

**Default period: 10 min (600s)**

Every 10 min, the parent receives:

```
[sm dispatch] Child update: <child_name> (<child_id_short>)
Duration: 12m running
Status: "investigating root cause — found 2 call sites" (3m ago)

Recent activity:
  Read: src/cli/commands.py (1m ago)
  Bash: grep -n remind src/ (2m ago)
  Read: docs/working/188_sm_remind.md (4m ago)
  Write: tests/test_dispatch.py (7m ago)
  Bash: python -m pytest tests/ -v (9m ago)
```

This contains everything the EM needs to decide whether to intervene, without any additional tool uses.

**Escalation: 5 min after no-progress**

"No-progress" is defined as: child's `agent_status_at` has not advanced since the last parent wake-up. If the parent is woken and the status timestamp is older than 10 min (i.e., child hasn't called `sm status` since the previous wake), the registration switches to 5 min period.

```
[sm dispatch] Child update: <child_name> — NO PROGRESS DETECTED
Duration: 22m running
Status: "investigating root cause" (13m ago) ← note: unchanged since last update
Warning: No status update in 13m. Hard remind was sent 6m ago.

Recent activity: ...
```

Escalation is one-way in a session: once switched to 5 min, it stays at 5 min for the remainder of the dispatch. EM can manually cancel: `sm remind <child_id> --stop` cancels the child remind; no command needed to cancel parent wake (it stops when child goes idle).

**Timing design:**

| Event | Time from last sm status |
|-------|--------------------------|
| Soft remind to child | 210s (3.5 min) |
| Hard remind (ESC interrupt) | 420s (7 min) |
| Parent wake-up (normal) | 600s (10 min) |
| Parent wake-up (escalated) | 300s (5 min) |

At the 10 min parent wake, the child has already called `sm status` twice (at 3.5 min and 7 min, assuming it responded). Parent sees 2 status updates in the digest.

**Why not 5 min soft remind:**
If soft remind fires at 5 min and parent wake fires at 10 min, there's only a 5 min window for the child to call `sm status` and have it seen. With a 3.5 min soft, by the time parent wakes at 10 min there are 2 status calls — at 3.5 min and ~7+ min (if child responded and got reminded again). Parent sees richer history.

Additionally, if soft remind is at 5 min and parent wakes at 10 min, the child may be mid-status-update when the parent digest is assembled, creating a race condition where parent sees stale status. At 3.5 min soft, child's 2nd update happens around 7 min — 3 min before parent wakes, giving the update time to settle.

---

### C. Parent Wake-Up Message Digest

The parent wake-up message is assembled by the server:

1. **Duration**: elapsed time since dispatch (from registration `registered_at`)
2. **Status text**: `session.agent_status_text` + age from `session.agent_status_at`
3. **Recent activity**: last 5 entries from `tool_usage.db` for the child session — same query `sm tail` uses (`SELECT timestamp, tool_name, target_file, bash_command FROM tool_usage WHERE session_id = ? AND hook_type = 'PreToolUse' ORDER BY timestamp DESC LIMIT 5`)
4. **No-progress flag**: if `session.agent_status_at` has not changed since previous parent wake-up

The digest is assembled server-side in `MessageQueueManager._send_parent_wake_digest()`. The server reads `tool_usage.db` directly (path from config). If the DB is not found, the "Recent activity" section is omitted.

---

### D. Default Templates

Ship a default `~/.sm/dispatch_templates.yaml` with the package. This replaces the sample in sm#187's spec with an actual installable file.

**Proposed defaults:**

```yaml
# ~/.sm/dispatch_templates.yaml
# Default dispatch templates. Edit to customize for your project.

repo:
  path: /path/to/your/repo  # Override this
  pr_target: dev
  test_command: "echo 'Configure test_command in .sm/dispatch_templates.yaml'"

roles:
  engineer:
    template: |
      As engineer, implement GitHub issue #{issue} in {repo.path}.
      Read the spec at {spec}.
      Read personas/engineer.md from ~/.agent-os/personas/.
      Work on a feature branch off {repo.pr_target}, create a PR to {repo.pr_target} when done.
      Run tests when done: {repo.test_command}
      Report the PR number back to me ({em_id}) via sm send.
      {extra}
    required: [issue, spec]
    optional: [extra]

  architect:
    template: |
      As architect, review PR #{pr} in {repo.path}.
      Read personas/architect.md from ~/.agent-os/personas/.
      Read the spec at {spec} for context.
      Report all feedback as blocking.
      Do NOT write code.
      sm send your verdict to me ({em_id}).
      {extra}
    required: [pr, spec]
    optional: [extra]

  scout:
    template: |
      As scout, investigate GitHub issue #{issue} in {repo.path}.
      Read personas/scout.md from ~/.agent-os/personas/.
      Write spec to {spec}.
      Send the spec to codex reviewer ({reviewer_id}) for review via sm send. Iterate with reviewer directly.
      When converged, commit and push, then report completion back to me ({em_id}) via sm send.
      {extra}
    required: [issue, spec, reviewer_id]
    optional: [extra]

  reviewer:
    template: |
      You are a spec reviewer. Working directory: {repo.path}.
      Review protocol is in ~/.agent-os/personas/em.md.
      You will receive a spec from scout agent ({scout_id}) via sm send.
      Classify feedback by severity. Send review to spec owner ({scout_id}) via sm send.
      Stand by.
      {extra}
    required: [scout_id]
    optional: [extra]
```

Templates ship in `src/cli/default_dispatch_templates.yaml`. Installation: `sm setup` (new subcommand, or extend existing `setup.sh`) copies this file to `~/.sm/dispatch_templates.yaml` if it doesn't already exist (never overwrites existing config).

---

## Implementation Approach

### Sub-ticket 1: Auto-remind in sm dispatch (new dispatch thresholds + auto-arm)

**Files changed:**

`config.yaml`:
- Add `dispatch.auto_remind.soft_threshold_seconds: 210`
- Add `dispatch.auto_remind.hard_threshold_seconds: 420`

`src/cli/commands.py:cmd_dispatch`:
- Read `dispatch.auto_remind` from config via client (or read config file directly)
- Pass `remind_soft_threshold` and `remind_hard_threshold` to `cmd_send` on every dispatch (no flag needed)
- `cmd_send` already accepts these params and wires them through to the queue manager

**Note:** `cmd_dispatch` currently calls `cmd_send(client, agent_id, expanded, delivery_mode, notify_on_stop=notify_on_stop)`. This must be extended to pass remind thresholds:
```python
return cmd_send(
    client, agent_id, expanded, delivery_mode,
    notify_on_stop=notify_on_stop,
    remind_soft_threshold=config.dispatch.auto_remind.soft_threshold_seconds,
    remind_hard_threshold=config.dispatch.auto_remind.hard_threshold_seconds,
)
```

The config is loaded by the server; `cmd_dispatch` must fetch it via `client.get_config()` (add endpoint) or read `config.yaml` locally using the existing path discovery. Simpler: add `auto_remind_soft` and `auto_remind_hard` as constants in `dispatch.py` (read from env var or config file at dispatch time).

### Sub-ticket 2: Remove --remind from sm send

**Files changed:**

`src/cli/main.py`:
- Remove `--remind` argument from `send_parser`
- Remove remind_seconds extraction from send args

`src/cli/commands.py:cmd_send`:
- Remove `remind_seconds` parameter
- Remove remind threshold computation block

`src/cli/client.py`:
- Optionally keep `remind_soft_threshold`/`remind_hard_threshold` in `send_input()` — only called from `cmd_dispatch` now

Any EM workflow using `sm send --remind` must migrate to `sm dispatch`. Document the migration in `CHANGELOG.md`.

### Sub-ticket 3: Parent wake-up registration

**Files changed:**

`src/models.py`:
- Add `ParentWakeRegistration` dataclass:
  ```python
  @dataclass
  class ParentWakeRegistration:
      id: str
      child_session_id: str
      parent_session_id: str
      period_seconds: int           # 600 initially, 300 after escalation
      registered_at: datetime
      last_wake_at: Optional[datetime]     # None before first wake
      last_status_at_prev_wake: Optional[datetime]  # agent_status_at at last wake
      escalated: bool = False
      is_active: bool = True
  ```

`src/message_queue.py`:
- New SQLite table `parent_wake_registrations`
- `_parent_wake_registrations: Dict[str, ParentWakeRegistration]` keyed by `child_session_id` (one per child)
- `register_parent_wake(child_session_id, parent_session_id, period_seconds) -> str` — called from delivery hook (same delivery-triggered pattern as remind registration)
- `cancel_parent_wake(child_session_id)` — called from stop hook, `sm clear`, `sm kill`
- `_run_parent_wake_task(child_session_id)` async task:
  1. Wait `period_seconds`
  2. Assemble digest (see Section C)
  3. Check escalation: if `session.agent_status_at == last_status_at_prev_wake` → escalate (set `period_seconds=300`, `escalated=True`)
  4. Queue digest as important message to parent
  5. Update `last_wake_at`, `last_status_at_prev_wake`
  6. Loop
- `_assemble_parent_wake_digest(child_session_id, registration) -> str` — builds message text
- `_read_child_tail(child_session_id, n=5) -> list[dict]` — reads `tool_usage.db` for child session

Delivery-triggered registration: in `_try_deliver_messages` and `_deliver_urgent`, after remind registration is armed, also call `register_parent_wake(target_session_id, sender_session_id, 600)` if sender_session_id is available. Sender session ID is the `em_id` in dispatch context — passed through `QueuedMessage` as a new optional field `parent_session_id`.

`src/message_queue.py` queue_message:
- Add `parent_session_id: Optional[str]` to `QueuedMessage`
- Persist to DB column `parent_session_id TEXT`
- Read on delivery; if set and `remind_soft_threshold` is also set, register both child remind and parent wake

`src/server.py`:
- Update `mark_session_idle` (stop hook path) to call `cancel_parent_wake(session_id)`
- Update `clear_session` handler to call `queue_mgr.cancel_parent_wake(session_id)`
- Update `kill_session` handlers to call `cancel_parent_wake`
- `_recover_parent_wake_registrations()` — restart tasks on server startup from DB

`src/cli/commands.py:cmd_dispatch`:
- Pass `em_id` as `parent_session_id` in the `cmd_send` call chain

`src/cli/client.py:send_input`:
- Accept and pass `parent_session_id: Optional[str]`

### Sub-ticket 4: Default dispatch templates + sm setup

**Files changed / added:**

`src/cli/default_dispatch_templates.yaml` (new file):
- Contains engineer, architect, scout, reviewer templates as shown in Section D

`src/cli/commands.py`:
- Add `cmd_setup(overwrite=False)` — copies default templates to `~/.sm/dispatch_templates.yaml`; exits with message if file exists and `overwrite=False`

`src/cli/main.py`:
- Add `setup` subparser: `sm setup [--overwrite]`

`setup.sh`:
- Call `sm setup` after installation if `~/.sm/dispatch_templates.yaml` doesn't exist

---

## Integration: Unified sm dispatch Flow

After all sub-tickets:

```bash
sm dispatch <child_id> --role engineer --issue 1668 --spec docs/working/1668.md
```

This single command:
1. Expands template → sends to child
2. Arms child remind: soft at 3.5 min, hard ESC at 7 min
3. Arms parent wake: every 10 min with digest; escalates to 5 min if child silent

EM sees periodic digests. If child goes idle (task done), both registrations cancel. EM is woken by the stop notification and sees the final digest.

---

## Test Plan

### Sub-ticket 1: Auto-remind in dispatch

1. `sm dispatch <id> --role engineer ...` → verify `remind_soft_threshold=210, remind_hard_threshold=420` passed to queue manager
2. Child receives soft remind at ~210s (no `sm status` call)
3. Child receives hard remind (ESC interrupt) at ~420s
4. After child calls `sm status "working"`, remind timer resets; soft fires at 210s from status call

### Sub-ticket 2: sm send --remind removal

5. `sm send <id> "msg" --remind 180` → error: unknown argument `--remind`
6. Existing EM workflow using `sm dispatch` continues to receive reminders without explicit flag

### Sub-ticket 3: Parent wake-ups

7. `sm dispatch <id> --role engineer ...` → parent (EM) receives wake digest after 10 min
8. Digest contains: duration, child status text with age, last 5 tool events
9. If child not calling `sm status`: at 10 min wake, parent sees status timestamp unchanged → next wake at 5 min (escalation)
10. Digest labels escalated state: "NO PROGRESS DETECTED", previous status age
11. Child goes idle (stop hook) → both remind and parent wake registrations cancel; no further digests
12. `sm clear <child_id>` → both registrations cancel
13. `sm kill <child_id>` → both registrations cancel
14. Crash recovery: server restart preserves active parent wake registration; task resumes with adjusted remaining time

### Sub-ticket 4: Default templates

15. `sm setup` → creates `~/.sm/dispatch_templates.yaml` with engineer, architect, scout, reviewer roles
16. `sm setup` when file exists → prints message, does not overwrite
17. `sm setup --overwrite` → overwrites existing
18. `sm dispatch <id> --role engineer --issue 123 --spec docs/working/123.md` → successful expansion using default templates

---

## Non-goals / Exclusions

- **Telegram mirroring of parent digests:** Not in scope for MVP. Add in a follow-up.
- **Per-role remind threshold override:** EM can configure via template YAML in future. Not in scope.
- **`sm send --remind` deprecation period:** Breaking change applied directly. No deprecation window needed — `sm dispatch` is the correct replacement.
- **Multi-child tracking:** Parent wake is per-dispatch (per child). One EM dispatching 3 engineers gets 3 separate wake streams. Aggregated EM dashboard is future work.

---

## Epic Structure

This is an epic. Four sub-tickets, each atomic and testable independently:

| # | Title | Depends On | Scope |
|---|-------|-----------|-------|
| A | Auto-remind in sm dispatch | sm#188 (✓ done) | `cmd_dispatch`, `config.yaml` |
| B | Remove --remind from sm send | A | `main.py`, `commands.py`, `client.py` |
| C | Parent wake-up registration + digest | A | `message_queue.py`, `server.py`, `models.py` |
| D | Default templates + sm setup | sm#187 (✓ done) | `default_dispatch_templates.yaml`, `commands.py`, `main.py` |

**Ordering:** A → B (B removes what A supersedes). C depends on A (parent wake arms alongside remind). D is independent.

File sub-tickets after architect review. Use this spec as the source of truth — all sub-ticket bodies should reference this doc.
