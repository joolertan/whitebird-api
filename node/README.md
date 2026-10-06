# whitebird-api

Unofficial TypeScript client for the [Whitebird](https://whitebird.io) crypto exchange API, for
Node.js 22+ and browsers. No runtime dependencies; ESM and CommonJS.

```sh
npm install whitebird-api
```

```ts
import { Whitebird } from 'whitebird-api';
import { totp } from 'whitebird-api/otp';

const client = new Whitebird({
  username: process.env.WHITEBIRD_USERNAME,
  password: process.env.WHITEBIRD_PASSWORD,
  otp: totp(process.env.WHITEBIRD_TOTP_SECRET!), // accounts with two-factor authentication
});

const book = await client.market.orderbook({ symbol: 'BTCUSDT', limit: 5 });
const wallet = await client.accounting.accounts.getEnhanced({ type: 'USER_BALANCE' });

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

The client logs in on the first authenticated call, refreshes the session before it expires and
keeps it in a `tokenStore` (in memory by default; implement `TokenStore` to persist it).

- **Two-factor login:** `otp` takes a code, a function returning one, or `totp(secret)`.
- **Captcha:** accounts without two-factor authentication get a slider captcha when logging in.
  Pass a `captcha` solver that returns the slider position; without one such a login fails with
  `CaptchaRequiredError`.
- **Operation MFA:** withdrawals need an operation MFA token. With `otp` configured the client
  gets one when needed; otherwise call `client.confirmOperation('WITHDRAWAL', code)` first.

## Errors

API errors are `APIError` subclasses (`BadRequestError`, `AuthenticationError`,
`MFARequiredError`, `CaptchaRequiredError`, `NotFoundError`, `RateLimitError`, ...) carrying
`status`, `code`, `errorCode`, `subStatus`, `fields` and the raw `body`. Network failures and
timeouts are `TransportError`. Idempotent requests are retried on transient failures.

Endpoints the SDK does not cover yet can be called with `client.request(service, method, path)`.
More in [the examples](https://github.com/joolertan/whitebird-api/tree/main/examples/node).

This project is not affiliated with Whitebird.
