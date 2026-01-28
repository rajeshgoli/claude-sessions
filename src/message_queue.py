"""Message queue manager for sequential delivery mode."""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


class MessageQueueManager:
    """Manages queued messages and delivers them when sessions become idle."""

    def __init__(self, session_manager):
        """
        Initialize message queue manager.

        Args:
            session_manager: SessionManager instance
        """
        self.session_manager = session_manager
        self.message_queue: Dict[str, List[dict]] = {}  # session_id -> list of queued messages
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self.idle_threshold = 30  # seconds

    async def start(self):
        """Start the queue monitoring service."""
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_queues())
        logger.info("Message queue manager started")

    async def stop(self):
        """Stop the queue monitoring service."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Message queue manager stopped")

    def queue_message(self, session_id: str, text: str) -> bool:
        """
        Queue a message for sequential delivery.

        Args:
            session_id: Target session ID
            text: Formatted message text

        Returns:
            True if queued successfully
        """
        if session_id not in self.message_queue:
            self.message_queue[session_id] = []

        self.message_queue[session_id].append({
            "text": text,
            "queued_at": datetime.now(),
        })

        logger.info(f"Queued message for {session_id} (queue length: {len(self.message_queue[session_id])})")
        return True

    def is_session_idle(self, session_id: str) -> bool:
        """
        Check if a session is idle.

        Args:
            session_id: Session ID to check

        Returns:
            True if session is idle
        """
        session = self.session_manager.get_session(session_id)
        if not session:
            return False

        time_since_activity = (datetime.now() - session.last_activity).total_seconds()
        return time_since_activity >= self.idle_threshold

    async def _monitor_queues(self):
        """Monitor queues and deliver messages when sessions become idle."""
        try:
            while self._running:
                # Check each session with queued messages
                sessions_to_check = list(self.message_queue.keys())

                for session_id in sessions_to_check:
                    if not self.message_queue.get(session_id):
                        # Queue is empty, clean up
                        self.message_queue.pop(session_id, None)
                        continue

                    # Check if session is idle
                    if self.is_session_idle(session_id):
                        # Send the first queued message
                        await self._deliver_next_message(session_id)

                # Poll every 5 seconds
                await asyncio.sleep(5)

        except asyncio.CancelledError:
            logger.info("Queue monitoring cancelled")
        except Exception as e:
            logger.error(f"Error in queue monitoring: {e}")

    async def _deliver_next_message(self, session_id: str):
        """
        Deliver the next queued message to a session.

        Args:
            session_id: Session ID
        """
        queue = self.message_queue.get(session_id)
        if not queue:
            return

        session = self.session_manager.get_session(session_id)
        if not session:
            logger.warning(f"Session {session_id} not found, clearing queue")
            self.message_queue.pop(session_id, None)
            return

        # Get the first message
        message = queue.pop(0)

        # Send it
        success = self.session_manager.tmux.send_input(session.tmux_session, message["text"])

        if success:
            session.last_activity = datetime.now()
            from .models import SessionStatus
            session.status = SessionStatus.RUNNING
            self.session_manager._save_state()

            queued_duration = (datetime.now() - message["queued_at"]).total_seconds()
            logger.info(f"Delivered queued message to {session_id} (queued for {queued_duration:.1f}s)")
        else:
            logger.error(f"Failed to deliver queued message to {session_id}")
            # Put it back at the front of the queue
            queue.insert(0, message)

        # Clean up empty queue
        if not queue:
            self.message_queue.pop(session_id, None)

    def get_queue_length(self, session_id: str) -> int:
        """
        Get the number of queued messages for a session.

        Args:
            session_id: Session ID

        Returns:
            Number of queued messages
        """
        return len(self.message_queue.get(session_id, []))
