package whitebird

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// mfaCheckPath answers whether an account has two-factor authentication enabled. It lives on
// the same service as the token endpoint.
const mfaCheckPath = "/api/v1/client/mfa"

const mfaHint = "a TOTP code is required; set a code provider with WithCodeProvider"

var errNoCredentials = fmt.Errorf("%w: no valid token and no credentials; use WithCredentials", ErrAuthentication)

// session is the token state of a client. Concurrent callers that find the token stale share a
// single refresh, run by the first of them.
type session struct {
	mu     sync.Mutex
	token  *Token
	loaded bool
	flight *flight
}

type flight struct {
	done  chan struct{}
	token *Token
	err   error
}

// accessToken returns a usable access token, refreshing or obtaining one if needed.
func (c *core) accessToken(ctx context.Context) (string, error) {
	token, err := c.currentToken(ctx)
	if err != nil {
		return "", err
	}
	if !token.fresh(c.now()) {
		stale := ""
		if token != nil {
			stale = token.AccessToken
		}
		if token, err = c.renew(ctx, stale); err != nil {
			return "", err
		}
	}
	return token.AccessToken, nil
}

func (c *core) currentToken(ctx context.Context) (*Token, error) {
	s := &c.session
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.loaded && c.cfg.tokens != nil {
		token, err := c.cfg.tokens.Load(ctx)
		if err != nil {
			return nil, fmt.Errorf("whitebird: load token: %w", err)
		}
		if token != nil && token.Expiry.IsZero() {
			token = &Token{AccessToken: token.AccessToken, RefreshToken: token.RefreshToken, Expiry: jwtExpiry(token.AccessToken)}
		}
		s.token = token
	}
	s.loaded = true
	return s.token, nil
}

// renew replaces the access token stale, which is about to expire or was rejected. If another
// caller has replaced it in the meantime, renew returns the new token.
func (c *core) renew(ctx context.Context, stale string) (*Token, error) {
	s := &c.session
	s.mu.Lock()
	if s.token.fresh(c.now()) && s.token.AccessToken != stale {
		token := s.token
		s.mu.Unlock()
		return token, nil
	}
	f := s.flight
	if f == nil {
		f = &flight{done: make(chan struct{})}
		s.flight = f
		// The refresh outlives the caller that started it, since others may be waiting for it.
		go c.refresh(context.WithoutCancel(ctx), f, s.token)
	}
	s.mu.Unlock()
	select {
	case <-f.done:
		return f.token, f.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (c *core) refresh(ctx context.Context, f *flight, current *Token) {
	token, err := c.obtain(ctx, current)
	if err == nil && c.cfg.tokens != nil {
		if err = c.cfg.tokens.Save(ctx, token); err != nil {
			err = fmt.Errorf("whitebird: save token: %w", err)
		}
	}
	s := &c.session
	s.mu.Lock()
	if token != nil {
		s.token = token
	}
	s.flight = nil
	s.mu.Unlock()
	f.token, f.err = token, err
	close(f.done)
}

// obtain gets a new token with the refresh token, falling back to the password.
func (c *core) obtain(ctx context.Context, current *Token) (*Token, error) {
	if current != nil && current.RefreshToken != "" {
		token, err := c.grant(ctx, url.Values{
			"grant_type":    {"refresh_token"},
			"refresh_token": {current.RefreshToken},
		})
		var apiErr *APIError
		if err == nil || c.cfg.password == "" || !errors.As(err, &apiErr) {
			return token, err
		}
	}
	if c.cfg.email == "" || c.cfg.password == "" {
		return nil, errNoCredentials
	}
	return c.login(ctx)
}

func (c *core) login(ctx context.Context) (*Token, error) {
	form := url.Values{
		"grant_type": {"password"},
		"username":   {strings.ToLower(c.cfg.email)},
		"password":   {c.cfg.password},
	}
	mfaOff := false
	if c.cfg.codes != nil {
		code, err := c.cfg.codes.Code(ctx)
		if err != nil {
			return nil, fmt.Errorf("whitebird: get TOTP code: %w", err)
		}
		form.Set("code", code)
	} else {
		// Without a code provider, find out up front whether a code is needed so that the
		// error says so instead of reporting bad credentials.
		enabled, err := c.mfaEnabled(ctx)
		if err == nil && enabled {
			return nil, &APIError{ErrorCode: errorCodeMFARequired, Message: mfaHint}
		}
		mfaOff = err == nil
	}
	token, err := c.grant(ctx, form)
	var apiErr *APIError
	if errors.As(err, &apiErr) && apiErr.ErrorCode == "invalid_grant" && !form.Has("code") && !mfaOff {
		apiErr.ErrorCode = errorCodeMFARequired
		apiErr.Message = strings.TrimSuffix(mfaHint+": "+apiErr.Message, ": ")
	}
	return token, err
}

// grant posts form to the token endpoint.
func (c *core) grant(ctx context.Context, form url.Values) (*Token, error) {
	p := &prepared{
		method: http.MethodPost,
		url:    c.baseURLs[tokenService] + tokenPath,
		header: http.Header{
			"Accept":        {"application/json"},
			"Authorization": {"Basic " + clientCredentials},
			"Content-Type":  {"application/x-www-form-urlencoded"},
		},
		body:    []byte(form.Encode()),
		public:  true,
		timeout: c.cfg.timeout,
	}
	r, err := c.do(ctx, p, false)
	if err != nil {
		return nil, err
	}
	var resp struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int64  `json:"expires_in"`
		MFAEnabled   bool   `json:"mfaEnabled"`
	}
	if err := json.Unmarshal(r.body, &resp); err != nil {
		return nil, fmt.Errorf("whitebird: decode token response: %w", err)
	}
	if resp.AccessToken == "" {
		e := &APIError{StatusCode: r.status, ErrorCode: "invalid_grant", Message: "no access token issued", Body: r.body}
		switch {
		case resp.MFAEnabled && form.Has("code"):
			e.Message = "invalid TOTP code"
		case resp.MFAEnabled:
			e.ErrorCode, e.Message = errorCodeMFARequired, mfaHint
		}
		return nil, e
	}
	token := &Token{
		AccessToken:  resp.AccessToken,
		RefreshToken: resp.RefreshToken,
		Expiry:       jwtExpiry(resp.AccessToken),
	}
	if token.Expiry.IsZero() && resp.ExpiresIn > 0 {
		token.Expiry = c.now().Add(time.Duration(resp.ExpiresIn) * time.Second)
	}
	return token, nil
}

func (c *core) mfaEnabled(ctx context.Context) (bool, error) {
	payload, err := json.Marshal(map[string]string{"username": strings.ToLower(c.cfg.email)})
	if err != nil {
		return false, err
	}
	p := &prepared{
		method: http.MethodPost,
		url:    c.baseURLs[tokenService] + mfaCheckPath,
		header: http.Header{
			"Accept":       {"application/json"},
			"Content-Type": {"application/json"},
		},
		body:    payload,
		public:  true,
		timeout: c.cfg.timeout,
	}
	r, err := c.do(ctx, p, false)
	if err != nil {
		return false, err
	}
	var resp struct {
		MFAEnabled bool `json:"mfaEnabled"`
	}
	err = json.Unmarshal(r.body, &resp)
	return resp.MFAEnabled, err
}
