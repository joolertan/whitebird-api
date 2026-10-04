# Calling endpoints the SDK does not cover

Whitebird changes often. When an endpoint is new, or a method's parameters lag behind the API,
call it through `request`. It goes through the same machinery as the generated methods: base URL
of the service, authentication, retries and error mapping. The service key is one of those in
[Environments](environments.md); the path is absolute from the host.

=== "Python"

    ```python
    from whitebird.models import MarketInstrument

    instruments = client.request(
        "market_data",
        "GET",
        "/api/v1/market/instruments",
        authenticated=False,
        envelope="retcode",
        cast_to=list[MarketInstrument],
    )

    result = client.request(
        "exchange",
        "POST",
        "/api/v1/client/orders/{order_id}/notes",
        path_params={"order_id": order_id},
        json={"text": "paid"},
    )
    ```

    Without `cast_to` the decoded JSON is returned, with non-integer numbers as `Decimal`.

=== "Node.js"

    ```ts
    const instruments = await client.request<MarketInstrument[]>('market_data', 'GET', '/api/v1/market/instruments', {
      auth: false,
      envelope: 'retcode',
    });

    await client.request('exchange', 'POST', `/api/v1/client/orders/${orderId}/notes`, {
      body: { text: 'paid' },
    });
    ```

=== "Go"

    The body is sent as JSON (or as a form for `url.Values`), and the response is decoded into
    `result`.

    ```go
    var instruments []whitebird.MarketInstrument
    err := client.Request(ctx, whitebird.ServiceMarketData, http.MethodGet, "/api/v1/market/instruments",
        nil, &instruments, whitebird.WithoutAuth(), whitebird.WithRetcodeEnvelope())

    err = client.Request(ctx, whitebird.ServiceExchange, http.MethodPost, "/api/v1/client/orders/"+orderID+"/notes",
        map[string]string{"text": "paid"}, nil)
    ```

=== "Rust"

    ```rust
    use whitebird::{Envelope, Method, Service};
    use whitebird::models::MarketInstrument;

    let instruments: Vec<MarketInstrument> = client
        .request(Service::MarketData, Method::GET, "/api/v1/market/instruments")
        .public()
        .envelope(Envelope::Retcode)
        .send()
        .await?
        .json()
        .await?;
    ```

!!! note
    Market data endpoints wrap results as `{"retCode": 0, "retMsg": "OK", "result": ...}`.
    Ask for the `retcode` envelope to get `result` directly and a non-zero `retCode` raised as an
    API error.

If you rely on a raw request, please [open an issue](https://github.com/joolertan/whitebird-api/issues)
so the endpoint can be added to the specification.
