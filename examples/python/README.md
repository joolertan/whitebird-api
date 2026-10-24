# Python examples

Short scripts using the `whitebird-api` package:

```sh
pip install whitebird-api
```

Every example except `market_data.py` signs in with these environment variables (see
[`account.py`](account.py)) and asks for the two-factor code in the terminal:

```sh
export WHITEBIRD_USERNAME=me@example.com
export WHITEBIRD_PASSWORD=...
```

To run them unattended, install `"whitebird-api[otp]"` and set `WHITEBIRD_TOTP_SECRET` to the
base32 secret shown when two-factor authentication was enabled: codes are then computed from it.

The session is kept in `~/.config/whitebird/session.json` and reused until it expires, so only
the first run logs in.

| Script | What it does |
| --- | --- |
| `market_data.py [SYMBOL] [--demo]` | Instruments, order book and hourly candles; public, no account needed |
| `login.py` | Signs in, saves the session and prints the account |
| `balances.py` | Wallet balances with their USD value |
| `order_history.py [--count N]` | The latest N exchange orders, fetched page by page |
| `exchange.py AMOUNT SELL BUY [--confirm]` | Quotes an exchange, e.g. `10 USDT BTC`; places the order only with `--confirm` |
| `notifications.py` | Prints account updates as they arrive, until Ctrl+C |
| `export_report.py [--days N] [--output FILE]` | Downloads the PDF statement of exchange operations |

```sh
python market_data.py ETHUSDT --demo
python exchange.py 10 USDT BTC
```

From a checkout of the repository, run them against the local sources with
`uv run --project ../../python python market_data.py`.
