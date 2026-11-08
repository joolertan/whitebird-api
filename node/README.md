# whitebird-api

[![npm](https://img.shields.io/npm/v/whitebird-api?label=npm)](https://www.npmjs.com/package/whitebird-api)
[![Docs](https://img.shields.io/readthedocs/whitebird-api)](https://whitebird-api.readthedocs.io)

Unofficial TypeScript client for the [Whitebird](https://whitebird.io) crypto exchange API, for
Node.js 22+ and browsers. No runtime dependencies; ESM and CommonJS.

Covers market data, exchange and spot trading, deposits and withdrawals, payment cards,
brokerage, ICO tokens, KYC, notifications and account settings.

```sh
npm install whitebird-api
```

```ts
import { Whitebird } from 'whitebird-api';
import { totp } from 'whitebird-api/otp';

const client = new Whitebird({
  username: process.env.WHITEBIRD_USERNAME,
  password: process.env.WHITEBIRD_PASSWORD,
  otp: totp(process.env.WHITEBIRD_TOTP_SECRET ?? ''), // accounts with two-factor authentication
});

const book = await client.market.orderbook({ symbol: 'BTCUSDT', limit: 5 });
const wallet = await client.accounting.accounts.getEnhanced({ type: 'USER_BALANCE' });
// Decimal amounts are strings with every digit the API sent, such as '0.00012345'.
console.log(wallet.balances?.[0]?.amount);

// Paginated methods: await for the first page, or iterate over every item.
for await (const order of client.exchange.orders.history({ size: 50 })) {
  console.log(order.id, order.status);
}

// Server-sent events; the stream reconnects on its own and ends when the signal aborts.
const signal = AbortSignal.timeout(60_000);
for await (const { data } of client.notifications.stream({ signal })) {
  console.log(data.type, data.payload);
}
```

## Authentication

The client logs in on the first authenticated call, refreshes
the session when it has less than a minute left and keeps it in a `tokenStore` (in memory by
default; implement `TokenStore` to persist it). `client.logout()` revokes the session and forgets
it.

- **Two-factor login:** `otp` takes a code, a function returning one (such as a prompt), or
  `totp(secret)`. It is asked for a code only when Whitebird asks for one; `client.login(code)`
  passes a code directly.
- **Captcha:** accounts without two-factor authentication get a slider captcha when logging in.
  Pass a `captcha` solver that returns the slider position; without one such a login fails with
  `CaptchaRequiredError` before the password is sent.
- **Operation MFA:** withdrawals need an operation MFA token. With `otp` configured the client
  gets one when the API asks for it and sends the operation again; otherwise call
  `client.confirmOperation('WITHDRAWAL', code)` first.

## Amounts and dates

Decimal amounts are strings in responses, with exactly the digits the API sent. In requests they
may be numbers or strings; strings are sent as JSON numbers with their exact digits. Timestamps are
ISO 8601 strings; request fields typed `string | Date` also take a `Date`.

## Errors

API errors are `APIError` subclasses (`BadRequestError`, `AuthenticationError`,
`MFARequiredError`, `CaptchaRequiredError`, `NotFoundError`, `RateLimitError`, ...) carrying
`status`, `code`, `errorCode`, `subStatus`, `fields` and the raw `body`. Network failures and
timeouts are `TransportError`; an aborted call rejects with the signal's reason. GET, HEAD and
OPTIONS requests are retried on transient failures; other requests only when they certainly did
not reach the server (the connection failed) or were rate limited (429).

Check errors with `instanceof` against the classes of the same import: when a program loads the
package both with `import` and `require`, each copy has its own classes.

Endpoints the SDK does not cover yet can be called with `client.request(service, method, path)`.
More in [the examples](https://github.com/joolertan/whitebird-api/tree/main/examples/node).

This project is not affiliated with Whitebird.
