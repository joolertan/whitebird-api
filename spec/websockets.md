# Trading terminal WebSockets

Real-time protocol of the Whitebird spot trading terminal, reconstructed from build
`index-By9aTaK4.js` (`vendor-wb-trading-2YFHl45Z.js`, exchange config `whitebird`, protocol class
`uT`/`lT`, connection class `bT`, subscription manager `yT`, request manager `vT`).

Status labels used below:

- **verified**: seen on the wire (public market socket, production, 2026-11-07, 20 s session
  subscribed to `orderbook.0.1.BTCUSDT`).
- **bundle**: read from the web app code, not observed. Field types follow how the app reads them.

The protocol is a Bybit v5 dialect: JSON text frames, `{"op": ..., "args": [...]}` requests, pushes
identified by `topic`. Symbols are written without separator (`BTCUSDT`); the app turns `BTC/USDT`
into `BTCUSDT` by removing `/`.

## Sockets

| Socket | Production | Demo | Auth | Used for |
|---|---|---|---|---|
| market | `wss://market-data-service.whitebird.io/ws/market` | `wss://market-data-demo-service.whitebird.io/ws/market` | none | tickers, candles, order book, public trades |
| private | `wss://order-management-service.whitebird.io/ws/private?access_token=<JWT>` | `wss://order-management-demo-service.whitebird.io/ws/private?access_token=<JWT>` | bearer token in the query | orders, executions, wallet |
| trade | `wss://order-management-service.whitebird.io/ws/trade?access_token=<JWT>` | `wss://order-management-demo-service.whitebird.io/ws/trade?access_token=<JWT>` | bearer token in the query | place / amend / cancel orders |
| admin | `wss://trading-admin-service.whitebird.io/ws/admin` | `wss://trading-admin-demo-service.whitebird.io/ws/admin` | none | instrument catalog and trading status |

The hosts are the `market_data`, `order_management` and `trading_admin` service URLs of
`spec/environments.yaml` with `http` replaced by `ws` (`https://` -> `wss://`). The SDK should
derive them from the configured service base URL the same way, so per-service overrides apply to
WebSockets too. Production and demo speak the same protocol; demo trades against a separate
virtual balance (topped up with `trading.demo.deposit`).

## Authentication

- The private and trade sockets carry the OAuth access token (the same JWT used as
  `Authorization: Bearer` on REST) in the `access_token` query parameter. The URL is rebuilt on
  every (re)connect, so a reconnect after a token refresh uses the new token. (bundle)
- No auth message is sent: the bearer strategy has no WS auth payload, so the client considers
  itself authenticated as soon as the socket opens and flushes queued messages. (bundle)
- The market and admin sockets are public. (market: verified; admin: bundle)
- The client recognises these server messages on any socket (bundle; not seen on the market
  socket):
  - `{"op": "connect", "conn_id": "<id>"}`: connection id, kept by the app but not sent anywhere.
  - `{"op": "auth" | "session_update", "authorized": <bool>}`: whether the session is authorized.
    Treat `authorized: false` as an authentication failure. A generic Bybit-style auth reply
    (`op: "auth"` with `retCode`/`success`) failing makes the app disconnect.
- Close code `1008` means the server terminated the session: the app does **not** reconnect. The
  SDK should surface it as an authentication error (and may reconnect once after refreshing the
  token).
- Terminal sessions (`/session/init`, `/session/extend`, `/session/terminate`) are **not** needed.
  They belong to the terminal's proxy mode (`baseProxyUrl`), which the Whitebird config does not
  use; with bearer auth the app skips `session/init` entirely. Do not model them.

## Framing

All frames are JSON text.

Client requests:

```json
{"op": "subscribe", "args": ["orderbook.0.1.BTCUSDT", "tickers.ETHUSDT"]}
{"op": "unsubscribe", "args": ["orderbook.0.1.BTCUSDT"]}
{"op": "ping"}
```

