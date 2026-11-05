package whitebird

import (
	"context"
	"errors"
	"net/http"
	"slices"
	"testing"
)

var sentinels = []error{
	ErrBadRequest, ErrVerificationRequired, ErrAuthentication, ErrMFARequired, ErrCaptchaRequired,
	ErrPermissionDenied, ErrNotFound, ErrConflict, ErrRateLimited, ErrServer,
}

func TestErrorClasses(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
		want   []error
	}{
		{"bad request", 400, `{"code":1001,"status":"VALIDATION_FAILED","message":"amount must be positive"}`, []error{ErrBadRequest}},
		{"sms verification", 400, `{"status":"SMS_VERIFICATION_REQUIRED","message":"confirm by SMS"}`, []error{ErrVerificationRequired, ErrBadRequest}},
		{"liveness verification", 400, `{"status":"LIVENESS_VERIFICATION_REQUIRED"}`, []error{ErrVerificationRequired, ErrBadRequest}},
		{"unauthorized", 401, `{"error":"invalid_token"}`, []error{ErrAuthentication}},
		{"invalid grant", 400, `{"error":"invalid_grant","error_description":"Bad credentials"}`, []error{ErrAuthentication}},
		{"mfa message", 403, `{"message":"Operation denied, requires mfa checking"}`, []error{ErrMFARequired, ErrAuthentication}},
		{"mfa status", 400, `{"status":"Operation denied, requires mfa checking"}`, []error{ErrMFARequired, ErrAuthentication}},
		{"mfa status with message", 400, `{"status":"Operation denied, requires mfa checking","message":"Denied"}`, []error{ErrMFARequired, ErrAuthentication}},
		{"forbidden", 403, `{"status":"FORBIDDEN"}`, []error{ErrPermissionDenied}},
		{"not found", 404, ``, []error{ErrNotFound}},
		{"conflict", 409, `{"message":"already exists"}`, []error{ErrConflict}},
		{"rate limited", 429, `Too many requests`, []error{ErrRateLimited}},
		{"server error", 500, `<html>oops</html>`, []error{ErrServer}},
		{"bad gateway", 502, ``, []error{ErrServer}},
		{"teapot", 418, ``, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			mux := http.NewServeMux()
			mux.HandleFunc("GET /fail", func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tt.status)
				w.Write([]byte(tt.body))
			})
			client := newTestClient(t, mux, WithMaxRetries(0))
			err := client.Request(context.Background(), testService, http.MethodGet, "/fail", nil, nil, WithoutAuth())

			var apiErr *APIError
			if !errors.As(err, &apiErr) || apiErr.StatusCode != tt.status || string(apiErr.Body) != tt.body {
				t.Fatalf("Request() = %#v, want an APIError with status %d and the body", err, tt.status)
			}
			for _, sentinel := range sentinels {
				want := slices.Contains(tt.want, sentinel)
				if errors.Is(err, sentinel) != want {
					t.Errorf("errors.Is(%v, %v) = %v, want %v", err, sentinel, !want, want)
				}
			}
		})
	}
}

func TestAPIErrorFields(t *testing.T) {
	err := newAPIError(&reply{
		status: http.StatusBadRequest,
		body:   []byte(`{"code":"1001","status":"VALIDATION_FAILED","subStatus":"AMOUNT_TOO_LOW","message":"invalid","fields":[{"field":"amount"}]}`),
	})
	if err.Code != 1001 || err.ErrorCode != "VALIDATION_FAILED" || err.SubStatus != "AMOUNT_TOO_LOW" || err.Message != "invalid" || string(err.Fields) != `[{"field":"amount"}]` {
		t.Errorf("newAPIError() = %+v", err)
	}
	if got, want := err.Error(), "whitebird: 400 Bad Request (VALIDATION_FAILED, AMOUNT_TOO_LOW, code 1001): invalid"; got != want {
		t.Errorf("Error() = %q, want %q", got, want)
	}

	spring := newAPIError(&reply{
		status: http.StatusNotFound,
		body:   []byte(`{"status":404,"error":"Not Found","message":"No such order"}`),
	})
	if spring.ErrorCode != "" || spring.Message != "No such order" {
		t.Errorf("Spring error = %+v, want no error code", spring)
	}
	if got, want := spring.Error(), "whitebird: 404 Not Found: No such order"; got != want {
		t.Errorf("Error() = %q, want %q", got, want)
	}
}

func TestEnvelopeError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /tickers", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]any{"retCode": 10001, "retMsg": "params error", "result": map[string]any{}})
	})
	client := newTestClient(t, mux)
	op := testOperation(http.MethodGet, "/tickers")
	op.public, op.envelope = true, true

	_, err := call[[]string](context.Background(), client.core, op, nil)
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Code != 10001 || apiErr.Message != "params error" || apiErr.StatusCode != http.StatusOK {
		t.Fatalf("call() = %#v, want an APIError with the retCode", err)
	}
	if got, want := err.Error(), "whitebird: API error (code 10001): params error"; got != want {
		t.Errorf("Error() = %q, want %q", got, want)
	}
}
