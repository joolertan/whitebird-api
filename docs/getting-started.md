# Getting started

## Install

=== "Python"

    Python 3.11 or newer.

    ```bash
    pip install whitebird-api
    pip install "whitebird-api[otp]"   # with TOTP code generation for 2FA accounts
    ```

=== "Node.js"

    Node.js 22 or newer, Deno, Bun or a browser. No runtime dependencies.

    ```bash
    npm install whitebird-api
    ```

=== "Go"

    Go 1.25 or newer. The `otp` subpackage computes TOTP codes.

    ```bash
    go get github.com/joolertan/whitebird-api/go
    ```

=== "Rust"

    Rust 1.85 or newer. Features: `otp` (TOTP codes), `blocking` (synchronous client),
    `rustls-tls` (default) or `native-tls`.

    ```bash
    cargo add whitebird-api --features otp
    cargo add tokio --features macros,rt-multi-thread
    ```

## Create a client

The client signs in on the first call that needs an account, using the email and password. For
accounts with two-factor authentication, give it the authenticator secret (or a function
returning the current code). Accounts without it have to pass a slider captcha when signing in,
through a solver you provide: see [Authentication](guides/authentication.md#captcha). The
examples read credentials from the environment.

=== "Python"

    ```python
    import os

    from whitebird import Whitebird
    from whitebird.otp import TOTP

    client = Whitebird(
        os.environ["WHITEBIRD_USERNAME"],
        os.environ["WHITEBIRD_PASSWORD"],
        otp=TOTP(os.environ["WHITEBIRD_TOTP_SECRET"]),
    )
    ```

    `AsyncWhitebird` has the same constructor and methods, as coroutines.

=== "Node.js"

    ```ts
    import { Whitebird } from 'whitebird-api';
    import { totp } from 'whitebird-api/otp';

    const client = new Whitebird({
      username: process.env.WHITEBIRD_USERNAME,
      password: process.env.WHITEBIRD_PASSWORD,
      otp: totp(process.env.WHITEBIRD_TOTP_SECRET!),
    });
    ```

=== "Go"

    ```go
    import (
        "github.com/joolertan/whitebird-api/go"
        "github.com/joolertan/whitebird-api/go/otp"
    )

    totp, err := otp.New(os.Getenv("WHITEBIRD_TOTP_SECRET"))
    if err != nil {
        log.Fatal(err)
    }
    client, err := whitebird.NewClient(
        whitebird.WithCredentials(os.Getenv("WHITEBIRD_USERNAME"), os.Getenv("WHITEBIRD_PASSWORD")),
        whitebird.WithCodeProvider(totp),
    )
    ```

=== "Rust"

    ```rust
    use whitebird::{Client, Totp};

    let client = Client::builder()
        .credentials(std::env::var("WHITEBIRD_USERNAME")?, std::env::var("WHITEBIRD_PASSWORD")?)
        .otp(Totp::new(&std::env::var("WHITEBIRD_TOTP_SECRET")?)?)
        .build()?;
    ```

## Call endpoints

Endpoints are methods grouped into resources that follow the API's structure, such as
`brokerage`, `exchange.orders` or `market`. Path parameters are positional; query and body fields
are named. The [endpoint reference](reference/endpoints.md) lists every method.

=== "Python"

    ```python
    me = client.user.me()
    balances = client.brokerage.balances()
    order = client.exchange.orders.get("8f6c0f4e-...")
    ```

=== "Node.js"

    ```ts
    const me = await client.user.me();
    const balances = await client.brokerage.balances();
    const order = await client.exchange.orders.get('8f6c0f4e-...');
    ```

=== "Go"

    ```go
    me, err := client.User.Me(ctx)
    balances, err := client.Brokerage.Balances(ctx)
    order, err := client.Exchange.Orders.Get(ctx, "8f6c0f4e-...")
    ```

=== "Rust"

    ```rust
    let me = client.user().me().await?;
    let balances = client.brokerage().balances().await?;
    let order = client.exchange().orders().get("8f6c0f4e-...").await?;
    ```

## Iterate over pages

Listings are iterable: the next page is fetched only when the loop reaches it.
See [Pagination](guides/pagination.md).

=== "Python"

    ```python
    for order in client.exchange.orders.history(size=50):
        print(order.number, order.status)
    ```

=== "Node.js"

    ```ts
    for await (const order of client.exchange.orders.history({ size: 50 })) {
      console.log(order.number, order.status);
    }
    ```

=== "Go"

    ```go
    for order, err := range client.Exchange.Orders.History(ctx, whitebird.ExchangeOrdersHistoryParams{Size: 50}) {
        if err != nil {
            log.Fatal(err)
        }
        fmt.Println(order.Number, order.Status)
    }
    ```

=== "Rust"

    ```rust
    use futures::TryStreamExt;
    use whitebird::params::ExchangeOrdersHistoryParams;

    let params = ExchangeOrdersHistoryParams::builder().size(50).build();
    let mut orders = client.exchange().orders().history(params);
    while let Some(order) = orders.try_next().await? {
        println!("{:?} {:?}", order.number, order.status);
    }
    ```

## Send files

Uploads (support attachments, qualification documents) take raw bytes or a file with its name and
media type. Without a type the client guesses it from the name, then from the content.

=== "Python"

    ```python
    from pathlib import Path

    from whitebird.models import SupportRequest

    client.support.send(
        request=SupportRequest(name="Ann", email="ann@example.com", topic="Documents", message="..."),
        files=[("passport.jpg", Path("passport.jpg").read_bytes(), "image/jpeg")],
    )
    ```

    A file is `bytes`, a binary file object, `(filename, content)` or
    `(filename, content, media_type)`.

=== "Node.js"

    ```ts
    import { openAsBlob } from 'node:fs';

    const passport = new File([await openAsBlob('passport.jpg')], 'passport.jpg', { type: 'image/jpeg' });
    await client.support.send({
      request: { name: 'Ann', email: 'ann@example.com', topic: 'Documents', message: '...' },
      files: [passport],
    });
    ```

    A file is a `Blob` or `File` (its name is kept), or a `Uint8Array`/`Buffer`.

=== "Go"

    ```go
    f, err := os.Open("passport.jpg")
    if err != nil {
        log.Fatal(err)
    }
    defer f.Close()
    _, err = client.Support.Send(ctx, whitebird.SupportSendParams{
        Request: whitebird.SupportRequest{Name: "Ann", Email: "ann@example.com", Topic: "Documents", Message: "..."},
        Files:   []io.Reader{whitebird.File{Name: "passport.jpg", ContentType: "image/jpeg", Body: f}},
    })
    ```

    Any `io.Reader` works; an `*os.File` keeps its name.

=== "Rust"

    ```rust
    use whitebird::File;
    use whitebird::models::SupportRequest;
    use whitebird::params::SupportSendParams;

    let request = SupportRequest::builder()
        .name("Ann")
        .email("ann@example.com")
        .topic("Documents")
        .message("...")
        .build();
    let params = SupportSendParams::builder()
        .request(request)
        .files(vec![File::read("passport.jpg")?])
        .build();
    client.support().send(params).await?;
    ```

    `File::new(bytes).with_name(..).with_content_type(..)` builds one from memory; a `Vec<u8>`
    works too.

## Language of messages

The web application tells Whitebird the user's interface language in the `Accept-Language`
header. To do the same, set it once on the client; it is sent with every request and the
notification stream:

=== "Python"

    ```python
    client = Whitebird(username, password, language="en")
    ```

=== "Node.js"

    ```ts
    const client = new Whitebird({ username, password, language: 'en' });
    ```

=== "Go"

    ```go
    client, err := whitebird.NewClient(whitebird.WithCredentials(username, password), whitebird.WithLanguage("en"))
    ```

=== "Rust"

    ```rust
    let client = Client::builder().credentials(username, password).language("en").build()?;
    ```

## Close the client

=== "Python"

    Use the client as a context manager (`with Whitebird(...) as client:`), or call
    `client.close()` when done. The async client supports `async with` and `await client.aclose()`.

=== "Node.js"

    Nothing to close: requests use `fetch`.

=== "Go"

    Nothing to close; the client can be shared between goroutines.

=== "Rust"

    Dropping the client releases its connections. `Client` is cheap to clone and can be shared
    between tasks.

## Next steps

- [Authentication](guides/authentication.md): sessions, 2FA, captcha and withdrawal confirmation.
- [Environments](guides/environments.md): the demo trading contour and custom hosts.
- [Errors](guides/errors.md): what can fail and how to tell failures apart.
- [Examples](examples.md): complete programs for every language.
