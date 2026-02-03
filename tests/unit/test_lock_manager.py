"""Unit tests for LockManager - ticket #61."""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

from src.lock_manager import (
    LockManager,
    LockInfo,
    LockResult,
    LOCK_FILE_NAME,
    STALE_THRESHOLD_MINUTES,
)


class TestLockInfo:
    """Tests for LockInfo dataclass."""

    def test_lock_info_is_stale_when_fresh(self):
        """is_stale() returns False for fresh lock."""
        lock = LockInfo(
            session_id="test123",
            task="testing",
            branch="main",
            started=datetime.now(),
        )
        assert lock.is_stale() is False

    def test_lock_info_is_stale_after_threshold(self):
        """is_stale() returns True after threshold."""
        # Create lock that started 31 minutes ago
        old_time = datetime.now() - timedelta(minutes=STALE_THRESHOLD_MINUTES + 1)
        lock = LockInfo(
            session_id="test123",
            task="testing",
            branch="main",
            started=old_time,
        )
        assert lock.is_stale() is True

    def test_lock_info_is_stale_just_before_threshold(self):
        """is_stale() returns False just before threshold (boundary test)."""
        # Create lock 1 second before threshold - should not be stale yet
        just_before_threshold = datetime.now() - timedelta(minutes=STALE_THRESHOLD_MINUTES) + timedelta(seconds=5)
        lock = LockInfo(
            session_id="test123",
            task="testing",
            branch="main",
            started=just_before_threshold,
        )
        # Just before the threshold, it should not be stale
        assert lock.is_stale() is False


class TestLockManager:
    """Tests for LockManager class."""

    def test_acquire_lock_creates_file(self, tmp_path):
        """acquire_lock creates .claude/workspace.lock."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))
                result = manager.acquire_lock("session123", "test task")

        assert result is True
        lock_file = tmp_path / ".claude" / "workspace.lock"
        assert lock_file.exists()

        # Verify lock file content
        content = lock_file.read_text()
        assert "session=session123" in content
        assert "task=test task" in content
        assert "branch=main" in content
        assert "started=" in content

    def test_acquire_lock_fails_if_already_locked(self, tmp_path):
        """Cannot acquire lock held by another session."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))

                # First session acquires lock
                result1 = manager.acquire_lock("session1", "task1")
                assert result1 is True

                # Second session tries to acquire - should fail
                result2 = manager.acquire_lock("session2", "task2")
                assert result2 is False

    def test_acquire_lock_succeeds_if_stale(self, tmp_path):
        """Can acquire lock if existing lock is stale (>30 min)."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))

                # Create stale lock manually
                lock_dir = tmp_path / ".claude"
                lock_dir.mkdir()
                lock_file = lock_dir / "workspace.lock"
                stale_time = (datetime.now() - timedelta(minutes=STALE_THRESHOLD_MINUTES + 5)).isoformat()
                lock_file.write_text(f"session=old_session\ntask=old task\nbranch=main\nstarted={stale_time}\n")

                # New session should be able to acquire lock
                result = manager.acquire_lock("new_session", "new task")
                assert result is True

                # Verify the lock was overwritten
                content = lock_file.read_text()
                assert "session=new_session" in content

    def test_release_lock_removes_file(self, tmp_path):
        """release_lock deletes the lock file."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))

                # Acquire then release
                manager.acquire_lock("session123", "test task")
                lock_file = tmp_path / ".claude" / "workspace.lock"
                assert lock_file.exists()

                result = manager.release_lock()
                assert result is True
                assert not lock_file.exists()

    def test_release_lock_only_if_owner(self, tmp_path):
        """Cannot release lock owned by another session."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))

                # Session 1 acquires lock
                manager.acquire_lock("session1", "task1")

                # Session 2 tries to release - should fail
                result = manager.release_lock(repo_root=str(tmp_path), session_id="session2")
                assert result is False

                # Lock file should still exist
                lock_file = tmp_path / ".claude" / "workspace.lock"
                assert lock_file.exists()

    def test_check_lock_returns_info(self, tmp_path):
        """check_lock returns LockInfo with correct fields."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch', return_value='feature/test'):
                manager = LockManager(working_dir=str(tmp_path))

                # Acquire lock
                manager.acquire_lock("session123", "test task")

                # Check lock
                lock_info = manager.check_lock()
                assert lock_info is not None
                assert lock_info.session_id == "session123"
                assert lock_info.task == "test task"
                assert lock_info.branch == "feature/test"
                assert isinstance(lock_info.started, datetime)

    def test_check_lock_returns_none_when_no_lock(self, tmp_path):
        """check_lock returns None when no lock file exists."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            manager = LockManager(working_dir=str(tmp_path))
            lock_info = manager.check_lock()
            assert lock_info is None

    def test_is_locked_false_when_no_lock(self, tmp_path):
        """is_locked returns False when no lock file exists."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            manager = LockManager(working_dir=str(tmp_path))
            assert manager.is_locked() is False

    def test_is_locked_true_when_active_lock(self, tmp_path):
        """is_locked returns True when active lock exists."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))
                manager.acquire_lock("session123", "test task")
                assert manager.is_locked() is True

    def test_is_locked_false_when_stale(self, tmp_path):
        """is_locked returns False when lock is stale."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            manager = LockManager(working_dir=str(tmp_path))

            # Create stale lock manually
            lock_dir = tmp_path / ".claude"
            lock_dir.mkdir()
            lock_file = lock_dir / "workspace.lock"
            stale_time = (datetime.now() - timedelta(minutes=STALE_THRESHOLD_MINUTES + 5)).isoformat()
            lock_file.write_text(f"session=old\ntask=old task\nbranch=main\nstarted={stale_time}\n")

            assert manager.is_locked() is False


