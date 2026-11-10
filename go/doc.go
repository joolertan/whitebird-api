// Package whitebird is an unofficial client for the API of the Whitebird crypto exchange
// (whitebird.io).
//
// Endpoints may change without notice; see [Client.Request] for calling endpoints this package
// does not cover yet.
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
// [Client.Login] is called. It refreshes the access token when less
// than a minute is left and once when the API rejects it. When the authorization server
// refuses the refresh token, the session is over: the client forgets it, also in the token
// store, and logs in again with the password if it has one. Other refresh failures, such as an
// unavailable server, are returned as they are. Concurrent calls share a single refresh; it is
// abandoned when every caller waiting for it has given up, so that a login stuck on a prompt
// does not block the calls that follow.
//
// Tokens live in memory unless [WithTokenStore] provides a [TokenStore], such as
// [FileTokenStore], that keeps them across restarts. A token that fails to save is still used.
// The logout operation (client.User.Logout) ends the session: the client forgets its tokens
// and the operation MFA token, also when the request fails, and a call made afterwards logs in
// again. Changing the password starts a new session, whose tokens the client adopts.
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
// A password login sends the password, then the code when the server asks for one; without a
// code provider, the login fails with [ErrMFARequired]. An unknown username fails with
// [ErrAuthentication]. The server may also ask for a slider captcha: [WithCaptchaSolver]
// provides the solution, sent with every later request of that login, and without it the login
// fails with [ErrCaptchaRequired].
//
// Sensitive operations such as withdrawals also need an operation MFA token, sent with every
// request once set. When the API refuses such an operation for want of a token, a client with
// a code provider gets one and sends the operation again; concurrent operations share one code.
// Without a code provider, call [Client.ConfirmOperation] with a TOTP code and try again.
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
// when the connection drops, resuming after the last event received, and end without an error
// when the context is done, when the loop breaks or when the server answers 204 No Content.
// Keep-alive events and events whose data does not decode are skipped.
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
// GET, HEAD and OPTIONS requests are retried twice on network errors and on 429, 502, 503 and
// 504 responses, with exponential backoff and jitter, honoring Retry-After. Other requests, which
// may have taken effect, are retried only when the connection could not be established or after
// a 429 response, unless marked with [WithIdempotent]. Each attempt is limited to 30 seconds.
// [WithMaxRetries] and [WithTimeout] change the defaults; the RequestOption variants change
// them for a single call. [WithLanguage] sets the language of the messages the API answers.
//
// # Types
//
// Parameter structs follow one rule: required fields are plain values and optional fields are
// pointers, which [Ptr] helps to set. Slices, maps, and the [Decimal], [Time] and [Date] types
// are not pointers, as their zero value already means "not set". Fields documented with a
// default get it when left unset. Uploads take any
// [io.Reader]; a [File] gives the file a name and a media type.
//
// In response types every field is a plain value that holds its zero value when the API omits
// it or sends null, except nested objects, which are pointers and nil when absent.
//
// Enumerations are string or integer types with a constant per known value. Values added to the
// API later are kept as they are, not rejected. Amounts of type [Decimal] keep every digit, and
// [Time] accepts the timestamp formats the services use and sends times with a numeric
// offset. Unknown response fields are ignored, and an amount that is not a number decodes as
// the zero Decimal rather than failing the response.
package whitebird
