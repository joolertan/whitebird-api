# Go examples

Each directory is a small program using the Go SDK in [`../../go`](../../go). The examples that
need an account read the credentials from the environment, ask for the two-factor code in the
terminal and keep the session in `whitebird-session.json` (see
[`internal/envclient`](internal/envclient/envclient.go)):

```sh
export WHITEBIRD_USERNAME=trader@example.com
export WHITEBIRD_PASSWORD=...
export WHITEBIRD_TOTP_SECRET=...   # optional: base32 2FA secret, computes the codes instead of asking
```

When Whitebird asks for a slider captcha at sign-in, the examples write it to `whitebird-captcha.html` in the temporary directory and ask for the slider position
shown there.

Run them from this directory:

| Command | What it does |
| --- | --- |
| `go run ./market_data [-demo] [-symbol BTCUSDT]` | Instruments, order book and candles; no account needed |
| `go run ./login` | Signs in, saves the session, prints the profile |
| `go run ./balances` | Prints the wallet balances |
| `go run ./order_history [-n 30]` | Iterates the exchange order history, stopping after n orders |
| `go run ./exchange [-from USDT -to BTC -amount 10] [-confirm]` | Quotes a conversion; places the order only with `-confirm` |
| `go run ./notifications` | Prints live notifications until Ctrl+C |
| `go run ./export_report [-days 30] [-o exchange-report.pdf]` | Downloads the PDF statement of exchange orders |
