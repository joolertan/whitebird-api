package whitebird

import (
	"context"
	"errors"
	"net"
	"net/http"
	"slices"
	"sync/atomic"
	"testing"
	"time"
)

// failingHandler answers with the given statuses in turn, then 200.
func failingHandler(attempts *atomic.Int32, header http.Header, statuses ...int) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		n := int(attempts.Add(1))
		if n <= len(statuses) {
			for key, values := range header {
				w.Header()[key] = values
			}
			w.WriteHeader(statuses[n-1])
			return
		}
		writeJSON(w, map[string]int{"attempt": n})
	}
}

func recordSleeps(client *Client) *[]time.Duration {
	var sleeps []time.Duration
	client.core.sleep = func(_ context.Context, d time.Duration) error {
		sleeps = append(sleeps, d)
		return nil
	}
	return &sleeps
}

func TestRetriesIdempotentRequests(t *testing.T) {
	for _, status := range []int{http.StatusTooManyRequests, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout} {
		var attempts atomic.Int32
		mux := http.NewServeMux()
		mux.Handle("GET /flaky", failingHandler(&attempts, nil, status, status))
		client := newTestClient(t, mux)
		sleeps := recordSleeps(client)

		err := client.Request(context.Background(), testService, http.MethodGet, "/flaky", nil, nil, WithoutAuth())
		if err != nil {
			t.Fatalf("status %d: %v", status, err)
		}
		if attempts.Load() != 3 {
			t.Errorf("status %d: attempts = %d, want 3", status, attempts.Load())
		}
		for i, d := range *sleeps {
			if base := minRetryDelay << i; d < base/2 || d > base {
				t.Errorf("status %d: delay %d = %v, want between %v and %v", status, i, d, base/2, base)
			}
		}
	}
}

func TestRetryAfter(t *testing.T) {
	var attempts atomic.Int32
	mux := http.NewServeMux()
	mux.Handle("GET /limited", failingHandler(&attempts, http.Header{"Retry-After": {"3"}}, http.StatusTooManyRequests))
	client := newTestClient(t, mux)
	sleeps := recordSleeps(client)

	if err := client.Request(context.Background(), testService, http.MethodGet, "/limited", nil, nil, WithoutAuth()); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(*sleeps, []time.Duration{3 * time.Second}) {
		t.Errorf("sleeps = %v, want [3s]", *sleeps)
	}
}

func TestRetryAfterTooLong(t *testing.T) {
	var attempts atomic.Int32
	mux := http.NewServeMux()
	mux.Handle("GET /limited", failingHandler(&attempts, http.Header{"Retry-After": {"3600"}}, http.StatusTooManyRequests))
	client := newTestClient(t, mux)

	err := client.Request(context.Background(), testService, http.MethodGet, "/limited", nil, nil, WithoutAuth())
	var apiErr *APIError
	if !errors.As(err, &apiErr) || !errors.Is(err, ErrRateLimited) || apiErr.RetryAfter != time.Hour {
		t.Fatalf("Request() = %#v, want ErrRateLimited with RetryAfter 1h", err)
	}
	if attempts.Load() != 1 {
		t.Errorf("attempts = %d, want 1", attempts.Load())
	}
}

func TestRetriesExhausted(t *testing.T) {
	var attempts atomic.Int32
	mux := http.NewServeMux()
	mux.Handle("GET /down", failingHandler(&attempts, nil, 503, 503, 503, 503, 503))
	client := newTestClient(t, mux, WithMaxRetries(3))

	err := client.Request(context.Background(), testService, http.MethodGet, "/down", nil, nil, WithoutAuth())
	if !errors.Is(err, ErrServer) {
		t.Fatalf("Request() = %v, want ErrServer", err)
	}
	if attempts.Load() != 4 {
		t.Errorf("attempts = %d, want 4", attempts.Load())
	}
}

func TestPostIsNotRetried(t *testing.T) {
	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodDelete, http.MethodPatch} {
		var attempts atomic.Int32
		mux := http.NewServeMux()
		mux.Handle(method+" /orders", failingHandler(&attempts, nil, 503))
		client := newTestClient(t, mux)

		err := client.Request(context.Background(), testService, method, "/orders", map[string]int{"amount": 1}, nil, WithoutAuth())
		if !errors.Is(err, ErrServer) || attempts.Load() != 1 {
			t.Errorf("%s: Request() = %v after %d attempts, want ErrServer after 1", method, err, attempts.Load())
		}
	}

	var attempts atomic.Int32
	mux := http.NewServeMux()
	mux.Handle("POST /orders", failingHandler(&attempts, nil, 503))
	client := newTestClient(t, mux)

	err := client.Request(context.Background(), testService, http.MethodPost, "/orders", map[string]int{"amount": 1}, nil, WithoutAuth(), WithIdempotent(true))
	if err != nil || attempts.Load() != 2 {
		t.Errorf("Request(WithIdempotent) = %v after %d attempts, want success after 2", err, attempts.Load())
	}
}

