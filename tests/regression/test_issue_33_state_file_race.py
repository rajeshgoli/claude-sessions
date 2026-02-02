"""
Regression tests for issue #33: Race condition in concurrent state file writes

Tests verify that concurrent calls to _save_state() don't corrupt the state file.
"""

import pytest
import json
import asyncio
import threading
from pathlib import Path
from unittest.mock import Mock

from src.session_manager import SessionManager
from src.models import Session, SessionStatus


@pytest.fixture
def temp_state_file(tmp_path):
    """Create a temporary state file."""
    state_file = tmp_path / "sessions.json"
    # Initialize with empty sessions
    with open(state_file, "w") as f:
        json.dump({"sessions": []}, f)
    return str(state_file)


@pytest.fixture
def session_manager(temp_state_file, tmp_path):
    """Create a SessionManager for testing."""
    manager = SessionManager(
        log_dir=str(tmp_path),
        state_file=temp_state_file,
    )
    # Replace tmux controller with mock to avoid actual tmux calls
    mock_tmux = Mock()
    mock_tmux.session_exists = Mock(return_value=True)
    manager.tmux = mock_tmux
    return manager


def test_concurrent_save_state_no_corruption(session_manager, temp_state_file):
    """Test that concurrent _save_state calls don't corrupt the file."""
    # Add some test sessions
    for i in range(5):
        session = Session(
            id=f"test-{i}",
            tmux_session=f"tmux-{i}",
            working_dir="/tmp",
            status=SessionStatus.RUNNING
        )
        session_manager.sessions[session.id] = session

    # Call _save_state concurrently from multiple threads
    def save_state_multiple_times():
        for _ in range(10):
            session_manager._save_state()

    threads = []
    for _ in range(5):  # 5 threads
        thread = threading.Thread(target=save_state_multiple_times)
        threads.append(thread)
        thread.start()

    # Wait for all threads to complete
    for thread in threads:
        thread.join()

    # Verify the state file is valid JSON and contains all sessions
    state_path = Path(temp_state_file)
    assert state_path.exists(), "State file should exist"

    with open(state_path, "r") as f:
        data = json.load(f)  # Should not raise JSONDecodeError

    assert "sessions" in data
    assert len(data["sessions"]) == 5
    session_ids = {s["id"] for s in data["sessions"]}
    assert session_ids == {f"test-{i}" for i in range(5)}


def test_atomic_write_no_partial_file(session_manager, temp_state_file):
    """Test that a crash during write doesn't leave a partial file."""
    # Add a test session
    session = Session(
        id="test-1",
        tmux_session="tmux-1",
        working_dir="/tmp",
        status=SessionStatus.RUNNING
    )
    session_manager.sessions[session.id] = session

    # Save initial state
    session_manager._save_state()

    # Verify state file exists and is valid
    state_path = Path(temp_state_file)
    with open(state_path, "r") as f:
        data = json.load(f)
    assert len(data["sessions"]) == 1

    # The temp file should not exist after successful write
    temp_file = state_path.with_suffix('.tmp')
    assert not temp_file.exists(), "Temp file should be cleaned up after rename"


def test_temp_file_cleanup_on_error(session_manager, temp_state_file):
    """Test that temp file is cleaned up if an error occurs during write."""
    import tempfile

    # Add a session with data that will cause an error during serialization
    # (We'll mock json.dump to raise an error)
    session = Session(
        id="test-1",
        tmux_session="tmux-1",
        working_dir="/tmp",
        status=SessionStatus.RUNNING
    )
    session_manager.sessions[session.id] = session

    # Mock json.dump to raise an error
    import src.session_manager
    original_json_dump = src.session_manager.json.dump

    def failing_json_dump(*args, **kwargs):
        raise ValueError("Simulated serialization error")

    src.session_manager.json.dump = failing_json_dump

    try:
        # This should fail but not leave a temp file
        session_manager._save_state()

        # Verify temp file was cleaned up
        temp_file = Path(temp_state_file).with_suffix('.tmp')
        assert not temp_file.exists(), "Temp file should be cleaned up on error"
    finally:
        # Restore original json.dump
        src.session_manager.json.dump = original_json_dump


@pytest.mark.asyncio
async def test_concurrent_async_save_state(session_manager, temp_state_file):
    """Test concurrent saves from async tasks."""
    # Add test sessions
    for i in range(3):
        session = Session(
            id=f"test-{i}",
            tmux_session=f"tmux-{i}",
            working_dir="/tmp",
            status=SessionStatus.RUNNING
        )
        session_manager.sessions[session.id] = session

    # Create multiple async tasks that save state
    async def save_state_async():
        for _ in range(5):
            session_manager._save_state()
            await asyncio.sleep(0.001)  # Small delay to encourage interleaving

    # Run multiple tasks concurrently
    tasks = [save_state_async() for _ in range(10)]
    await asyncio.gather(*tasks)

    # Verify the state file is valid
    state_path = Path(temp_state_file)
    with open(state_path, "r") as f:
        data = json.load(f)

    assert "sessions" in data
    assert len(data["sessions"]) == 3
    session_ids = {s["id"] for s in data["sessions"]}
    assert session_ids == {f"test-{i}" for i in range(3)}


def test_state_file_always_complete(session_manager, temp_state_file):
    """Test that readers always see a complete, valid state file."""
    import time

    # Add initial session
    session = Session(
        id="test-1",
        tmux_session="tmux-1",
        working_dir="/tmp",
        status=SessionStatus.RUNNING
    )
    session_manager.sessions[session.id] = session
    session_manager._save_state()

    # Start a writer thread that continuously updates state
    stop_flag = threading.Event()

    def continuous_writer():
        counter = 0
        while not stop_flag.is_set():
            # Add/remove sessions to change state
            session_id = f"test-{counter % 5}"
            if session_id in session_manager.sessions:
                del session_manager.sessions[session_id]
            else:
                session_manager.sessions[session_id] = Session(
                    id=session_id,
                    tmux_session=f"tmux-{counter}",
                    working_dir="/tmp",
                    status=SessionStatus.RUNNING
                )
            session_manager._save_state()
            counter += 1
            time.sleep(0.001)

    writer_thread = threading.Thread(target=continuous_writer)
    writer_thread.start()

    try:
        # Reader thread that continuously reads and validates
        read_count = 0
        invalid_count = 0

        for _ in range(50):
            try:
                with open(temp_state_file, "r") as f:
                    data = json.load(f)  # Should never fail
                assert "sessions" in data
                read_count += 1
            except (json.JSONDecodeError, FileNotFoundError) as e:
                # Should never happen with atomic writes
                invalid_count += 1
            time.sleep(0.001)

        assert read_count > 0, "Should have read the file at least once"
        assert invalid_count == 0, f"Found {invalid_count} invalid reads (should be 0 with atomic writes)"

    finally:
        stop_flag.set()
        writer_thread.join()
