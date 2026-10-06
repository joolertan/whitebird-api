"""Print instruments, an order book and recent candles. Public data: no account needed."""

import argparse
import time
from datetime import UTC, datetime

from whitebird import Whitebird


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--demo", action="store_true", help="use the demo trading contour")
    args = parser.parse_args()

    with Whitebird(environment="demo" if args.demo else "production") as client:
        instruments = {instrument.symbol: instrument for instrument in client.market.instruments()}
        instrument = instruments[args.symbol]
        print(
            f"{len(instruments)} instruments; {instrument.symbol} trades in steps of "
            f"{instrument.tick_size} {instrument.quote_coin}, "
            f"from {instrument.min_order_qty} {instrument.base_coin}"
        )

        book = client.market.orderbook(symbol=args.symbol, limit=5)
        print(f"\n{'bid':>11} {'size':>9} | {'ask':>10} {'size':>9}")
        for (bid, bid_size), (ask, ask_size) in zip(book.bids, book.asks, strict=False):
            print(f"{bid:>11} {bid_size:>9} | {ask:>10} {ask_size:>9}")

        day_ago = int((time.time() - 24 * 3600) * 1000)
        candles = client.market.klines(symbol=args.symbol, interval="1h", start=day_ago)
        print(f"\n{'UTC':5} {'open':>11} {'high':>9} {'low':>9} {'close':>9}")
        for candle in candles[-6:]:
            hour = datetime.fromtimestamp(candle.start / 1000, UTC)
            print(
                f"{hour:%H:%M} {candle.open:>11} {candle.high:>9} {candle.low:>9} {candle.close:>9}"
            )


if __name__ == "__main__":
    main()
