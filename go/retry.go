package whitebird

import (
	"context"
	"errors"
	"math/rand/v2"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// idempotentMethods are retried after any transient failure. Other requests are retried only
// when they certainly did not reach the server, or were rate limited.
var idempotentMethods = map[string]bool{
	http.MethodGet:     true,
	http.MethodHead:    true,
	http.MethodOptions: true,
}

const (
	minRetryDelay = 500 * time.Millisecond
	maxRetryDelay = 8 * time.Second
	// maxRetryAfter is the longest Retry-After the client waits for; the error is returned
	// instead when the server asks for more.
	maxRetryAfter = time.Minute
	// retryAfterCap bounds the Retry-After values reported in [APIError.RetryAfter].
	retryAfterCap = 24 * time.Hour
)

func retryable(ctx context.Context, idempotent bool, r *reply, err error) bool {
	if ctx.Err() != nil {
		return false
	}
	if err != nil {
		var transport *TransportError
		return errors.As(err, &transport) && (idempotent || notSent(err))
	}
	switch r.status {
	case http.StatusTooManyRequests:
		return true
	case http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return idempotent
	}
	return false
}

// notSent reports whether err means that the request never reached the server: the connection
// could not be established.
func notSent(err error) bool {
	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return true
	}
	var opErr *net.OpError
	return errors.As(err, &opErr) && (opErr.Op == "dial" || opErr.Op == "proxyconnect")
}

// retryDelay returns how long to wait before retry number n (counted from 0) and whether to
// retry at all.
func retryDelay(n int, r *reply, now time.Time) (time.Duration, bool) {
	if r != nil {
		if delay, ok := retryAfter(r.header, now); ok {
			return delay, delay <= maxRetryAfter
		}
	}
	return jitter(backoff(minRetryDelay, maxRetryDelay, n)), true
}

// backoff returns base doubled n times, up to limit.
func backoff(base, limit time.Duration, n int) time.Duration {
	d := base
	for range n {
		if d >= limit {
			break
		}
		d *= 2
	}
	return max(min(d, limit), 0)
}

// jitter spreads d over [d/2, d] so that clients failing together do not retry together.
func jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return 0
	}
	return d/2 + rand.N(d/2+1)
}

func retryAfter(header http.Header, now time.Time) (time.Duration, bool) {
	value := strings.TrimSpace(header.Get("Retry-After"))
	if value == "" {
		return 0, false
	}
	if seconds, err := strconv.ParseInt(value, 10, 64); err == nil || errors.Is(err, strconv.ErrRange) {
		switch {
		case seconds <= 0:
			return 0, true
		case seconds > int64(retryAfterCap/time.Second):
			return retryAfterCap, true
		}
		return time.Duration(seconds) * time.Second, true
	}
	if at, err := http.ParseTime(value); err == nil {
		return min(max(at.Sub(now), 0), retryAfterCap), true
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
