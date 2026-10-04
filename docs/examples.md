# Examples

Complete programs live in the repository under
[`examples/`](https://github.com/joolertan/whitebird-api/tree/main/examples), one directory per
language, each with a README explaining how to run it. They read credentials from
`WHITEBIRD_USERNAME`, `WHITEBIRD_PASSWORD` and `WHITEBIRD_TOTP_SECRET`.

| Example | What it shows |
|---|---|
| `market_data` | public order book, klines and instruments, no account needed |
| `login_totp` | sign-in with a TOTP secret and a session kept in a file |
| `balances` | account balances |
| `order_history` | iterating over a paginated listing |
| `exchange` | a quote and an exchange order, placed only with `--confirm` |
| `notifications` | the real-time notification stream |
| `export_report` | downloading a report to a file |
