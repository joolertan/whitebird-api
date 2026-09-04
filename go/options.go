package whitebird

import (
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
	maxRetries  int
	timeout     time.Duration
	userAgent   string
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

// WithCodeProvider sets the source of TOTP codes for accounts with two-factor authentication.
// The [github.com/joolertan/whitebird-api/go/otp] package computes them from the secret.
func WithCodeProvider(provider CodeProvider) Option {
	return func(c *config) {
		c.codes = provider
	}
}

// WithMaxRetries sets how many times an idempotent request is retried after a network error or
// a 429, 502, 503 or 504 response. The default is 2; 0 disables retries.
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

// WithIdempotent states whether the request is safe to retry. By default GET, HEAD, PUT, DELETE
// and OPTIONS requests are retried and POST and PATCH requests are not.
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
