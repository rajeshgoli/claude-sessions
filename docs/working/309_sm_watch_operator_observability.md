# sm#309: `sm watch` operator controls + observability dashboard

## Problem

`sm watch` works as a session picker, but it is not yet a strong operator dashboard.
Current gaps observed in the implementation and UX:

1. `sm watch` is currently launchable from managed agent sessions; this surface should be operator-only.
2. Kill from watch is hard-gated to child sessions only, which blocks operator fleet-control workflows.
3. Rows are rendered as free-form strings with no column headers, so scanning is difficult and alignment drifts with tree depth/name length.
4. Visual hierarchy is weak (no color system), so activity states do not stand out.
5. Main list does not expose enough high-value runtime signals (`provider`, raw `status`, and reliable "last tool/action" context).
6. No inline drill-down mode for per-session observability without attaching.

User request for this ticket:
- enable kill from watch for operator use
- improve table UX (headers, alignment, colors)
- show provider + status + last tool call in main view
- `Tab` toggles per-session inline details with:
  - last 10 tool calls
  - thinking duration
  - context size
  - last 10 tail lines
- add `n` to rename the selected session

## Root Cause Analysis

### 0) Watch has no operator-only gate

`sm watch` is available through normal command dispatch with no dedicated operator gate. In practice this means managed agent shells can enter watch unless separately blocked.

### 1) Kill behavior is locked to child-only in watch TUI

In `src/cli/watch_tui.py`, `K` handling enforces:
- selected row must have `parent_session_id`
- selected row parent must match `client.session_id` (if set)

This is safety-oriented for agent mode, but it blocks operator workflows.

### 2) No true table model

`build_watch_rows()` currently emits one preformatted `text` string per session row (tree prefix + role + activity + age). Because this is monolithic text, there is:
- no header row
- no explicit column boundaries
- no robust truncation/allocation strategy

Result: perceived misalignment and poor readability as tree depth and names vary.

### 3) No color palette initialization/use

`_render()` uses mostly `A_NORMAL`/`A_REVERSE` with no color pairs initialized for state semantics. Status differences are therefore low contrast.

### 4) Missing data plumbing for dashboard fields

`/sessions` already returns `provider`, `status`, and `activity_state`, but watch currently displays only a subset. It also lacks a stable, cheap "last action" payload shape for main-row display without per-row DB/API fan-out.

### 5) No per-row expand/collapse model

Current watch loop has no row-level expansion state. There is no mechanism to expand one or more selected sessions inline while leaving others collapsed.

### 6) Current I/O model would block the curses loop

Watch client/server/database calls are synchronous today. If detail fetches run directly in the input/render loop, UI responsiveness degrades under slow API/DB reads.

## Proposed Solution

## 0) Operator-only availability policy

Define `sm watch` as operator-only for now.

Behavior:
- In `src/cli/main.py`, reject `sm watch` when `CLAUDE_SESSION_MANAGER_ID` is set.
- Error text is explicit: watch is operator-only; run from a non-managed shell.
- No password flow in this ticket.

Security note:
- This is a policy/UX guard for accidental in-agent usage.
- It is not a hard security boundary against same-user local bypass.

## 1) Kill semantics in watch (operator surface)

Because watch is operator-only:
- allow kill of any selected session
- keep explicit confirmation prompt (`type yes`)
- footer/help text uses `K: kill`

No server API change required; existing endpoint already supports unrestricted kill when requester is omitted.

## 2) Convert list to real columns with header

Refactor watch row rendering from one free-form text string to structured columns:

- `Session` (tree + name + id)
- `Role`
- `Provider`
- `Activity` (working/thinking/idle/...)
- `Status` (running/idle/stopped)
- `Last`
- `Age`

Add:
- column header row under top title
- deterministic width allocation + truncation
- consistent alignment by column type

Grouping by repo and tree hierarchy remain unchanged.

## 3) Add color semantics

Initialize curses colors (fallback to monochrome when unavailable):