Several topics may be subscribed in one message (the app batches tickers this way and merges
queued subscribe/unsubscribe requests).

Server acknowledgements and errors (verified):

```json
{"op": "subscribe", "success": true, "args": ["orderbook.0.1.BTCUSDT"]}
{"op": "ping", "success": false, "args": [], "error": "op is invalid"}
```

Pushes (verified for the order book; the other topics are read the same way by the app):

```json
{"topic": "<topic>", "type": "snapshot" | "delta", "ts": 1794066456337, "data": ...}
```

Route pushes by `topic` (exact string match with the subscribed topic). Fall back to `channel` or
`stream` if `topic` is missing (the app does). `type` and `ts` are only guaranteed on the order book.

## Heartbeat

- The app sends `{"op": "ping"}` 1 s after opening and then every 20 s on every socket except
  admin, and treats `{"op": "pong"}` (or a message with `retMsg`/`message` equal to `"pong"`) as
  the answer, measuring latency and loss. (bundle)
- The market socket rejects it: `{"op": "ping", "success": false, "args": [], "error": "op is
  invalid"}` (verified). So there is no application-level pong there; whether `/ws/private` and
  `/ws/trade` answer `pong` is unknown.
- SDK recommendation: keep the connection alive with WebSocket protocol ping frames (RFC 6455)
  every 20 s and detect dead connections by a missing control-frame pong. If an application ping
  is also sent, accept `{"op":"pong"}` and the `op is invalid` error both as proof of liveness
  and never surface the error to the user.

## Reconnect

Behaviour of the web app (bundle), which the SDK should mirror:

- Reconnect automatically after any close except code 1008, without attempt limit.
- Delay `min(2000 ms * 2^attempt, 30 s)` (attempt = 0 for the first retry), reset to 0 after a
  successful open. The terminal configures 2000 ms as the base delay (the class default is 1000).
- After reconnecting, resubscribe every active topic in one `subscribe` message. Order book
  topics send a fresh snapshot, so drop local book state on disconnect.
- Messages sent while disconnected are queued and flushed on open; a queued `subscribe` and
  `unsubscribe` for the same single topic cancel each other.
- The admin socket uses its own loop: delay `min(1000 ms * 2^attempt, 30 s)`, no heartbeat,
  resubscribe `instruments` on every open.

## Market socket topics

| Topic | Example | Notes |
|---|---|---|
| `tickers.{symbol}` | `tickers.BTCUSDT` | 24 h ticker |
| `kline.{interval}.{symbol}` | `kline.60.BTCUSDT` | interval in Bybit form, see below |
| `orderbook.{step}.{symbol}` | `orderbook.0.1.BTCUSDT` | step = price grouping (verified) |
| `publicTrade.{symbol}` | `publicTrade.BTCUSDT` | public trades |

### Order book: `orderbook.{step}.{symbol}` (verified)

`step` is the price aggregation step: the instrument `tickSize` times a multiplier (the terminal
offers 1, 10 and 100 by default), printed with the tick size's number of decimals and trailing
zeros removed. For `BTCUSDT` (tick 0.1): `orderbook.0.1.BTCUSDT`, `orderbook.1.BTCUSDT`,
`orderbook.10.BTCUSDT`. Only the first was tested.

First push after subscribing is a snapshot (50 levels per side at step 0.1), then deltas about
every 100 ms:

```json
{"ts": 1794066456337, "type": "snapshot", "topic": "orderbook.0.1.BTCUSDT",
 "data": {"s": "BTCUSDT", "b": [["75694.8", "3.234521"], ...], "a": [["75694.9", "3.441991"], ...], "u": 41000509127}}
{"ts": 1794066456497, "type": "delta", "topic": "orderbook.0.1.BTCUSDT",
 "data": {"s": "BTCUSDT", "b": [["75683.7", "0"], ["75666.6", "0.001052"]], "a": [], "u": 41000509155}}
```

