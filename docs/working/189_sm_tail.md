# sm tail: Quick Agent Output Visibility

**Issue:** #189
**Status:** Spec — rev2 (post-review)

---

## Problem

EM has no quick way to see what an agent is doing:

- `sm what` — Haiku AI summarization. Imprecise, costs tokens, sometimes fails.
- `sm output` — Raw tmux dump. Floods EM context with 30+ lines of TUI chrome.
- `sm children` — Shows "running" or "idle" only. No detail.

## Investigation

### tool_usage.db — Confirmed Active

**DB path:** `~/.local/share/claude-sessions/tool_usage.db`

The DB is actively populated. As of investigation:
- **670,295 total records** across all sessions
- Both `PreToolUse` and `PostToolUse` events are logged per tool call
- Session_id, session_name, tool_name, target_file, bash_command, and timestamp are all available and well-populated
- **118,098 records** have `NULL session_id` — sessions not managed by SM or env not yet injected at hook time

Schema columns relevant to `sm tail`:
```
session_id TEXT        -- Our CLAUDE_SESSION_MANAGER_ID
session_name TEXT      -- Friendly name (may change mid-session)
hook_type TEXT         -- PreToolUse or PostToolUse
tool_name TEXT         -- Read, Write, Edit, Bash, Grep, Glob, Task, etc.
target_file TEXT       -- For Read/Write/Edit operations
bash_command TEXT      -- For Bash tool calls
timestamp DATETIME     -- When the event occurred
```

### Hook Coverage

**Claude sessions:** Hooks fully configured in `~/.claude/settings.json`:
- `PreToolUse` → `log_tool_use.sh`
- `PostToolUse` → `log_tool_use.sh`

**Codex CLI sessions:** `~/.codex/config.toml` has **no hooks configured**. Codex CLI does not use Claude Code's hook system. Structured mode will show no data for Codex agents.

**Codex-app sessions:** Headless, no Claude Code hooks. Codex-app sessions should fall back to raw mode using `get_last_message`.

### Deduplication

The DB logs **both** PreToolUse and PostToolUse per tool call. Showing both would double-count every action. The structured view must filter to `PreToolUse` only (when the tool was invoked). PostToolUse data (e.g., `exit_code`) is available for future enhancement but not surfaced in v1.

### Timestamp Format

Timestamps are stored as UTC naive strings (`2026-02-19 05:37:41`).

**`format_relative_time` in `formatting.py` cannot be used here** for two reasons:
1. It accepts a string and parses it against `datetime.now()` (local naive). On a UTC-offset host, the delta goes negative, producing wrong output.
2. It returns strings like `"1min ago"` — appending ` ago` again in the caller would produce `"1min ago ago"`.

`sm tail` must compute relative time inline using `datetime.utcnow()` to correctly compare against UTC DB timestamps. Sub-minute precision (seconds) is also needed, since agents make multiple tool calls per minute and "just now" is too coarse.

### Raw Mode Baseline

`cmd_output` (existing `sm output`) already calls `TmuxController.capture_pane()` which runs `tmux capture-pane -p -S -N`. The raw text is returned without ANSI stripping. `sm tail --raw` reuses this path. Default `-n` is 10 for both modes (see below).

---

## Proposed Solution

Add `sm tail <id> [-n N] [--raw]`:

- **Default (structured):** Query last N `PreToolUse` entries from `tool_usage.db` for the resolved session.
- **`--raw`:** Capture tmux pane output, strip ANSI codes, print last N lines.
- **`-n N`:** Number of entries (structured) or lines (raw). Default: 10. Must be ≥ 1 (validated before DB query).
- **`--db-path PATH`:** Override DB path (default: `~/.local/share/claude-sessions/tool_usage.db`). For non-default server deployments where `tool_logging.db_path` is customized in config.

### Example output — structured mode

```
$ sm tail engineer-188
Last 10 actions (engineer-188 5399edcb):
  [4m12s ago] Read: src/cli/commands.py
  [3m55s ago] Read: src/cli/main.py
  [2m30s ago] Bash: git diff HEAD~1
  [2m10s ago] Edit: src/cli/main.py
  [1m45s ago] Bash: source venv/bin/activate && python -m pytest tests/ -v
  [1m44s ago] Edit: src/cli/main.py
  [55s ago]  Bash: git diff HEAD
  [30s ago]  Read: docs/working/188_sm_remind_periodic.md
```

