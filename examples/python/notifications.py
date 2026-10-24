"""Print account updates (orders, deposits, withdrawals, KYC) as they happen, until Ctrl+C."""

import contextlib

from account import connect
from whitebird.models import NotificationStreamMessageType


def main() -> None:
    with connect() as client, client.notifications.stream() as events:
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
