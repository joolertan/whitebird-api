"""Time-based one-time passwords (RFC 6238) for accounts with two-factor authentication.

Needs the `otp` extra: `pip install "whitebird-api[otp]"`.
"""

from __future__ import annotations

from datetime import datetime

try:
    import pyotp
except ImportError as error:
    raise ImportError(
        'whitebird.otp needs pyotp, install it with: pip install "whitebird-api[otp]"'
    ) from error

__all__ = ["TOTP"]


class TOTP:
    """Generates the codes of an authenticator app from its base32 secret.

    Pass an instance as `otp=` to the client: it is called for a fresh code whenever Whitebird
    asks for one, at login or to confirm an operation.

    Args:
        secret: Base32 secret shown when two-factor authentication was enabled (spaces and
            letter case are ignored).
        digits: Code length.
        period: Validity of a code in seconds.

    Examples:
        ```python
        client = Whitebird("me@example.com", "password", otp=TOTP("JBSWY3DPEHPK3PXP"))
        ```
    """

    def __init__(self, secret: str, digits: int = 6, period: int = 30) -> None:
        self._totp = pyotp.TOTP(secret.replace(" ", "").upper(), digits=digits, interval=period)

    def __call__(self) -> str:
        """Returns the code valid now."""
        return self._totp.now()

    def at(self, when: datetime | float) -> str:
        """Returns the code valid at a given time.

        Args:
            when: A datetime or a Unix timestamp.
        """
        return self._totp.at(when if isinstance(when, datetime) else int(when))

    def __repr__(self) -> str:
        return f"TOTP(digits={self._totp.digits}, period={self._totp.interval})"
