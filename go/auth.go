package whitebird

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const mfaHint = "a TOTP code is required; set a code provider with WithCodeProvider"

const noMFACaptchaHint = "the account has no two-factor authentication, so Whitebird asks for a captcha at sign-in: " +
	"configure WithCaptchaSolver or enable two-factor authentication"

const unknownUserHint = "no Whitebird account with this username"

// The messages the authorization server answers when it refuses a sign-in for a while.
const (
	messageUserBlocked  = "Username is blocked"
	messageManyRequests = "Too many requests"
)

var errNoCredentials = fmt.Errorf("%w: no valid token and no credentials; use WithCredentials", ErrAuthentication)

var errSessionEnded = fmt.Errorf("%w: the session ended while signing in", ErrAuthentication)

// session is the token state of a client. Concurrent callers that find the token stale share a
// single refresh, run by the first of them.
type session struct {
	mu     sync.Mutex
	token  *Token
	loaded bool
	flight *flight
	// generation changes when the session ends, so that a sign-in that was under way does not
	// bring it back.
	generation atomic.Uint64
	// storeMu orders the writes to the token store.
	storeMu sync.Mutex
}

// flight is a refresh or sign-in shared by the callers waiting for it. It is canceled when all
// of them have given up, so that a sign-in stuck on a code prompt does not block later calls.
type flight struct {
	done       chan struct{}
	cancel     context.CancelFunc
	waiters    int
	generation uint64
	token      *Token
	err        error
}

// accessToken returns a usable access token, refreshing or obtaining one if needed.
func (c *core) accessToken(ctx context.Context) (string, error) {
	token, err := c.currentToken(ctx)
	if err != nil {
		return "", err
	}
	now := c.now()
	if token.fresh(now) || token.valid(now) && !c.renewable(token) {
		// A token about to expire that cannot be renewed is used until it does.
		return token.AccessToken, nil
	}
	stale := ""
	if token != nil {
		stale = token.AccessToken
	}
	renewed, err := c.renew(ctx, stale)
	if err != nil {
		// A refresh ahead of time that failed for a passing reason, such as an unavailable
		// server, leaves the token in use until it expires.
		if token.valid(c.now()) && ctx.Err() == nil && !errors.Is(err, ErrAuthentication) {
			return token.AccessToken, nil
		}
		return "", err
	}
	return renewed.AccessToken, nil
}

// renewable reports whether the client can replace token, with its refresh token or by signing
// in again.
func (c *core) renewable(token *Token) bool {
	return token.refreshable(c.now()) || c.hasCredentials()
}

func (c *core) hasCredentials() bool {
	return c.cfg.email != "" && *c.password.Load() != ""
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
		s.token = token.withExpiry()
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
		if err := ctx.Err(); err != nil {
			s.mu.Unlock()
			return nil, err
		}
		// The refresh outlives the caller that started it, as long as someone waits for it.
		flightCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
		f = &flight{done: make(chan struct{}), cancel: cancel, generation: s.generation.Load()}
		s.flight = f
		go c.refresh(flightCtx, f, s.token)
	}
	f.waiters++
	s.mu.Unlock()
	select {
	case <-f.done:
		return f.token, f.err
	case <-ctx.Done():
		s.mu.Lock()
		f.waiters--
		if f.waiters == 0 && s.flight == f {
			// Nobody waits anymore: give up, so that the next call starts afresh.
			s.flight = nil
			f.cancel()
		}
		s.mu.Unlock()
		return nil, ctx.Err()
	}
}

func (c *core) refresh(ctx context.Context, f *flight, current *Token) {
	defer f.cancel()
	token, save, err := c.obtain(ctx, current)
	s := &c.session
	s.mu.Lock()
	if s.generation.Load() != f.generation {
		token, err = nil, errSessionEnded
	}
	if err == nil {
		s.token = token
	}
	if s.flight == f {
		s.flight = nil
	}
	s.mu.Unlock()
	if save {
		c.saveToken(ctx, token, f.generation)
	}
	f.token, f.err = token, err
	close(f.done)
}

// saveToken saves token in the token store unless the session it belongs to has ended. A
// failure to save does not fail the call that obtained the token: the client goes on with the
// token in memory.
func (c *core) saveToken(ctx context.Context, token *Token, generation uint64) {
	if c.cfg.tokens == nil || token == nil {
		return
	}
	s := &c.session
	s.storeMu.Lock()
	defer s.storeMu.Unlock()
	if s.generation.Load() == generation {
		_ = c.cfg.tokens.Save(ctx, token)
	}
}

