package whitebird

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
)

// confirmOperation exchanges a TOTP code, from the code provider if code is empty, for an
// operation MFA token and sets it on the client.
func (c *core) confirmOperation(ctx context.Context, name, code string) (string, error) {
	code = strings.TrimSpace(code)
	if code == "" {
		if c.cfg.codes == nil {
			return "", fmt.Errorf("%w: confirming %s needs a TOTP code; pass one or set a code provider with WithCodeProvider", ErrMFARequired, name)
		}
		var err error
		if code, err = c.code(ctx); err != nil {
			return "", err
		}
	}
	op := &operation{
		service: operationMFAService,
		method:  http.MethodPost,
		path:    operationMFAPath,
		body:    jsonBody(map[string]string{"operation": name, "mfaCode": code}),
	}
	p, err := c.prepare(op, "application/json", nil)
	if err != nil {
		return "", err
	}
	r, err := c.do(ctx, p, false)
	if err != nil {
		return "", err
	}
	token := mfaToken(r)
	if token == "" {
		return "", fmt.Errorf("whitebird: confirm %s: no MFA token in the response", name)
	}
	c.mfaToken.Store(&token)
	return token, nil
}

// mfaToken finds the operation MFA token in a response: in the mfaToken field of the body, as
// the body itself, or in the x-mfa-token header.
func mfaToken(r *reply) string {
	var fields struct {
		MFAToken string `json:"mfaToken"`
	}
	if json.Unmarshal(r.body, &fields) == nil && strings.TrimSpace(fields.MFAToken) != "" {
		return strings.TrimSpace(fields.MFAToken)
	}
	var s string
	if json.Unmarshal(r.body, &s) == nil && strings.TrimSpace(s) != "" {
		return strings.TrimSpace(s)
	}
	if body := bytes.TrimSpace(r.body); len(body) > 0 && !json.Valid(body) {
		return string(body)
	}
	return strings.TrimSpace(r.header.Get("X-Mfa-Token"))
}

// doConfirmed sends p for op. If the API refuses an operation that needs an operation MFA
// token, and a code provider is set, it gets a token and sends p once more, as the web
// application asks for a code only when the server wants one.
func (c *core) doConfirmed(ctx context.Context, op *operation, p *prepared) (*reply, error) {
	sent := c.mfaToken.Load()
	r, err := c.do(ctx, p, false)
	if op.mfaOperation == "" || c.cfg.codes == nil || !errors.Is(err, ErrMFARequired) {
		return r, err
	}
	if err := c.reconfirm(ctx, op.mfaOperation, sent); err != nil {
		return nil, err
	}
	return c.do(ctx, p, false)
}

// reconfirm replaces the operation MFA token sent, refused by the API. Concurrent operations
// refused together share one confirmation, so that the code is asked for once.
func (c *core) reconfirm(ctx context.Context, name string, sent *string) error {
	select {
	case c.mfaGate <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	}
	defer func() { <-c.mfaGate }()
	if current := c.mfaToken.Load(); current != nil && (sent == nil || *current != *sent) {
		// Another operation got a new token while this one waited.
		return nil
	}
	_, err := c.confirmOperation(ctx, name, "")
	return err
}