| Field | Type | Meaning |
|---|---|---|
| `ts` | integer | event time, epoch ms |
| `type` | string | `snapshot` or `delta` |
| `topic` | string | subscribed topic |
| `data.s` | string | symbol |
| `data.b` | array of `[price: string, size: string]` | bids, best first in snapshots |
| `data.a` | array of `[price: string, size: string]` | asks, best first in snapshots |
| `data.u` | integer | update id; increases but is not contiguous (gaps of ~30 between deltas) |

Applying deltas: for each level, size `"0"` removes the price, any other size replaces it; keep
bids sorted descending and asks ascending. Deltas may have empty `b` or `a`. Because `u` is not
contiguous, gaps cannot be detected; the app does not try. Resynchronise by resubscribing (a new
snapshot follows). `u` uses the same sequence as `id` of the REST snapshot
`GET /api/v1/market/orderbook`.

### Ticker: `tickers.{symbol}` (bundle)

`data` is an object:

| Field | Type | Meaning |
|---|---|---|
| `symbol` | string | `BTCUSDT` |
| `lastPrice` | decimal string | last price |
| `highPrice24h` | decimal string | 24 h high |
| `lowPrice24h` | decimal string | 24 h low |
| `volume24h` | decimal string | 24 h volume, base coin |
| `turnover24h` | decimal string | 24 h volume, quote coin |
| `price24hPcnt` | decimal string | 24 h change as a fraction (`0.0123` = +1.23 %) |

The app parses every value with `parseFloat`, so numbers would work as well. Bybit tickers usually
push the full object each time; the app merges partial updates field by field, so treat missing
fields as unchanged.

### Candles: `kline.{interval}.{symbol}` (bundle)

Interval mapping from the terminal timeframes (the REST `interval` values) to topic intervals:

| REST | `1m` | `5m` | `15m` | `30m` | `1h` | `4h` | `6h` | `12h` | `1D` | `1W` | `1M` |
|---|---|---|---|---|---|---|---|---|---|---|---|
| topic | `1` | `5` | `15` | `30` | `60` | `240` | `360` | `720` | `D` | `W` | `M` |

`data` is an array of candles (a single object is tolerated); the app uses the first element.
Candle fields are those of the REST `MarketKline` (verified on REST):

| Field | Type | Meaning |
|---|---|---|
| `start` | integer | open time, epoch ms |
| `end` | integer | close time, epoch ms |
| `interval` | string | Bybit form (`60`, `D`, ...) |
| `open`, `high`, `low`, `close` | decimal string | prices |
| `volume` | decimal string | base coin volume |
| `turnover` | decimal string | quote coin volume |
| `confirm` | boolean | `true` once the candle is closed |
| `timestamp` | integer | update time, epoch ms |

Following Bybit, the open candle is expected to be pushed repeatedly with `confirm: false` (not
verified); the app ignores pushes older than the last candle it holds.

### Public trades: `publicTrade.{symbol}` (bundle)

`data` is an array of trades (order within a push unspecified):

| Field | Type | Meaning |
|---|---|---|
| `tradeId` | string | trade id |
| `symbol` | string | `BTCUSDT` |
| `side` | string | taker side, `Buy` or `Sell` |
| `price` | decimal string | price |
| `size` | decimal string | quantity, base coin |
| `timestamp` | integer | epoch ms |

Note: these are Whitebird's own field names; they differ from Bybit's short keys (`T`, `S`, `v`,
`p`, `i`).

## Private socket topics

Subscribe with `{"op": "subscribe", "args": ["order", "execution", "wallet"]}` on `/ws/private`.
Pushes carry `topic` equal to the topic name and the payload in `data`. All three are bundle-only.

### `order`

`data` is an array of orders in the shape of the REST `TradingOrder`
(`order-management.yaml`, same as `GET /api/v1/orders/active`):

