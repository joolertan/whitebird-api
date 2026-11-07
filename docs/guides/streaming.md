# Real-time notifications

Account updates are pushed over Server-Sent Events: order progress, balance operations, KYC and
client status changes. `notifications.stream()` opens the stream and exposes it as the language's
native stream type. Each event's data is a `NotificationStreamMessage` with a `type`
(`ORDER_UPDATE`, `BALANCE_OPERATION_UPDATE`, `APPLICANT_STATUS_UPDATE`, `CLIENT_STATUS_UPDATE`, or
a value this version does not know yet), a timestamp and a `payload`.

The stream is reopened automatically when the connection drops, with `Last-Event-ID` so that the
server can resume after the last event you received. It ends when you stop iterating or cancel it
(quietly, without an error), when the server answers 204 (no more events), or with an error the
client cannot recover from (for example when the session is revoked). An event whose data cannot
be decoded is skipped rather than ending the stream.

=== "Python"

    ```python
    with client.notifications.stream() as events:
        for event in events:
            print(event.data.type, event.data.payload)

    async with async_client.notifications.stream() as events:
        async for event in events:
            ...
    ```

=== "Node.js"

    Pass an `AbortSignal` to stop the stream from elsewhere.

    ```ts
    const controller = new AbortController();
    process.on('SIGINT', () => controller.abort());

    for await (const event of client.notifications.stream({ signal: controller.signal })) {
      console.log(event.data.type, event.data.payload);
    }
    ```

=== "Go"

    Cancel the context to close the stream.

    ```go
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
    defer stop()

    for event, err := range client.Notifications.Stream(ctx) {
        if err != nil {
            return err
        }
        fmt.Println(event.Data.Type, event.Data.Payload)
    }
    ```

=== "Rust"

    Events keep the raw data; `json()` decodes it into the message type. Dropping the stream
    closes it.

    ```rust
    use futures::TryStreamExt;

    let mut events = client.notifications().stream();
    while let Some(event) = events.try_next().await? {
        match event.json() {
            Ok(message) => println!("{:?} {:?}", message.r#type, message.payload),
            Err(error) => eprintln!("skipped an event: {error}"),
        }
    }
    ```

## Market data over WebSocket

The trading terminal receives live order books, trades and tickers over WebSocket
(`wss://.../ws/market` and friends). The SDKs do not wrap these yet; the message format is
described in [`spec/websockets.md`](https://github.com/joolertan/whitebird-api/blob/main/spec/websockets.md).
