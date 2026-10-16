# Whitebird API

[![PyPI](https://img.shields.io/pypi/v/whitebird-api?label=PyPI)](https://pypi.org/project/whitebird-api/)
[![npm](https://img.shields.io/npm/v/whitebird-api?label=npm)](https://www.npmjs.com/package/whitebird-api)
[![Go Reference](https://pkg.go.dev/badge/github.com/joolertan/whitebird-api/go.svg)](https://pkg.go.dev/github.com/joolertan/whitebird-api/go)
[![crates.io](https://img.shields.io/crates/v/whitebird-api?label=crates.io)](https://crates.io/crates/whitebird-api)
[![Docs](https://img.shields.io/readthedocs/whitebird-api)](https://whitebird-api.readthedocs.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Unofficial clients for the [Whitebird](https://whitebird.io) crypto exchange API in Python,
Node.js, Go and Rust.

| Language | Package | Install |
|---|---|---|
| Python 3.11+ | [`python/`](python) | `pip install "whitebird-api[otp]"` |
| Node.js 22+ | [`node/`](node) | `npm install whitebird-api` |
| Go 1.25+ | [`go/`](go) | `go get github.com/joolertan/whitebird-api/go` |
| Rust 1.85+ | [`rust/`](rust) | `cargo add whitebird-api --features otp` |

```python
from whitebird import Whitebird
from whitebird.otp import TOTP

with Whitebird("me@example.com", "password", otp=TOTP("BASE32SECRET")) as client:
    print(client.market.orderbook(symbol="BTCUSDT", limit=5).bids[0])

    for order in client.exchange.orders.history(size=50):
        print(order.number, order.status)
```

`AsyncWhitebird` has the same methods as coroutines, with `async for` over listings and
notifications.

## What you can do

| Resource | |
|---|---|
| `market` | Spot instruments, order books and candles, without an account |
| `exchange` | Quotes, exchange orders and their history, limits and rates |
| `trading` | Spot orders: place, amend, cancel; open orders, executions, fees, portfolio history |
| `balance`, `accounting` | Balances, fiat and crypto deposits and withdrawals with fee calculation, ledger transactions and receipts |
| `payments`, `providers` | Payment methods and commissions, bank cards (Alfa-Bank, Assist, bePaid, MTS Bank), SBP QR codes, crypto cards |
| `brokerage` | OTC orders, subaccounts, crypto addresses and balance orders of a broker |
| `ico` | Token emissions, purchases and buybacks, investor portfolio, issuer reports |
| `kyc` | Verification questionnaire, liveness check, SumSub and MSI identification, investor qualification |
| `user`, `client`, `registration` | Registration, profile, password, email and phone, turning 2FA on and off |
| `notifications` | Live account events as a stream, notification history |
| `merchant`, `referral`, `reports`, `current_account`, `support` | Merchant API keys and webhooks, referral programme, statements, invoices, support messages |

Every SDK also takes care of:

- **Sign-in:** password with a TOTP code, the slider captcha through your own solver, token
  refresh before expiry and pluggable session storage.
- **Withdrawals:** the operation MFA token is obtained automatically when a TOTP secret is set.
- **Models:** typed responses and decimal amounts without floating point; unknown fields and enum
  values do not break parsing.
- **Pagination:** listings are iterated with the language's own loop: `for` and `async for`,
  `for await`, `range`, `Stream`.
- **Errors and retries:** typed errors with the API's code and invalid fields, retries with
  backoff for idempotent requests.
- **Raw requests:** `client.request(...)` reaches endpoints that have no method yet, with the same
  authentication and retries.

The same in the other languages, guides and the full method list are in the
[documentation](https://whitebird-api.readthedocs.io); runnable programs are in
[`examples/`](examples).

See [CONTRIBUTING.md](CONTRIBUTING.md) to work on the project and [SECURITY.md](SECURITY.md) to
report a vulnerability.

This project is not affiliated with Whitebird. Released under the [MIT License](LICENSE).
