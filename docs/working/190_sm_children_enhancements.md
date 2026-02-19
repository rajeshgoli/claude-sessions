# sm children: Show Thinking Duration + Last Tool Use

**Issue:** #190
**Status:** Spec — rev4 (post-review)

---

## Problem

`sm children` shows "running" or "idle" with no operational detail. EM cannot distinguish between an agent deep in thought (normal) and an agent stuck in a loop (needs intervention). This leads to either premature nudging (wastes agent work) or late nudging (wastes time).

---

## Investigation

### Current `sm children` output

```
sm-engineer (66a8c9ee) | running | 2m ago
app-scout   (340d0709) | running | 5s ago
app-codex   (d638c897) | idle    | unknown
```

`last_activity` in the response comes from `s.last_activity.isoformat()` in `list_children_sessions` (server.py:2012) — this is the session manager's own `last_activity` field on the session object, updated when SM last heard from the session (e.g., status changes, messages), not from agent tool calls. This is why it's not a reliable indicator of current agent activity.

### Signal 1: Last Tool Use (tool_usage.db)

**DB path:** `~/.local/share/claude-sessions/tool_usage.db`
**Confirmed active** with 670k+ records logged.

Relevant schema:
```sql
session_id TEXT       -- Our CLAUDE_SESSION_MANAGER_ID
hook_type TEXT        -- PreToolUse or PostToolUse
tool_name TEXT        -- Read, Write, Edit, Bash, Grep, Glob, Task, etc.
target_file TEXT      -- For Read/Write/Edit
bash_command TEXT     -- For Bash
timestamp DATETIME    -- UTC naive string: "2026-02-19 05:37:41"
```

Query to get last tool use per session:
```sql
SELECT tool_name, target_file, bash_command, timestamp
FROM tool_usage
WHERE hook_type = 'PreToolUse' AND session_id = ?
ORDER BY timestamp DESC
LIMIT 1
```

**Confirmed working** against live sessions. Current session `340d0709` returns correct entries.

**Codex CLI sessions: no data.** `~/.codex/config.toml` has no hooks configured. Codex CLI does not use Claude Code's hook system. Sessions `94f9dac5`, `d638c897`, `0afe1d07` were confirmed to have **zero entries** in `tool_usage.db`. This matches the finding in sm#189 spec.

### Signal 2: Thinking Duration

**Issue's proposed approach: `#{pane_last_activity}` from tmux**

Tested against all active sessions:
```
claude-340d0709: pane_last_activity=  (empty)
claude-c3bbc6b9: pane_last_activity=  (empty)
codex-94f9dac5:  pane_last_activity=  (empty)
```

`#{pane_last_activity}` requires `monitor-activity on` (per-window option). Globally: `monitor-activity off`. All sessions return empty — the proposed tmux approach does not work without enabling monitor-activity.

**`#{session_activity}` works, but is invalid for Claude sessions:**

```
claude-c3bbc6b9: session_activity=1771480032  (works, no monitor-activity needed)
codex-94f9dac5:  session_activity=1771463530  (works)
```

However, Claude Code displays a real-time spinner ("Thinking...") in the terminal while waiting for the API response. This means `session_activity` is updated **continuously during LLM generation**, not just when a tool is called. For an active Claude agent, `session_activity` ≈ "just now" regardless of whether it's thinking or working. **`session_activity` must not be used for claude sessions.**

**Correct approach per provider:**

| Provider   | Thinking duration source                                               |
|------------|------------------------------------------------------------------------|
| claude     | `datetime.utcnow() - max(timestamp WHERE PreToolUse)` from tool_usage.db |
| codex      | `time.time() - #{session_activity}` from tmux (Unix epoch)             |
| codex-app  | N/A (headless, no tmux pane) — omit entirely                           |

No tmux fallback for claude. If tool_usage.db has no entry for a claude session, thinking duration is omitted.

