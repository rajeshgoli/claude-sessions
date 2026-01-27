"""Data models for Claude Session Manager."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid


class SessionStatus(Enum):
    """Session lifecycle status."""
    STARTING = "starting"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_PERMISSION = "waiting_permission"
    IDLE = "idle"
    STOPPED = "stopped"
    ERROR = "error"


class NotificationChannel(Enum):
    """Available notification channels."""
    TELEGRAM = "telegram"
    EMAIL = "email"


@dataclass
class Session:
    """Represents a Claude Code session in tmux."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    working_dir: str = ""
    tmux_session: str = ""
    log_file: str = ""
    status: SessionStatus = SessionStatus.STARTING
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    telegram_chat_id: Optional[int] = None
    telegram_root_msg_id: Optional[int] = None
    telegram_topic_id: Optional[int] = None  # Forum topic ID (message_thread_id)
    error_message: Optional[str] = None
    transcript_path: Optional[str] = None  # Claude's transcript file path
    friendly_name: Optional[str] = None  # User-friendly name
    current_task: Optional[str] = None  # What the session is currently working on
    git_remote_url: Optional[str] = None  # Git remote URL for repo matching

    def __post_init__(self):
        if not self.name:
            self.name = f"claude-{self.id}"
        if not self.tmux_session:
            self.tmux_session = self.name

    def to_dict(self) -> dict:
        """Convert session to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "working_dir": self.working_dir,
            "tmux_session": self.tmux_session,
            "log_file": self.log_file,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_root_msg_id": self.telegram_root_msg_id,
            "telegram_topic_id": self.telegram_topic_id,
            "error_message": self.error_message,
            "transcript_path": self.transcript_path,
            "friendly_name": self.friendly_name,
            "current_task": self.current_task,
            "git_remote_url": self.git_remote_url,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        """Create session from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            working_dir=data["working_dir"],
            tmux_session=data["tmux_session"],
            log_file=data["log_file"],
            status=SessionStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_activity=datetime.fromisoformat(data["last_activity"]),
            telegram_chat_id=data.get("telegram_chat_id"),
            telegram_root_msg_id=data.get("telegram_root_msg_id"),
            telegram_topic_id=data.get("telegram_topic_id"),
            error_message=data.get("error_message"),
            transcript_path=data.get("transcript_path"),
            friendly_name=data.get("friendly_name"),
            current_task=data.get("current_task"),
            git_remote_url=data.get("git_remote_url"),
        )


@dataclass
class NotificationEvent:
    """An event that should trigger a notification."""
    session_id: str
    event_type: str  # "permission_prompt", "idle", "error", "complete"
    message: str
    context: str = ""  # Recent output for context
    urgent: bool = False
    channel: Optional[NotificationChannel] = None  # None = use default


@dataclass
class UserInput:
    """Input received from user via Telegram or Email."""
    session_id: str
    text: str
    source: NotificationChannel
    chat_id: Optional[int] = None
    message_id: Optional[int] = None
