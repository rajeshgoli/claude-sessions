# Claude Session Manager

A local macOS server that manages Claude Code sessions in tmux, with bidirectional communication via Telegram and Email.

## Features

- **Session Management**: Create, list, kill Claude Code sessions running in tmux
- **Telegram Bot**: Control sessions via Telegram commands, reply to threads to send input
- **Email Notifications**: Reuses existing email harness for urgent notifications
- **Output Monitoring**: Detects permission prompts, errors, and idle sessions
- **Terminal Integration**: Open sessions in Terminal.app windows

## Prerequisites

- macOS
- Python 3.11+
- tmux (`brew install tmux`)
- Claude Code CLI installed

## Quick Setup

```bash
# Clone and enter directory
cd claude-session-manager

# Run setup script
chmod +x setup.sh
./setup.sh

# Edit configuration
vim config.yaml

# Start the server
source venv/bin/activate
python -m src.main
```

## Setting Up the Telegram Bot

1. **Create a bot with BotFather**:
   - Open Telegram and search for `@BotFather`
   - Send `/newbot`
   - Choose a name (e.g., "Claude Session Manager")
   - Choose a username (e.g., "my_claude_sessions_bot")
   - Copy the token provided

2. **Get your chat ID**:
   - Send a message to your new bot
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   - Find your chat ID in the response

3. **Configure**:
   ```yaml
   telegram:
     token: "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
     allowed_chat_ids:
       - 123456789  # Your chat ID
   ```

## Usage

### Telegram Commands

| Command | Description |
|---------|-------------|
| `/new [path]` | Create new Claude session in directory |
| `/list` | List active sessions |
| `/status <id>` | Get session status |
| `/kill <id>` | Kill a session |
| `/open <id>` | Open session in Terminal.app |
| `/help` | Show help message |

**Reply to a session message** to send input to Claude.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sessions` | POST | Create new session |
| `/sessions` | GET | List all sessions |
| `/sessions/{id}` | GET | Get session details |
| `/sessions/{id}/input` | POST | Send input to session |
| `/sessions/{id}/key` | POST | Send key (y/n) to session |
| `/sessions/{id}` | DELETE | Kill session |
| `/sessions/{id}/open` | POST | Open in Terminal.app |
| `/sessions/{id}/output` | GET | Capture recent output |
| `/notify` | POST | Send notification |

### Example API Usage

```bash
# Create a session
curl -X POST http://localhost:8420/sessions \
  -H "Content-Type: application/json" \
  -d '{"working_dir": "~/projects/myapp"}'

# List sessions
curl http://localhost:8420/sessions

# Send input
curl -X POST http://localhost:8420/sessions/abc123/input \
  -H "Content-Type: application/json" \
  -d '{"text": "Fix the bug in login.py"}'

# Send permission response
curl -X POST http://localhost:8420/sessions/abc123/key \
  -H "Content-Type: application/json" \
  -d '{"key": "y"}'

# Request email notification (from Claude hook)
curl -X POST http://localhost:8420/notify \
  -H "Content-Type: application/json" \
  -d '{"message": "Task complete!", "channel": "email", "urgent": true}'
```

## Email Integration

The session manager reuses the existing email harness from `../claude-email-automation/`. Ensure that directory contains:
- `email.yaml` - SMTP configuration
- `imap.yaml` - IMAP configuration

Claude can request email notifications via the `/notify` endpoint with `"channel": "email"`.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐
│  Telegram Bot   │────▶│  Session Manager │
└─────────────────┘     └────────┬─────────┘
                                 │
┌─────────────────┐              │
│   FastAPI       │◀─────────────┤
│   Server        │              │
└─────────────────┘              ▼
                        ┌──────────────────┐
┌─────────────────┐     │  tmux Controller │
│ Output Monitor  │────▶│                  │
└─────────────────┘     └────────┬─────────┘
        │                        │
        ▼                        ▼
┌─────────────────┐     ┌──────────────────┐
│   Notifier      │     │  tmux sessions   │
│ (Telegram/Email)│     │  (Claude Code)   │
└─────────────────┘     └──────────────────┘
```

## Configuration Reference

```yaml
server:
  host: "127.0.0.1"      # Bind address
  port: 8420             # Server port

paths:
  log_dir: "/tmp/claude-sessions"
  state_file: "/tmp/claude-sessions/sessions.json"

monitor:
  idle_timeout: 300      # Seconds before idle notification
  poll_interval: 1.0     # Output check frequency

telegram:
  token: "BOT_TOKEN"     # From @BotFather
  allowed_chat_ids: []   # Empty = allow all

email:
  smtp_config: ""        # Path to email.yaml (optional)
  imap_config: ""        # Path to imap.yaml (optional)
```

## Troubleshooting

**Bot not responding**: Verify the token is correct and the bot is started.

**Session not created**: Check that tmux is installed and Claude Code CLI is available.

**No notifications**: Ensure the session has a Telegram chat ID associated (create via `/new`).

**Email not sending**: Verify the email harness at `../claude-email-automation/` is configured.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with debug logging
LOG_LEVEL=DEBUG python -m src.main
```
