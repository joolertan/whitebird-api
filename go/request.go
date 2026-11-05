package whitebird

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// core holds what every resource shares: configuration, the session and the MFA token.
type core struct {
	cfg      config
	baseURLs map[Service]string
	session  session
	mfaToken atomic.Pointer[string]
	// mfaGate lets one operation at a time replace a refused operation MFA token.
	mfaGate chan struct{}
	// password is the password of the credentials, replaced after a password change.
	password atomic.Pointer[string]
	now      func() time.Time
	sleep    func(ctx context.Context, d time.Duration) error
}

func newCore(cfg config, baseURLs map[Service]string) *core {
	c := &core{cfg: cfg, baseURLs: baseURLs, mfaGate: make(chan struct{}, 1), now: time.Now, sleep: sleep}
	c.password.Store(&cfg.password)
	return c
}

// sessionEffect is what an operation does to the session of the client.
type sessionEffect int

const (
	// sessionReplace adopts the token pair the response carries, as after a password change.
	sessionReplace sessionEffect = iota + 1
	// sessionEnd forgets the session after the call, whatever its outcome, as after a logout.
	sessionEnd
)

// operation describes a call. Generated methods fill it in from their arguments.
type operation struct {
	service Service
	method  string
	// path is absolute from the host of service. Its {name} placeholders are replaced in turn
	// by pathParams, escaped.
	path       string
	pathParams []string
	public     bool
	// params is a struct whose fields tagged "query" and "header" become query parameters and
	// headers.
	params any
	body   encoder
	// envelope is set for responses wrapped in {"retCode", "retMsg", "result"}.
	envelope bool
	// mfaOperation names the operation an operation MFA token is needed for, if any.
	mfaOperation string
	session      sessionEffect
}

// An encoder produces a request body and its content type.
type encoder func() (contentType string, data []byte, err error)

func jsonBody(v any) encoder {
	return func() (string, []byte, error) {
		data, err := json.Marshal(v)
		return "application/json", data, err
	}
}

func formBody(v any) encoder {
	return func() (string, []byte, error) {
		values, err := encodeValues(v, "form")
		return "application/x-www-form-urlencoded", []byte(values.Encode()), err
	}
}

// anyBody encodes the body given to [Client.Request].
func anyBody(body any) encoder {
	switch b := body.(type) {
	case nil:
		return nil
	case url.Values:
		return func() (string, []byte, error) {
			return "application/x-www-form-urlencoded", []byte(b.Encode()), nil
		}
	case []byte:
		return func() (string, []byte, error) {
			return "application/octet-stream", b, nil
		}
	case io.Reader:
		return func() (string, []byte, error) {
			data, err := io.ReadAll(b)
			return "application/octet-stream", data, err
		}
	}
	return jsonBody(body)
}

// prepared is an operation encoded once and ready to be sent any number of times.
type prepared struct {
	method     string
	url        string
	header     http.Header
	body       []byte
	public     bool
	timeout    time.Duration
	maxRetries int
	idempotent bool
	envelope   bool
	// token, if set, is the access token to send, as is: it is not renewed.
	token string
}

func (c *core) prepare(op *operation, accept string, opts []RequestOption) (*prepared, error) {
	call := callConfig{
		header:     http.Header{},
		query:      url.Values{},
		timeout:    c.cfg.timeout,
		maxRetries: c.cfg.maxRetries,
		idempotent: idempotentMethods[op.method],
		public:     op.public,
		envelope:   op.envelope,
	}
	for _, opt := range opts {
		opt(&call)
	}
	base, ok := c.baseURLs[op.service]
	if !ok {
		return nil, fmt.Errorf("whitebird: unknown service %q; set its base URL with WithBaseURL", op.service)
	}
	path, err := expandPath(op.path, op.pathParams)
	if err != nil {
		return nil, err
	}
	query, err := encodeValues(op.params, "query")
	if err != nil {
		return nil, err
	}
	headers, err := encodeValues(op.params, "header")
	if err != nil {
		return nil, err
	}
	p := &prepared{
		method:     op.method,
		url:        base + path,
		header:     http.Header{"Accept": {accept}},
		public:     call.public,
		timeout:    call.timeout,
		maxRetries: call.maxRetries,
		idempotent: call.idempotent,
		envelope:   call.envelope,
	}
	for key, values := range call.query {
		query[key] = append(query[key], values...)
	}
	if len(query) > 0 {
		p.url += "?" + query.Encode()
	}
	for key, values := range headers {
		for _, value := range values {
			p.header.Add(key, value)
		}
	}
	for key, values := range call.header {
		p.header[key] = values
	}
	if op.body != nil {
		contentType, data, err := op.body()
		if err != nil {
			return nil, fmt.Errorf("whitebird: encode request body: %w", err)
		}
		p.header.Set("Content-Type", contentType)
		p.body = data
	}
	return p, nil
}

