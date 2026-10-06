"""Print the latest exchange orders, fetching pages lazily until enough were printed."""

import argparse
import itertools
import os
from pathlib import Path

from whitebird import FileTokenStore, Whitebird
from whitebird.models import ExchangeLeg
from whitebird.otp import TOTP

SESSION = Path.home() / ".config" / "whitebird" / "session.json"


def describe(leg: ExchangeLeg | None) -> str:
    return f"{leg.amount} {leg.asset}" if leg else "?"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50, help="number of orders to print")
    args = parser.parse_args()

    with Whitebird(
        os.environ["WHITEBIRD_USERNAME"],
        os.environ["WHITEBIRD_PASSWORD"],
        otp=TOTP(os.environ["WHITEBIRD_TOTP_SECRET"]),
        token_store=FileTokenStore(SESSION),
    ) as client:
        orders = client.exchange.orders.history(size=20, sort=["creationDate,desc"])
        for order in itertools.islice(orders, args.count):
            print(
                f"#{order.number} {order.creation_date} {order.status}: "
                f"{describe(order.input)} -> {describe(order.output)}"
            )


if __name__ == "__main__":
    main()
