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
	"unicode/utf8"
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
// authorization server as {"error", "error_description"}, and market data as {"retCode",
// "retMsg"}, also in HTTP 200 responses.
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
	// status is the "status" field as sent, which some services fill with a sentence.
	status string
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
	case e.ErrorCode == errorCodeMFARequired || isMFARefusal(e.Message) || isMFARefusal(e.status):
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

// isMFARefusal reports whether text is the refusal of an operation that needs an operation MFA
// token: "Operation denied, requires mfa checking".
func isMFARefusal(text string) bool {
	return strings.Contains(strings.ToLower(text), "requires mfa checking")
}

// maxErrorText is the length of a non-JSON error body kept as the message.
const maxErrorText = 300

func newAPIError(r *reply) *APIError {
	e := &APIError{StatusCode: r.status, Body: r.body}
	if r.status == http.StatusTooManyRequests || r.status == http.StatusServiceUnavailable {
		e.RetryAfter, _ = retryAfter(r.header, time.Now())
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(r.body, &fields); err != nil {
		e.Message = errorText(r.body)
		return e
	}
	e.Code = jsonInt(fields["code"])
	if e.Code == 0 {
		e.Code = jsonInt(fields["retCode"])
	}
	e.status = firstString(fields, "status")
	e.Message = firstString(fields, "message", "error_description", "retMsg", "detail")
	for _, key := range []string{"status", "error"} {
		value := firstString(fields, key)
		if !strings.Contains(value, " ") {
			if e.ErrorCode = value; value != "" {
				break
			}
			continue
		}
		// A sentence, not a code: Spring's default error body puts the reason phrase in
		// "error", and some services explain the error in "status".
		if e.Message == "" {
			e.Message = value
		}
	}
	e.SubStatus = firstString(fields, "subStatus")
	if f := fields["fields"]; len(f) > 0 && !bytes.Equal(f, []byte("null")) {
		e.Fields = f
	}
	return e
}

// errorText returns the text of a non-JSON error body: the title of an HTML page, or the text
// itself, shortened.
func errorText(body []byte) string {
	text := strings.TrimSpace(string(body))
	if strings.HasPrefix(text, "<") {
		lower := strings.ToLower(text)
		start, end := strings.Index(lower, "<title>"), strings.Index(lower, "</title>")
		if start < 0 || end < start {
			return ""
		}
		text = strings.TrimSpace(text[start+len("<title>") : end])
	}
	text = strings.Join(strings.Fields(text), " ")
	if len(text) > maxErrorText {
		cut := maxErrorText
		for cut > 0 && !utf8.RuneStart(text[cut]) {
			cut--
		}
		text = text[:cut] + "…"
	}
	return text
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