// expandPath replaces the {name} placeholders of path with params, escaped. Values that would
// change the meaning of the path, such as ".." or the empty string, are refused.
func expandPath(path string, params []string) (string, error) {
	if len(params) == 0 {
		return path, nil
	}
	var b strings.Builder
	for _, value := range params {
		start := strings.IndexByte(path, '{')
		end := strings.IndexByte(path, '}')
		if start < 0 || end < start {
			return "", fmt.Errorf("whitebird: path %q has fewer parameters than given", path)
		}
		name := path[start+1 : end]
		if value == "" || value == "." || value == ".." {
			return "", fmt.Errorf("whitebird: invalid path parameter %s: %q", name, value)
		}
		b.WriteString(path[:start])
		b.WriteString(url.PathEscape(value))
		path = path[end+1:]
	}
	b.WriteString(path)
	return b.String(), nil
}

// send performs op and returns the response body, unwrapped from its envelope if any.
func (c *core) send(ctx context.Context, op *operation, accept string, opts []RequestOption) ([]byte, error) {
	p, err := c.prepare(op, accept, opts)
	if err != nil {
		return nil, err
	}
	if op.session == sessionEnd {
		return c.sendEnding(ctx, p)
	}
	generation := c.session.generation.Load()
	r, err := c.doConfirmed(ctx, op, p)
	if err != nil {
		return nil, err
	}
	body := r.body
	if p.envelope {
		if body, err = unwrapEnvelope(body); err != nil {
			return nil, err
		}
	}
	if op.session == sessionReplace {
		c.replaceSession(ctx, generation, p, body)
	}
	return body, nil
}

// sendEnding performs an operation that ends the session, such as a logout, then forgets the
// session whatever the outcome. It uses the token the client holds, renewed with the refresh
// token if needed, and sends nothing without one: the client does not sign in only to sign
// out.
func (c *core) sendEnding(ctx context.Context, p *prepared) ([]byte, error) {
	var body []byte
	var err error
	if p.public {
		var r *reply
		if r, err = c.do(ctx, p, false); err == nil {
			body = r.body
		}
	} else if token := c.endingToken(ctx); token != "" {
		p.token = token
		var r *reply
		if r, err = c.do(ctx, p, false); err == nil {
			body = r.body
		}
	}
	if endErr := c.endSession(context.WithoutCancel(ctx)); err == nil {
		err = endErr
	}
	if err == nil && p.envelope {
		body, err = unwrapEnvelope(body)
	}
	return body, err
}

// endingToken returns the access token of the current session, refreshed if it has expired,
// or "" if there is no session.
func (c *core) endingToken(ctx context.Context) string {
	token, err := c.currentToken(ctx)
	switch now := c.now(); {
	case err != nil:
		return ""
	case token.valid(now):
		return token.AccessToken
	case token.refreshable(now):
		if renewed, err := c.refreshGrant(ctx, token.RefreshToken); err == nil {
			return renewed.AccessToken
		}
	}
	return ""
}

// reply is a response whose body has been read, or, for event streams, is left open.
type reply struct {
	status int
	header http.Header
	body   []byte
	stream io.ReadCloser
}

// do sends p, refreshing the access token once on a 401 response and retrying requests on
// transient failures: idempotent requests on any of them, others only if they did not reach
// the server or were rate limited. It returns an [*APIError] for error responses. With stream
// set, a successful reply keeps its body open for the caller to read and close.
func (c *core) do(ctx context.Context, p *prepared, stream bool) (*reply, error) {
	retries, refreshed := 0, false
	for {
		token := p.token
		if token == "" && !p.public {
			var err error
			if token, err = c.accessToken(ctx); err != nil {
				return nil, err
			}
		}
		r, err := c.attempt(ctx, p, token, stream)
		if err == nil && r.status == http.StatusUnauthorized && token != "" && p.token == "" && !refreshed {
			refreshed = true
			if _, err := c.renew(ctx, token); err != nil {
				return nil, err
			}
			continue
		}
		if retries < p.maxRetries && retryable(ctx, p.idempotent, r, err) {
			if delay, ok := retryDelay(retries, r, c.now()); ok {
				if err := c.sleep(ctx, delay); err != nil {
					return nil, err
				}
				retries++
				continue
			}
		}
		switch {
		case err != nil:
			return nil, err
		case r.status >= http.StatusBadRequest:
			return nil, newAPIError(r)
		}
		return r, nil
	}
}

