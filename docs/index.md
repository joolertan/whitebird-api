# Whitebird API

Unofficial clients for the [Whitebird](https://whitebird.io) crypto exchange API in **Python**,
**Node.js**, **Go** and **Rust**.

All four SDKs are generated from one OpenAPI specification, so they offer the same resources and
behave the same way (see [API changes](guides/api-changes.md) for what your code can rely on).

!!! warning "Unofficial"
    This project is not affiliated with Whitebird. Endpoints marked *experimental* in the
    [endpoint reference](reference/endpoints.md) may change, and their response models may be
    incomplete.

## Features

- Brokerage, exchange, balances, trading, market data, KYC, notifications, ICO tokens, payment
  providers and more, grouped into resources.
- Sign-in: password, a 2FA code from your callback or a TOTP secret,
  the slider captcha through your own solver; automatic token refresh, pluggable session storage
  and the operation MFA flow for withdrawals.
- Typed models with lossless decimal amounts and open enums: unknown fields and values do not
  break parsing.
- Paginated endpoints that are natively iterable, and Server-Sent Events as native streams.
- Retries that never repeat an order or a payment: reads are retried on transient failures, other
  requests only when they did not reach the server. An escape hatch calls endpoints not covered
  yet.

## Install

=== "Python"

    ```bash
    pip install whitebird-api            # or: pip install "whitebird-api[otp]"
    ```

=== "Node.js"

    ```bash
    npm install whitebird-api
    ```

=== "Go"

    ```bash
    go get github.com/joolertan/whitebird-api/go
    ```

=== "Rust"

    ```bash
    cargo add whitebird-api                # or: cargo add whitebird-api --features otp
    ```

## A first call

Market data is public, so no account is needed:

=== "Python"

    ```python
    from whitebird import Whitebird

    with Whitebird() as client:
        book = client.market.orderbook(symbol="BTCUSDT", limit=5)
        print(book.bids[0], book.asks[0])
    ```

=== "Node.js"

    ```ts
    import { Whitebird } from 'whitebird-api';

    const client = new Whitebird();
    const book = await client.market.orderbook({ symbol: 'BTCUSDT', limit: 5 });
    console.log(book.bids[0], book.asks[0]);
    ```

=== "Go"

    ```go
    client, err := whitebird.NewClient()
    if err != nil {
        log.Fatal(err)
    }
    book, err := client.Market.Orderbook(ctx, whitebird.MarketOrderbookParams{Symbol: "BTCUSDT", Limit: whitebird.Ptr(5)})
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println(book.Bids[0], book.Asks[0])
    ```

=== "Rust"

    ```rust
    use whitebird::Client;
    use whitebird::params::MarketOrderbookParams;

    let client = Client::builder().build()?;
    let params = MarketOrderbookParams::builder().symbol("BTCUSDT").limit(5).build();
    let book = client.market().orderbook(params).await?;
    println!("{:?} {:?}", book.bids[0], book.asks[0]);
    ```

Continue with [Getting started](getting-started.md).
