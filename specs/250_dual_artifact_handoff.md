# sm#250: Dual-Artifact Handoff via Session Archive

**GitHub Issue:** #250
**Spec file:** `specs/250_dual_artifact_handoff.md`

---

## Investigation Findings

### Hypothesis from the issue: Ctrl+O / Ctrl+E needed for full capture

**Status: WRONG.**

`tmux capture-pane -pS -` already captures the full scrollback buffer without entering copy mode or sending any key sequences. The `-S -` flag means "start from the beginning of available history." No Ctrl+O, Ctrl+E, or copy-mode navigation is required.

Experiment run:
```bash
# Standard capture (visible area only): 50 lines
tmux capture-pane -t <session> -p | wc -l

# Full scrollback capture: 2011 lines
tmux capture-pane -t <session> -pS - | wc -l
```

The copy-mode hypothesis was a misunderstanding of how capture-pane works.

---

### Real limitation: the 2000-line scrollback ceiling

The tmux default `history-limit` is 2000 lines. The session manager creates sessions without overriding this. For any session that has run long enough to generate more than 2000 lines of terminal output, the oldest lines are silently dropped from the scrollback buffer.

Measured on an active Claude session running for ~1.5 hours:
```
pipe-pane log:            86,784 lines  (1.3 MB)
capture-pane scrollback:   1,232 lines
```

The tmux scrollback contains ~1.4% of the session's actual terminal history. For long-running sessions, `tmux capture-pane -pS -` is insufficient as a full archive.

---

### Existing full archive: the pipe-pane log

The session manager already captures all terminal output via `pipe-pane` at session creation:

```python
# src/tmux_controller.py
self._run_tmux("pipe-pane", "-t", session_name, f"cat >> {log_file}")
```

Log files are written to `/tmp/claude-sessions/{session_name}.log` (e.g., `/tmp/claude-sessions/claude-117897d1.log`). These grow continuously since session start and are never truncated. The files are available on `Session.log_file`.

**The full archive already exists. No additional capture step is required at handoff time.**

---

### Pipe-pane log usability

The pipe-pane log contains raw terminal bytes: ANSI escape codes, cursor positioning sequences, bracketed-paste mode markers, and rendered Claude Code UI components. It is not human-readable without stripping.

Attempts to clean it with standard tools (`col -b`, `sed` ANSI patterns) leave residual control codes. The utility `ansi2txt` is not installed by default; `pyte` (a Python terminal emulator that would parse it correctly) is also absent.

However, `grep` still works for locating specific text:
```bash
grep -a "keyword" /tmp/claude-sessions/claude-<id>.log
```
Text content appears in the log even when surrounded by ANSI codes, and grep finds it reliably.

---

### Capture-pane output quality

`tmux capture-pane -pS -` produces clean, rendered text — exactly what appeared on screen — with no ANSI codes. It includes tool call headers, output blocks, user messages, and the Claude Code UI chrome. It is human-readable and agent-searchable.

Sample from a real session: 75 KB, 1232 lines for a 1.5-hour session.

---

### History-limit increase: not viable without global change

Experiment confirmed: `tmux set-option -t <session> history-limit 50000` after `new-session` does **not** affect the already-created first pane. In tmux 3.6a, this updates session defaults for new windows only. The active Claude pane retains the original 2000-line limit.

```
After set-option -t <session> history-limit 50000:
  tmux display-message #{history_limit} → still 2000
  After 3000 echoed lines: ~2003 captured
```

The only way to raise the limit for the existing pane would be `set-option -g` (global, affects all tmux users) or recreating the window (disruptive). Neither is acceptable.

**Conclusion: drop the history-limit increase from this design.** The pipe-pane log already provides full history. The capture-pane snapshot gives the last ~2000 lines as a clean, readable recent view. This is sufficient.

---

## Proposed Design

### Two artifacts at handoff

When `sm handoff <path>` triggers, the server produces one new artifact before clearing context and always references the existing archive:

1. **Agent handoff doc** — already written by the agent, unchanged. Primary continuation artifact; path is whatever the agent chose.

2. **Terminal snapshot** (`dump.txt`) — a clean `tmux capture-pane -pS -` capture of the last ~2000 lines, written to a system-managed location. Readable recent transcript. Written conditionally (skipped silently if tmux capture fails).

3. **Pipe-pane log** (`Session.log_file`) — always referenced unconditionally in the wake message. Complete raw terminal log since session start; searchable with grep.

---

### Artifact location

The dump is written to:
```
~/.local/share/claude-sessions/handoffs/<session_id>-<YYYYMMDD-HHMMSS>/dump.txt
```

The agent's handoff doc stays at its agent-chosen path. The dump directory is server-managed and outside any git working tree, preventing accidental commits.

---

### Wake message format

Current (single artifact):
```
Read {file_path} and continue from where you left off.
```

New (always includes log path; dump path only when capture succeeded):
```
Read {file_path} and continue from where you left off.

Full session log (complete since start, raw terminal bytes): {log_file}
Search it with: grep -a "keyword" {log_file}
{if dump succeeded}
Recent terminal transcript (last ~2000 lines, readable): {dump_path}
{end if}
```

