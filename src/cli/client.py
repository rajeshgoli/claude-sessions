"""HTTP client for Session Manager API."""

import os
import sys
from typing import Optional
import urllib.request
import urllib.error
import json

# Default API endpoint
DEFAULT_API_URL = "http://127.0.0.1:8420"
API_TIMEOUT = 2  # seconds


class SessionManagerClient:
    """Client for Session Manager API."""

    def __init__(self, api_url: Optional[str] = None):
        """
        Initialize client.

        Args:
            api_url: Base URL for API (default: http://127.0.0.1:8420)
        """
        self.api_url = api_url or os.environ.get("SM_API_URL", DEFAULT_API_URL)
        self.session_id = os.environ.get("CLAUDE_SESSION_MANAGER_ID")

    def _request(self, method: str, path: str, data: Optional[dict] = None) -> tuple[Optional[dict], bool]:
        """
        Make an HTTP request.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            path: API path
            data: Optional JSON data

        Returns:
            Tuple of (response_data, success)
        """
        url = f"{self.api_url}{path}"

        try:
            headers = {"Content-Type": "application/json"}
            body = json.dumps(data).encode() if data else None

            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
                if response.status in (200, 201):
                    return json.loads(response.read().decode()), True
                return None, False

        except urllib.error.URLError as e:
            # Connection refused, timeout, etc.
            return None, False
        except Exception as e:
            return None, False

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get session details."""
        data, success = self._request("GET", f"/sessions/{session_id}")
        return data if success else None

    def list_sessions(self) -> Optional[list]:
        """List all sessions."""
        data, success = self._request("GET", "/sessions")
        if success and data:
            return data.get("sessions", [])
        return None

    def update_friendly_name(self, session_id: str, friendly_name: str) -> bool:
        """Update session friendly name."""
        data, success = self._request(
            "PATCH",
            f"/sessions/{session_id}",
            {"friendly_name": friendly_name}
        )
        return success

    def update_task(self, session_id: str, task: str) -> bool:
        """Update session current task."""
        data, success = self._request(
            "PUT",
            f"/sessions/{session_id}/task",
            {"task": task}
        )
        return success

    def get_summary(self, session_id: str, lines: int = 100) -> Optional[str]:
        """Get AI-generated summary of session activity."""
        data, success = self._request("GET", f"/sessions/{session_id}/summary?lines={lines}")
        if success and data:
            return data.get("summary")
        return None
