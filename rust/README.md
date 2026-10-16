# whitebird-api

[![crates.io](https://img.shields.io/crates/v/whitebird-api?label=crates.io)](https://crates.io/crates/whitebird-api)
[![docs.rs](https://img.shields.io/docsrs/whitebird-api)](https://docs.rs/whitebird-api)

Unofficial Rust client for the [Whitebird](https://whitebird.io) crypto exchange API.

- Typed methods for market data, exchange and spot trading, deposits and withdrawals, payment
  cards, brokerage, ICO tokens, KYC, notifications and account settings, grouped by resource:
  `client.brokerage().otc_orders().list(params)`.
- Handles the whole login flow: the TOTP code when two-factor authentication is on, a captcha
  through your solver otherwise, and the MFA token of withdrawals. Sessions are refreshed
  before they expire and after a 401.
- Retries idempotent requests on network errors, 429 and 5xx, honouring `Retry-After`.
- Paginated endpoints are lazy `Stream`s, and notifications are a `Stream` of server-sent events
  that reconnects by itself.
- Tolerates API changes: unknown fields are ignored, unknown enum values are kept, timestamps are
  parsed leniently and amounts are decoded without going through floating point.

This crate is not affiliated with Whitebird.

## Installation

```toml
[dependencies]
whitebird-api = "0.1"
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
futures = "0.3"
```

The library is imported as `whitebird`. Rust 1.85 or newer is required.

| Feature | Default | |
| --- | --- | --- |
| `rustls-tls` | yes | TLS through rustls |
| `native-tls` | | TLS through the platform library |
| `otp` | | `Totp` computes two-factor codes from the base32 secret |
| `blocking` | | a synchronous client with the same resources |

## Usage

```rust,no_run
use futures::TryStreamExt;
use whitebird::params::BrokerageOtcOrdersListParams;
use whitebird::{Client, Environment, Totp};

#[tokio::main]
async fn main() -> whitebird::Result<()> {
    let client = Client::builder()
        .credentials("me@example.com", "password")
        .otp(Totp::new("JBSWY3DPEHPK3PXP")?) // or a fixed code: .otp("123456")
        .environment(Environment::Demo)
        .build()?;

    // Signs in on the first call.
    let me = client.brokerage().subaccounts().me().await?;
    println!("{:?}", me.balances);

    // Pages are fetched as the stream is polled.
    let params = BrokerageOtcOrdersListParams::builder().size(50).build();
    let mut orders = client.brokerage().otc_orders().list(params);
    while let Some(order) = orders.try_next().await? {
        println!("{:?} {:?}", order.uuid, order.status);
    }

    // Server-sent notifications; the stream reconnects with `Last-Event-ID`.
    let mut events = client.notifications().stream();
    while let Some(event) = events.try_next().await? {
        let message = event.json()?;
        println!("{:?} {:?}", message.r#type, message.payload);
    }
    Ok(())
}
```

### Errors

Every method returns `whitebird::Result<T>`. API errors carry the HTTP status, the service's
`code`, `status` and `message`, the invalid `fields` and the raw body:

```rust,no_run
# async fn run(client: whitebird::Client, params: whitebird::params::BrokerageOtcOrdersCreateParams) {
use whitebird::ErrorKind;

match client.brokerage().otc_orders().create(params).await {
    Ok(order) => println!("created {:?}", order.uuid),
    Err(error) if error.is_mfa_required() => println!("two-factor code needed"),
    Err(error) => match error.api() {
        Some(api) if api.kind() == ErrorKind::BadRequest => {
            println!("{}: {:?}", api.message(), api.fields());
        }
        _ => println!("{error}"),
    },
}
# }
```

### Two-factor codes, operation MFA and captchas

Withdrawals need an operation MFA token. With a code provider (`.otp(..)`) the client obtains
one before the first withdrawal and renews it when it is refused. Without a provider, pass a
code yourself; the token is then sent as `x-mfa-token` with every request:

```rust,no_run
# async fn run(client: whitebird::Client) -> whitebird::Result<()> {
client.confirm_operation("WITHDRAWAL", Some("123456")).await?;
# Ok(())
# }
```

Accounts without two-factor authentication must pass a slider captcha to sign in. The SDK does
not solve it: set a `CaptchaSolver` that shows the images to a person or calls a solving service.

### Sessions

Tokens live in memory by default. Implement `TokenStore` to keep them across restarts. Use
`Client::request` for endpoints the SDK does not cover yet; it applies the same authentication,
retries and error handling:

```rust,no_run
# async fn run(client: whitebird::Client) -> whitebird::Result<()> {
use whitebird::{Method, Service};

let body: serde_json::Value = client
    .request(Service::Brokerage, Method::GET, "/api/v1/brokerage/payment/methods")
    .query("currency", "RUB")
    .send()
    .await?
    .json()
    .await?;
# Ok(())
# }
```

More programs are in [`examples/rust`](https://github.com/joolertan/whitebird-api/tree/main/examples/rust).

## License

MIT
