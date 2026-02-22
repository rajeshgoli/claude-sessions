# sm#309: `sm watch` operator controls + observability dashboard

## Problem

`sm watch` works as a session picker, but it is not yet a strong operator dashboard.
Current gaps observed in the implementation and UX:

1. Kill from watch is hard-gated to child sessions only, even when running as an operator outside any managed session.
2. Rows are rendered as free-form strings with no column headers, so scanning is difficult and alignment drifts with tree depth/name length.
3. Visual hierarchy is weak (no color system), so activity states do not stand out.
4. Main list does not expose enough high-value runtime signals (`provider`, raw `status`, and useful "last tool/action" context).
5. No inline drill-down mode for per-session observability without attaching.

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

### 1) Kill behavior is locked to child-only in watch TUI

In `src/cli/watch_tui.py`, `K` handling enforces:
- selected row must have `parent_session_id`
- selected row parent must match `client.session_id` (if set)

This makes sense for in-agent safety, but it blocks operator workflows when `client.session_id` is absent (plain terminal user using `sm watch` as fleet dashboard).

### 2) No true table model

`build_watch_rows()` currently emits one preformatted `text` string per session row (tree prefix + role + activity + age). Because this is monolithic text, there is:
- no header row
- no explicit column boundaries
- no robust truncation/allocation strategy

Result: perceived misalignment and poor readability as tree depth and names vary.

### 3) No color palette initialization/use

`_render()` uses mostly `A_NORMAL`/`A_REVERSE` with no color pairs initialized for state semantics. Status differences are therefore low contrast.

### 4) Missing data plumbing for dashboard fields

`/sessions` already returns `provider`, `status`, and `activity_state`, but watch currently displays only a subset. It also does not expose/use a concise "last tool/action" summary for each row.

### 5) No per-row expand/collapse model

Current watch loop has no row-level expansion state. There is no mechanism to expand one or more selected sessions inline while leaving others collapsed.

## Proposed Solution

## 1) Kill semantics: agent mode vs operator mode

Define watch kill policy by caller context:

- **Agent mode** (`client.session_id` present):
  - unchanged safety: can only kill direct children of current session.
- **Operator mode** (`client.session_id` absent):
  - allow kill of any selected session.

Behavior:
- Keep explicit confirmation prompt (`type yes`).
- Footer/help text is dynamic:
  - agent mode: `K: kill child`
  - operator mode: `K: kill`
- Keep clear flash errors for denied kills in agent mode.

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
- consistent left/right alignment by column type

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

## 4) Main-row data enrichment

Populate/display:
- `provider` (already in `/sessions`)
- raw `status` alongside `activity_state`
- `Last` summary:
  - Claude tmux: last `PreToolUse` timestamp/name when available
  - Codex tmux: no hook-backed tool log today; show `n/a (no hooks)` and rely on thinking/activity age
  - codex-app: latest projected action summary
  - fallback: `-`

To avoid N-per-row DB queries on every refresh:
- expose existing `session.last_tool_call` as lightweight `last_tool_call` (ISO) in session response
- update `session.last_tool_call` in `hook_tool_use` on `PreToolUse` (currently not updated there)
- optionally expose a compact `last_action_summary` for codex-app from projection getter when available

## 5) `Tab`-toggle per-session inline expansion

`Tab` toggles expansion for the currently selected session only.

Model:
- maintain `expanded_session_ids: set[str]`
- when `Tab` is pressed on selected session:
  - if collapsed: add to set (expand)
  - if expanded: remove from set (collapse)
- multiple sessions may be expanded at the same time

Render behavior:
- details are inserted directly under the expanded session row
- other sessions stay collapsed unless explicitly toggled
- tree grouping/navigation remains unchanged

Detail content:

1. **Session metadata**
   - name/id, provider, activity, status, role
2. **Thinking duration**
   - derived from most recent actionable timestamp per provider
3. **Context size**
   - show token count from session context telemetry when available
   - display `n/a` if monitor not enabled/no data
4. **Last 10 tool calls / actions**
   - Claude tmux: query `tool_usage.db` for `PreToolUse` rows (`LIMIT 10`)
   - Codex tmux: show `n/a (no hooks)` (same behavior as `sm children`)
   - codex-app: use `/activity-actions?limit=10`
5. **Last 10 tail lines**
   - fetch `/sessions/{id}/output?lines=10`
   - for codex-app, enforce `lines` semantics server-side (tail last N lines of stored message) so inline panel stays bounded
   - strip ANSI, display clipped lines

Interaction:
- `j/k` or arrows still navigate sessions
- `Tab` toggles expansion for selected row only
- expanded rows remain open while navigating elsewhere

Performance controls:
- detail queries run only for expanded sessions
- per-session detail cache with short TTL (e.g., 1-2s)
- immediate refresh on expand and on manual refresh (`r`)
- non-blocking refresh budget: update expanded-session details in round-robin with per-request timeout (e.g., 200-300ms), keep stale cache on timeout/error
- never block keyboard/render loop waiting on detail I/O

## 6) `n` rename selected session

