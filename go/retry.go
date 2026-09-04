package whitebird

import (
	"context"
	"errors"
	"math/rand/v2"
	"net/http"
	"strconv"
	"time"
)

var idempotentMethods = map[string]bool{
	http.MethodGet:     true,
	http.MethodHead:    true,
	http.MethodPut:     true,
	http.MethodDelete:  true,
	http.MethodOptions: true,
}

const (
	minRetryDelay = 500 * time.Millisecond
	maxRetryDelay = 8 * time.Second
	// maxRetryAfter is the longest Retry-After the client waits for; the error is returned
	// instead when the server asks for more.
	maxRetryAfter = time.Minute
)

func retryable(ctx context.Context, r *reply, err error) bool {
	if ctx.Err() != nil {
		return false
	}
	if err != nil {
		var transport *TransportError
		return errors.As(err, &transport)
	}
	switch r.status {
	case http.StatusTooManyRequests, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	}
	return false
}

// retryDelay returns how long to wait before retry number n (counted from 0) and whether to
// retry at all.
func retryDelay(n int, r *reply, now time.Time) (time.Duration, bool) {
	if r != nil {
		if delay, ok := retryAfter(r.header, now); ok {
			return delay, delay <= maxRetryAfter
		}
	}
	return jitter(min(minRetryDelay<<n, maxRetryDelay)), true
}

// jitter spreads d over [d/2, d] so that clients failing together do not retry together.
func jitter(d time.Duration) time.Duration {
	return d/2 + rand.N(d/2+1)
}

func retryAfter(header http.Header, now time.Time) (time.Duration, bool) {
	value := header.Get("Retry-After")
	if value == "" {
		return 0, false
	}
	if seconds, err := strconv.Atoi(value); err == nil {
		return max(time.Duration(seconds)*time.Second, 0), true
	}
	if at, err := http.ParseTime(value); err == nil {
		return max(at.Sub(now), 0), true
	}
	return 0, false
}

func sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
