# whitebird

[![Go Reference](https://pkg.go.dev/badge/github.com/joolertan/whitebird-api/go.svg)](https://pkg.go.dev/github.com/joolertan/whitebird-api/go)
[![Docs](https://img.shields.io/readthedocs/whitebird-api)](https://whitebird-api.readthedocs.io)

Unofficial Go client for the [Whitebird](https://whitebird.io) crypto exchange API. Standard
library only.

Covers market data, exchange and spot trading, deposits and withdrawals, payment cards,
brokerage, ICO tokens, KYC, notifications and account settings.

```sh
go get github.com/joolertan/whitebird-api/go
```

```go
package main

import (
	"context"
	"fmt"
	"log"
	"os"

	"github.com/joolertan/whitebird-api/go"
	"github.com/joolertan/whitebird-api/go/otp"
)

func main() {
	ctx := context.Background()
	totp, err := otp.New(os.Getenv("WHITEBIRD_TOTP_SECRET"))
	if err != nil {
		log.Fatal(err)
	}
	client, err := whitebird.NewClient(
		whitebird.WithCredentials(os.Getenv("WHITEBIRD_USERNAME"), os.Getenv("WHITEBIRD_PASSWORD")),
		whitebird.WithCodeProvider(totp),
	)
	if err != nil {
		log.Fatal(err)
	}

	// Paginated methods return an iter.Seq2; pages are fetched as the loop goes.
	for order, err := range client.Exchange.Orders.History(ctx, whitebird.ExchangeOrdersHistoryParams{Size: 50}) {
		if err != nil {
			log.Fatal(err)
		}
		fmt.Println(order.Number, order.Status)
	}
}
```

- **Sessions:** the client signs in on the first call that needs it, as the web application does,
  refreshes the token a minute before it expires and keeps it in a `TokenStore`;
  `client.User.Logout` ends the session.
- **Two-factor codes:** `WithCodeProvider` takes an `*otp.TOTP`, a `CodeFunc` that asks for the
  code, so the secret stays out of the program, or a `StaticCode`.
- **Captcha:** `WithCaptchaSolver` answers the slider captcha of accounts without 2FA.
- **Withdrawals:** when the API asks for the operation MFA token, a client with a code provider
  obtains it and sends the operation again; `client.ConfirmOperation` does it explicitly.
- **Errors:** API errors are `*whitebird.APIError`; match classes with `errors.Is`
  (`whitebird.ErrNotFound`, `whitebird.ErrMFARequired`, ...).
- **Streams:** `client.Notifications.Stream(ctx)` yields server-sent events and reconnects on its
  own.
- **Uploads:** any `io.Reader`, or a `whitebird.File` with a file name and media type.
- **Escape hatch:** `client.Request` calls endpoints the package does not cover yet.

Documentation: <https://whitebird-api.readthedocs.io> ·
Examples: [`examples/go`](https://github.com/joolertan/whitebird-api/tree/main/examples/go)

This project is not affiliated with Whitebird.