func TestRequestMaxRetriesOption(t *testing.T) {
	var attempts atomic.Int32
	mux := http.NewServeMux()
	mux.Handle("GET /down", failingHandler(&attempts, nil, 503, 503))
	client := newTestClient(t, mux)

	err := client.Request(context.Background(), testService, http.MethodGet, "/down", nil, nil, WithoutAuth(), WithRequestMaxRetries(0))
	if !errors.Is(err, ErrServer) || attempts.Load() != 1 {
		t.Errorf("Request() = %v after %d attempts, want ErrServer after 1", err, attempts.Load())
	}
}

func TestRetriesConnectionErrors(t *testing.T) {
	var attempts atomic.Int32
	mux := http.NewServeMux()
	mux.HandleFunc("GET /drop", func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) == 1 {
			conn, _, err := w.(http.Hijacker).Hijack()
			if err != nil {
				t.Error(err)
				return
			}
			conn.Close()
			return
		}
		writeJSON(w, "ok")
	})
	client := newTestClient(t, mux)

	var out string
	if err := client.Request(context.Background(), testService, http.MethodGet, "/drop", nil, &out, WithoutAuth()); err != nil {
		t.Fatal(err)
	}
	if out != "ok" || attempts.Load() != 2 {
		t.Errorf("got %q after %d attempts, want ok after 2", out, attempts.Load())
	}
}

func TestTimeout(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /slow", func(_ http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(time.Second):
		}
	})
	client := newTestClient(t, mux, WithMaxRetries(0))

	err := client.Request(context.Background(), testService, http.MethodGet, "/slow", nil, nil, WithoutAuth(), WithRequestTimeout(20*time.Millisecond))
	var transport *TransportError
	if !errors.As(err, &transport) || !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("Request() = %v, want a TransportError wrapping context.DeadlineExceeded", err)
	}
}

func TestPostRetriedWhenNotSent(t *testing.T) {
	var attempts atomic.Int32
	mux := http.NewServeMux()
	mux.Handle("POST /orders", failingHandler(&attempts, nil, http.StatusTooManyRequests))
	client := newTestClient(t, mux)
	recordSleeps(client)
	if err := client.Request(context.Background(), testService, http.MethodPost, "/orders", nil, nil, WithoutAuth()); err != nil || attempts.Load() != 2 {
		t.Errorf("Request() after a 429 = %v after %d attempts, want success after 2", err, attempts.Load())
	}

	var dials atomic.Int32
	refused := &http.Client{Transport: &http.Transport{
		DialContext: func(context.Context, string, string) (net.Conn, error) {
			dials.Add(1)
			return nil, &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("connection refused")}
		},
	}}
	client = newTestClient(t, http.NewServeMux(), WithHTTPClient(refused))
	recordSleeps(client)
	err := client.Request(context.Background(), testService, http.MethodPost, "/orders", nil, nil, WithoutAuth())
	var transport *TransportError
	if !errors.As(err, &transport) || dials.Load() != 3 {
		t.Errorf("Request() = %v after %d dials, want a TransportError after 3", err, dials.Load())
	}
}

func TestPostNotRetriedAfterSending(t *testing.T) {
	var attempts atomic.Int32
	mux := http.NewServeMux()
	mux.HandleFunc("POST /drop", func(w http.ResponseWriter, _ *http.Request) {
		attempts.Add(1)
		conn, _, err := w.(http.Hijacker).Hijack()
		if err == nil {
			conn.Close()
		}
	})
	client := newTestClient(t, mux)
	err := client.Request(context.Background(), testService, http.MethodPost, "/drop", nil, nil, WithoutAuth())
	var transport *TransportError
	if !errors.As(err, &transport) || attempts.Load() != 1 {
		t.Errorf("Request() = %v after %d attempts, want a TransportError after 1", err, attempts.Load())
	}
}

func TestBackoffDoesNotOverflow(t *testing.T) {
	for n := range 100 {
		d, ok := retryDelay(n, nil, time.Now())
		if !ok || d < 0 || d > maxRetryDelay {
			t.Fatalf("retryDelay(%d) = %v, %v", n, d, ok)
		}
	}
}

func TestRetryAfterHeader(t *testing.T) {
	now := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	tests := map[string]time.Duration{
		"7":                             7 * time.Second,
		"-1":                            0,
		"Fri, 02 Jan 2026 03:04:15 GMT": 10 * time.Second,
		"99999999999":                   retryAfterCap,
		"99999999999999999999999":       retryAfterCap,
		"-99999999999999999999999":      0,
		"Fri, 31 Dec 9999 23:59:59 GMT": retryAfterCap,
	}
	for value, want := range tests {
		got, ok := retryAfter(http.Header{"Retry-After": {value}}, now)
		if !ok || got != want {
			t.Errorf("retryAfter(%q) = %v, %v; want %v", value, got, ok, want)
		}
	}
	if _, ok := retryAfter(http.Header{"Retry-After": {"soon"}}, now); ok {
		t.Error("retryAfter accepted an invalid value")
	}
}
