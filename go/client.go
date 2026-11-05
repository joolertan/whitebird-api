package whitebird

import (
	"context"
	"net/http"
	"strings"
)

// NewClient returns a client configured by opts. A client without credentials can call public
// endpoints only.
func NewClient(opts ...Option) (*Client, error) {
	cfg := defaultConfig()
	for _, opt := range opts {
		opt(&cfg)
	}
	if cfg.httpClient == nil {
		cfg.httpClient = &http.Client{}
	}
	baseURLs, err := resolveBaseURLs(cfg.environment, cfg.baseURLs)
	if err != nil {
		return nil, err
	}
	c := &Client{core: newCore(cfg, baseURLs)}
	c.initResources()
	return c, nil
}

// Login makes sure the client holds a valid access token: it loads one from the token store,
// refreshes it if it is about to expire, or logs in with the credentials. Calling Login is
// optional, as the first call that needs a token does the same; it is a way to fail early.
//
// A password login follows the web application: the client asks whether the account has
// two-factor authentication, then sends the password, and the TOTP code from the code provider
// when the server asks for it. Accounts without two-factor authentication answer a captcha
// instead (see [WithCaptchaSolver]).
func (c *Client) Login(ctx context.Context) error {
	_, err := c.core.accessToken(ctx)
	return err
}

// ConfirmOperation exchanges a TOTP code for the operation MFA token that sensitive operations
// such as withdrawals require, sets it on the client like [Client.SetMFAToken] and returns it.
// With an empty code, the code comes from the provider set with [WithCodeProvider].
//
// The generated methods of these operations confirm on their own when the client has a code
// provider: if the API refuses the operation for want of a valid token, they get one and send
// the operation again. Without a code provider, they fail with [ErrMFARequired]; confirm the
// operation and call them again:
//
//	if _, err := client.ConfirmOperation(ctx, "WITHDRAWAL", code); err != nil {
//		return err
//	}
//	withdrawal, err := client.Balance.Withdrawals.CreateCrypto(ctx, params)
func (c *Client) ConfirmOperation(ctx context.Context, operation, code string) (string, error) {
	return c.core.confirmOperation(ctx, operation, code)
}

// SetMFAToken sets the operation MFA token that sensitive operations such as withdrawals
// require. The client sends it in the x-mfa-token header of every request until it is replaced
// or cleared with an empty token.
func (c *Client) SetMFAToken(token string) {
	if token = strings.TrimSpace(token); token == "" {
		c.core.mfaToken.Store(nil)
		return
	}
	c.core.mfaToken.Store(&token)
}

// Request calls an endpoint that this package does not cover yet, with the same
// authentication, retries and error handling as the generated methods. The path is absolute
// from the host of service and may not contain a query string; add query parameters with
// [WithQuery], and use [WithoutAuth] for public endpoints.
//
// A nil body sends none; [net/url.Values] is sent as a form, []byte and [io.Reader] as is, and
// anything else as JSON. The response is decoded as JSON into result unless result is nil or a
// *[]byte, which receives the raw body. [WithRetcodeEnvelope] unwraps the envelope of market
// data responses first:
//
//	var instruments []json.RawMessage
//	err := client.Request(ctx, whitebird.ServiceMarketData, http.MethodGet, "/api/v1/market/instruments",
//		nil, &instruments, whitebird.WithoutAuth(), whitebird.WithRetcodeEnvelope())
func (c *Client) Request(ctx context.Context, service Service, method, path string, body, result any, opts ...RequestOption) error {
	op := &operation{service: service, method: method, path: path, body: anyBody(body)}
	data, err := c.core.send(ctx, op, "application/json", opts)
	if err != nil || result == nil {
		return err
	}
	if raw, ok := result.(*[]byte); ok {
		*raw = data
		return nil
	}
	return decodeResult(data, result)
}