- header/repo rows: bold cyan
- selected row: reverse + bold
- activity colors:
  - `working`: green
  - `thinking`: yellow
  - `waiting_permission`: red
  - `idle`: dim/white
  - `stopped`: red dim
- flash messages:
  - success: green
  - warning/error: yellow/red

## 4) Main-row data enrichment with explicit payloads

Populate/display:
- `provider`
- raw `status` alongside `activity_state`
- `Last` summary via cheap, precomputed response fields

Field model for watch rows:
- `last_tool_call` (optional ISO timestamp)
- `last_tool_name` (optional string; Claude hook path)
- `last_action_summary` (optional string; codex-app projection)
- `last_action_at` (optional ISO timestamp; codex-app projection)
- `tokens_used` (int)
- `context_monitor_enabled` (**non-optional bool**)

Provider behavior:
- Claude tmux: `last_tool_name` + age from `last_tool_call` when available
- Codex tmux: show `n/a (no hooks)`
- codex-app: `last_action_summary` (+ age from `last_action_at`) when available
- fallback: `-`

To avoid N-per-row DB queries on every refresh:
- update `session.last_tool_call` and `session.last_tool_name` in `hook_tool_use` on `PreToolUse`
- expose codex-app compact summary/timestamp from existing projection getter
- keep watch main list on `/sessions` data only

## 5) `Tab`-toggle per-session inline expansion

`Tab` toggles expansion for the currently selected session only.

Model:
- maintain `expanded_session_ids: set[str]`
- toggle selected session membership on `Tab`
- multiple sessions may remain expanded simultaneously

Detail content:
1. **Session metadata**
   - name/id, provider, activity, status, role
2. **Thinking duration**
   - derived from most recent actionable timestamp per provider
3. **Context size**
   - if `context_monitor_enabled == true`: show `tokens_used`
   - else: show `n/a (monitor off)`
4. **Last 10 tool calls / actions**
   - Claude tmux: query `tool_usage.db` for `PreToolUse` rows (`LIMIT 10`)
   - Codex tmux: show `n/a (no hooks)`
   - codex-app: use `/activity-actions?limit=10`
5. **Last 10 tail lines**
   - fetch `/sessions/{id}/output?lines=10`
   - for codex-app, enforce `lines` semantics server-side (tail last N lines of stored message)
   - strip ANSI and clip by width

### Non-blocking refresh architecture

To keep keyboard/render responsive:
- run detail fetches in a background worker (thread) with request queue + shared cache
- curses loop only reads cached detail snapshots (never direct blocking I/O)
- on expand/manual refresh (`r`), enqueue fetch work for affected sessions
- worker uses bounded per-request timeouts and round-robin scheduling
- stale cache remains visible on timeout/error

## 6) `n` rename selected session

Add `n` keybinding in watch:
- prompts `name> `
- calls existing `PATCH /sessions/{id}` path
- blank input cancels (no change)
- success/failure shown via flash message

Because watch is operator-only, this is operator rename behavior in a single surface.

## Implementation Approach

### `src/cli/main.py`
- Add operator-only gate for `sm watch`:
  - if `CLAUDE_SESSION_MANAGER_ID` is present, print explicit error and exit non-zero.

### `src/cli/watch_tui.py`
- Introduce structured column row model and header rendering.
- Add color pair setup and state-based styling.
- Simplify kill path to operator behavior (no child-parent checks inside watch).
- Add per-session expansion state (`expanded_session_ids`) and `Tab` handling.
- Add background detail-fetch worker + cache (non-blocking loop contract).
- Add inline expanded-row renderer and detail formatting helpers.
- Add `n` rename flow + flash messaging.

### `src/cli/client.py`
- Add `get_output(session_id, lines=10, timeout=...)` helper for tail panel.
- Add timeout override parameters for watch detail helpers.

