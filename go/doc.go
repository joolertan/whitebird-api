// Package whitebird is an unofficial client for the API of the Whitebird crypto exchange
// (whitebird.io).
//
// Whitebird publishes no API reference. The API described here is reconstructed from the web
// application, so endpoints may change without notice; see [Client.Request] for calling
// endpoints this package does not cover yet.
//
// # Getting started
//
// Create a [Client] with [NewClient]. Without options it can call public endpoints only; for
// everything else, give it the credentials of the account:
//
//	client, err := whitebird.NewClient(
//		whitebird.WithCredentials("trader@example.com", "password"),
//		whitebird.WithEnvironment(whitebird.Demo),
//	)
//
// Operations are grouped by resource, following the layout of the API. Path parameters are
// positional arguments; query, header and body fields go in a parameter struct named after the
// method:
//
//	order, err := client.Brokerage.OTCOrders.Create(ctx, whitebird.BrokerageOTCOrdersCreateParams{
//		SubaccountID: subaccountID,
//		FromAsset:    "USDT",
//		FromAmount:   whitebird.MustDecimal("100"),
//		ToAsset:      "BTC",
//		// ...
//	})
//
// # Authentication
//
// The client logs in with the password grant on the first call that needs a token, or when
// [Client.Login] is called. It refreshes the access token shortly before it expires and once
// when the API rejects it, and logs in again when the refresh token stops working. Concurrent
// calls share a single refresh.
//
// Tokens live in memory unless [WithTokenStore] provides a [TokenStore], such as
// [FileTokenStore], that keeps them across restarts.
//
// Accounts with two-factor authentication need a TOTP code at every login. Supply them with
// [WithCodeProvider]: [StaticCode] for a code at hand, [CodeFunc] to prompt for one, or
// otp.TOTP from the [github.com/joolertan/whitebird-api/go/otp] package to compute codes from
// the secret:
//
//	totp, err := otp.New(os.Getenv("WHITEBIRD_TOTP_SECRET"))
//	...
//	client, err := whitebird.NewClient(
//		whitebird.WithCredentials(email, password),
//		whitebird.WithCodeProvider(totp),
//	)
//
// Without a code provider, a login to such an account fails with [ErrMFARequired].
//
// Sensitive operations such as withdrawals also need an operation MFA token. Once obtained, set
// it with [Client.SetMFAToken]; the client then sends it with every request.
//
// # Pagination
//
// A paginated operation has two methods. The one named after the operation returns an iterator
// over the items of all pages, fetched as the loop advances; the one ending in Page returns a
// single [Page] with its metadata:
//
//	for order, err := range client.Brokerage.OTCOrders.List(ctx, whitebird.BrokerageOTCOrdersListParams{Size: 100}) {
//		if err != nil {
//			return err
//		}
//		fmt.Println(order.UUID, order.Status)
//	}
//
//	page, err := client.Brokerage.OTCOrders.ListPage(ctx, whitebird.BrokerageOTCOrdersListParams{Page: 2})
//
// # Streaming
//
// Server-sent event streams are iterators of [StreamEvent] values. They reconnect on their own
// when the connection drops, resuming after the last event received, and end when the context
// is done or the loop breaks.
//
// # Errors
//
// Errors answered by the API are [*APIError] values. Test their class with [errors.Is] and the
// sentinel errors, and read the details with [errors.As]:
//
//	_, err := client.Brokerage.Subaccounts.Get(ctx, id)
//	switch {
//	case errors.Is(err, whitebird.ErrNotFound):
//		// ...
//	case errors.Is(err, whitebird.ErrVerificationRequired):
//		// ...
//	}
//	var apiErr *whitebird.APIError
//	if errors.As(err, &apiErr) {
//		log.Println(apiErr.StatusCode, apiErr.ErrorCode, apiErr.Message)
//	}
//
// Requests that got no response fail with a [*TransportError].
//
// Idempotent requests (GET, HEAD, PUT, DELETE) are retried twice on network errors and on 429,
// 502, 503 and 504 responses, with exponential backoff and jitter, honoring Retry-After. POST
// requests are never retried unless marked with [WithIdempotent]. Each attempt is limited to 30
// seconds. [WithMaxRetries] and [WithTimeout] change the defaults; the RequestOption variants
// change them for a single call.
//
// # Types
//
// Parameter structs follow one rule: required fields are plain values and optional fields are
// pointers, which [Ptr] helps to set. Slices, maps, and the [Decimal], [Time] and [Date] types
// are not pointers, as their zero value already means "not set".
//
// In response types every field is a plain value that holds its zero value when the API omits
// it or sends null, except nested objects, which are pointers and nil when absent.
//
// Enumerations are string or integer types with a constant per known value. Values added to the
// API later are kept as they are, not rejected. Amounts of type [Decimal] keep every digit, and
// [Time] accepts the timestamp formats the services use. Unknown response fields are ignored.
package whitebird
