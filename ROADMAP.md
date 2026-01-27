# Claude Session Manager - Roadmap

## Backlog (Not Prioritized)

### User Input Detection from Tmux
**Status:** Research Complete
**Complexity:** Medium
**Description:** Detect when user types messages directly in tmux terminal and forward them to Telegram.

**Technical Details:**
- Monitor tmux log files for lines containing `❯` prompt
- Strip ANSI escape codes and extract user input text
- Send notifications to Telegram when new input detected
- Avoid duplicates using state tracking
- Filter out Claude's thinking messages ("Tomfoolering…", etc.)

**Implementation Notes:**
- Add pattern detector in OutputMonitor (similar to permission/error detectors)
- Use regex pattern: `❯\s+(.+)` to capture input
- Estimated 50-100 lines of code
- Would provide bidirectional visibility: see both Claude responses AND user prompts in Telegram

**Use Case:** When switching between devices/locations, user can see full conversation history in Telegram including what they asked Claude, not just Claude's responses.

---

## Potential Future Enhancements

### Session Grouping/Tagging
- Add tags to sessions (e.g., "backend", "frontend", "research")
- Filter `/list` by tag
- Useful for managing many concurrent sessions

### Session Snapshots
- Save full transcript at specific points
- Resume from snapshot later
- Useful for experimentation with rollback capability

### Cost Tracking
- Track API usage per session
- Show cumulative costs in `/status`
- Budget alerts

### Multi-User Support
- Allow multiple Telegram users to share sessions
- Permission levels (view-only, interact, admin)
- Useful for team collaboration

### Session Templates
- Pre-configured session types (e.g., "Python Dev", "System Admin")
- Auto-load specific working directories and initial prompts
- Quick session creation with `/new template:python-dev`

### Web Dashboard
- Alternative to Telegram for session management
- Visual session browser
- Real-time output streaming
- Mobile-friendly interface

### Session Recording/Replay
- Record full session for later playback
- Export to video or text format
- Training/documentation purposes

### Smart Notifications
- ML-based notification filtering (detect truly important events)
- Quiet hours configuration
- Priority levels for different notification types

### Integration Hooks
- Webhook support for external services
- Slack/Discord integration in addition to Telegram
- Custom notification handlers

---

## Completed Features

- ✅ Basic session management (create, list, kill)
- ✅ Telegram bot interface with commands
- ✅ Real-time notifications via hooks
- ✅ Forum topic organization
- ✅ Friendly session naming
- ✅ Tmux status bar updates
- ✅ AI-powered session summaries
- ✅ Session activity tracking
- ✅ Notification filtering (config-based)
- ✅ Message retrieval (`/message` command)
- ✅ Terminal attachment
- ✅ Session interrupt (Escape key)

---

## Notes

This roadmap is not prioritized. Features are added as interesting ideas emerge during development and usage. Implementation depends on user need and development time availability.
