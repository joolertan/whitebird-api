"""Sign in, keep the session in a file and print the account; later runs skip the login.

Needs WHITEBIRD_USERNAME and WHITEBIRD_PASSWORD. The two-factor code is asked for in the terminal,
or computed from WHITEBIRD_TOTP_SECRET when that is set (with the otp extra installed).
"""

from account import SESSION, connect


def main() -> None:
    with connect() as client:
        # The first call logs in, unless the stored session is still valid.
        profile = client.user.me()
        print(f"Signed in as {profile.email} (two-factor: {profile.mfa_enabled})")

        account = client.client.get()
        print(f"Client {account.identity}: {account.client_type}, status {account.status}")
        print(f"Session saved to {SESSION}")


if __name__ == "__main__":
    main()
