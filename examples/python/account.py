"""Client of the examples that sign in, built from environment variables."""

import os
from pathlib import Path

from whitebird import CodeProvider, FileTokenStore, Whitebird

SESSION = Path.home() / ".config" / "whitebird" / "session.json"


def two_factor_codes() -> CodeProvider:
    """Asks for each two-factor code, or computes it from WHITEBIRD_TOTP_SECRET when that is set."""
    secret = os.environ.get("WHITEBIRD_TOTP_SECRET")
    if not secret:
        return lambda: input("Two-factor code: ").strip()
    from whitebird.otp import TOTP  # noqa: PLC0415 - the otp extra is only needed here

    return TOTP(secret)


def connect() -> Whitebird:
    """Client of WHITEBIRD_USERNAME and WHITEBIRD_PASSWORD; the session is kept in SESSION."""
    return Whitebird(
        os.environ["WHITEBIRD_USERNAME"],
        os.environ["WHITEBIRD_PASSWORD"],
        otp=two_factor_codes(),
        token_store=FileTokenStore(SESSION),
    )