func (c *core) attempt(ctx context.Context, p *prepared, token string, stream bool) (*reply, error) {
	var cancel context.CancelFunc
	var timer *time.Timer
	switch {
	case p.timeout <= 0:
		ctx, cancel = context.WithCancel(ctx)
	case stream:
		ctx, cancel = context.WithCancel(ctx)
		timer = time.AfterFunc(p.timeout, cancel)
	default:
		ctx, cancel = context.WithTimeout(ctx, p.timeout)
	}
	req, err := http.NewRequestWithContext(ctx, p.method, p.url, bytes.NewReader(p.body))
	if err != nil {
		cancel()
		return nil, err
	}
	req.Header = p.header.Clone()
	req.Header.Set("User-Agent", c.cfg.userAgent)
	if c.cfg.language != "" && req.Header.Get("Accept-Language") == "" {
		req.Header.Set("Accept-Language", c.cfg.language)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if mfa := c.mfaToken.Load(); mfa != nil {
		req.Header.Set("X-Mfa-Token", *mfa)
	}
	resp, err := c.cfg.httpClient.Do(req)
	if err != nil {
		cancel()
		return nil, &TransportError{Method: p.method, URL: p.url, Err: err}
	}
	r := &reply{status: resp.StatusCode, header: resp.Header}
	if stream && resp.StatusCode < http.StatusMultipleChoices {
		if timer != nil {
			timer.Stop()
		}
		r.stream = &cancelOnClose{ReadCloser: resp.Body, cancel: cancel}
		return r, nil
	}
	defer cancel()
	defer resp.Body.Close()
	if r.body, err = io.ReadAll(resp.Body); err != nil {
		return nil, &TransportError{Method: p.method, URL: p.url, Err: err}
	}
	return r, nil
}

// cancelOnClose releases the context of a streamed response when its body is closed.
type cancelOnClose struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (b *cancelOnClose) Close() error {
	err := b.ReadCloser.Close()
	b.cancel()
	return err
}

// unwrapEnvelope returns the result of a response wrapped in {"retCode", "retMsg", "result"}.
// A retCode (or code) other than 0 and 200 is an error. A response without a result is
// returned whole.
func unwrapEnvelope(body []byte) ([]byte, error) {
	var envelope struct {
		RetCode json.RawMessage `json:"retCode"`
		Code    json.RawMessage `json:"code"`
		RetMsg  string          `json:"retMsg"`
		Message string          `json:"message"`
		Result  json.RawMessage `json:"result"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		return body, nil //nolint:nilerr // Not an envelope: the body is the result.
	}
	raw := envelope.RetCode
	if isNull(raw) {
		raw = envelope.Code
	}
	if !isNull(raw) {
		var code json.Number
		if json.Unmarshal(raw, &code) != nil || code != "0" && code != "200" {
			n, _ := strconv.Atoi(code.String())
			return nil, &APIError{
				StatusCode: http.StatusOK,
				Code:       n,
				Message:    cmp.Or(envelope.RetMsg, envelope.Message),
				Body:       body,
			}
		}
	}
	if isNull(envelope.Result) {
		return body, nil
	}
	return envelope.Result, nil
}

func isNull(raw json.RawMessage) bool {
	return len(raw) == 0 || bytes.Equal(raw, []byte("null"))
}

// decode unmarshals a JSON response into v, keeping numbers in untyped values as json.Number
// so that they lose no precision. An empty body leaves v unchanged.
func decode(data []byte, v any) error {
	if len(bytes.TrimSpace(data)) == 0 {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("whitebird: decode response: %w", err)
	}
	return nil
}

// call performs a JSON operation and decodes its result.
func call[T any](ctx context.Context, c *core, op *operation, opts []RequestOption) (T, error) {
	var out T
	body, err := c.send(ctx, op, "application/json", opts)
	if err != nil {
		return out, err
	}
	if err = decodeResult(body, &out); err != nil {
		return out, err
	}
	// An empty or null body gives an empty result rather than a nil pointer.
	if v := reflect.ValueOf(&out).Elem(); v.Kind() == reflect.Pointer && v.IsNil() {
		v.Set(reflect.New(v.Type().Elem()))
	}
	return out, nil
}

// decodeResult decodes a response into v. Untyped and string results accept a body that is not
// JSON: some endpoints documented as JSON answer plain text.
func decodeResult(data []byte, v any) error {
	switch out := v.(type) {
	case *string:
		if err := decode(data, out); err != nil {
			*out = string(data)
		}
		return nil
	case *json.RawMessage:
		trimmed := bytes.TrimSpace(data)
		switch {
		case len(trimmed) == 0:
			*out = nil
		case json.Valid(trimmed):
			*out = trimmed
		default:
			*out, _ = json.Marshal(string(data))
		}
		return nil
	}
	return decode(data, v)
}

// exec performs an operation that answers with no content.
func (c *core) exec(ctx context.Context, op *operation, opts []RequestOption) error {
	_, err := c.send(ctx, op, "application/json", opts)
	return err
}

// binary performs an operation that answers with a file.
func (c *core) binary(ctx context.Context, op *operation, opts []RequestOption) ([]byte, error) {
	return c.send(ctx, op, "*/*", opts)
}

// text performs an operation that answers with plain text.
func (c *core) text(ctx context.Context, op *operation, opts []RequestOption) (string, error) {
	body, err := c.send(ctx, op, "text/plain, */*", opts)
	return string(body), err
}
