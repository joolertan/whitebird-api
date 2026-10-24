# Rust examples

Small programs using the [`whitebird-api`](../../rust) crate from this repository.

| Example | What it does |
| --- | --- |
| `market_data` | Public instruments, order book and candles; no account needed |
| `login` | Signs in and keeps the session in `whitebird-session.json` |
| `balances` | Prints the wallet balances |
| `order_history` | Walks the exchange order history, stopping after N orders |
| `exchange` | Quotes a wallet conversion; places the order only with `--confirm` |
| `notifications` | Prints account notifications until Ctrl+C |
| `export_report` | Saves the PDF statement of exchange operations |

The examples that need an account read `WHITEBIRD_USERNAME` and `WHITEBIRD_PASSWORD`, ask for
the two-factor code in the terminal and share the session saved in `whitebird-session.json` (see
[`src/lib.rs`](src/lib.rs)). With `WHITEBIRD_TOTP_SECRET` set to the base32 secret of the
authenticator app, the codes are computed instead.

```sh
cargo run --bin market_data -- ETHUSDT --demo

export WHITEBIRD_USERNAME=me@example.com WHITEBIRD_PASSWORD=...
cargo run --bin login
cargo run --bin balances
cargo run --bin order_history -- 50
cargo run --bin exchange -- 10 USDT BTC            # dry run
cargo run --bin exchange -- 10 USDT BTC --confirm  # places the order
cargo run --bin notifications
cargo run --bin export_report -- statement.pdf
```