// obtain gets a new token: the one in the token store if another process has renewed it, else
// with the refresh token, else with the password. It reports whether the token is new and must
// be saved.
func (c *core) obtain(ctx context.Context, current *Token) (*Token, bool, error) {
	now := c.now()
	if c.cfg.tokens != nil {
		if stored, err := c.cfg.tokens.Load(ctx); err == nil && stored != nil && (current == nil || stored.AccessToken != current.AccessToken) {
			stored = stored.withExpiry()
			if stored.fresh(now) {
				return stored, false, nil
			}
			current = stored
		}
	}
	if current.refreshable(now) {
		token, err := c.refreshGrant(ctx, current.RefreshToken)
		if err == nil {
			return token, true, nil
		}
		var apiErr *APIError
		if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusBadRequest && apiErr.StatusCode != http.StatusUnauthorized {
			return nil, false, err
		}
		// The refresh token is no longer valid: the session is over.
		c.forget(ctx)
		if !c.hasCredentials() {
			if !errors.Is(apiErr, ErrAuthentication) {
				apiErr.sentinel = ErrAuthentication
			}
			return nil, false, err
		}
	}
	if !c.hasCredentials() {
		return nil, false, errNoCredentials
	}
	token, err := c.login(ctx)
	return token, err == nil, err
}

func (c *core) refreshGrant(ctx context.Context, refreshToken string) (*Token, error) {
	reply, err := c.grant(ctx, url.Values{
		"grant_type":    {"refresh_token"},
		"refresh_token": {refreshToken},
	}, "")
	if err != nil {
		return nil, err
	}
	if reply.token == nil {
		return nil, reply.noToken("")
	}
	if reply.token.RefreshToken == "" {
		reply.token.RefreshToken = refreshToken
	}
	return reply.token, nil
}

// forget drops the tokens of a session that is no longer valid, in memory and in the token
// store, along with the operation MFA token.
func (c *core) forget(ctx context.Context) {
	s := &c.session
	s.mu.Lock()
	s.token, s.loaded = nil, true
	s.mu.Unlock()
	c.mfaToken.Store(nil)
	// The client forgets the session even if the store keeps it: it is invalid anyway.
	_ = c.clearStore(ctx)
}

func (c *core) clearStore(ctx context.Context) error {
	if c.cfg.tokens == nil {
		return nil
	}
	s := &c.session
	s.storeMu.Lock()
	defer s.storeMu.Unlock()
	return c.cfg.tokens.Clear(ctx)
}

// endSession forgets the session after a logout: a sign-in under way is abandoned and its
// result discarded.
func (c *core) endSession(ctx context.Context) error {
	s := &c.session
	s.mu.Lock()
	s.generation.Add(1)
	s.token, s.loaded = nil, true
	if f := s.flight; f != nil {
		s.flight = nil
		f.cancel()
	}
	s.mu.Unlock()
	c.mfaToken.Store(nil)
	if err := c.clearStore(ctx); err != nil {
		return fmt.Errorf("whitebird: clear token store: %w", err)
	}
	return nil
}

// replaceSession adopts the token pair answered by an operation that starts a new session,
// such as a password change, sent as p while the session was at generation. The new password
// of the request, if any, replaces the one of the credentials for later logins. Nothing changes
// if the session ended in the meantime.
func (c *core) replaceSession(ctx context.Context, generation uint64, p *prepared, body []byte) {
	reply, err := c.tokenReply(body)
	if err != nil || reply.token == nil {
		return
	}
	s := &c.session
	s.mu.Lock()
	if s.generation.Load() != generation {
		s.mu.Unlock()
		return
	}
	if reply.token.RefreshToken == "" && s.token != nil {
		reply.token.RefreshToken = s.token.RefreshToken
	}
	s.token, s.loaded = reply.token, true
	if password := newPassword(p); password != "" {
		c.password.Store(&password)
	}
	s.mu.Unlock()
	c.saveToken(ctx, reply.token, generation)
}

