# Issue #175 — sm send Truncates First Characters / Missing Enter

**Repo:** rajeshgoli/session-manager
**Issue:** https://github.com/rajeshgoli/session-manager/issues/175

---

## Problem Statement

`sm send` (and `sm clear`) intermittently drop the first 1–2 characters of a message, or fail to submit the Enter key, when dispatching to tmux. Three distinct failures observed in a single EM session:

1. `sm clear <id>` → `/clear` arrived in the child pane as `lear` (missing `/c`)
2. An 8-line sequential `sm send` → text appeared in pane but Enter was never submitted; agent sat idle
3. `sm send --urgent` → `[Input from em-...` arrived as `nput from em-...` (missing `[I`)

---

## Root Cause Analysis

There are **two independent bugs**, one causing char-dropping (symptoms 1 and 3) and one causing the missing Enter (symptom 2).

### Bug A — Race between Escape delivery and next tmux send-keys (symptoms 1 and 3)

Both `sm clear` and `sm send --urgent` send an `Escape` key to interrupt the target pane, then wait a fixed delay before sending the next payload.

**`sm clear` path** (`cli/commands.py:cmd_clear`, lines 1686–1708):
```python
subprocess.run(["tmux", "send-keys", "-t", tmux_session, "Escape"], ...)
time.sleep(0.5)
subprocess.run(["tmux", "send-keys", "-t", tmux_session, clear_command], ...)  # e.g. "/clear"
time.sleep(1)
subprocess.run(["tmux", "send-keys", "-t", tmux_session, "Enter"], ...)
```

**`sm send --urgent` path** (`message_queue.py:_deliver_urgent`, lines 830–841):
```python
proc = await asyncio.create_subprocess_exec("tmux", "send-keys", "-t", session.tmux_session, "Escape", ...)
await asyncio.wait_for(proc.communicate(), timeout=self.subprocess_timeout)  # 2s
await asyncio.sleep(self.urgent_delay_ms / 1000)  # 0.5s default
success = await self.session_manager._deliver_direct(session, msg.text)
# which calls send_input_async → tmux send-keys -- text
```

**The race:** `tmux send-keys` exits (subprocess returncode 0) when tmux has *accepted* the command into its own queue. It does NOT wait for the target pane's application to process the keystroke. Claude Code runs in an independent process with its own async event loop. If Claude Code is mid-response when Escape arrives (cancelling a streaming API call, writing to disk, clearing its input buffer), processing the Escape can take >0.5 s on a loaded system.

When the next `send-keys` call fires before Claude Code has settled, tmux delivers the new text into Claude Code's readline input handler while Claude Code's state machine is still transitioning. The first 1–2 characters of the new input are consumed by the state-reset logic rather than appearing in the input buffer.

The specific patterns observed are consistent with this: `/c` from `/clear` and `[I` from `[Input from em-...` are the characters most likely consumed during a readline state flush. For long strings, tmux wraps the text in bracketed-paste sequences (`ESC[200~…ESC[201~`). If the preceding Escape key is not yet fully processed, the `[` and `I` that open the user's message can be misread as part of an escape sequence (`ESC[I` = cursor tab forward / CSI I), and only `nput from em-...` reaches the input buffer.

**Evidence:**
- Delay after Escape is time-based, not state-based: `time.sleep(0.5)` in `cmd_clear`, `await asyncio.sleep(0.5)` in `_deliver_urgent`
- No confirmation (via pane capture or prompt detection) that Claude Code is ready before sending
- Intermittent nature matches a race condition: fails on loaded systems, succeeds otherwise
- All three observed truncations drop exactly the leading 1–2 chars, which is the precise window where readline's state-reset runs

### Bug B — Separate text and Enter subprocess calls with no atomic guarantee (symptom 2)

`send_input_async` (`tmux_controller.py:339–397`) makes two independent `create_subprocess_exec` calls — one for text, one for Enter — with a 0.3 s `asyncio.sleep` between them:

```python
# Send text
proc = await asyncio.create_subprocess_exec('tmux', 'send-keys', '-t', session_name, '--', text, ...)
await asyncio.wait_for(proc.wait(), timeout=self.send_keys_timeout_seconds)  # 5s
...
await asyncio.sleep(self.send_keys_settle_seconds)  # 0.3s

# Send Enter — separate subprocess
proc = await asyncio.create_subprocess_exec('tmux', 'send-keys', '-t', session_name, 'Enter', ...)
await asyncio.wait_for(proc.wait(), timeout=self.send_keys_timeout_seconds)  # 5s
```

The text and Enter are not atomic. If the second `create_subprocess_exec` fails for any reason — the tmux session was killed after the text landed, a system call error, or any unhandled exception in the asyncio task — the text arrives in the pane without Enter. The code's error handling returns `False` but does not retry, and the caller (`_deliver_direct`) propagates the failure without any recovery attempt. Additionally, the 5 s `asyncio.wait_for(proc.wait(), ...)` timeout, if triggered, abandons the Enter send even though the subprocess continues running and the text was already delivered.