### Example output — raw mode

```
$ sm tail engineer-188 --raw
[raw tmux pane output, last 10 lines, ANSI stripped]
```

---

## Implementation

### CLI layer only — no server changes needed

`sm output` already calls `TmuxController` directly from the CLI, without a server API endpoint. `sm tail` follows the same pattern: query the local SQLite DB directly.

### 1. Add `cmd_tail` to `src/cli/commands.py`

```python
def cmd_tail(
    client: SessionManagerClient,
    identifier: str,
    n: int = 10,
    raw: bool = False,
    db_path_override: Optional[str] = None,
) -> int:
    """
    Show recent activity for a session.

    Structured mode (default): last N PreToolUse events from tool_usage.db.
    Raw mode (--raw): last N lines of tmux pane output with ANSI stripped.
    """
    import sqlite3
    import re as re_module
    from datetime import datetime, timezone
    from pathlib import Path

    # Validate -n
    if n < 1:
        print("Error: -n must be at least 1", file=sys.stderr)
        return 1

    # Resolve identifier to session ID
    session_id, session = resolve_session_id(client, identifier)
    if session_id is None:
        sessions = client.list_sessions()
        if sessions is None:
            print("Error: Session manager unavailable", file=sys.stderr)
            return 2
        else:
            print(f"Error: Session '{identifier}' not found", file=sys.stderr)
            return 1

    name = session.get("friendly_name") or session.get("name") or session_id
    provider = session.get("provider", "claude")

    # --- Raw mode ---
    if raw:
        if provider == "codex-app":
            message = client.get_last_message(session_id)
            if not message:
                print("No output available for this Codex app session", file=sys.stderr)
                return 1
            print(message)
            return 0

        tmux_session = session.get("tmux_session")
        if not tmux_session:
            print("Error: Session has no tmux session", file=sys.stderr)
            return 1

        from ..tmux_controller import TmuxController
        tmux = TmuxController()
        output = tmux.capture_pane(tmux_session, lines=n)
        if output is None:
            print(f"Error: Failed to capture output from {tmux_session}", file=sys.stderr)
            return 1

        # Strip ANSI escape codes
        ansi_escape = re_module.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean = ansi_escape.sub('', output)
        print(clean, end="")
        return 0

    # --- Structured mode: query tool_usage.db ---
    default_db = "~/.local/share/claude-sessions/tool_usage.db"
    db_path = Path(db_path_override or default_db).expanduser()
    if not db_path.exists():
        print(f"No tool usage data available (DB not found: {db_path})", file=sys.stderr)
        return 1

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT timestamp, tool_name, target_file, bash_command
            FROM tool_usage
            WHERE session_id = ? AND hook_type = 'PreToolUse'
            ORDER BY timestamp DESC
            LIMIT ?
        """, (session_id, n))
        rows = cursor.fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"Error: Failed to query tool usage: {e}", file=sys.stderr)
        return 1

    if not rows:
        print(f"No tool usage data for {name} ({session_id})")
        print("(Hooks may not be active for this session, or it's a Codex agent)")
        return 0

    # Format output — compute relative time against UTC (DB stores UTC naive strings)
    now_utc = datetime.utcnow()
    print(f"Last {len(rows)} actions ({name} {session_id[:8]}):")

    def _rel(ts_str: str) -> str:
        try:
            ts = datetime.fromisoformat(ts_str)  # naive UTC
            delta_s = int((now_utc - ts).total_seconds())
            if delta_s < 60:
                return f"{delta_s}s"
            elif delta_s < 3600:
                return f"{delta_s // 60}m{delta_s % 60:02d}s"
            else:
                return f"{delta_s // 3600}h{(delta_s % 3600) // 60}m"
        except Exception:
            return "?"

    for ts_str, tool_name, target_file, bash_command in reversed(rows):
        elapsed = _rel(ts_str)

        # Format action description
        if tool_name == "Bash" and bash_command:
            desc = bash_command[:80].split('\n')[0]  # first line, truncated
            action = f"Bash: {desc}"
        elif tool_name in ("Read", "Write", "Edit", "Glob") and target_file:
            action = f"{tool_name}: {target_file}"
        elif tool_name == "Grep":
            action = "Grep: (search)"
        elif tool_name == "Task":
            action = "Task: (subagent)"
        else:
            action = tool_name

        print(f"  [{elapsed} ago] {action}")

    return 0
```

