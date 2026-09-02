"""OAuth tokens and the stores that keep them between requests and runs."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

__all__ = ["FileTokenStore", "MemoryTokenStore", "TokenStore", "Tokens"]


@dataclass(frozen=True)
class Tokens:
    """An OAuth session.

    Attributes:
        access_token: Bearer token sent with authenticated requests.
        refresh_token: Token used to obtain a new access token without the password.
        expires_at: Unix time at which the access token expires, if known.
    """

    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None

    def expires_within(self, seconds: float) -> bool:
        """Tells whether the access token expires in less than `seconds`.

        Args:
            seconds: Safety margin.

        Returns:
            False when the expiry is unknown.
        """
        return self.expires_at is not None and self.expires_at - time.time() < seconds


class TokenStore(Protocol):
    """Keeps the current `Tokens`. Implement it to share sessions across processes."""

    def load(self) -> Tokens | None:
        """Returns the stored tokens, or None if there are none."""
        ...

    def save(self, tokens: Tokens) -> None:
        """Replaces the stored tokens."""
        ...

    def clear(self) -> None:
        """Forgets the stored tokens."""
        ...


class MemoryTokenStore:
    """Keeps tokens in memory for the lifetime of the client (the default).

    Args:
        tokens: Initial tokens, e.g. a session obtained elsewhere.
    """

    def __init__(self, tokens: Tokens | None = None) -> None:
        self._tokens = tokens

    def load(self) -> Tokens | None:
        """Returns the stored tokens, or None if there are none."""
        return self._tokens

    def save(self, tokens: Tokens) -> None:
        """Replaces the stored tokens."""
        self._tokens = tokens

    def clear(self) -> None:
        """Forgets the stored tokens."""
        self._tokens = None


class FileTokenStore:
    """Keeps tokens in a JSON file readable only by the current user, so sessions survive restarts.

    Args:
        path: File to use; `~` is expanded and missing directories are created on save.

    Examples:
        ```python
        client = Whitebird(email, password, token_store=FileTokenStore("~/.whitebird/session.json"))
        ```
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> Tokens | None:
        """Returns the stored tokens, or None if the file is missing or unreadable."""
        try:
            data = json.loads(self.path.read_text())
            return Tokens(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                expires_at=data.get("expires_at"),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, tokens: Tokens) -> None:
        """Atomically replaces the file with `tokens`, with 0600 permissions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            json.dump(asdict(tokens), file)
        os.replace(temporary, self.path)

    def clear(self) -> None:
        """Deletes the file."""
        self.path.unlink(missing_ok=True)