class TestTryAcquire:
    """Tests for try_acquire method used by auto-lock feature."""

    def test_try_acquire_success(self, tmp_path):
        """try_acquire returns LockResult with acquired=True on success."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch_for_path', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))
                result = manager.try_acquire(str(tmp_path), "session123")

                assert isinstance(result, LockResult)
                assert result.acquired is True
                assert result.locked_by_other is False
                assert result.owner_session_id is None

    def test_try_acquire_blocked_by_other(self, tmp_path):
        """try_acquire returns LockResult indicating locked by another session."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch_for_path', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))

                # Session 1 acquires lock
                manager.try_acquire(str(tmp_path), "session1")

                # Session 2 tries
                result = manager.try_acquire(str(tmp_path), "session2")

                assert result.acquired is False
                assert result.locked_by_other is True
                assert result.owner_session_id == "session1"

    def test_try_acquire_succeeds_for_same_session(self, tmp_path):
        """try_acquire succeeds if same session already holds lock."""
        # Create a mock git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch.object(LockManager, '_find_repo_root', return_value=tmp_path):
            with patch.object(LockManager, '_get_current_branch_for_path', return_value='main'):
                manager = LockManager(working_dir=str(tmp_path))

                # Session acquires lock twice
                result1 = manager.try_acquire(str(tmp_path), "session123")
                result2 = manager.try_acquire(str(tmp_path), "session123")

                assert result1.acquired is True
                assert result2.acquired is True


class TestLockResult:
    """Tests for LockResult dataclass."""

    def test_lock_result_defaults(self):
        """LockResult has correct default values."""
        result = LockResult(acquired=True)
        assert result.acquired is True
        assert result.locked_by_other is False
        assert result.owner_session_id is None

    def test_lock_result_with_owner(self):
        """LockResult stores owner session ID."""
        result = LockResult(
            acquired=False,
            locked_by_other=True,
            owner_session_id="other_session"
        )
        assert result.acquired is False
        assert result.locked_by_other is True
        assert result.owner_session_id == "other_session"
