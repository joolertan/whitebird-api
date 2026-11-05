package whitebird

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"iter"
	"mime"
	"net/http"
	"strconv"
	"time"
)

// StreamEvent is an event received from a server-sent event stream.
type StreamEvent[T any] struct {
	// Name is the event type, such as "order-updates"; "message" when the server sends none.
	Name string
	// ID is the ID of the event or of the last event that had one. The client sends it back as
	// Last-Event-ID when it reconnects, so that the server can resume the stream.
	ID string
	// Data is the event data decoded from JSON.
	Data T
	// Raw is the event data as received.
	Raw []byte
}

const (
	defaultReconnectDelay = time.Second
	maxReconnectDelay     = 30 * time.Second
)

// stream subscribes to an event stream and reconnects whenever the connection drops, until the
// consumer stops, ctx is done or the server answers 204 No Content. Transient failures to
// reconnect are retried with backoff; other errors, including a 401 or 403 that survives a
// token refresh, end the stream. Failing to connect the first time also ends it, so that a
// misconfiguration does not go unnoticed. When ctx is done the stream ends without an error.
//
// Events without data, such as keep-alives, and events whose data does not decode as T are
// skipped.
func stream[T any](ctx context.Context, c *core, op *operation, opts []RequestOption) iter.Seq2[StreamEvent[T], error] {
	return func(yield func(StreamEvent[T], error) bool) {
		var lastID string
		delay := defaultReconnectDelay
		failures := 0
		connected := false
		for {
			body, err := c.openStream(ctx, op, opts, lastID)
			switch {
			case ctx.Err() != nil:
				if body != nil {
					body.Close()
				}
				return
			case errors.Is(err, errStreamEnded):
				return
			case err != nil:
				if !connected || !transient(err) {
					yield(StreamEvent[T]{}, err)
					return
				}
				failures++
				if c.sleep(ctx, jitter(backoff(delay, maxReconnectDelay, failures))) != nil {
					return
				}
				continue
			}
			connected, failures = true, 0
			events := newEventReader(body)
			for {
				raw, err := events.next()
				if err != nil {
					break
				}
				if len(bytes.TrimSpace(raw.data)) == 0 {
					continue
				}
				event := StreamEvent[T]{Name: raw.name, ID: raw.id, Raw: raw.data}
				if decode(raw.data, &event.Data) != nil {
					continue
				}
				if !yield(event, nil) {
					body.Close()
					return
				}
			}
			body.Close()
			lastID = events.lastID
			if events.retry > 0 {
				delay = events.retry
			}
			if c.sleep(ctx, delay) != nil {
				return
			}
		}
	}
}

// errStreamEnded reports that the server closed the stream for good with 204 No Content.
var errStreamEnded = errors.New("whitebird: event stream ended")

func (c *core) openStream(ctx context.Context, op *operation, opts []RequestOption, lastID string) (io.ReadCloser, error) {
	p, err := c.prepare(op, "text/event-stream", opts)
	if err != nil {
		return nil, err
	}
	p.header.Set("Cache-Control", "no-cache")
	if lastID != "" {
		p.header.Set("Last-Event-ID", lastID)
	}
	r, err := c.do(ctx, p, true)
	switch {
	case err != nil:
		return nil, err
	case r.stream == nil:
		return nil, newAPIError(r)
	case r.status == http.StatusNoContent:
		r.stream.Close()
		return nil, errStreamEnded
	}
	if mediaType, _, _ := mime.ParseMediaType(r.header.Get("Content-Type")); mediaType != "text/event-stream" {
		r.stream.Close()
		return nil, &APIError{
			StatusCode: r.status,
			Message:    fmt.Sprintf("expected an event stream, got %q", r.header.Get("Content-Type")),
			sentinel:   ErrServer,
		}
	}
	return r.stream, nil
}

func transient(err error) bool {
	var transport *TransportError
	return errors.As(err, &transport) || errors.Is(err, ErrServer) || errors.Is(err, ErrRateLimited)
}

// eventReader parses the text/event-stream format.
type eventReader struct {
	lines *bufio.Scanner
	// idBuffer is the last ID received; lastID becomes it when an event is dispatched, so that
	// an event cut short by a dropped connection is received again.
	idBuffer string
	lastID   string
	retry    time.Duration
}

type rawEvent struct {
	name string
	id   string
	data []byte
}

func newEventReader(r io.Reader) *eventReader {
	lines := bufio.NewScanner(r)
	lines.Buffer(make([]byte, 0, 64<<10), 1<<20)
	lines.Split(scanLines)
	return &eventReader{lines: lines}
}

// next returns the next event, or an error when the stream ends.
func (r *eventReader) next() (rawEvent, error) {
	var name string
	var data []byte
	for r.lines.Scan() {
		line := r.lines.Bytes()
		if len(line) == 0 {
			r.lastID = r.idBuffer
			if data == nil {
				name = ""
				continue
			}
			if name == "" {
				name = "message"
			}
			return rawEvent{name: name, id: r.lastID, data: data[:len(data)-1]}, nil
		}
		field, value, _ := bytes.Cut(line, []byte(":"))
		value = bytes.TrimPrefix(value, []byte(" "))
		switch string(field) {
		case "event":
			name = string(value)
		case "data":
			data = append(append(data, value...), '\n')
		case "id":
			if !bytes.ContainsRune(value, 0) {
				r.idBuffer = string(value)
			}
		case "retry":
			if ms, err := strconv.ParseUint(string(value), 10, 32); err == nil {
				r.retry = time.Duration(ms) * time.Millisecond
			}
		}
	}
	if err := r.lines.Err(); err != nil {
		return rawEvent{}, err
	}
	return rawEvent{}, io.EOF
}

// scanLines splits on "\n", "\r\n" and "\r", the line endings allowed in event streams.
func scanLines(data []byte, atEOF bool) (int, []byte, error) {
	i := bytes.IndexAny(data, "\r\n")
	switch {
	case i < 0 && atEOF && len(data) > 0:
		return len(data), data, nil
	case i < 0:
		return 0, nil, nil
	case data[i] == '\n':
		return i + 1, data[:i], nil
	case i+1 < len(data):
		if data[i+1] == '\n' {
			return i + 2, data[:i], nil
		}
		return i + 1, data[:i], nil
	case atEOF:
		return i + 1, data[:i], nil
	}
	// A trailing "\r" may be the first half of "\r\n".
	return 0, nil, nil
}
