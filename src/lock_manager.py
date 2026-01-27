"""Lock file management for multi-agent coordination fallback."""

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LOCK_FILE_NAME = ".claude/workspace.lock"
STALE_THRESHOLD_MINUTES = 30


@dataclass
class LockInfo:
    """Information about a workspace lock."""
    session_id: str
    task: str
    branch: str
    started: datetime

    def is_stale(self) -> bool:
        """Check if lock is older than threshold."""
        age = datetime.now() - self.started
        return age > timedelta(minutes=STALE_THRESHOLD_MINUTES)


class LockManager:
    """Manages workspace lock files for agent coordination."""

    def __init__(self, working_dir: str = "."):
        """
        Initialize lock manager.

        Args:
            working_dir: Working directory (will find git root)
        """
        self.working_dir = Path(working_dir).resolve()
        self.repo_root = self._find_repo_root()
        self.lock_file = self.repo_root / LOCK_FILE_NAME if self.repo_root else None

    def _find_repo_root(self) -> Optional[Path]:
        """Find git repository root."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return Path(result.stdout.strip())
            return None
        except Exception as e:
            logger.debug(f"Failed to find git root: {e}")
            return None

    def _get_current_branch(self) -> str:
        """Get current git branch."""
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip() or "unknown"
            return "unknown"
        except Exception:
            return "unknown"

    def acquire_lock(self, session_id: str, task: str) -> bool:
        """
        Acquire a workspace lock.

        Args:
            session_id: Session ID acquiring lock
            task: Task description

        Returns:
            True if lock acquired, False if lock exists
        """
        if not self.lock_file:
            logger.warning("Not in a git repository, cannot acquire lock")
            return False

        # Check if lock already exists
        existing_lock = self.check_lock()
        if existing_lock and not existing_lock.is_stale():
            logger.info(f"Lock already held by session {existing_lock.session_id}")
            return False

        # Create .claude directory if needed
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        # Write lock file
        branch = self._get_current_branch()
        started = datetime.now().isoformat()

        try:
            with open(self.lock_file, "w") as f:
                f.write(f"session={session_id}\n")
                f.write(f"task={task}\n")
                f.write(f"branch={branch}\n")
                f.write(f"started={started}\n")
            logger.info(f"Lock acquired by session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to write lock file: {e}")
            return False

    def release_lock(self, session_id: Optional[str] = None) -> bool:
        """
        Release a workspace lock.

        Args:
            session_id: Optional session ID (only releases if it owns the lock)

        Returns:
            True if lock released or didn't exist
        """
        if not self.lock_file or not self.lock_file.exists():
            return True

        # If session_id provided, verify ownership
        if session_id:
            existing_lock = self.check_lock()
            if existing_lock and existing_lock.session_id != session_id:
                logger.warning(
                    f"Lock held by {existing_lock.session_id}, "
                    f"not releasing for {session_id}"
                )
                return False

        try:
            self.lock_file.unlink()
            logger.info("Lock released")
            return True
        except Exception as e:
            logger.error(f"Failed to release lock: {e}")
            return False

    def check_lock(self) -> Optional[LockInfo]:
        """
        Check if a lock exists.

        Returns:
            LockInfo if lock exists, None otherwise
        """
        if not self.lock_file or not self.lock_file.exists():
            return None

        try:
            with open(self.lock_file) as f:
                lines = f.readlines()

            lock_data = {}
            for line in lines:
                line = line.strip()
                if "=" in line:
                    key, value = line.split("=", 1)
                    lock_data[key] = value

            if not all(k in lock_data for k in ["session", "task", "branch", "started"]):
                logger.warning("Invalid lock file format")
                return None

            return LockInfo(
                session_id=lock_data["session"],
                task=lock_data["task"],
                branch=lock_data["branch"],
                started=datetime.fromisoformat(lock_data["started"]),
            )
        except Exception as e:
            logger.error(f"Failed to read lock file: {e}")
            return None

    def is_locked(self) -> bool:
        """
        Check if workspace is locked (and not stale).

        Returns:
            True if locked by another active session
        """
        lock = self.check_lock()
        return lock is not None and not lock.is_stale()
