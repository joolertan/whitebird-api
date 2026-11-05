package whitebird

import (
	"context"
	"net/http"
	"net/url"
	"time"
)

// An Option configures a [Client] in [NewClient].
type Option func(*config)

type config struct {
	environment Environment
	baseURLs    map[Service]string
	httpClient  *http.Client
	email       string
	password    string
	tokens      TokenStore
	codes       CodeProvider
	captcha     func(ctx context.Context, captcha Captcha) (int, error)
	maxRetries  int
	timeout     time.Duration
	userAgent   string
	language    string
}

func defaultConfig() config {
	return config{
		environment: defaultEnvironment,
		baseURLs:    map[Service]string{},
		httpClient:  &http.Client{},
		maxRetries:  2,
		timeout:     30 * time.Second,
		userAgent:   "whitebird-api-go/" + Version,
	}
}

// WithCredentials sets the account email and password. The client logs in with them on the first
// call that needs a token, and again whenever the refresh token stops working.
func WithCredentials(email, password string) Option {
	return func(c *config) {
		c.email, c.password = email, password
	}
}

// WithEnvironment selects the environment. The default is [Production].
func WithEnvironment(env Environment) Option {
	return func(c *config) {
		c.environment = env
	}
}

// WithBaseURL overrides the base URL of one service, for example to follow a host change
// before a new release of this module or to point the client at a test server.
func WithBaseURL(service Service, baseURL string) Option {
	return func(c *config) {
		c.baseURLs[service] = baseURL
	}
}

// WithHTTPClient sets the HTTP client used for every request. Timeouts are applied per request
// (see [WithTimeout]); a client-wide [http.Client.Timeout] would also cut event streams short.
func WithHTTPClient(client *http.Client) Option {
	return func(c *config) {
		c.httpClient = client
	}
}

// WithTokenStore sets where tokens are kept between runs. Without it tokens live in memory only.
func WithTokenStore(store TokenStore) Option {
	return func(c *config) {
		c.tokens = store
	}
}

// WithCodeProvider sets the source of TOTP codes for accounts with two-factor authentication:
// the client asks it for a code when the server wants one at sign-in, and to confirm sensitive
// operations such as withdrawals (see [Client.ConfirmOperation]). It is not called for
// accounts without two-factor authentication. The [github.com/joolertan/whitebird-api/go/otp]
// package computes codes from the secret.
func WithCodeProvider(provider CodeProvider) Option {
	return func(c *config) {
		c.codes = provider
	}
}

// WithCaptchaSolver sets the function that solves the slider captcha the authorization server
// requires when an account without two-factor authentication signs in. A rejected answer is
// retried with a fresh captcha, for up to three attempts. Without a solver, such a login fails
// with [ErrCaptchaRequired].
//
// The solver returns the horizontal position of the puzzle piece in background pixels, for
// example as entered by a person shown the images:
//
//	whitebird.WithCaptchaSolver(func(ctx context.Context, c whitebird.Captcha) (int, error) {
//		os.WriteFile("background.png", c.BackgroundImage, 0o600)
//		os.WriteFile("piece.png", c.PuzzlePiece, 0o600)
//		fmt.Print("Slider position: ")
//		var x int
//		_, err := fmt.Scan(&x)
//		return x, err
//	})
func WithCaptchaSolver(solve func(ctx context.Context, captcha Captcha) (int, error)) Option {
	return func(c *config) {
		c.captcha = solve
	}
}

// WithMaxRetries sets how many times a request is retried. GET, HEAD and OPTIONS requests are
// retried after a network error or a 429, 502, 503 or 504 response; other requests only when
// the connection could not be established or after a 429 response, as they may have taken
// effect otherwise. The default is 2; 0 disables retries.
func WithMaxRetries(n int) Option {
	return func(c *config) {
		c.maxRetries = max(n, 0)
	}
}

// WithTimeout limits each attempt of a request, including reading the response. For event
// streams it limits connecting only. The default is 30 seconds; 0 disables the limit.
func WithTimeout(d time.Duration) Option {
	return func(c *config) {
		c.timeout = d
	}
}

// WithLanguage sets the Accept-Language header of every request, such as "en" or "ru", which
// selects the language of the messages the API answers.
func WithLanguage(language string) Option {
	return func(c *config) {
		c.language = language
	}
}

// WithUserAgent replaces the User-Agent header, whitebird-api-go/<version> by default.
func WithUserAgent(userAgent string) Option {
	return func(c *config) {
		c.userAgent = userAgent
	}
}

// A RequestOption adjusts a single call.
type RequestOption func(*callConfig)

type callConfig struct {
	header     http.Header
	query      url.Values
	timeout    time.Duration
	maxRetries int
	idempotent bool
	public     bool
	envelope   bool
}

// WithHeader adds a header to the request.
func WithHeader(key, value string) RequestOption {
	return func(c *callConfig) {
		c.header.Add(key, value)
	}
}

// WithQuery adds a query parameter to the request.
func WithQuery(key, value string) RequestOption {
	return func(c *callConfig) {
		c.query.Add(key, value)
	}
}

// WithRequestTimeout overrides [WithTimeout] for the request.
func WithRequestTimeout(d time.Duration) RequestOption {
	return func(c *callConfig) {
		c.timeout = d
	}
}

// WithRequestMaxRetries overrides [WithMaxRetries] for the request.
func WithRequestMaxRetries(n int) RequestOption {
	return func(c *callConfig) {
		c.maxRetries = max(n, 0)
	}
}

// WithIdempotent states whether the request is safe to retry after any transient failure, as
// GET, HEAD and OPTIONS requests are by default. Other requests are retried only when they did
// not reach the server or were rate limited.
func WithIdempotent(idempotent bool) RequestOption {
	return func(c *callConfig) {
		c.idempotent = idempotent
	}
}

// WithoutAuth sends the request without an access token, for public endpoints called through
// [Client.Request].
func WithoutAuth() RequestOption {
	return func(c *callConfig) {
		c.public = true
	}
}

// WithRetcodeEnvelope unwraps a response of the form {"retCode", "retMsg", "result"}, as market
// data endpoints answer, for a call through [Client.Request]: the result is decoded, and a
// retCode other than 0 and 200 fails the call with an [*APIError].
func WithRetcodeEnvelope() RequestOption {
	return func(c *callConfig) {
		c.envelope = true
	}
}
