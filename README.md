# Whitebird API

[![PyPI](https://img.shields.io/pypi/v/whitebird-api?label=PyPI)](https://pypi.org/project/whitebird-api/)
[![npm](https://img.shields.io/npm/v/whitebird-api?label=npm)](https://www.npmjs.com/package/whitebird-api)
[![Go Reference](https://pkg.go.dev/badge/github.com/joolertan/whitebird-api/go.svg)](https://pkg.go.dev/github.com/joolertan/whitebird-api/go)
[![crates.io](https://img.shields.io/crates/v/whitebird-api?label=crates.io)](https://crates.io/crates/whitebird-api)
[![Docs](https://img.shields.io/readthedocs/whitebird-api)](https://whitebird-api.readthedocs.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Unofficial clients for the [Whitebird](https://whitebird.io) crypto exchange API in Python,
Node.js, Go and Rust.

Whitebird publishes no API reference, so the API is reconstructed from its web application into
an OpenAPI description, the four SDKs are generated from it, and a daily job compares it with the
live application to catch new and changed endpoints.

- Every endpoint the web application uses: brokerage, exchange, balances, spot trading, market
  data, KYC, notifications, ICO tokens and payment providers.
- Sign-in with password and TOTP, automatic token refresh, captcha solver hook and the MFA flow
  for withdrawals.
- Typed models, lossless decimal amounts, natively iterable pagination, Server-Sent Events as
  native streams, retries with backoff.

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

The same in the other languages, guides and the full endpoint list are in the
[documentation](https://whitebird-api.readthedocs.io); runnable programs are in
[`examples/`](examples).

## Repository

| Path | |
|---|---|
| [`spec/`](spec) | OpenAPI description of every Whitebird service, the source of truth |
| [`tools/wbscan/`](tools/wbscan) | extracts endpoints from the whitebird.io bundles and reports drift |
| [`tools/codegen/`](tools/codegen) | generates models and resources for the four SDKs |
| [`docs/`](docs) | documentation site ([architecture](docs/development/architecture.md)) |

See [CONTRIBUTING.md](CONTRIBUTING.md) to work on the project and [SECURITY.md](SECURITY.md) to
report a vulnerability.

This project is not affiliated with Whitebird. Released under the [MIT License](LICENSE).
