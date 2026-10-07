# Go examples

Each directory is a small program using the Go SDK in [`../../go`](../../go). The examples that
need an account read the credentials from the environment:

```sh
export WHITEBIRD_USERNAME=trader@example.com
export WHITEBIRD_PASSWORD=...
export WHITEBIRD_TOTP_SECRET=...   # base32 2FA secret, for accounts with two-factor authentication
```

Run them from this directory:

| Command | What it does |
| --- | --- |
| `go run ./market_data [-demo] [-symbol BTCUSDT]` | Instruments, order book and candles; no account needed |
| `go run ./login_totp` | Signs in with a TOTP code, saves the session to `whitebird-session.json`, prints the profile |
| `go run ./balances` | Prints the wallet balances |
| `go run ./order_history [-n 30]` | Iterates the exchange order history, stopping after n orders |
| `go run ./exchange [-from USDT -to BTC -amount 10] [-confirm]` | Quotes a conversion; places the order only with `-confirm` |
| `go run ./notifications` | Prints live notifications until Ctrl+C |
| `go run ./export_report [-days 30] [-o exchange-report.pdf]` | Downloads the PDF statement of exchange orders |