// newPassword returns the newPassword field of the body of p, a JSON object or a form.
func newPassword(p *prepared) string {
	mediaType, _, _ := mime.ParseMediaType(p.header.Get("Content-Type"))
	if mediaType == "application/x-www-form-urlencoded" {
		values, _ := url.ParseQuery(string(p.body))
		return values.Get("newPassword")
	}
	var fields struct {
		NewPassword string `json:"newPassword"`
	}
	if isJSON(mediaType) && json.Unmarshal(p.body, &fields) == nil {
		return fields.NewPassword
	}
	return ""
}

// login signs in with the password, as the web application does: it asks whether the account
// has two-factor authentication, then sends the password alone, adding a TOTP code if the
// server asks for one, or along with the answer to a captcha.
func (c *core) login(ctx context.Context) (*Token, error) {
	twoFactor, err := c.mfaEnabled(ctx)
	if err != nil {
		return nil, err
	}
	if twoFactor {
		return c.passwordGrant(ctx, "")
	}
	if c.cfg.captcha == nil {
		return nil, &APIError{Message: noMFACaptchaHint, sentinel: ErrCaptchaRequired}
	}
	for attempt := 1; ; attempt++ {
		answer, err := c.solveCaptcha(ctx)
		if err != nil {
			return nil, err
		}
		token, err := c.passwordGrant(ctx, answer)
		var apiErr *APIError
		if errors.As(err, &apiErr) && strings.EqualFold(apiErr.ErrorCode, "INVALID_CAPTCHA") && attempt < captchaAttempts {
			continue
		}
		return token, err
	}
}

// passwordGrant sends the password, with the answer to a captcha if not empty, and the TOTP
// code if the server asks for it.
func (c *core) passwordGrant(ctx context.Context, captcha string) (*Token, error) {
	reply, err := c.grant(ctx, c.passwordForm(""), captcha)
	if err != nil {
		return nil, loginError(err, captcha != "")
	}
	if reply.token != nil {
		return reply.token, nil
	}
	if !reply.mfa {
		return nil, reply.noToken("")
	}
	if c.cfg.codes == nil {
		return nil, &APIError{ErrorCode: errorCodeMFARequired, Message: mfaHint}
	}
	code, err := c.code(ctx)
	if err != nil {
		return nil, err
	}
	if reply, err = c.grant(ctx, c.passwordForm(code), ""); err != nil {
		return nil, loginError(err, false)
	}
	if reply.token == nil {
		return nil, reply.noToken("invalid TOTP code")
	}
	return reply.token, nil
}

// code asks the code provider for a TOTP code.
func (c *core) code(ctx context.Context) (string, error) {
	code, err := c.cfg.codes.Code(ctx)
	if err != nil {
		return "", fmt.Errorf("whitebird: get TOTP code: %w", err)
	}
	if code = strings.TrimSpace(code); code == "" {
		return "", errors.New("whitebird: get TOTP code: the code provider returned no code")
	}
	return code, nil
}

func (c *core) passwordForm(code string) url.Values {
	form := url.Values{
		"grant_type": {"password"},
		"username":   {strings.ToLower(c.cfg.email)},
		"password":   {*c.password.Load()},
	}
	if code != "" {
		form.Set("code", code)
	}
	return form
}

// loginError classifies an error answered to a password grant. Wrong credentials and wrong
// codes are authentication errors whatever their status, and a request for a captcha that was
// not sent is [ErrCaptchaRequired].
func loginError(err error, sentCaptcha bool) error {
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode >= http.StatusInternalServerError {
		return err
	}
	switch {
	case strings.EqualFold(apiErr.Message, messageManyRequests):
		apiErr.sentinel = ErrRateLimited
	case strings.EqualFold(apiErr.Message, messageUserBlocked):
		apiErr.sentinel = ErrPermissionDenied
	case !sentCaptcha && strings.Contains(strings.ToUpper(apiErr.ErrorCode), "CAPTCHA"):
		apiErr.sentinel = ErrCaptchaRequired
	case apiErr.StatusCode == http.StatusBadRequest || apiErr.StatusCode == http.StatusUnauthorized ||
		apiErr.ErrorCode == "invalid_grant":
		apiErr.sentinel = ErrAuthentication
	}
	return err
}

// grantReply is a successful answer of the token endpoint: a token, or a request for a TOTP
// code.
type grantReply struct {
	status int
	body   []byte
	token  *Token
	mfa    bool
}

