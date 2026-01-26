# Claude Session Manager

## Overview

A local macOS server that manages Claude Code sessions in visible Terminal.app windows, providing bidirectional communication via Telegram (primary) and Email (secondary). User sees everything locally in real terminal windows AND can interact remotely from phone/anywhere.

## Goals

1. Never lose access to a Claude session (no more "email timed out" dead ends)
2. See Claude's work in real Terminal.app windows when at laptop
3. Get notified and respond via Telegram when away
4. Support multiple concurrent sessions without juggling session IDs
5. Let Claude explicitly request email for longer-form communication

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Mac (local)                                                        │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Terminal.app (visible windows)                                │  │
│  │                                                               │  │
│  │   ┌─────────────────┐  ┌─────────────────┐                    │  │
│  │   │ tmux: claude-a1 │  │ tmux: claude-b2 │  ...               │  │
│  │   │ └─► claude code │  │ └─► claude code │                    │  │
│  │   └─────────────────┘  └─────────────────┘                    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│           │                        │                                │
│           │ pipe-pane              │ pipe-pane                      │
│           ▼                        ▼                                │
│     /tmp/claude-a1.log       /tmp/claude-b2.log                     │
│           │                        │                                │
│           └──────────┬─────────────┘                                │
│                      ▼                                              │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Session Manager Server (Python, runs locally)                 │  │
│  │                                                               │  │
│  │  • Spawns sessions via osascript + tmux                       │  │
│  │  • Tails log files for output monitoring                      │  │
│  │  • Detects permission prompts and idle states                 │  │
│  │  • Receives hook calls from Claude Code                       │  │
│  │  • Routes notifications to Telegram or Email                  │  │
│  │  • Receives replies, injects into correct PTY                 │  │
│  │  • Polls IMAP for email replies                               │  │
│  └───────────────────────────────────────────────────────────────┘  │
│              ▲                              │                        │
│              │ HTTP hooks                   │                        │
│              │ (localhost:8420)             │                        │
└──────────────┼──────────────────────────────┼────────────────────────┘
               │                              │
               │                              ▼
        Claude Code               ┌─────────────────────┐
        (calls hooks)             │ Telegram Bot API    │
                                  │ SMTP/IMAP           │
                                  └─────────────────────┘
                                             │
                                             ▼
                                      ┌─────────────┐
                                      │ Your Phone  │
                                      └─────────────┘
```

## Session Lifecycle

### Creating a Session

1. User sends `/new ~/projects/myapp` to Telegram bot (or starts locally)
2. Server generates short session ID (e.g., `a1b2`)
3. Server runs:
   ```bash
   osascript -e 'tell app "Terminal" to do script "tmux new-session -s claude-a1b2 \"claude\""'
   ```
4. Server sets up output capture:
   ```bash
   tmux pipe-pane -t claude-a1b2 "cat >> /tmp/claude-a1b2.log"
   ```
5. Server starts tailing `/tmp/claude-a1b2.log`
6. Server sends Telegram message: "🚀 Session `a1b2` started: ~/projects/myapp"
7. All subsequent updates for this session reply to this root message (threading)

### Session State

```python
@dataclass
class Session:
    id: str                          # short id, e.g. "a1b2"
    tmux_session: str                # "claude-a1b2"
    log_file: Path                   # /tmp/claude-a1b2.log
    cwd: str                         # working directory
    telegram_root_msg_id: int        # root message for threading
    started_at: datetime
    last_output_at: datetime
    status: Literal["running", "waiting_permission", "idle", "dead"]
```

## Output Monitoring

Server tails each session's log file and:

1. **Tracks last output time** → detects idle
2. **Pattern matches for permission prompts** → detects waiting state
3. **Extracts relevant content** for notifications

### Permission Prompt Detection

Look for patterns like:
- `Do you want to proceed?`
- `Allow this action?`
- `[Y/n]`
- `Permission required`
- Tool use confirmations

When detected:
1. Set session status to `waiting_permission`
2. Extract the question/context
3. Send Telegram notification

### Idle Detection

- If no output for 60 seconds → send Telegram "💤 Session `a1b2` idle"
- If no output for 10 minutes → optionally send email summary
- Reset timer on any new output

## Notification Routing

### Default: Telegram

Everything goes to Telegram unless Claude explicitly requests email.

| Event | Channel | Format |
|-------|---------|--------|
| Permission prompt | Telegram | "🔔 **a1b2** Allow: `rm -rf node_modules`?" |
| Idle (60s) | Telegram | "💤 **a1b2** idle for 60s" |
| Error/crash | Telegram | "❌ **a1b2** error: ..." |
| Session complete | Telegram | "✅ **a1b2** finished" |
| Claude requests email | Email | Full content Claude provides |

### Claude-Initiated Email

Claude can call a hook/script to explicitly send email:

```bash
# Claude runs this when it wants to send email
curl http://localhost:8420/notify \
  -H "Content-Type: application/json" \
  -d '{
    "channel": "email",
    "subject": "Task Summary: Refactored auth module",
    "body": "Here is the detailed summary..."
  }'
