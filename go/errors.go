package whitebird

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Sentinel errors classify an [*APIError]; test for them with [errors.Is]. An error matches its
// own class and the class above it: [ErrVerificationRequired] is also an [ErrBadRequest], and
// [ErrMFARequired] and [ErrCaptchaRequired] are also an [ErrAuthentication].
var (
	ErrBadRequest           = errors.New("whitebird: bad request")
	ErrVerificationRequired = errors.New("whitebird: verification required")
	ErrAuthentication       = errors.New("whitebird: authentication failed")
	ErrMFARequired          = errors.New("whitebird: MFA required")
	ErrCaptchaRequired      = errors.New("whitebird: captcha required")
	ErrPermissionDenied     = errors.New("whitebird: permission denied")
	ErrNotFound             = errors.New("whitebird: not found")
	ErrConflict             = errors.New("whitebird: conflict")
	ErrRateLimited          = errors.New("whitebird: rate limited")
	ErrServer               = errors.New("whitebird: server error")
)

// APIError is an error answered by the API.
//
// Services answer errors as {"code", "status", "subStatus", "message", "fields"}, the
// authorization server as {"error", "error_description"}, and market data with a non-zero
// retCode in an HTTP 200 response.
type APIError struct {
	// StatusCode is the HTTP status code.
	StatusCode int
	// Code is the numeric error code, or the retCode of a market data response.
	Code int
	// ErrorCode is the machine-readable error, such as "SMS_VERIFICATION_REQUIRED" or
	// "invalid_grant".
	ErrorCode string
	// SubStatus refines ErrorCode for some errors.
	SubStatus string
	// Message is the human-readable description.
	Message string
	// Fields holds the per-field validation errors as sent by the server.
	Fields json.RawMessage
	// RetryAfter is the delay requested by a 429 or 503 response, if any.
	RetryAfter time.Duration
	// Body is the raw response body.
	Body []byte

	// sentinel is the class of an error that the client recognizes from the context of the
	// request rather than from the response alone.
	sentinel error
}

func (e *APIError) Error() string {
	var b strings.Builder
	b.WriteString("whitebird: ")
	if e.StatusCode >= 400 {
		fmt.Fprintf(&b, "%d %s", e.StatusCode, http.StatusText(e.StatusCode))
	} else {
		b.WriteString("API error")
	}
	var details []string
	if e.ErrorCode != "" {
		details = append(details, e.ErrorCode)
	}
	if e.SubStatus != "" {
		details = append(details, e.SubStatus)
	}
	if e.Code != 0 {
		details = append(details, "code "+strconv.Itoa(e.Code))
	}
	if len(details) > 0 {
		fmt.Fprintf(&b, " (%s)", strings.Join(details, ", "))
	}
	if e.Message != "" {
		b.WriteString(": ")
		b.WriteString(e.Message)
	}
	return b.String()
}

// Is reports whether e belongs to the class of the sentinel target.
func (e *APIError) Is(target error) bool {
	class := e.class()
	return class != nil && (target == class || target == parentClass[class])
}

var parentClass = map[error]error{
	ErrVerificationRequired: ErrBadRequest,
	ErrMFARequired:          ErrAuthentication,
	ErrCaptchaRequired:      ErrAuthentication,
}

func (e *APIError) class() error {
	switch {
	case e.sentinel != nil:
		return e.sentinel
	case e.ErrorCode == "SMS_VERIFICATION_REQUIRED" || e.ErrorCode == "LIVENESS_VERIFICATION_REQUIRED":
		return ErrVerificationRequired
	case e.ErrorCode == "MFA_CHECKING_REQUIRED" || e.ErrorCode == errorCodeMFARequired ||
		strings.Contains(strings.ToLower(e.Message), "requires mfa checking"):
		return ErrMFARequired
	case e.StatusCode == http.StatusUnauthorized || e.ErrorCode == "invalid_grant":
		return ErrAuthentication
	case e.StatusCode == http.StatusBadRequest:
		return ErrBadRequest
	case e.StatusCode == http.StatusForbidden:
		return ErrPermissionDenied
	case e.StatusCode == http.StatusNotFound:
		return ErrNotFound
	case e.StatusCode == http.StatusConflict:
		return ErrConflict
	case e.StatusCode == http.StatusTooManyRequests:
		return ErrRateLimited
	case e.StatusCode >= 500:
		return ErrServer
	}
	return nil
}

// errorCodeMFARequired marks a login refused because a TOTP code is needed. The authorization
// server has no dedicated error for it, so the client uses the name common to OAuth servers.
const errorCodeMFARequired = "mfa_required"

func newAPIError(r *reply) *APIError {
	e := &APIError{StatusCode: r.status, Body: r.body}
	if r.status == http.StatusTooManyRequests || r.status == http.StatusServiceUnavailable {
		e.RetryAfter, _ = retryAfter(r.header, time.Now())
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(r.body, &fields); err != nil {
		if text := strings.TrimSpace(string(r.body)); len(text) <= 200 && !strings.HasPrefix(text, "<") {
			e.Message = text
		}
		return e
	}
	e.Code = jsonInt(fields["code"])
	e.ErrorCode = firstString(fields, "status", "error")
	if strings.Contains(e.ErrorCode, " ") {
		// Spring's default error body puts the reason phrase in "error".
		e.ErrorCode = ""
	}
	e.SubStatus = firstString(fields, "subStatus")
	e.Message = firstString(fields, "message", "error_description", "detail")
	if f := fields["fields"]; len(f) > 0 && !bytes.Equal(f, []byte("null")) {
		e.Fields = f
	}
	return e
}

func firstString(fields map[string]json.RawMessage, keys ...string) string {
	for _, key := range keys {
		var s string
		if json.Unmarshal(fields[key], &s) == nil && s != "" {
			return s
		}
	}
	return ""
}

func jsonInt(raw json.RawMessage) int {
	var n json.Number
	if json.Unmarshal(raw, &n) != nil {
		return 0
	}
	i, _ := strconv.Atoi(n.String())
	return i
}

// TransportError reports a request that got no response: a network failure, a timeout or a
// canceled context. It unwraps to the underlying error, so errors.Is(err,
// context.DeadlineExceeded) works as expected.
type TransportError struct {
	Method string
	URL    string
	Err    error
}

func (e *TransportError) Error() string {
	err := e.Err
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		err = urlErr.Err
	}
	return fmt.Sprintf("whitebird: %s %s: %v", e.Method, e.URL, err)
}

func (e *TransportError) Unwrap() error {
	return e.Err
}
