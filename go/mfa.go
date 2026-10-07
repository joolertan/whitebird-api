package whitebird

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
)

// confirmOperation exchanges a TOTP code, from the code provider if code is empty, for an
// operation MFA token and sets it on the client.
func (c *core) confirmOperation(ctx context.Context, name, code string) (string, error) {
	if code == "" {
		if c.cfg.codes == nil {
			return "", fmt.Errorf("%w: confirming %s needs a TOTP code; pass one or set a code provider with WithCodeProvider", ErrMFARequired, name)
		}
		var err error
		if code, err = c.cfg.codes.Code(ctx); err != nil {
			return "", fmt.Errorf("whitebird: get TOTP code: %w", err)
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
	if json.Unmarshal(r.body, &fields) == nil && fields.MFAToken != "" {
		return fields.MFAToken
	}
	var s string
	if json.Unmarshal(r.body, &s) == nil && s != "" {
		return s
	}
	if body := bytes.TrimSpace(r.body); len(body) > 0 && !json.Valid(body) {
		return string(body)
	}
	return r.header.Get("X-Mfa-Token")
}

// doConfirmed sends p for op. If op needs an operation MFA token and a code provider is set, it
// gets a token first when the client has none, and a new one if the API refuses the request for
// lack of a valid token, then sends p again once.
func (c *core) doConfirmed(ctx context.Context, op *operation, p *prepared) (*reply, error) {
	confirm := op.mfaOperation != "" && c.cfg.codes != nil
	if confirm && c.mfaToken.Load() == nil {
		if _, err := c.confirmOperation(ctx, op.mfaOperation, ""); err != nil {
			return nil, err
		}
	}
	r, err := c.do(ctx, p, false)
	if confirm && errors.Is(err, ErrMFARequired) {
		if _, err := c.confirmOperation(ctx, op.mfaOperation, ""); err != nil {
			return nil, err
		}
		return c.do(ctx, p, false)
	}
	return r, err
}
