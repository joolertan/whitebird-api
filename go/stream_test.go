package whitebird

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestEventReader(t *testing.T) {
	input := ": comment\n" +
		"event: order-updates\nid: 1\ndata: {\"id\":\ndata:7}\n\n" +
		"data: plain\r\n\r\n" +
		"retry: 2500\rid: 2\revent: sse-status\rdata\r\r" +
		"id\nevent: ignored\n\n" +
		"data: unterminated"
	r := newEventReader(strings.NewReader(input))
	var got []rawEvent
	for {
		event, err := r.next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, event)
	}
	want := []rawEvent{
		{name: "order-updates", id: "1", data: []byte("{\"id\":\n7}")},
		{name: "message", id: "1", data: []byte("plain")},
		{name: "sse-status", id: "2", data: []byte("")},
	}
	if len(got) != len(want) {
		t.Fatalf("events = %q, want %q", got, want)
	}
	for i := range want {
		if got[i].name != want[i].name || got[i].id != want[i].id || string(got[i].data) != string(want[i].data) {
			t.Errorf("event %d = %q, want %q", i, got[i], want[i])
		}
	}
	if r.retry != 2500*time.Millisecond || r.lastID != "" {
		t.Errorf("retry = %v, last ID = %q; want 2.5s and the ID reset by the empty id field", r.retry, r.lastID)
	}
}

type notification struct {
	Order string `json:"order"`
}

func TestStreamReconnects(t *testing.T) {
	var connections atomic.Int32
	var mu sync.Mutex
	var lastIDs []string
	mux := http.NewServeMux()
	newAuthServer(t, mux).mfa = true
	mux.HandleFunc("GET /sse", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Accept") != "text/event-stream" || !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") {
			t.Errorf("headers = %v", r.Header)
		}
		mu.Lock()
		lastIDs = append(lastIDs, r.Header.Get("Last-Event-ID"))
		mu.Unlock()
		w.Header().Set("Content-Type", "text/event-stream")
		switch connections.Add(1) {
		case 1:
			fmt.Fprint(w, "retry: 10\n\nevent: order-updates\nid: 1\ndata: {\"order\":\"a\"}\n\n")
			fmt.Fprint(w, "event: order-updates\nid: 2\ndata: {\"order\":\"b\"}\n\n")
		case 2:
			w.WriteHeader(http.StatusBadGateway)
		default:
			fmt.Fprint(w, "event: order-updates\nid: 3\ndata: {\"order\":\"c\"}\n\n")
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		}
	})
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithMaxRetries(0))
	var delays []time.Duration
	client.core.sleep = func(ctx context.Context, d time.Duration) error {
		delays = append(delays, d)
		return ctx.Err()
	}
	op := testOperation(http.MethodGet, "/sse")

	var orders []string
	for event, err := range stream[notification](context.Background(), client.core, op, nil) {
		if err != nil {
			t.Fatal(err)
		}
		if event.Name != "order-updates" {
			t.Errorf("event name = %q", event.Name)
		}
		orders = append(orders, event.ID+event.Data.Order)
		if len(orders) == 3 {
			break
		}
	}
	if !slices.Equal(orders, []string{"1a", "2b", "3c"}) {
		t.Errorf("events = %v", orders)
	}
	mu.Lock()
	defer mu.Unlock()
	if !slices.Equal(lastIDs, []string{"", "2", "2"}) {
		t.Errorf("Last-Event-ID headers = %q, want none, then 2 twice", lastIDs)
	}
	if len(delays) != 2 || delays[0] != 10*time.Millisecond || delays[1] < 10*time.Millisecond {
		t.Errorf("reconnect delays = %v, want the server's 10ms, then a longer backoff", delays)
	}
}

func TestStreamStopsOnRejectedToken(t *testing.T) {
	var connections atomic.Int32
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	mux.HandleFunc("GET /sse", func(w http.ResponseWriter, _ *http.Request) {
		connections.Add(1)
		w.WriteHeader(http.StatusUnauthorized)
	})
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode)

	var errs []error
	for _, err := range stream[notification](context.Background(), client.core, testOperation(http.MethodGet, "/sse"), nil) {
		errs = append(errs, err)
	}
	if len(errs) != 1 || !errors.Is(errs[0], ErrAuthentication) {
		t.Errorf("errors = %v, want a single ErrAuthentication", errs)
	}
	if connections.Load() != 2 || len(auth.grants()) != 2 {
		t.Errorf("connections = %d, grants = %v; want 2 connections around one refresh", connections.Load(), auth.grants())
	}
}

func TestStreamFailsFirstConnection(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /sse", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	client := newTestClient(t, mux, WithMaxRetries(0))
	op := testOperation(http.MethodGet, "/sse")
	op.public = true
	for _, err := range stream[notification](context.Background(), client.core, op, nil) {
		if !errors.Is(err, ErrServer) {
			t.Errorf("error = %v, want ErrServer", err)
		}
	}
}

func TestStreamDecodeError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /sse", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, "event: sse-status\ndata: connected\n\ndata: {\"order\":\"a\"}\n\n")
	})
	client := newTestClient(t, mux)
	op := testOperation(http.MethodGet, "/sse")
	op.public = true

	var events []StreamEvent[notification]
	var errs []error
	for event, err := range stream[notification](context.Background(), client.core, op, nil) {
		events = append(events, event)
		errs = append(errs, err)
		if len(events) == 2 {
			break
		}
	}
	if errs[0] == nil || string(events[0].Raw) != "connected" || events[0].Name != "sse-status" {
		t.Errorf("first event = %+v, %v; want the raw data and a decoding error", events[0], errs[0])
	}
	if errs[1] != nil || events[1].Data.Order != "a" {
		t.Errorf("second event = %+v, %v", events[1], errs[1])
	}
}

func TestStreamEndsWithContext(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /sse", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "data: {}\n\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	client := newTestClient(t, mux)
	op := testOperation(http.MethodGet, "/sse")
	op.public = true

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var errs []error
	for _, err := range stream[notification](ctx, client.core, op, nil) {
		errs = append(errs, err)
		cancel()
	}
	if len(errs) != 2 || errs[0] != nil || !errors.Is(errs[1], context.Canceled) {
		t.Errorf("errors = %v, want an event, then context.Canceled", errs)
	}
}
