"""Print account updates (orders, deposits, withdrawals, KYC) as they happen, until Ctrl+C."""

import contextlib
import os
from pathlib import Path

from whitebird import FileTokenStore, Whitebird
from whitebird.models import NotificationStreamMessageType
from whitebird.otp import TOTP

SESSION = Path.home() / ".config" / "whitebird" / "session.json"


def main() -> None:
    with (
        Whitebird(
            os.environ["WHITEBIRD_USERNAME"],
            os.environ["WHITEBIRD_PASSWORD"],
            otp=TOTP(os.environ["WHITEBIRD_TOTP_SECRET"]),
            token_store=FileTokenStore(SESSION),
        ) as client,
        client.notifications.stream() as events,
    ):
        # The stream reconnects on its own and resumes after the last event received.
        for event in events:
            message = event.data
            if message.type == NotificationStreamMessageType.ORDER_UPDATE:
                update = message.payload or {}
                print(f"Order {update.get('orderId')}: {update.get('type')}")
            else:
                print(f"{message.type}: {message.payload}")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
