package whitebird

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync/atomic"
	"time"
)

// core holds what every resource shares: configuration, the session and the MFA token.
type core struct {
	cfg      config
	baseURLs map[Service]string
	session  session
	mfaToken atomic.Pointer[string]
	now      func() time.Time
	sleep    func(ctx context.Context, d time.Duration) error
}

// operation describes a call. Generated methods fill it in from their arguments.
type operation struct {
	service Service
	method  string
	path    string
	public  bool
	// params is a struct whose fields tagged "query" and "header" become query parameters and
	// headers.
	params any
	body   encoder
	// envelope is set for responses wrapped in {"retCode", "retMsg", "result"}.
	envelope bool
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
}

func (c *core) prepare(op *operation, accept string, opts []RequestOption) (*prepared, error) {
	call := callConfig{
		header:     http.Header{},
		query:      url.Values{},
		timeout:    c.cfg.timeout,
		maxRetries: c.cfg.maxRetries,
		idempotent: idempotentMethods[op.method],
		public:     op.public,
	}
	for _, opt := range opts {
		opt(&call)
	}
	base, ok := c.baseURLs[op.service]
	if !ok {
		return nil, fmt.Errorf("whitebird: unknown service %q; set its base URL with WithBaseURL", op.service)
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
		url:        base + op.path,
		header:     http.Header{"Accept": {accept}},
		public:     call.public,
		timeout:    call.timeout,
		maxRetries: call.maxRetries,
		idempotent: call.idempotent,
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

// send performs op and returns the response body, unwrapped from its envelope if any.
func (c *core) send(ctx context.Context, op *operation, accept string, opts []RequestOption) ([]byte, error) {
	p, err := c.prepare(op, accept, opts)
	if err != nil {
		return nil, err
	}
	r, err := c.do(ctx, p, false)
	if err != nil {
		return nil, err
	}
	if op.envelope {
		return unwrapEnvelope(r.body)
	}
	return r.body, nil
}

// reply is a response whose body has been read, or, for event streams, is left open.
type reply struct {
	status int
	header http.Header
	body   []byte
	stream io.ReadCloser
}

// do sends p, refreshing the access token once on a 401 response and retrying idempotent
// requests on transient failures. It returns an [*APIError] for error responses. With stream
// set, a successful reply keeps its body open for the caller to read and close.
func (c *core) do(ctx context.Context, p *prepared, stream bool) (*reply, error) {
	retries, refreshed := 0, false
	for {
		token := ""
		if !p.public {
			var err error
			if token, err = c.accessToken(ctx); err != nil {
				return nil, err
			}
		}
		r, err := c.attempt(ctx, p, token, stream)
		if err == nil && r.status == http.StatusUnauthorized && token != "" && !refreshed {
			refreshed = true
			if _, err := c.renew(ctx, token); err != nil {
				return nil, err
			}
			continue
		}
		if p.idempotent && retries < p.maxRetries && retryable(ctx, r, err) {
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

func unwrapEnvelope(body []byte) ([]byte, error) {
	var envelope struct {
		RetCode *int            `json:"retCode"`
		RetMsg  string          `json:"retMsg"`
		Result  json.RawMessage `json:"result"`
	}
	if json.Unmarshal(body, &envelope) == nil && envelope.RetCode != nil {
		if *envelope.RetCode != 0 {
			return nil, &APIError{StatusCode: http.StatusOK, Code: *envelope.RetCode, Message: envelope.RetMsg, Body: body}
		}
		return envelope.Result, nil
	}
	return body, nil
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
	err = decode(body, &out)
	return out, err
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