### 2. Register `sm tail` in `src/cli/main.py`

In the `subparsers` section (alongside `sm output`):

```python
# sm tail <session> [-n N] [--raw] [--db-path PATH]
tail_parser = subparsers.add_parser(
    "tail",
    help="Show recent agent activity (structured tool log or raw tmux output)"
)
tail_parser.add_argument(
    "session",
    help="Session ID or friendly name"
)
tail_parser.add_argument(
    "-n",
    type=int,
    default=10,
    help="Number of entries (structured) or lines (raw) to show (default: 10)"
)
tail_parser.add_argument(
    "--raw",
    action="store_true",
    help="Show raw tmux pane output with ANSI stripped"
)
tail_parser.add_argument(
    "--db-path",
    default=None,
    help="Override tool_usage.db path (default: ~/.local/share/claude-sessions/tool_usage.db)"
)
```

In the dispatch block:

```python
elif args.command == "tail":
    sys.exit(commands.cmd_tail(
        client, args.session, args.n, args.raw,
        db_path_override=getattr(args, 'db_path', None),
    ))
```

Also add `"tail"` to the `no_session_needed` list.

### 3. No server changes needed

`sm tail` is read-only and accesses only local resources (SQLite DB, tmux). No new API endpoint required.

---

## Test Plan

### Unit tests (`tests/test_cmd_tail.py`)

1. **`_rel()` timestamp formatting:**
   - Input UTC naive string that is 30s old → `"30s ago"`
   - Input UTC naive string that is 90s old → `"1m30s ago"`
   - Input malformed string → `"? ago"` (no crash)
   - **Does not** use `format_relative_time` (different function, different semantics)

2. **`-n` validation:**
   - `cmd_tail(..., n=0)` → exit code 1, error message on stderr
   - `cmd_tail(..., n=-1)` → exit code 1, error message on stderr

3. **Structured mode — no DB:**
   - `cmd_tail(..., db_path_override="/nonexistent/path.db")` → exit code 1, informative message

4. **Structured mode — no rows:**
   - Session exists but DB has no records for that session_id → prints "no data" message, exit code 0

5. **Session not found:**
   - Unresolvable identifier → exit code 1

6. **Session manager unavailable:**
   - Server unreachable → exit code 2

### Manual integration checks

7. **Structured mode — active session:**
   - `sm tail <id>` shows recent tool calls with correct names and relative timestamps
   - Most recent last; max 10 entries by default

8. **Structured mode — `-n` flag:**
   - `sm tail <id> -n 5` returns exactly 5 entries

9. **Structured mode — Codex agent:**
   - `sm tail <codex-id>` returns empty with hook-inactive message, no crash

10. **Raw mode:**
    - `sm tail <id> --raw` returns pane output without ANSI escape sequences
    - `sm tail <id> -n 20 --raw` returns up to 20 lines

---

## Known Limitations

- **Codex CLI agents**: No tool_usage.db data — structured mode shows empty. Use `--raw`.
- **Session_name may be stale**: The DB stores `session_name` at log time. If a session was renamed, DB rows will reflect the old name. Query uses `session_id`, so this doesn't affect correctness.
- **Gaps without explanation**: The structured view shows tool calls only, not agent reasoning between calls. Large time gaps indicate thinking time.
- **DB path**: Defaults to `~/.local/share/claude-sessions/tool_usage.db`. If the operator configured a custom `tool_logging.db_path` in `config.yaml`, use `sm tail --db-path <path>` to match. Phase 2: expose DB path via server API so CLI can auto-discover it.

---

## Ticket Classification

**Single ticket.** One agent can complete this end-to-end:
- All changes are in `src/cli/commands.py` and `src/cli/main.py`
- No server changes
- Tests are straightforward
- No dependencies on other open issues
