# Environments and hosts

Whitebird is split into services, each on its own host: `brokerage-service.whitebird.io`,
`exchange-core.whitebird.io`, `market-data-service.whitebird.io` and so on. The client knows them
all, and every method is sent to the right one.

## Production and demo

The `demo` environment is Whitebird's sandbox for trading: the `api`, `market_data`,
`order_management`, `trading` and `trading_admin` services have demo hosts with demo balances
(top them up with `trading.demo.deposit`). Services without a demo host are reached on production
in both environments, and accounts are shared between the two.

=== "Python"

    ```python
    client = Whitebird(username, password, environment="demo")
    ```

=== "Node.js"

    ```ts
    const client = new Whitebird({ username, password, environment: 'demo' });
    ```

=== "Go"

    ```go
    client, err := whitebird.NewClient(whitebird.WithCredentials(username, password), whitebird.WithEnvironment(whitebird.Demo))
    ```

=== "Rust"

    ```rust
    let client = Client::builder().credentials(username, password).environment(Environment::Demo).build()?;
    ```

## Overriding a host

Each service URL can be replaced, for example to go through a proxy or to follow a service that
moved before a new SDK version is out:

=== "Python"

    ```python
    client = Whitebird(base_urls={"market_data": "https://md.example.com"})
    ```

=== "Node.js"

    ```ts
    const client = new Whitebird({ baseUrls: { market_data: 'https://md.example.com' } });
    ```

=== "Go"

    ```go
    client, err := whitebird.NewClient(whitebird.WithBaseURL(whitebird.ServiceMarketData, "https://md.example.com"))
    ```

=== "Rust"

    ```rust
    let client = Client::builder().base_url(Service::MarketData, "https://md.example.com").build()?;
    ```

## Services

| Key | Production host | Demo host |
|---|---|---|
| `accounting` | `accounting-service.whitebird.io` | |
| `alfa` | `alfa-service.whitebird.io` | |
| `api` | `api.whitebird.io` | `api-demo.whitebird.io` |
| `assist` | `assist-service.whitebird.io` | |
| `auth` | `authorization-server.whitebird.io` | |
| `bepaid` | `bepaid-service.whitebird.io` | |
| `brokerage` | `brokerage-service.whitebird.io` | |
| `carusell` | `carusell-service.whitebird.io` | |
| `current_account` | `current-account-service.whitebird.io` | |
| `exchange` | `exchange-core.whitebird.io` | |
| `ico` | `ico-service.whitebird.io` | |
| `kyc` | `kyc-service.whitebird.io` | |
| `market_data` | `market-data-service.whitebird.io` | `market-data-demo-service.whitebird.io` |
| `mts` | `mtsbank-service.whitebird.io` | |
| `notification` | `notification-service.whitebird.io` | |
| `order_management` | `order-management-service.whitebird.io` | `order-management-demo-service.whitebird.io` |
| `statusbank` | `status-bank-service.whitebird.io` | |
| `trading` | `trading-service.whitebird.io` | `trading-demo-service.whitebird.io` |
| `vtb` | `vtb-service.whitebird.io` | |

`spec/environments.yaml` in the repository lists a few more hosts that the web application
configures but that no SDK method uses yet; they can still be called with
[raw requests](raw-requests.md).
