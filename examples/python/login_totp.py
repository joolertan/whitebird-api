"""Sign in with a TOTP secret and keep the session in a file, so later runs skip the login.

Needs WHITEBIRD_USERNAME, WHITEBIRD_PASSWORD and WHITEBIRD_TOTP_SECRET (the base32 secret shown
when two-factor authentication was enabled) and the otp extra: pip install "whitebird-api[otp]".
"""

import os
from pathlib import Path

from whitebird import FileTokenStore, Whitebird
from whitebird.otp import TOTP

SESSION = Path.home() / ".config" / "whitebird" / "session.json"


def main() -> None:
    with Whitebird(
        os.environ["WHITEBIRD_USERNAME"],
        os.environ["WHITEBIRD_PASSWORD"],
        otp=TOTP(os.environ["WHITEBIRD_TOTP_SECRET"]),
        token_store=FileTokenStore(SESSION),
    ) as client:
        # The first call logs in, unless the stored session is still valid.
        profile = client.user.me()
        print(f"Signed in as {profile.email} (two-factor: {profile.mfa_enabled})")

        account = client.client.get()
        print(f"Client {account.identity}: {account.client_type}, status {account.status}")
        print(f"Session saved to {SESSION}")


if __name__ == "__main__":
    main()