A secondary issue on the same path: `send_input_async` contains dead code at line 357 (`escaped_text = shlex.quote(text)` is computed but never used). This is harmless but is a signal the method was partially refactored without cleanup.

---

## Proposed Fix

### Fix A — Replace time-based Escape delay with prompt-state detection

Add a helper `_wait_for_claude_prompt_async(tmux_session, timeout=3.0, poll_interval=0.1)` to **`message_queue.py`** (not TmuxController). It must use `asyncio.create_subprocess_exec` to run `tmux capture-pane -p` — not `TmuxController.capture_pane()`, which is synchronous (uses `subprocess.run` via `_run_tmux`) and would block the event loop, violating the constraint established by issue #37.

The idle check must be `last_line.rstrip() == '>'` — i.e., the prompt has no trailing user text. `last_line.startswith('> ')` is NOT sufficient: it also matches `'> <typed text>'`, which is the user-typing state, not the idle state. This matches the existing correct pattern in `_check_codex_prompt` (message_queue.py:1118).

```python
async def _wait_for_claude_prompt_async(
    self, tmux_session: str, timeout: float = 3.0, poll_interval: float = 0.1
) -> bool:
    """Poll capture-pane until Claude Code shows bare "> " prompt, or timeout.

    Uses asyncio.create_subprocess_exec (non-blocking) to avoid violating
    the no-blocking-IO-in-async constraint (issue #37).
    Returns True if prompt detected, False if timed out (caller proceeds anyway).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "capture-pane", "-p", "-t", tmux_session,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.subprocess_timeout
            )
            if proc.returncode == 0:
                output = stdout.decode().rstrip('\n')
                if output:
                    last_line = output.split('\n')[-1]
                    if last_line.rstrip() == '>':
                        return True
        except Exception:
            pass
        await asyncio.sleep(poll_interval)
    return False  # Timed out — proceed anyway (best-effort)
```

Apply this in `message_queue.py:_deliver_urgent`: replace `await asyncio.sleep(self.urgent_delay_ms / 1000)` with a call to `_wait_for_claude_prompt_async`. Keep the existing sleep as a fallback minimum floor (e.g., `await asyncio.sleep(0.1)` before the poll loop starts) so the function still works in test environments where no real tmux pane exists.

For `cmd_clear` in `cli/commands.py` (synchronous): replace the `time.sleep(0.5)` / `time.sleep(1.5)` delays with a blocking poll loop using `subprocess.run(["tmux", "capture-pane", "-p", "-t", tmux_session], ...)` and the same `last_line.rstrip() == '>'` check. Since `cmd_clear` is already synchronous (not in the async event loop), blocking subprocess calls are safe here.

### Fix B — Make text + Enter atomic via a single tmux send-keys call

Instead of two separate subprocess calls, send `text` and `\r` (Enter) in a **single** `tmux send-keys` invocation:

```python
payload = text + "\r"
proc = await asyncio.create_subprocess_exec(
    'tmux', 'send-keys', '-t', session_name, '--', payload,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.send_keys_timeout_seconds)
```

Benefits:
- Text and Enter are delivered atomically by tmux — no window where Enter can be lost
- Replaces `proc.wait()` (warning per Python docs for PIPE) with `proc.communicate()` which properly drains stdout/stderr
- Removes dead `escaped_text = shlex.quote(text)` line
- Removes the separate Enter subprocess and its associated settle sleep

The `\r` character (carriage return) is the standard "Enter" in terminal emulators and is what `tmux send-keys Enter` delivers internally. Sending it as part of the data string bypasses the need for a second subprocess.

---

## Acceptance Criteria

1. `sm send --urgent "long multi-line message" <child-id>` delivers the full message header `[Input from: ...]` without any leading characters missing, even on a system under load
2. `sm clear <id>` delivers `/clear` to the child pane as the complete string, without any leading characters missing
3. `sm send <child-id> "8-line message"` results in the message appearing in the pane AND being submitted (agent processes the message, not left waiting with unsent input)
4. A new unit test for Bug B: in `tests/unit/test_tmux_controller.py` (or equivalent), mock `asyncio.create_subprocess_exec` so the single combined `send-keys -- text+"\r"` call returns a non-zero returncode; verify `send_input_async` returns `False` and logs the error. After Fix B there is no separate Enter subprocess — the test must target the unified call, not a "second call". `test_issue_88_urgent_completed.py` does NOT cover this path.
5. A regression test for Bug A: mock `asyncio.create_subprocess_exec` in `_deliver_urgent` and verify that `_wait_for_claude_prompt_async` is awaited before `_deliver_direct` is called; separately, integration-test that the polling correctly returns `True` when capture-pane output ends with `>`.

---

## Ticket Classification

**Single ticket.** The two bugs touch three files (`tmux_controller.py`, `message_queue.py`, `cli/commands.py`) but the changes are focused and an engineer can complete them without context compaction.
