# Node.js examples

Small scripts using the Node.js SDK in [`../../node`](../../node). Node.js 22.18 or later runs
them as they are, TypeScript included. Build the SDK first, then install the examples:

```sh
(cd ../../node && npm ci && npm run build)
npm install
```

The examples that need an account read the credentials from the environment, ask for the
two-factor code in the terminal and keep the session in `.session.json` (see
[`client.ts`](client.ts)):

```sh
export WHITEBIRD_USERNAME=trader@example.com
export WHITEBIRD_PASSWORD=...
export WHITEBIRD_TOTP_SECRET=...   # optional: base32 2FA secret, computes the codes instead of asking
```

| Command | What it does |
| --- | --- |
| `node market-data.ts [demo]` | Instruments, order book and candles; no account needed |
| `node login.ts` | Signs in, saves the session, prints the profile |
| `node balances.ts` | Prints the wallet balances |
| `node order-history.ts [count]` | Iterates the exchange order history, stopping after `count` orders |
| `node exchange.ts [--confirm]` | Quotes 10 USDT to BTC; places the order only with `--confirm` |
| `node notifications.ts` | Prints live notifications until Ctrl+C |
| `node export-report.ts [file]` | Downloads the PDF statement of the last 30 days of exchange orders |

`npm run typecheck` checks them against the built SDK.