### Signal 2: Timestamp Gotcha (same as sm#189)

Timestamps in tool_usage.db are UTC naive strings (`"2026-02-19 05:37:41"`). `format_relative_time` in `formatting.py` compares against `datetime.now()` (local time). On a UTC-offset host, the delta goes negative. Thinking duration must be computed inline using `datetime.utcnow()`, not via `format_relative_time`.

### Pre-existing Bug in cmd_children

`cmd_children` currently passes `datetime` objects (not strings) to `format_relative_time`, which expects an ISO string (confirmed in `src/cli/formatting.py:7`). This causes `elapsed` to silently fall back to `"unknown"` due to the bare `except:`. This bug predates this ticket and is not introduced by this change.

**Decision: Fix in scope.** The existing `last_activity` display is broken. This ticket touches the same print line. The engineer should fix `format_relative_time(activity_time)` → `format_relative_time(last_activity)` (pass the raw ISO string) as part of this implementation. The `agent_status_at` path has the same issue.

### Shared Logic with sm#189 (sm tail)

`sm tail` (#189) established: CLI-side SQLite query, no server roundtrip, `datetime.utcnow()` for UTC comparison. The per-session last-tool query is identical. In v1, inline the query directly in `cmd_children` (no premature abstraction). If both sm tail and sm children are being implemented in the same session, extracting a shared helper is reasonable.

### EM Decision Matrix (from issue)

| Thinking time | Last tool use | Interpretation |
|--------------|---------------|----------------|
| <2m          | any           | Normal operation |
| 2–10m        | recent (<2m)  | Deep thinking between tool calls — working |
| 10m+         | stale (10m+)  | Likely stuck — nudge |
| 10m+         | recent (<2m)  | Reading/processing complex output — let it work |

---

## Proposed Solution

Enhance `cmd_children` in `src/cli/commands.py` to query `tool_usage.db` (claude) or tmux `session_activity` (codex) per running child, then display `thinking duration` and `last tool` inline.

### Line Composition (exact ordering)

The full output line for a running session, preserving existing #188 fields:

```
{name} ({id}) | {status} | {elapsed} [| thinking Xs] [| last tool: X] [| "{agent_status_text}"({age} ago)] [| "{completion_msg}"]
```

Rules:
- `elapsed`: the existing `last_activity` relative time — **fixed** to pass ISO string to `format_relative_time`
- `thinking Xs` and `last tool: X`: appended only when `status == "running"`; omitted for idle/completed
- `agent_status_text` / `completion_msg`: preserved exactly as today (appended last, unchanged logic)
- For codex sessions: `last tool: n/a (no hooks)` — explicit, not silent
- If tool_usage.db is unavailable (file missing, locked): skip thinking/last-tool columns with a single `stderr` warning on first failure; do not repeat per-session

### Thinking Duration Display Format

Same as sm tail: `Xm Ys` for ≥1 minute, `Xs` for <1 minute. Sub-minute precision matters since agents can make multiple tool calls per minute.

### Example Output

```
$ sm children
sm-engineer (66a8c9ee) | running | 2m ago | thinking 4m32s | last tool: Edit src/cli/commands.py (4m ago)
app-scout   (340d0709) | running | 5s ago | thinking 12s   | last tool: Bash: pytest (12s ago)
app-codex   (d638c897) | running | 8m ago | thinking 8m21s | last tool: n/a (no hooks)
app-idle    (bed4ab86) | idle    | 1h ago |
```

---

## Implementation Approach

### Files Changed

| File | Change |
|------|--------|
| `src/server.py` | Add `provider` field to `list_children_sessions` response (1 line) |
| `src/cli/commands.py` | Fix `format_relative_time` call; add `_query_last_tool`, `_get_tmux_session_activity`; modify `cmd_children` |
| `src/cli/main.py` | Add `--db-path` arg to `children` subparser; pass to `cmd_children` call |
| `tests/unit/test_cmd_children.py` | New file: behavior tests for output ordering, thinking/last-tool display, provider paths, DB warning, #188 regression |
| `tests/unit/test_cli_parsing.py` | Add parsing test for `--db-path` on `children` subcommand |

### 1. Server: Add `provider` to children response (server.py)

```python
# In list_children_sessions, add to the dict comprehension:
"provider": s.provider,
```

One-line addition. Required so the CLI can choose the correct thinking-duration source without fragile prefix detection.

### 2. CLI: `_query_last_tool` helper

```python
def _query_last_tool(session_id: str, db_path: str) -> Optional[dict]:
    """
    Query tool_usage.db for the most recent PreToolUse event for a session.

    Returns dict with: tool_name, target_file, bash_command, timestamp_str (UTC)
    Returns None if DB unavailable or no entries found.
    """
```

### 3. CLI: `_get_tmux_session_activity` helper

```python
def _get_tmux_session_activity(tmux_session_name: str) -> Optional[int]:
    """
    Returns Unix epoch of last tmux session activity via:
      tmux display-message -p -t <name> '#{session_activity}'
    Returns None if session not found or tmux unavailable.
    """
```

### 4. CLI: Modify `cmd_children`

For each child:
- Derive `tmux_session = f"{provider}-{child_id}"` (no slicing — `id` is the full session ID from the API)
- For `status == "running"`:
  - If `provider == "claude"`: call `_query_last_tool(child_id, db_path)` for both thinking duration and last tool
  - If `provider == "codex"`: call `_get_tmux_session_activity(tmux_session)` for thinking duration; last tool = `"n/a (no hooks)"`
  - If `provider == "codex-app"`: skip both signals
- Fix the broken `format_relative_time` call: pass `last_activity` (ISO string) not the parsed `datetime`
- Same fix for `agent_status_at` path
- DB path defaults to `~/.local/share/claude-sessions/tool_usage.db`; expose as `--db-path` (consistent with sm#189)

### 5. main.py: Wire `--db-path` into children subparser

```python
# In children_parser block (src/cli/main.py, near line 181):
children_parser.add_argument("--db-path", default=None, help="Override tool_usage.db path")
```

And in the dispatch block (near line 415):
```python
commands.cmd_children(client, parent_id, args.recursive, args.status, args.json, args.db_path)
```

`cmd_children` signature gains `db_path: Optional[str] = None`.

---

## Test Plan

1. **`_query_last_tool`**
   - Returns correct fields when DB has entries for a session
   - Returns None when DB has no entries (e.g., codex session ID)
   - Returns None when DB path does not exist

2. **`_get_tmux_session_activity`**
   - Mock subprocess: asserts correct tmux command invoked
   - Returns int epoch on valid output
   - Returns None on empty/error output

3. **`cmd_children` output**
   - Running claude session with DB entry: shows `thinking Xs | last tool: <name>: <detail> (Ys ago)`
   - Running codex session (no DB entry): shows `thinking Xs | last tool: n/a (no hooks)`
   - Idle session: no thinking/tool columns
   - DB unavailable: completes without error, skips signals, one warning to stderr
   - `agent_status_text` still appended after thinking/tool fields (regression: #188 behavior)
   - `completion_msg` still appended for completed sessions

4. **`format_relative_time` fix**
   - `elapsed` field no longer shows `"unknown"` for sessions with valid `last_activity` timestamps

5. **Manual smoke test**
   - Run `sm children` with live running children
   - Confirm thinking duration increases between refreshes
   - Confirm last tool name/detail match visible pane output

---

## Ticket Classification

**Single ticket.** Changes span 5 files: server (1 line), CLI commands (~80 lines), CLI main (~5 lines for parser + dispatch), and two test files. An engineer can complete this without context compaction. Builds directly on the `sm tail` pattern from #189.

**Soft dependency:** sm#189 (sm tail) ideally merged first to share discovery context, but not a hard blocker.