Add `n` keybinding in watch:
- prompts `name> `
- updates selected session friendly name via existing client endpoint (`PATCH /sessions/{id}`)
- blank input cancels (no change)
- success/failure shown via flash message

This keeps operator workflow in one surface (no need to exit watch just to rename).

## Implementation Approach

### `src/cli/watch_tui.py`
- Introduce structured column row model and header rendering.
- Add color pair setup and state-based styling.
- Add kill policy helper (`can_kill_selected(...)`) with agent/operator mode logic.
- Add per-session expansion state (`expanded_session_ids`) and `Tab` handling.
- Add inline expanded-row renderer and detail data formatting helpers.
- Add detail cache structure + refresh timing with round-robin, bounded per-tick I/O budget.
- Add `n` rename flow + flash messaging.

### `src/cli/commands.py`
- Extend `cmd_watch(...)` to pass optional watch-specific DB override path for tool-history queries.

### `src/cli/main.py`
- Add optional `sm watch --db-path` (default same as `sm tail` / `sm children`) for deterministic environments/tests.

### `src/cli/client.py`
- Add `get_output(session_id, lines=10, timeout=...)` helper for detail tail pane.
- Add optional timeout override for detail helpers used by watch expansion refresh path.
- If needed, add helper(s) for any new watch metadata endpoint fields.

### `src/server.py`
- Extend `SessionResponse` / `_session_to_response()` with watch-needed observability fields:
  - `last_tool_call` (optional ISO)
  - `tokens_used` (context size source)
  - (optional) `context_monitor_enabled` to disambiguate `0` vs unavailable
- Update `hook_tool_use` path to refresh in-memory `session.last_tool_call` on `PreToolUse`.

### `src/session_manager.py`
- Update `capture_output()` for `provider=codex-app` to honor `lines` by returning only trailing N lines.

### `tests/unit/test_watch_tui.py`
- Add tests for:
  - column header + stable formatting/truncation
  - provider/status/last columns present in row output
  - kill policy matrix (agent mode vs operator mode)
  - `Tab` expand/collapse behavior per selected session
  - multi-expanded sessions render inline details independently
  - `n` rename prompt/submit/cancel flows

### `tests/unit/test_cmd_watch.py`
- Assert new `db_path` argument wiring into `run_watch_tui(...)`.

### `tests/unit/test_cli_parsing.py`
- Add parser coverage for `sm watch --db-path`.

### `tests/integration/test_api_endpoints.py` (or equivalent server endpoint suite)
- Add coverage for watch-observability API fields in `/sessions`/`/sessions/{id}` response.
- Add coverage that `PreToolUse` updates `last_tool_call`.
- Add coverage that `/sessions/{id}/output?lines=N` returns bounded tail output for codex-app sessions.

## Test Plan

### Unit

1. Watch row formatting with varied tree depths and long names remains aligned.
2. Color fallback path works when terminal has no color support.
3. Kill authorization logic:
   - agent mode: own child allowed, non-child denied
   - operator mode: any selected session allowed
4. Detail panel:
   - `Tab` expands/collapses selected session inline
   - multiple expanded sessions can remain open
   - detail data refreshes for expanded sessions
   - provider-specific data fallback (`n/a`) does not crash
5. Rename flow:
   - `n` updates friendly name for selected session
   - cancel/blank input leaves session name unchanged
6. Session response model includes new watch fields and remains backward-compatible.
7. `hook_tool_use` (`PreToolUse`) updates `last_tool_call` as expected.
8. `/sessions/{id}/output?lines=N` honors line bounds for codex-app responses.
9. Expanded-row refresh remains responsive under slow API/DB calls (stale-cache fallback, no input-loop freeze).

### Manual

1. Run `sm watch` from plain terminal (no `CLAUDE_SESSION_MANAGER_ID`):
   - `K` can kill selected session after confirmation.
2. Run `sm watch` from within a managed agent:
   - `K` only works for direct children.
3. Validate table readability:
   - headers present
   - columns aligned
   - activity colors visible.
4. Press `Tab`:
   - selected session expands inline with details
   - move to another session and press `Tab`; both can stay expanded
   - last 10 tool calls/actions shown
   - thinking duration and context size visible (or clear `n/a`)
   - last 10 tail lines visible.
5. Press `Tab` again:
   - only that session collapses; other expanded sessions remain open.
6. Press `n`:
   - selected session can be renamed in-place from watch.

## Risks and Mitigations

1. **DB contention on `tool_usage.db`**
   - Mitigation: query only expanded sessions; keep TTL cache.
2. **Terminal compatibility (colors/split panes)**
   - Mitigation: feature-detect color support and degrade to monochrome.
3. **Context size ambiguity when monitoring disabled**
   - Mitigation: expose monitor-enabled bit and render explicit `n/a (monitor off)`.
4. **UI stutter from detail fetch latency**
   - Mitigation: per-request timeout, round-robin refresh budget, stale-cache render on failures/timeouts.

## Ticket Classification

**Single ticket.** Scope is cohesive (watch/dashboard UX + observability plumbing) and can be implemented/tested by one engineer without splitting into an epic.