// noToken returns the error for a reply without a token.
func (r *grantReply) noToken(message string) error {
	return &APIError{
		StatusCode: r.status,
		ErrorCode:  "invalid_grant",
		Message:    cmp.Or(message, "no access token issued"),
		Body:       r.body,
		sentinel:   ErrAuthentication,
	}
}

// grant posts form to the token endpoint, with the answer to a captcha if not empty.
func (c *core) grant(ctx context.Context, form url.Values, captcha string) (*grantReply, error) {
	p := &prepared{
		method: http.MethodPost,
		url:    c.baseURLs[tokenService] + tokenPath,
		header: http.Header{
			"Accept":        {"application/json"},
			"Authorization": {"Basic " + clientCredentials},
			"Content-Type":  {"application/x-www-form-urlencoded"},
		},
		body:       []byte(form.Encode()),
		public:     true,
		timeout:    c.cfg.timeout,
		maxRetries: c.cfg.maxRetries,
	}
	if captcha != "" {
		p.header.Set("Captcha", captcha)
	}
	r, err := c.do(ctx, p, false)
	if err != nil {
		return nil, err
	}
	reply, err := c.tokenReply(r.body)
	if err != nil {
		return nil, err
	}
	reply.status = r.status
	return reply, nil
}

// tokenReply decodes the token pair of a token endpoint response.
func (c *core) tokenReply(body []byte) (*grantReply, error) {
	var resp struct {
		AccessToken  string          `json:"access_token"`
		RefreshToken string          `json:"refresh_token"`
		ExpiresIn    json.Number     `json:"expires_in"`
		MFAEnabled   json.RawMessage `json:"mfaEnabled"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, fmt.Errorf("whitebird: decode token response: %w", err)
	}
	reply := &grantReply{body: body, mfa: truthy(resp.MFAEnabled)}
	if access := strings.TrimSpace(resp.AccessToken); access != "" {
		token := &Token{
			AccessToken:  access,
			RefreshToken: strings.TrimSpace(resp.RefreshToken),
			Expiry:       jwtExpiry(access),
		}
		if seconds, err := resp.ExpiresIn.Int64(); err == nil && token.Expiry.IsZero() && seconds > 0 && seconds < 1<<31 {
			token.Expiry = c.now().Add(time.Duration(seconds) * time.Second)
		}
		reply.token = token
	}
	return reply, nil
}

// truthy tells whether a JSON value is true in JavaScript, as the web application reads the
// flags the server answers.
func truthy(raw json.RawMessage) bool {
	switch s := strings.TrimSpace(string(raw)); {
	case s == "" || s == "null" || s == "false" || s == `""`:
		return false
	case s[0] == '-' || s[0] >= '0' && s[0] <= '9':
		f, err := strconv.ParseFloat(s, 64)
		return err != nil || f != 0
	}
	return true
}

// mfaEnabled asks whether the account has two-factor authentication. If the server cannot
// tell, the password is not sent at all.
func (c *core) mfaEnabled(ctx context.Context) (bool, error) {
	payload, err := json.Marshal(map[string]string{"username": c.cfg.email})
	if err != nil {
		return false, err
	}
	p := &prepared{
		method: http.MethodPost,
		url:    c.baseURLs[tokenService] + mfaStatusPath,
		header: http.Header{
			"Accept":       {"application/json"},
			"Content-Type": {"application/json"},
		},
		body:       payload,
		public:     true,
		timeout:    c.cfg.timeout,
		maxRetries: c.cfg.maxRetries,
	}
	r, err := c.do(ctx, p, false)
	var apiErr *APIError
	if errors.As(err, &apiErr) && (apiErr.StatusCode == http.StatusNotFound || apiErr.ErrorCode == "NOT_FOUND") {
		apiErr.sentinel = ErrAuthentication
		apiErr.Message = withHint(unknownUserHint, apiErr.Message)
	}
	if err != nil {
		return false, err
	}
	var resp struct {
		MFAEnabled json.RawMessage `json:"mfaEnabled"`
	}
	if err := json.Unmarshal(r.body, &resp); err != nil {
		return false, fmt.Errorf("whitebird: decode two-factor status: %w", err)
	}
	return truthy(resp.MFAEnabled), nil
}

func withHint(hint, message string) string {
	if message == "" {
		return hint
	}
	return hint + ": " + message
}