| Field | Type | Meaning |
|---|---|---|
| `id` | string | order id |
| `orderLinkId` | string | client order id |
| `symbol` | string | `BTCUSDT` |
| `category` | string | `spot` |
| `side` | string | `Buy` / `Sell` |
| `type` | string | `Limit` / `Market` |
| `status` | string | `CREATED`, `PLACED`, `PROCESSING`, `CANCELLED`, `REJECTED`, `CLOSED`, `TRIGGERED`, `UNTRIGGERED`, `UNKNOWN` |
| `price`, `qty` | decimal string | limit price, quantity |
| `leavesQty`, `leavesValue` | decimal string | remaining quantity / value |
| `cumExecQty`, `cumExecValue` | decimal string | filled quantity / value |
| `timeInForce` | string | `GTC`, `PostOnly`, `IOC`, `FOK` |
| `takeProfit`, `stopLoss` | decimal string | attached TP/SL trigger prices (`"0"` = none) |
| `tpLimitPrice`, `slLimitPrice` | decimal string | TP/SL limit prices |
| `tpOrderType`, `slOrderType` | string | `Limit` / `Market` |
| `stopOrderType` | string | `TakeProfit` / `StopLoss` for TP/SL legs |
| `createdTime`, `updatedTime` | string | epoch ms |
| `translate` | object, optional | ready-made notification: `{"kind": <status>, "en": {"title", "message"}, "ru": {"title", "message"}}` |

Status mapping used by the app: `CREATED`/`PLACED` -> New, `PROCESSING` -> PartiallyFilled,
`CLOSED` -> Filled, `CANCELLED`, `REJECTED`, `TRIGGERED`, `UNTRIGGERED`, `UNKNOWN` as is. An
order leaves the open-orders list when it is cancelled, rejected or filled, or when it is
`TRIGGERED` with no remaining quantity. Each push contains only the changed orders; merge by `id`.

### `execution`

`data` is an array of fills, same shape as `GET /api/v1/executions` (`TradingExecution`): `id`,
`orderId`, `symbol`, `category`, `side`, `type`, `execPrice`, `execQty`, `execValue`, `execTime`
(epoch ms string), fee in `execFee`, `totalFee` or `fee` (the app takes the first present; may be
a number). Deduplicate by `id`.

### `wallet`

`data` is either an array of balance entries or `{"balances": [...]}`. An entry has:

| Field | Type | Meaning |
|---|---|---|
| `currency` (or `coin`) | string | asset |
| `type` | string | `USER_BALANCE` (available) or `USER_SPOT_RESERVING` (locked in orders) |
| `amount` (or `walletBalance`) | decimal (string or number) | amount of that balance type |
| `availableBalance`, `locked` | decimal, optional | when present, used instead of `type` + `amount` |
| `usdValue` | decimal, optional | USD valuation |
| `usdRate` | decimal, optional | USD rate, used when `usdValue` is absent |

The app sums entries per asset: available = `USER_BALANCE` amounts, locked =
`USER_SPOT_RESERVING` amounts, total = available + locked. These are the same account types the
REST balance call uses (`POST https://api.whitebird.io/api/v2/accounting/client/account/enhanced`
with `{"type": "USER_BALANCE" | "USER_SPOT_RESERVING", "assets": [...]}`, one request per type).

## Trade socket

`/ws/trade` executes order commands with request/response correlation. Request (bundle):

```json
{"reqId": "req-1794066456337-1", "op": "order.create", "args": [{...}], "header": {}}
```

| `op` | `args` | REST equivalent |
|---|---|---|
| `order.create` | `[TradingOrderCreateRequest]` | `POST /api/v1/orders` |
| `order.amend` | `[TradingOrderAmendRequest]` | `PUT /api/v1/orders` |
| `order.cancel` | `[{"id", "symbol", "category": "spot"}, ...]` | `DELETE /api/v1/orders` |

- `reqId` is any unique string (the app uses `req-<epoch ms>-<counter>`); `header` is an empty
  object with bearer auth.