### `src/server.py`
- Extend `SessionResponse` / `_session_to_response()` with watch fields:
  - `last_tool_call`
  - `last_tool_name`
  - `last_action_summary`
  - `last_action_at`
  - `tokens_used`
  - `context_monitor_enabled` (required bool)
- Update `hook_tool_use` so `PreToolUse` refreshes in-memory `session.last_tool_call` + `session.last_tool_name`.

### `src/session_manager.py`
- Update `capture_output()` for `provider=codex-app` to honor `lines` by returning only trailing N lines.

### `tests/unit/test_watch_tui.py`
- Add tests for:
  - column header + stable formatting/truncation
  - provider/status/last columns present in row output
  - operator kill behavior for selected sessions
  - `Tab` expand/collapse behavior per selected session
  - multi-expanded sessions render inline details independently
  - `n` rename prompt/submit/cancel flows
  - render loop remains responsive while detail worker is slow

### `tests/unit/test_cli_main.py` (new) or equivalent main-dispatch test suite
- Add coverage that managed-session invocation of `sm watch` is rejected when `CLAUDE_SESSION_MANAGER_ID` is present.

### `tests/integration/test_api_endpoints.py` (or equivalent server endpoint suite)
- Add coverage for new watch observability fields in `/sessions`/`/sessions/{id}`.
- Add coverage that `PreToolUse` updates `last_tool_call` and `last_tool_name`.
- Add coverage that `/sessions/{id}/output?lines=N` returns bounded tail output for codex-app sessions.

## Test Plan

### Unit

1. `sm watch` is rejected in managed shells (`CLAUDE_SESSION_MANAGER_ID` set).
2. Watch row formatting with varied tree depths and long names remains aligned.
3. Color fallback path works when terminal has no color support.
4. Kill behavior:
   - selected session can be killed in operator watch after confirmation.
5. Detail panel:
   - `Tab` expands/collapses selected session inline
   - multiple expanded sessions can remain open
   - provider-specific data fallback (`n/a`) does not crash
6. Rename flow:
   - `n` updates friendly name for selected session
   - cancel/blank input leaves session name unchanged
7. Session response model includes new watch fields and remains backward-compatible.
8. `hook_tool_use` (`PreToolUse`) updates `last_tool_call` and `last_tool_name`.
9. `/sessions/{id}/output?lines=N` honors line bounds for codex-app responses.
10. Slow detail fetches do not freeze input/render loop (stale-cache fallback).

### Manual

1. Run `sm watch` from plain terminal (no `CLAUDE_SESSION_MANAGER_ID`):
   - watch opens normally.
   - `K` can kill selected session after confirmation.
2. Run `sm watch` from within a managed agent session:
   - command exits with explicit operator-only error.
3. Validate table readability:
   - headers present
   - columns aligned
   - activity colors visible
4. Press `Tab`:
   - selected session expands inline with details
   - move to another session and press `Tab`; both can stay expanded
   - last 10 tool calls/actions shown
   - context size shows token count or `n/a (monitor off)`
   - last 10 tail lines visible
5. Press `Tab` again:
   - only that session collapses; other expanded sessions remain open.
6. Press `n`:
   - selected session can be renamed in-place from watch.

## Risks and Mitigations

1. **This is policy gating, not hard local-security isolation**
   - Mitigation: document operator-only intent; treat this as accidental-misuse prevention.
2. **DB contention on `tool_usage.db`**
   - Mitigation: query only expanded sessions; keep TTL cache.
3. **Terminal compatibility (colors/split panes)**
   - Mitigation: feature-detect color support and degrade to monochrome.
4. **Context size ambiguity when monitoring disabled**
   - Mitigation: expose `context_monitor_enabled` as required bool and render explicit `n/a (monitor off)`.
5. **UI stutter from detail fetch latency**
   - Mitigation: background worker, bounded timeouts, stale-cache render.

## Ticket Classification

**Single ticket.** Scope is cohesive (operator-only watch UX + dashboard observability plumbing) and can be implemented/tested by one engineer without splitting into an epic.
