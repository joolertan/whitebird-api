"""Quote an exchange between two wallet assets, and place it with --confirm.

Without --confirm only the quote is printed: placing the order spends real funds.
"""

import argparse
from decimal import Decimal

from account import connect
from whitebird.models import (
    ExchangeDestination,
    ExchangeLeg,
    ExchangeLegType,
    ExchangeQuoteLegRequest,
)


def describe(leg: ExchangeLeg | None) -> str:
    return f"{leg.amount} {leg.asset}" if leg else "?"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("amount", type=Decimal, help="amount to sell, e.g. 10")
    parser.add_argument("sell", help="asset to sell, e.g. USDT")
    parser.add_argument("buy", help="asset to buy, e.g. BTC")
    parser.add_argument("--confirm", action="store_true", help="place the order")
    args = parser.parse_args()

    with connect() as client:
        wallet = ExchangeLegType.INTERNAL_BALANCE
        quote = client.exchange.quotes.create(
            input=ExchangeQuoteLegRequest(asset=args.sell, type=wallet, amount=args.amount),
            output=ExchangeQuoteLegRequest(asset=args.buy, type=wallet),
        )
        print(
            f"{describe(quote.input)} -> {describe(quote.output)} "
            f"at {quote.exchange_rate_value}, valid until {quote.expiration_date}"
        )
        if not args.confirm:
            print("Dry run: pass --confirm to place the order.")
        elif quote.id:
            order = client.exchange.orders.create(
                quote_id=quote.id, destination=ExchangeDestination.ACCOUNTING
            )
            print(f"Order #{order.number} placed: {order.status}")


if __name__ == "__main__":
    main()
