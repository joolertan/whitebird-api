# Examples

Complete programs live in the repository under
[`examples/`](https://github.com/joolertan/whitebird-api/tree/main/examples), one directory per
language, each with a README explaining how to run it. They read credentials from
`WHITEBIRD_USERNAME` and `WHITEBIRD_PASSWORD` and ask for the two-factor code in the terminal;
with `WHITEBIRD_TOTP_SECRET` set, the codes are computed from the authenticator secret instead.

| Example | What it shows |
|---|---|
| `market_data` | public order book, klines and instruments, no account needed |
| `login` | sign-in with a two-factor code and a session kept in a file |
| `balances` | account balances |
| `order_history` | iterating over a paginated listing |
| `exchange` | a quote and an exchange order, placed only with `--confirm` |
| `notifications` | the real-time notification stream |
| `export_report` | downloading a report to a file |