```

This goes via email. Reply to that email also works (see below).

## Inbound Messages

### Telegram (Primary)

User replies to a session's message thread. Server:

1. Looks up which session based on `reply_to_message_id`
2. Extracts text
3. Handles commands or injects as input

**Commands (prefix with /):**

| Command | Action |
|---------|--------|
| `/new <path>` | Start new session at path |
| `/list` | List active sessions |
| `/clear` | Send `/clear` to session |
| `/compact` | Send `/compact` to session |
| `/status` | Show session status |
| `/kill` | Terminate session |
| `/output` | Get last N lines of output |
| (any other text) | Inject as input to Claude |

**Input injection:**
```bash
tmux send-keys -t claude-a1b2 "user's message here" Enter
```

### Email (Secondary)

Server polls IMAP periodically (every 30s). When reply arrives:

1. Parse subject for session ID (e.g., "Re: [claude-a1b2] Task Summary")
2. Extract reply body (strip quoted content)
3. Inject into session via `tmux send-keys`

If no session ID found in subject, attempt to match by most recent email sent.

## Hook Integration

Claude Code hooks should call the server for custom notifications.

### Claude Code Settings (~/.claude/settings.json)

```json
{
  "hooks": {
    "on_tool_use": "curl -s http://localhost:8420/hook -d \"event=tool_use&tool=$TOOL_NAME&session=$CLAUDE_SESSION_ID\"",
    "on_permission_request": "curl -s http://localhost:8420/hook -d \"event=permission&prompt=$PROMPT\""
  }
}
```

### Hook Endpoint

```
POST /hook
Content-Type: application/x-www-form-urlencoded

event=permission&prompt=Run+bash+command?&session=a1b2
```

Server uses hooks as additional signal alongside log file monitoring.

## API Endpoints

```
POST /session/new
  body: { "cwd": "/path/to/project" }
  returns: { "session_id": "a1b2" }

GET /session/:id
  returns: { session state }

POST /session/:id/input
  body: { "text": "user input here" }
  action: tmux send-keys

POST /session/:id/kill
  action: tmux kill-session

GET /sessions
  returns: [ list of sessions ]

POST /hook
  body: { event, session?, ... }
  action: process hook event

POST /notify
  body: { "channel": "email"|"telegram", "message": "...", "subject?": "..." }
  action: send notification (used by Claude to explicitly request channel)
```

## Telegram Message Threading

```
┌─────────────────────────────────────────────────────────────────┐
│ Telegram Chat with Bot                                          │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ 🚀 Session a1b2 started: ~/projects/foo                     │ │ ← root msg
│ │                                                             │ │
│ │   ├─ 🔔 Allow: run `npm install`?                           │ │
│ │   │     └─ You: y                                           │ │
│ │   │                                                         │ │
│ │   ├─ 🔔 Allow: edit src/auth.ts?                            │ │
│ │   │     └─ You: y                                           │ │
│ │   │                                                         │ │
│ │   ├─ 💤 Idle for 60s                                        │ │
│ │   │     └─ You: continue with the tests                     │ │
│ │   │                                                         │ │
│ │   ├─ 🔔 Allow: run test suite?                              │ │
│ │   │     └─ You: /clear                                      │ │
│ │   │                                                         │ │
│ │   └─ ✅ Session complete                                    │ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ 🚀 Session c3d4 started: ~/projects/bar                     │ │ ← different
│ │   ...                                                       │ │   session
│ └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

Replying to any message in a thread routes to that session. No session IDs needed.

## Email Format

### Outbound (server → user)

```
From: claude-bot@yourdomain.com
To: you@yourdomain.com
Subject: [claude-a1b2] Task Summary: Refactored auth module

<content from Claude>

---
Reply to this email to send input to session a1b2.
```

### Inbound (user → server)

Server polls IMAP, parses:
- Subject line for `[claude-XXXX]` pattern
- Body for reply content (strips quoted text)

## Tech Stack

- **Python 3.11+**
- **libtmux** - tmux session management
- **python-telegram-bot** - Telegram bot (async, webhook or polling)
- **aiofiles** - async file tailing
- **aiosmtplib** - async email sending
- **aioimaplib** - async IMAP polling
- **FastAPI** - hook endpoint server
- **uvicorn** - ASGI server

## File Structure

```
claude-session-manager/
├── pyproject.toml
├── README.md
├── config.yaml                 # user configuration
├── src/
│   ├── __init__.py
│   ├── main.py                 # entry point
│   ├── server.py               # FastAPI app
│   ├── session_manager.py      # session lifecycle
│   ├── tmux_controller.py      # tmux operations
│   ├── output_monitor.py       # log file tailing & parsing
│   ├── telegram_bot.py         # telegram integration
│   ├── email_handler.py        # smtp/imap
│   ├── notifier.py             # routing logic
│   └── models.py               # dataclasses
└── tests/
```

## Configuration

```yaml
# config.yaml

server:
  host: "127.0.0.1"
  port: 8420

telegram:
  bot_token: "YOUR_BOT_TOKEN"
  chat_id: "YOUR_CHAT_ID"         # your personal chat with the bot

email:
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  imap_host: "imap.gmail.com"
  imap_port: 993
  username: "your-email@gmail.com"
  password: "app-specific-password"
  from_address: "claude-bot@gmail.com"
  to_address: "you@gmail.com"
  poll_interval_seconds: 30

sessions:
  idle_warning_seconds: 60
  idle_email_seconds: 600         # 10 min
  log_dir: "/tmp"

claude:
  default_args: []                # additional args for claude command
```

## Startup

```bash
# Install
pip install -e .

# Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your credentials

# Run
claude-session-manager

# Or with custom config
claude-session-manager --config /path/to/config.yaml
```

Server starts:
- FastAPI on localhost:8420
- Telegram bot (polling mode)
- IMAP polling loop
- Ready to manage sessions

## Security Notes

- Server binds to localhost only
- Telegram bot only responds to configured chat_id
- Email only processes replies from configured to_address
- No auth on local API (trusted local environment)

## Future Enhancements

- [ ] Web UI for session monitoring
- [ ] Output streaming to Telegram (optional, configurable)
- [ ] Session persistence across server restarts
- [ ] Multiple Telegram users (team mode)
- [ ] Voice messages → speech-to-text → input
- [ ] Screenshot capture of Terminal window
