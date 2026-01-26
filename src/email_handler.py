"""Wrapper around existing email harness for sending/receiving emails."""

import sys
import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Path to existing email automation
EMAIL_HARNESS_PATH = Path(__file__).parent.parent.parent.parent / "claude-email-automation"


class EmailHandler:
    """Handles email notifications using existing email harness."""

    def __init__(
        self,
        email_config: str = "",
        imap_config: str = "",
    ):
        # Use provided paths or default to existing harness location
        self.email_config = Path(email_config) if email_config else EMAIL_HARNESS_PATH / "email.yaml"
        self.imap_config = Path(imap_config) if imap_config else EMAIL_HARNESS_PATH / "imap.yaml"

        # Add harness path to sys.path for imports
        harness_str = str(EMAIL_HARNESS_PATH)
        if harness_str not in sys.path:
            sys.path.insert(0, harness_str)

        self._send_module = None
        self._wait_module = None

    def _load_modules(self):
        """Lazy load the email modules."""
        if self._send_module is None:
            try:
                import send_completion_email as send_module
                import wait_for_response as wait_module
                self._send_module = send_module
                self._wait_module = wait_module
                logger.info("Loaded email harness modules")
            except ImportError as e:
                logger.error(f"Failed to import email harness: {e}")
                logger.error(f"Expected harness at: {EMAIL_HARNESS_PATH}")
                raise

    def is_available(self) -> bool:
        """Check if email harness is available and configured."""
        if not EMAIL_HARNESS_PATH.exists():
            logger.warning(f"Email harness not found at {EMAIL_HARNESS_PATH}")
            return False

        if not self.email_config.exists():
            logger.warning(f"Email config not found at {self.email_config}")
            return False

        return True

    async def send_notification(
        self,
        session_id: str,
        message: str,
        urgent: bool = False,
    ) -> bool:
        """
        Send email notification for a session.

        Args:
            session_id: Session ID for tracking
            message: Message body
            urgent: If True, also send to SMS gateways

        Returns:
            True if sent successfully
        """
        if not self.is_available():
            logger.warning("Email not available, skipping notification")
            return False

        try:
            self._load_modules()

            # Load config
            config = self._send_module.load_email_config(str(self.email_config))

            # Send email
            success = self._send_module.send_completion_email(
                session_id=session_id,
                body_content=message,
                config=config,
                urgent=urgent,
            )

            if success:
                logger.info(f"Email sent for session {session_id}")
            else:
                logger.error(f"Failed to send email for session {session_id}")

            return success

        except Exception as e:
            logger.error(f"Email send error: {e}")
            return False

    async def wait_for_response(
        self,
        session_id: str,
        timeout: int = 3600,
    ) -> Optional[str]:
        """
        Wait for email response with session ID.

        Args:
            session_id: Session ID to wait for
            timeout: Maximum wait time in seconds

        Returns:
            Email body if received, None if timeout
        """
        if not self.is_available():
            logger.warning("Email not available")
            return None

        if not self.imap_config.exists():
            logger.warning(f"IMAP config not found at {self.imap_config}")
            return None

        try:
            self._load_modules()

            # Load IMAP config
            config = self._wait_module.load_imap_config(str(self.imap_config))

            # Run blocking wait in thread pool
            loop = asyncio.get_event_loop()
            body = await loop.run_in_executor(
                None,
                self._wait_module.wait_for_response,
                session_id,
                config,
                timeout,
            )

            if body:
                logger.info(f"Received email response for session {session_id}")

            return body

        except Exception as e:
            logger.error(f"Email wait error: {e}")
            return None


async def test_email_handler():
    """Test the email handler."""
    handler = EmailHandler()

    print(f"Email available: {handler.is_available()}")

    if handler.is_available():
        # Send a test notification
        success = await handler.send_notification(
            session_id="test123",
            message="This is a test notification from Claude Session Manager",
            urgent=False,
        )
        print(f"Send result: {success}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(test_email_handler())
