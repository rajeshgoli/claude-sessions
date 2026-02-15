# #137 — Native Codex /review Support for Code Reviews

**Status:** Draft v5
**Author:** Claude (Opus 4.6)
**Created:** 2026-02-14
**Updated:** 2026-02-14
**Ticket:** [#137](https://github.com/rajeshgoli/claude-sessions/issues/137)

---

## 1. Problem Statement

### Current Workflow

When the EM (Engineering Manager) agent wants a code review, it spawns a child session with a free-form prompt:

```bash
sm spawn codex "Review PR #42 on branch feat/login against main. \
  Check error handling, test coverage, security. \
  Read persona doc at docs/review-persona.md and apply checklist." \
  --name reviewer --wait 900
```

This child session is a full agent that burns regular tokens to:
1. Figure out what changed (manually running `git diff` or reading files)
2. Interpret the review criteria from the prompt
3. Produce unstructured text output

### Why This Is Suboptimal

| Issue | Impact |
|-------|--------|
| **Token waste** | Full agent session uses regular token budget for what should be a specialized review operation |
| **Unstructured output** | Free-form text vs. structured findings with priority levels, confidence scores, and precise code locations |
| **No diff awareness** | Agent must figure out what changed; `/review` natively computes diffs between branches |
| **Inconsistent quality** | Each review agent reinvents the review approach; `/review` has a battle-tested prompt with consistent output schema |

### Why This Matters

The session manager already supports Codex as a provider (`codex` for tmux CLI, `codex-app` for app-server). Adding native `/review` support means the EM can trigger reviews that:
- Use Codex's built-in review prompt (tuned for high-quality code review)
- Return structured JSON with P0-P3 prioritized findings
- Run inside visible tmux sessions the user can attach to and observe
- Produce consistent, parseable output that can be forwarded to Telegram, summarized, or acted upon programmatically

### Design Constraint: Visible Sessions

A non-interactive `codex review --base main` subprocess exists but is explicitly **not** the approach here. The core value of session manager is observable, attachable sessions. The user must be able to `sm attach` to a review session and watch Codex work in the TUI. This means reviews run inside interactive Codex CLI sessions via the `/review` slash command.

---

## 2. What We Found in the Docs

### Codex `/review` Interactive Slash Command

The `/review` command inside the interactive Codex CLI is a **read-only, specialized code reviewer**. It never modifies the working tree.

**Activation:** Type `/review` in the interactive CLI. A menu appears with four modes:

1. **Review against a base branch** — Select a local branch (e.g., `main`). Codex finds the merge base, computes the diff, and reviews your work against it.
2. **Review uncommitted changes** — Reviews staged, unstaged, and untracked files in the working tree.
3. **Review a specific commit** — Select a commit SHA from a list. Reviews that exact changeset.
4. **Custom review instructions** — Free-form prompt like `/review Focus on security vulnerabilities`.

**Under the hood:**
- Codex computes a `git diff` (unified format, 5 lines context) between comparison points
- The diff + file metadata is assembled into a review prompt
- Sent to the configured model with a strict JSON output schema
- Returns structured findings without modifying files

### Review Output Schema

```json
{
  "findings": [
    {
      "title": "[P1] Un-padding slices along wrong tensor dimensions",
      "body": "Markdown explanation with file/line/function citations...",
      "confidence_score": 0.85,
      "priority": 1,
      "code_location": {
        "absolute_file_path": "/path/to/file.py",
        "line_range": {"start": 42, "end": 48}
      }
    }
  ],
  "overall_correctness": "patch is correct",
  "overall_explanation": "1-3 sentence justification",
  "overall_confidence_score": 0.92
}
```

**Priority levels:** P0 (blocking), P1 (urgent), P2 (normal), P3 (low priority).

### Steering (Interactive TUI Only)

Codex supports mid-turn steering via the Enter key in the interactive CLI:
- While a turn is running, press **Enter** to inject new instructions into the **current turn**
- Press **Tab** to queue for the **next turn**
- In review context: can refine focus mid-review (e.g., "actually focus on the database queries")

This is an interactive TUI feature — it does not exist in the non-interactive `codex review` subcommand. This is another reason the TUI path is preferred.

### Known Limitation: `--base`/`--commit` + `[PROMPT]` Are Mutually Exclusive

In Codex CLI 0.101.0, the non-interactive `codex review --base main "Focus on security"` produces an error: `--base cannot be used with [PROMPT]`. The same applies to `--commit` + prompt. This limitation also affects how the interactive `/review` works — the four menu modes are distinct, you cannot combine branch-mode with custom instructions in a single invocation.

To add custom focus to a branch review, steering must be used after the review starts.

### Review Model Configuration

```toml
# ~/.codex/config.toml
review_model = "gpt-5.2-codex"
```

Review model can be configured separately from the session model.

### Quota

- Local CLI `/review` commands count toward **general local message quota** (not the separate weekly code review quota)
- The separate "Code Reviews/week" quota applies only to GitHub-integrated reviews (`@codex review` on PRs)
- This means local reviews are effectively free relative to the agent's regular budget — they just cost one message turn

---

## 3. Design

### 3.1 Core Concept: Reuse Existing Sessions

Reviews are sent to **existing** Codex tmux sessions via the `/review` slash command. This means:

- No new session needed if a Codex session already exists in the right working directory
- The EM can `sm clear <session>` then `sm review <session> ...` to repurpose a child
- Or `sm review` can spawn a fresh Codex session if none is available
- In all cases, the review runs in a visible tmux session the user can `sm attach` to

```
EM Agent                         Session Manager                   Codex (tmux)
   │                                    │                              │
   ├─ sm review <session> --base main   │                              │
   │   --steer "read persona doc"       │                              │
   │                                    │                              │
   │                          ┌─────────┴──────────┐                   │
   │                          │ 1. resolve session  │                   │
   │                          │ 2. send "/review"   │──── /review ────>│
   │                          │ 3. navigate menu    │──── ↓ Enter ────>│
   │                          │    (select mode)    │                   │
   │                          │ 4. select branch    │──── ↓↓ Enter ──>│
   │                          └─────────┬──────────┘                   │
   │                                    │                              │
   │  (user can: sm attach <session>)   │ (review visible in TUI)     │
   │                                    │                              │
   │                                    │ 5. steer if requested        │
   │                                    │    Enter + text + Enter ────>│
   │                                    │                              │
   │                                    │ 6. review completes          │
   │                                    │    (Stop hook fires)         │
   │                                    │                              │
   │<─── completion notification ───────│                              │
```

### 3.2 The `sm review` Command

```bash
# Review against a base branch (on an existing session)
sm review <session> --base <branch> [options]

# Review uncommitted changes
sm review <session> --uncommitted [options]

# Review a specific commit
sm review <session> --commit <sha> [options]

# Custom review instructions (free-form, no branch mode)
sm review <session> --custom "Focus on security" [options]

# Spawn a new session and immediately start review
sm review --new --base main [options]
```

**Options:**
```
--name <name>          Friendly name (only when --new)
--wait <seconds>       Monitor and notify when review completes (default: 600 in managed session, None standalone)
--model <model>        Model override for the spawned session (only when --new)
--working-dir <dir>    Override working directory (only when --new)
--steer <text>         Additional instructions to inject mid-review via Enter key
```

**Examples:**
```bash
# Reuse an existing reviewer session for a branch review
sm review reviewer --base main --wait 600

# Same, but also steer the review with custom focus
sm review reviewer --base main --steer "Focus on auth security. Apply checklist from docs/review-persona.md."

# Review uncommitted changes on an existing session
sm review reviewer --uncommitted

# Spawn fresh session and start review
sm review --new --base main --name pr42-review --wait 600

# Custom free-form review
sm review reviewer --custom "Check for SQL injection vulnerabilities in the auth module"
```

### 3.3 How It Works: Tmux Interaction Sequence

The `/review` slash command presents an interactive menu navigated with arrow keys. The session manager automates this via tmux `send-keys`.

**Branch review (`sm review <session> --base main`):**

```
Step  Delay    Key Sequence              What It Does
────  ─────  ─ ────────────────────────  ─────────────────────────────
1     0s       /review                   Send /review slash command
2     0.3s     Enter                     Submit the command
3     1s       (wait for menu)           Menu appears with 4 review modes
4     0s       Enter                     Select "Review against a base branch" (1st item)
5     1s       (wait for branch list)    Branch picker appears
6     0s       ↓ × N                     Navigate to target branch
7     0.3s     Enter                     Confirm branch selection
8     —        (review runs)             Codex computes diff and reviews
```

**Uncommitted changes (`--uncommitted`):**

```
Step  Delay    Key Sequence              What It Does
────  ─────  ─ ────────────────────────  ─────────────────────────────
1-3   (same as above — send /review, wait for menu)
4     0s       ↓                         Move to "Review uncommitted changes" (2nd item)
5     0.3s     Enter                     Select it
6     —        (review runs)
```

**Specific commit (`--commit <sha>`):**

```
Step  Delay    Key Sequence              What It Does
────  ─────  ─ ────────────────────────  ─────────────────────────────
1-3   (same — send /review, wait for menu)
4     0s       ↓↓                        Move to "Review a specific commit" (3rd item)
5     0.3s     Enter                     Select it
6     1s       (wait for commit list)    Commit picker appears
7     0s       (navigate to commit)      Navigate to target SHA
8     0.3s     Enter                     Confirm
9     —        (review runs)
```

**Custom review (`--custom "..."`):**

```
Step  Delay    Key Sequence              What It Does
────  ─────  ─ ────────────────────────  ─────────────────────────────
1     0s       /review <custom text>     Send /review with custom prompt directly
2     0.3s     Enter                     Submit — bypasses menu, runs immediately
```

### 3.4 Branch Navigation Strategy

For branch mode, we need to select the target branch from a picker list. Approach:

1. Before sending `/review`, run `git branch --list` in the working directory to get the sorted branch list
2. Find the position of the target branch in that list
3. Send that many `↓` keys after the branch picker appears

If the branch isn't found in the list, fail before sending `/review` with a clear error.

### 3.5 Steering Mechanism

After a review starts, the EM may want to inject additional focus instructions. Since branch mode and custom prompt are mutually exclusive, steering is the **only way** to add custom instructions to a branch/commit review.

**Via `--steer` flag (at review start):**

When `--steer` is provided:
1. Wait for review output to begin (configurable delay: `steer_delay_seconds`)
2. Send `Enter` to open the steer input field
3. Send the steer text
4. Send `Enter` to submit

**Via `sm send` (during review):**

The EM can steer an active review using the existing message queue:
```bash
sm send reviewer "Also check for SQL injection" --sequential
```

This uses `--sequential` (default) delivery, which waits for the session to be idle before injecting. This is the correct mode because:
- `--urgent` sends Escape first (interrupts the current turn — wrong for mid-turn steering)
- `--sequential` waits for idle then injects — but reviews don't go idle mid-turn

For true mid-turn steering via `sm send`, a new delivery mode would be needed (future work):
```bash
sm send reviewer "Also check for SQL injection" --steer  # future: Enter-based injection
```

**v1 scope:** Only `--steer` flag at review start is supported. Deferred steering via `sm send --steer` is Phase 2.

### 3.6 Review Session Model

Add `review_config` to the Session model to track review metadata:

```python
@dataclass
class ReviewConfig:
    """Configuration for a Codex review session."""
    mode: str  # "branch", "uncommitted", "commit", "custom"
    base_branch: Optional[str] = None    # For branch mode
    commit_sha: Optional[str] = None     # For commit mode
    custom_prompt: Optional[str] = None  # For custom mode
    steer_text: Optional[str] = None     # Instructions to inject after review starts
    steer_delivered: bool = False         # Whether steer text was injected
```

Note: no `fix_branch` field. The review always runs against the current HEAD. The user must be on the fix branch before starting the review. No automatic checkout.

### 3.7 API Endpoint

**POST `/sessions/{session_id}/review`**

Starts a review on an existing session.

```json
{
  "mode": "branch",
  "base_branch": "main",
  "steer": "Focus on auth security. Apply checklist."
}
```

**Response:**
```json
{
  "session_id": "def456",
  "review_mode": "branch",
  "base_branch": "main",
  "status": "started",
  "steer_queued": true
}
```

**POST `/sessions/review`** (with `--new` flag)

Spawns a new Codex session and starts a review.

```json
{
  "parent_session_id": "abc123",
  "mode": "branch",
  "base_branch": "main",
  "name": "pr42-review",
  "wait": 600,
  "working_dir": "/path/to/repo",
  "steer": "Focus on auth security."
}
```

### 3.8 Completion Detection & `--wait`

The existing `OutputMonitor` (via tmux pipe-pane log files) detects review completion the same way it detects any Codex session going idle — via the Stop hook firing when Codex finishes the review turn and returns to the prompt.

**`--wait` has two distinct paths depending on invocation mode:**

**Existing-session reviews (`sm review <session> --base main --wait 600`):**

Uses the existing `watch_session()` infrastructure in `MessageQueueManager` (`src/message_queue.py:1068`). This polls the session's `delivery_state.is_idle` flag (set by the Stop hook) and notifies the caller when the review session goes idle.

- **Critical:** Before registering the watch, `start_review()` must call `message_queue_manager.mark_session_active(session_id)` (line 288). Otherwise, if the session was idle before `/review` was sent, `watch_session()` resolves immediately on the first poll (`_watch_for_idle` at line 1112-1115 checks `is_idle` with no grace period). Uses the public API to safely create the delivery state entry if it doesn't exist yet.
- Requires the caller to have a session context (`CLAUDE_SESSION_MANAGER_ID` set) so there's a session to notify
- If `--wait` is None (standalone user who didn't request it), no watch is registered — no warning needed
- Does **not** use `ChildMonitor` — no parent-child relationship needed

**Spawn-and-review (`sm review --new --base main --wait 600`):**

Uses `ChildMonitor.register_child()` (`src/child_monitor.py:44`) since a parent-child relationship exists.

- **Idle baseline fix:** When `start_review()` is called, set `session.last_tool_call = datetime.now()`. This is semantically imprecise (no tool call actually happened) but it correctly ensures the idle-time calculation at `child_monitor.py:101-102` uses the review start time as baseline, rather than falling through to `spawned_at` (line 110).
- Without this fix, `last_tool_call=None` causes fallback to `spawned_at/created_at`, which would declare the review idle immediately if the session was spawned minutes ago.

**Completion notification format:**

```
Review reviewer (def456) completed: review finished on branch main
```

### 3.9 Configuration

```yaml
# config.yaml additions (under existing codex section)
codex:
  review:
    default_wait: 600                # Default --wait seconds for reviews
    menu_settle_seconds: 1.0         # Wait for /review menu to appear
    branch_settle_seconds: 1.0       # Wait for branch picker to appear
    steer_delay_seconds: 5.0         # Wait before injecting steer text
```

No `menu_settle_seconds` or `branch_settle_seconds` TUI timing config needed in the main `codex` section — these are review-specific.

---

## 4. Implementation Plan

### Phase 1: Core `sm review` with branch mode

#### Step 1: Add ReviewConfig model

**File:** `src/models.py`

Add after `CompletionStatus` enum (~line 50):

```python
@dataclass
class ReviewConfig:
    """Configuration for a Codex review session."""
    mode: str  # "branch", "uncommitted", "commit", "custom"
    base_branch: Optional[str] = None
    commit_sha: Optional[str] = None
    custom_prompt: Optional[str] = None
    steer_text: Optional[str] = None
    steer_delivered: bool = False

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "base_branch": self.base_branch,
            "commit_sha": self.commit_sha,
            "custom_prompt": self.custom_prompt,
            "steer_text": self.steer_text,
            "steer_delivered": self.steer_delivered,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
```

Add `review_config: Optional[ReviewConfig] = None` field to the `Session` dataclass (~line 112). Update `to_dict()` and `from_dict()` to serialize/deserialize it.

#### Step 2: Add review key-sequence methods to TmuxController

**File:** `src/tmux_controller.py`

Add two new async methods:

**`send_review_sequence()`** — Sends `/review`, waits for menu, navigates to the correct mode, selects branch if needed.

```python
async def send_review_sequence(
    self,
    session_name: str,
    mode: str,
    base_branch: Optional[str] = None,
    commit_sha: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    branch_position: Optional[int] = None,  # Pre-computed position in branch list
    config: Optional[dict] = None,
) -> bool:
```

Logic by mode:
- `branch`: Send `/review` + Enter → wait → Enter (1st item) → wait → ↓×N + Enter (branch)
- `uncommitted`: Send `/review` + Enter → wait → ↓ + Enter (2nd item)
- `commit`: Send `/review` + Enter → wait → ↓↓ + Enter (3rd item) → wait → navigate to SHA
- `custom`: Send `/review <custom_prompt>` + Enter (bypasses menu)

**`send_steer_text()`** — Injects steer text into an active turn via Enter.

```python
async def send_steer_text(self, session_name: str, text: str) -> bool:
    """Inject steer text into an active Codex turn.

    Sends: Enter (open steer field) → text → Enter (submit).
    """
```

#### Step 3: Add `start_review()` to SessionManager

**File:** `src/session_manager.py`

Add new method:

```python
async def start_review(
    self,
    session_id: str,
    mode: str,
    base_branch: Optional[str] = None,
    commit_sha: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    steer_text: Optional[str] = None,
    wait: Optional[int] = None,
    watcher_session_id: Optional[str] = None,
) -> dict:
```

This method:
1. Resolves session — must be an existing Codex session (`provider == "codex"`)
2. Validates: session exists, is a codex session, is idle, working dir is a git repo
3. For branch mode: runs `git branch --list` in working_dir to find branch position
4. Stores `ReviewConfig` on the session
5. Sets `session.last_tool_call = datetime.now()` (resets idle baseline for ChildMonitor)
6. Marks session active in delivery state: `message_queue_manager.mark_session_active(session_id)` (`src/message_queue.py:288`) — **critical for `--wait`**, otherwise `watch_session()` resolves immediately on first poll because the session was idle before `/review` was sent. Uses the public API rather than direct map access to avoid KeyError on fresh sessions where no delivery state exists yet (states are created lazily via `_get_or_create_state`)
7. Calls `tmux_controller.send_review_sequence()`
8. If `steer_text` provided, schedules steer injection after `steer_delay_seconds`
9. If `wait` and `watcher_session_id`: registers via `message_queue_manager.watch_session(session_id, watcher_session_id, wait)`
10. Returns status dict

Also add a separate method for spawn-and-review (`--new` flag):

```python
async def spawn_review_session(
    self,
    parent_session_id: str,
    mode: str,
    base_branch: Optional[str] = None,
    commit_sha: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    steer_text: Optional[str] = None,
    name: Optional[str] = None,
    wait: Optional[int] = None,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
) -> Session:
```

This method:
1. Spawns a new Codex session via `spawn_child_session()` with `provider="codex"` and **no initial prompt**
2. Waits for Codex CLI to initialize (`claude_init_seconds`)
3. Calls `start_review()` on the new session
4. If `wait` specified, registers with `ChildMonitor`

#### Step 4: Add API endpoints

**File:** `src/server.py`

Add Pydantic request models:

```python
class StartReviewRequest(BaseModel):
    """Start a review on an existing session."""
    mode: str = "branch"
    base_branch: Optional[str] = None
    commit_sha: Optional[str] = None
    custom_prompt: Optional[str] = None
    steer: Optional[str] = None
    wait: Optional[int] = None              # Seconds to watch for completion
    watcher_session_id: Optional[str] = None  # Session to notify when review completes

class SpawnReviewRequest(BaseModel):
    """Spawn a new session and start a review."""
    parent_session_id: str
    mode: str = "branch"
    base_branch: Optional[str] = None
    commit_sha: Optional[str] = None
    custom_prompt: Optional[str] = None
    steer: Optional[str] = None
    name: Optional[str] = None
    wait: Optional[int] = None
    model: Optional[str] = None
    working_dir: Optional[str] = None
```

Add endpoints:

```python
@app.post("/sessions/{session_id}/review")
async def start_review(session_id: str, request: StartReviewRequest):
    """Start a Codex review on an existing session."""

@app.post("/sessions/review")
async def spawn_review(request: SpawnReviewRequest):
    """Spawn a new Codex session and start a review."""
```

#### Step 5: Add CLI command and dispatch

**File:** `src/cli/main.py`

Add argparse subparser:

```python
review_parser = subparsers.add_parser("review", help="Start a Codex code review")
review_parser.add_argument("session", nargs="?", help="Session ID or name to review on")
review_parser.add_argument("--base", help="Review against this base branch")
review_parser.add_argument("--uncommitted", action="store_true", help="Review uncommitted changes")
review_parser.add_argument("--commit", help="Review a specific commit SHA")
review_parser.add_argument("--custom", help="Custom review instructions")
review_parser.add_argument("--new", action="store_true", help="Spawn a new session for the review")
review_parser.add_argument("--name", help="Friendly name (with --new)")
review_parser.add_argument("--wait", type=int, default=None, help="Notify when review completes (seconds; defaults to 600 when in managed session)")
review_parser.add_argument("--model", help="Model override (with --new)")
review_parser.add_argument("--working-dir", help="Working directory (with --new)")
review_parser.add_argument("--steer", help="Instructions to inject after review starts")
```

Add `review` to `no_session_needed` list (for standalone invocation without parent context) and add dispatch.

Dispatch logic:
- If `session` provided and not `--new`: call `start_review()` on existing session
- If `--new`: call `spawn_review_session()` (requires `CLAUDE_SESSION_MANAGER_ID` for parent)
- If neither: error

**File:** `src/cli/commands.py`

Add `cmd_review()`:

```python
def cmd_review(
    client: SessionManagerClient,
    parent_session_id: Optional[str],
    session: Optional[str] = None,
    base: Optional[str] = None,
    uncommitted: bool = False,
    commit: Optional[str] = None,
    custom: Optional[str] = None,
    new: bool = False,
    name: Optional[str] = None,
    wait: Optional[int] = None,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    steer: Optional[str] = None,
) -> int:
```

Validation and defaulting:
- Exactly one mode required: `--base`, `--uncommitted`, `--commit`, or `--custom`
- If `--new` not set, `session` is required
- If `--new` set, `parent_session_id` is required (must be in a managed session)
- If no `parent_session_id` and no `--new`, runs standalone (review on existing session, no parent tracking)
- **`--wait` defaulting:** If `wait` is None and caller has session context (`parent_session_id` is set), default to 600. If no session context, leave as None (no watching). This avoids spurious warnings for standalone users who never asked to wait.

#### Step 6: Add API client methods

**File:** `src/cli/client.py`

```python
def start_review(self, session_id: str, mode: str, **kwargs) -> Optional[dict]:
    """POST /sessions/{session_id}/review"""

def spawn_review(self, parent_session_id: str, mode: str, **kwargs) -> Optional[dict]:
    """POST /sessions/review"""
```

#### Step 7: Add config section

**File:** `config.yaml`

Add under existing `codex` section:

```yaml
codex:
  # ... existing codex config ...
  review:
    default_wait: 600
    menu_settle_seconds: 1.0
    branch_settle_seconds: 1.0
    steer_delay_seconds: 5.0
```

---

### Phase 2: Deferred Steering & Output Parsing (follow-up)

- Add `--steer` delivery mode to `sm send` for Enter-based mid-turn injection (distinct from `--urgent` which sends Escape)
- Parse review output from tmux pane to extract structured findings
- Forward parsed findings to Telegram with formatting
- Add `GET /sessions/{id}/review-results` endpoint

### Phase 3: App-server Integration (stretch)

- Support review via `codex-app` provider if Codex app-server exposes a review RPC method

---

## 5. Key Files to Modify

| File | Change |
|------|--------|
| `src/models.py` | Add `ReviewConfig` dataclass; add `review_config` field to `Session` |
| `src/tmux_controller.py` | Add `send_review_sequence()` and `send_steer_text()` async methods |
| `src/session_manager.py` | Add `start_review()` and `spawn_review_session()` methods |
| `src/server.py` | Add `StartReviewRequest`, `SpawnReviewRequest` models and two endpoints |
| `src/cli/commands.py` | Add `cmd_review()` function |
| `src/cli/main.py` | Add `review` subparser and dispatch |
| `src/cli/client.py` | Add `start_review()` and `spawn_review()` API client methods |
| `config.yaml` | Add `codex.review` configuration section |

---

## 6. Edge Cases & Risks

### Menu Navigation Reliability
The biggest risk is that Codex's TUI menu layout changes between versions, breaking positional navigation. Mitigations:
- **Version pinning**: Document which Codex CLI versions this was tested against.
- **Output monitoring**: After sending key sequences, capture tmux pane output to verify expected state transitions before proceeding to next step.
- **Custom mode fallback**: If branch/commit mode navigation fails, the user can always use `--custom` which bypasses the menu entirely (at the cost of losing native diff computation).

### Branch Not Found
If the specified base branch doesn't exist locally:
- Run `git branch --list <branch>` in the working directory before sending `/review`
- Fail fast with a clear error message from the CLI

### Working Directory Not a Git Repo
`/review` requires a git repository. Validate before sending:
- Run `git rev-parse --git-dir` in the session's working directory
- Return error if not a git repo

### Premature Idle Detection with `--wait`
For reused sessions, `ChildMonitor` calculates idle time from `last_tool_call` or falls back to `spawned_at/created_at` (child_monitor.py:101-110). Reviews don't make tool calls, so `last_tool_call` would be `None`, causing fallback to the original spawn time.

**Mitigation:** When `start_review()` is called, set `session.last_tool_call = datetime.now()`. This is a semantic hack (no tool call actually happened) but correctly puts the idle calculation into the `if last_tool_call` branch (line 101) using the review start time as baseline. Setting it to `None` would be counterproductive — it triggers the `else` fallback to `spawned_at`.

For existing-session reviews, `--wait` uses `watch_session()` (Stop-hook-based idle detection) instead of `ChildMonitor`, so the `last_tool_call` hack is only needed for `--new` reviews that go through `ChildMonitor`.

### Session Provider Mismatch
`sm review` only works on Codex CLI sessions (`provider == "codex"`). If called on a Claude or codex-app session:
- Return clear error: "Review requires a Codex CLI session (provider=codex)"

### Custom Instructions + Branch Mode
Codex CLI 0.101.0 does not support combining branch/commit mode with custom instructions in a single invocation (they are mutually exclusive menu options). The only way to add custom focus to a branch review is via steering after the review starts.

**User guidance:** Use `--base main --steer "focus on security"` rather than trying to combine `--base` with `--custom`.

### Concurrent Reviews on Same Session
Sending `/review` to a session that's mid-review will likely interrupt or queue a new review.
- Validate that the session is idle before sending `/review`
- If session is not idle, return error: "Session is busy. Wait for current work to complete or use sm clear first."
