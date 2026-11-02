"""Print the latest exchange orders, fetching pages lazily until enough were printed."""

import argparse
import itertools

from account import connect
from whitebird.models import ExchangeLeg


def describe(leg: ExchangeLeg | None) -> str:
    return f"{leg.amount} {leg.asset}" if leg else "?"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50, help="number of orders to print")
    args = parser.parse_args()

    with connect() as client:
        # Newest first: the default sort is "creationDate,desc".
        orders = client.exchange.orders.history(size=20)
        for order in itertools.islice(orders, args.count):
            print(
                f"#{order.number} {order.creation_date} {order.status}: "
                f"{describe(order.input)} -> {describe(order.output)}"
            )


if __name__ == "__main__":
    main()
