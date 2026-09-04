package whitebird

import (
	"context"
	"net/http"
	"time"
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
	c := &Client{core: &core{cfg: cfg, baseURLs: baseURLs, now: time.Now, sleep: sleep}}
	c.initResources()
	return c, nil
}

// Login makes sure the client holds a valid access token: it loads one from the token store,
// refreshes it if it is about to expire, or logs in with the credentials. Calling Login is
// optional, as the first call that needs a token does the same; it is a way to fail early.
func (c *Client) Login(ctx context.Context) error {
	_, err := c.core.accessToken(ctx)
	return err
}

// SetMFAToken sets the operation MFA token that sensitive operations such as withdrawals
// require. The client sends it in the x-mfa-token header of every request until it is replaced
// or cleared with an empty token.
func (c *Client) SetMFAToken(token string) {
	if token == "" {
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
// *[]byte, which receives the raw body.
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
	return decode(data, result)
}