The log path is always present. The dump path is a conditional bonus. Both include explicit "don't read proactively" framing to prevent agents from loading large files into context.

Concrete prompt string:

```python
log_ref = (
    f"\n\nFull session log (complete since start, raw bytes): {log_file}"
    f"\nDon't read this file — search it only if you need a specific detail: grep -a \"keyword\" {log_file}"
)
dump_ref = (
    f"\nRecent terminal transcript (~2000 lines, readable): {dump_path}"
    f"\nDon't read this proactively — grep it if needed."
) if dump_path else ""
handoff_prompt = f"Read {file_path} and continue from where you left off.{log_ref}{dump_ref}"
```

---

### Implementation approach

**`src/message_queue.py` — `_execute_handoff`:**

Insert a new step 0 (before arming the skip fence at step 1). Uses async subprocess pattern consistent with the rest of the file:

```python
# 0. Capture terminal snapshot to dump file (non-blocking, failure-tolerant)
from datetime import datetime
ts = datetime.now().strftime("%Y%m%d-%H%M%S")
dump_dir = Path.home() / ".local/share/claude-sessions/handoffs" / f"{session_id}-{ts}"
dump_dir.mkdir(parents=True, exist_ok=True)
dump_path = dump_dir / "dump.txt"
try:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "capture-pane", "-t", tmux_session, "-pS", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode == 0 and stdout:
        dump_path.write_bytes(stdout)
    else:
        logger.warning(f"Handoff: tmux capture-pane returned {proc.returncode}, skipping dump")
        dump_path = None
except Exception as e:
    logger.warning(f"Handoff: terminal snapshot failed: {e}")
    dump_path = None
```

Modify step 6 (wake message):

```python
log_file = session.log_file or None
log_ref = (
    f"\n\nFull session log (complete since start, raw bytes): {log_file}"
    f"\nDon't read this file — search it only if you need a specific detail: grep -a \"keyword\" {log_file}"
) if log_file else ""
dump_ref = (
    f"\nRecent terminal transcript (~2000 lines, readable): {dump_path}"
    f"\nDon't read this proactively — grep it if needed."
) if dump_path else ""
handoff_prompt = f"Read {file_path} and continue from where you left off.{log_ref}{dump_ref}"
```

**`src/tmux_controller.py`:** No changes required (history-limit increase dropped).

No other files require changes.

---

### Context monitor trigger (70% threshold)

The issue mentions triggering at 70%. Current config:
- Warning: 50% → "Consider writing a handoff doc"
- Critical: 65% → "Write your handoff doc NOW"

Thresholds are configurable via `config.yaml`. This ticket changes what happens when the agent calls `sm handoff`, not the threshold logic. Threshold tuning is out of scope.

---

### Autocompact interaction

This ticket does not disable autocompact. Agent-driven handoff (`sm handoff`) and autocompact coexist: autocompact fires when the agent does not handoff voluntarily. Disabling autocompact is a separate decision requiring its own analysis.

---

## Test Plan

1. **Unit: terminal snapshot captured on success**
   Mock `asyncio.create_subprocess_exec` to return returncode=0 and non-empty stdout. After `_execute_handoff`, verify `~/.local/share/claude-sessions/handoffs/<id>-<ts>/dump.txt` exists with expected content.

2. **Unit: wake message always includes log path**
   With snapshot success: verify prompt contains both `session.log_file` and `dump_path`.
   With snapshot failure: verify prompt contains `session.log_file` but NOT a dump path reference.

3. **Unit: snapshot failure is non-fatal**
   Simulate `asyncio.create_subprocess_exec` raising an exception. Verify `_execute_handoff` still completes the full handoff sequence (skip fence, /clear, wake prompt). No exception propagates.

4. **Unit: non-zero returncode treated as failure**
   Mock capture-pane returning returncode=1 with non-empty stdout. Verify `dump_path = None` (no file written, no dump reference in wake message).

5. **Manual: end-to-end handoff with dump**
   Trigger a handoff from a session that has run 30+ minutes. Verify:
   - `~/.local/share/claude-sessions/handoffs/` contains a new directory with `dump.txt`
   - `dump.txt` contains readable terminal content (tool calls, output blocks visible)
   - Wake message contains both log file path and dump path
   - Agent does not proactively open either artifact

6. **Edge: codex-app sessions rejected before snapshot**
   `sm handoff` returns an error early in the `schedule_handoff` API handler for `provider == "codex-app"` (the codex-app guard in `schedule_handoff`). `_execute_handoff` is never called, so no dump directory is created. Verify this path returns the expected error and leaves the handoffs directory untouched.
   Codex CLI sessions (`provider == "codex"`) have a valid `tmux_session` and should produce a dump normally — verify this path works end-to-end.

---

## Classification

**Single ticket.** Changes are confined to `_execute_handoff` in `message_queue.py` (new step 0 + modified wake message). No changes to `tmux_controller.py`. One agent can complete without compacting context.
