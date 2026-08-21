"""Fake-key authentication for local/dev use with model_proxy_v3.

Loads a static list of allowed keys from a JSON file and validates
incoming Authorization / x-api-key / x-goog-api-key headers against it.
This is a passthrough auth service intended for testing the
[remote.authentication] protocol end-to-end — it does not perform any
real credential verification (e.g. against a database or external IdP).
"""

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_KEYS_FILE = os.environ.get("AUTH_KEYS_FILE", "auth_keys.json")


class FakeKeyAuth:
    """Loads and validates against a static set of fake API keys."""

    def __init__(self, keys_file: str = DEFAULT_KEYS_FILE):
        self.keys_file = keys_file
        self._keys: set = set()
        self._mtime: Optional[float] = None
        self.reload()

    def reload(self) -> None:
        """(Re)load the keys file from disk. Safe to call repeatedly."""
        path = Path(self.keys_file)
        if not path.exists():
            logger.warning(
                f"Auth keys file not found: {self.keys_file}. "
                f"All requests will be rejected until it exists."
            )
            self._keys = set()
            self._mtime = None
            return

        mtime = path.stat().st_mtime
        if self._mtime is not None and mtime == self._mtime:
            return  # unchanged, skip re-parse

        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        self._keys = {key for key in data.get("keys", []) if key}
        self._mtime = mtime
        logger.info(f"Loaded {len(self._keys)} auth key(s) from {self.keys_file}")

    def _maybe_reload(self) -> None:
        """Reload if the underlying file changed since last load."""
        path = Path(self.keys_file)
        if not path.exists():
            return
        mtime = path.stat().st_mtime
        if mtime != self._mtime:
            self.reload()

    def validate(self, raw_key: Optional[str]) -> Optional[str]:
        """
        Validate a raw auth key (already stripped of any 'Bearer ' prefix).

        Returns the matched key if known, else None.
        """
        if not raw_key:
            return None

        self._maybe_reload()

        return raw_key if raw_key in self._keys else None

    @staticmethod
    def extract_key(
        authorization: Optional[str],
        x_api_key: Optional[str],
        x_goog_api_key: Optional[str],
    ) -> Optional[str]:
        """
        Extract the raw key from whichever auth header the client sent,
        matching model_proxy_v3's forwarding order: Authorization, then
        x-api-key, then x-goog-api-key.
        """
        if authorization:
            if authorization.lower().startswith("bearer "):
                return authorization[7:].strip()
            return authorization.strip()
        if x_api_key:
            return x_api_key.strip()
        if x_goog_api_key:
            return x_goog_api_key.strip()
        return None

    @staticmethod
    def generate_otac() -> str:
        """Generate a one-time auth code (OTAC) to link auth -> stats record."""
        return f"otac-{uuid.uuid4()}"


# Global instance, lazily configured via AUTH_KEYS_FILE env var or default path.
fake_auth = FakeKeyAuth()
