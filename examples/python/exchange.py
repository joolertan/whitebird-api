"""Quote an exchange between two wallet assets, and place it with --confirm.

Without --confirm only the quote is printed: placing the order spends real funds.
"""

import argparse
import os
from decimal import Decimal
from pathlib import Path

from whitebird import FileTokenStore, Whitebird
from whitebird.models import (
    ExchangeDestination,
    ExchangeLeg,
    ExchangeLegType,
    ExchangeQuoteLegRequest,
)
from whitebird.otp import TOTP

SESSION = Path.home() / ".config" / "whitebird" / "session.json"


def describe(leg: ExchangeLeg | None) -> str:
    return f"{leg.amount} {leg.asset}" if leg else "?"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("amount", type=Decimal, help="amount to sell, e.g. 10")
    parser.add_argument("sell", help="asset to sell, e.g. USDT")
    parser.add_argument("buy", help="asset to buy, e.g. BTC")
    parser.add_argument("--confirm", action="store_true", help="place the order")
    args = parser.parse_args()

    with Whitebird(
        os.environ["WHITEBIRD_USERNAME"],
        os.environ["WHITEBIRD_PASSWORD"],
        otp=TOTP(os.environ["WHITEBIRD_TOTP_SECRET"]),
        token_store=FileTokenStore(SESSION),
    ) as client:
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