- `order.cancel` accepts several orders in one request; the app uses it for "cancel all".
- Payloads are exactly the REST bodies of `order-management.yaml` (create: `symbol`, `side`
  `Buy`/`Sell`, `type` `Limit`/`Market`, `qty`, `price`, `timeInForce`, `orderLinkId`,
  `takeProfit`, `stopLoss`, `tpOrderType`, `slOrderType`, `tpLimitPrice`, `slLimitPrice`,
  `category: "spot"`; amend uses `id` for the order id). Amounts are decimal strings. For market
  buys `qty` is the amount to spend in the quote coin; for market sells it is the base quantity.

Response, matched by `reqId` (bundle):

```json
{"reqId": "req-1794066456337-1", "op": "order.create", "retCode": 0, "retMsg": "OK", "data": {"orderId": "..."}}
```

- Success when `success` is `true`, or `retCode` (alias `code`) is `0` or `200`, or there is no
  code and no `error`. Otherwise the error is `retCode` + `retMsg` (alias `message`), with the
  `retCode` values listed in `order-management.yaml` (12 = order not found, 3 = insufficient
  balance, ...).
- The order id is read from `result.orderId`, then `data.orderId`; accept both.
- The app falls back to REST when the trade socket is not connected; the SDK can expose the trade
  socket as an optional low-latency transport for the same three methods.

## Admin socket: instrument catalog

The terminal takes its instrument list and per-symbol trading switch from the trading admin
service, not from `GET /api/v1/market/instruments` (bundle):

- Connect to `/ws/admin`, send `{"op": "subscribe", "args": ["instruments"]}` on every open.
- Pushes: `{"topic": "instruments" | "instruments.<suffix>", "type": "snapshot" | ..., "data": <entry> | [<entry>, ...]}`.
  A `snapshot` on topic `instruments` replaces the whole set; other pushes update the entries they
  contain.
- Entries without a string `symbol` and boolean `status` are ignored. Fields:

| Field | Type | Meaning |
|---|---|---|
| `symbol` | string | match symbols after removing non-alphanumerics and upper-casing |
| `status` | boolean | instrument enabled in the terminal |
| `tradable` | boolean, optional | `false` -> status `untradable` |
| `category` | string, optional | default `spot` |
| `baseCoin`, `quoteCoin` | string | coins |
| `tickSize` | decimal | price step (an entry without `baseCoin`, `quoteCoin` and `tickSize` only updates `status`) |
| `basePrecision`, `quotePrecision` | decimal | decimals or step |
| `minBaseAmount` / `maxBaseAmount` | decimal | quantity limits (alias `minOrderQty` / `maxOrderQty`) |
| `minQuoteAmount` / `maxQuoteAmount` | decimal | value limits (alias `minOrderAmt` / `maxOrderAmt`) |
| `maxMarketOrderQty`, `maxLimitOrderQty` | decimal | per-type quantity limits |

Values may be strings or numbers (the app stringifies them).

## Not part of the Whitebird API

- `https://wb-exchange-proxy.onrender.com` (`/api/market/info`, `/market-info`): third-party proxy
  for coin metadata (market cap, supply), with a CoinGecko fallback. Not a Whitebird service.
- The Bybit, Bitget, Gate and CoreX configs in the same bundle (CoreX is a localhost development
  backend; its `/api/v1/wallet/balance` and `/ws/instruments` are not exposed by Whitebird hosts).

## Suggested SDK surface

- `client.market.stream()` -> public socket; `subscribe_orderbook(symbol, step)`,
  `subscribe_tickers(*symbols)`, `subscribe_klines(symbol, interval)`, `subscribe_trades(symbol)`;
  maintains the local order book from snapshot + deltas.
- `client.trading.stream()` -> private socket; `orders()`, `executions()`, `wallet()`.
- Optional `client.trading.ws_orders` (trade socket) with `create`, `amend`, `cancel` returning the
  same `TradingOrderAck` as REST.
- Base URLs derived from the service URLs (`http` -> `ws`), so `environment="demo"` and per-service
  overrides work unchanged.
