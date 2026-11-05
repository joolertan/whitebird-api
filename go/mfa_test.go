package whitebird

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// mfaServer issues operation MFA tokens and serves a withdrawal that needs one.
type mfaServer struct {
	issued    atomic.Int32
	withdrawn atomic.Int32
	refused   atomic.Int32
	refuse    atomic.Bool // refuse every token
	inStatus  atomic.Bool // explain the refusal in "status" rather than "message"
}

func newMFAServer(t *testing.T, mux *http.ServeMux) *mfaServer {
	m := &mfaServer{}
	mux.HandleFunc("POST "+operationMFAPath, func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["operation"] != "WITHDRAWAL" || body["mfaCode"] != "654321" || r.Header.Get("Authorization") == "" {
			t.Errorf("MFA token request = %v with Authorization %q", body, r.Header.Get("Authorization"))
		}
		m.issued.Add(1)
		writeJSON(w, map[string]string{"mfaToken": "fresh"})
	})
	mux.HandleFunc("POST /withdraw", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Mfa-Token") != "fresh" || m.refuse.Load() {
			m.refused.Add(1)
			field := "message"
			if m.inStatus.Load() {
				field = "status"
			}
			w.WriteHeader(http.StatusForbidden)
			writeJSON(w, map[string]string{field: "Operation denied, requires mfa checking"})
			return
		}
		m.withdrawn.Add(1)
	})
	return m
}

func withdraw(client *Client) error {
	op := testOperation(http.MethodPost, "/withdraw")
	op.mfaOperation = "WITHDRAWAL"
	return client.core.exec(context.Background(), op, nil)
}

func TestConfirmOperation(t *testing.T) {
	tests := []struct {
		name   string
		header string
		body   string
	}{
		{"field", "", `{"mfaToken":"mfa-1"}`},
		{"JSON string", "", `"mfa-1"`},
		{"plain string", "", "mfa-1\n"},
		{"header", "mfa-1", `{}`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			mux := http.NewServeMux()
			mux.HandleFunc("POST "+operationMFAPath, func(w http.ResponseWriter, _ *http.Request) {
				if tt.header != "" {
					w.Header().Set("X-Mfa-Token", tt.header)
				}
				w.Write([]byte(tt.body))
			})
			mux.HandleFunc("GET /echo", func(w http.ResponseWriter, r *http.Request) {
				w.Write([]byte(r.Header.Get("X-Mfa-Token")))
			})
			client := newTestClient(t, mux, withSession())

			token, err := client.ConfirmOperation(context.Background(), "WITHDRAWAL", "654321")
			if err != nil || token != "mfa-1" {
				t.Fatalf("ConfirmOperation() = %q, %v; want mfa-1", token, err)
			}
			var sent []byte
			if err := client.Request(context.Background(), testService, http.MethodGet, "/echo", nil, &sent); err != nil || string(sent) != "mfa-1" {
				t.Errorf("x-mfa-token sent = %q, %v; want mfa-1", sent, err)
			}
		})
	}
}

func TestConfirmOperationErrors(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("POST "+operationMFAPath, func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{}`))
	})
	client := newTestClient(t, mux, withSession())

	if _, err := client.ConfirmOperation(context.Background(), "WITHDRAWAL", ""); !errors.Is(err, ErrMFARequired) {
		t.Errorf("ConfirmOperation() without a code = %v, want ErrMFARequired", err)
	}
	if _, err := client.ConfirmOperation(context.Background(), "WITHDRAWAL", "654321"); err == nil {
		t.Error("ConfirmOperation() succeeded without a token in the response")
	}
}

func TestMFAOperationConfirmsWhenRefused(t *testing.T) {
	mux := http.NewServeMux()
	newAuthServer(t, mux).mfa = true
	m := newMFAServer(t, mux)
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), WithCodeProvider(StaticCode("654321")))

	for range 2 {
		if err := withdraw(client); err != nil {
			t.Fatal(err)
		}
	}
	if m.issued.Load() != 1 || m.withdrawn.Load() != 2 || m.refused.Load() != 1 {
		t.Errorf("issued %d tokens for %d withdrawals after %d refusals, want 1 for 2 after 1", m.issued.Load(), m.withdrawn.Load(), m.refused.Load())
	}
}

func TestMFARefusalInStatus(t *testing.T) {
	mux := http.NewServeMux()
	newAuthServer(t, mux).mfa = true
	m := newMFAServer(t, mux)
	m.inStatus.Store(true)
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), WithCodeProvider(StaticCode("654321")))
	if err := withdraw(client); err != nil {
		t.Fatal(err)
	}
	if m.issued.Load() != 1 || m.withdrawn.Load() != 1 {
		t.Errorf("issued %d tokens, withdrew %d times; want 1 and 1", m.issued.Load(), m.withdrawn.Load())
	}
}

func TestConcurrentRefusalsShareOneConfirmation(t *testing.T) {
	mux := http.NewServeMux()
	m := newMFAServer(t, mux)
	var asked atomic.Int32
	client := newTestClient(t, mux, withSession(), WithCodeProvider(CodeFunc(func(context.Context) (string, error) {
		asked.Add(1)
		time.Sleep(20 * time.Millisecond)
		return "654321", nil
	})))
	var wg sync.WaitGroup
	for range 5 {
		wg.Go(func() {
			if err := withdraw(client); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if asked.Load() != 1 || m.issued.Load() != 1 || m.withdrawn.Load() != 5 {
		t.Errorf("asked %d codes and issued %d tokens for %d withdrawals, want 1 and 1 for 5", asked.Load(), m.issued.Load(), m.withdrawn.Load())
	}
}

func TestMFAOperationRenewsRefusedToken(t *testing.T) {
	mux := http.NewServeMux()
	newAuthServer(t, mux).mfa = true
	m := newMFAServer(t, mux)
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), WithCodeProvider(StaticCode("654321")))
	client.SetMFAToken("expired")

	if err := withdraw(client); err != nil {
		t.Fatal(err)
	}
	if m.issued.Load() != 1 || m.withdrawn.Load() != 1 {
		t.Errorf("issued %d tokens, withdrew %d times; want 1 and 1", m.issued.Load(), m.withdrawn.Load())
	}

	// A token refused again is not renewed twice.
	m.refuse.Store(true)
	if err := withdraw(client); !errors.Is(err, ErrMFARequired) {
		t.Errorf("withdraw() = %v, want ErrMFARequired", err)
	}
	if m.issued.Load() != 2 {
		t.Errorf("issued %d tokens, want 2", m.issued.Load())
	}
}

func TestMFAOperationWithoutCodeProvider(t *testing.T) {
	mux := http.NewServeMux()
	m := newMFAServer(t, mux)
	client := newTestClient(t, mux, withSession())

	if err := withdraw(client); !errors.Is(err, ErrMFARequired) {
		t.Errorf("withdraw() = %v, want ErrMFARequired", err)
	}
	if m.issued.Load() != 0 {
		t.Error("token requested without a code provider")
	}
}
