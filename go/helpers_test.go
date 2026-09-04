package whitebird

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"
	"time"
)

// testService is served by the test server, alongside the token endpoint.
const testService Service = "test"

func newTestClient(t *testing.T, handler http.Handler, opts ...Option) *Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	opts = append([]Option{WithBaseURL(tokenService, srv.URL), WithBaseURL(testService, srv.URL)}, opts...)
	client, err := NewClient(opts...)
	if err != nil {
		t.Fatal(err)
	}
	client.core.sleep = func(ctx context.Context, _ time.Duration) error { return ctx.Err() }
	return client
}

func testJWT(subject string, exp time.Time) string {
	enc := base64.RawURLEncoding
	claims := fmt.Sprintf(`{"sub":%q,"exp":%d}`, subject, exp.Unix())
	return enc.EncodeToString([]byte(`{"alg":"none"}`)) + "." + enc.EncodeToString([]byte(claims)) + ".sig"
}

// authServer imitates the authorization server.
type authServer struct {
	t   *testing.T
	ttl time.Duration
	// mfa is the answer of the MFA check; mfaFails makes the check fail.
	mfa      bool
	mfaFails bool
	// reply, if set, answers token requests instead of issuing a token.
	reply func(form url.Values) (status int, body string)
	delay time.Duration

	mu        sync.Mutex
	forms     []url.Values
	mfaChecks int
	issued    int
}

func newAuthServer(t *testing.T, mux *http.ServeMux) *authServer {
	a := &authServer{t: t, ttl: time.Hour}
	mux.HandleFunc("POST "+tokenPath, a.token)
	mux.HandleFunc("POST "+mfaCheckPath, a.mfaCheck)
	return a
}

func (a *authServer) token(w http.ResponseWriter, r *http.Request) {
	if got := r.Header.Get("Authorization"); got != "Basic "+clientCredentials {
		a.t.Errorf("token request Authorization = %q", got)
	}
	if err := r.ParseForm(); err != nil {
		a.t.Error(err)
	}
	time.Sleep(a.delay)
	a.mu.Lock()
	a.forms = append(a.forms, r.PostForm)
	reply := a.reply
	a.issued++
	n := a.issued
	a.mu.Unlock()
	if reply != nil {
		status, body := reply(r.PostForm)
		w.WriteHeader(status)
		fmt.Fprint(w, body)
		return
	}
	writeJSON(w, map[string]any{
		"access_token":  testJWT(fmt.Sprint("access-", n), time.Now().Add(a.ttl)),
		"refresh_token": fmt.Sprint("refresh-", n),
		"token_type":    "bearer",
		"expires_in":    int(a.ttl.Seconds()),
	})
}

func (a *authServer) mfaCheck(w http.ResponseWriter, r *http.Request) {
	var body struct{ Username string }
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		a.t.Error(err)
	}
	a.mu.Lock()
	a.mfaChecks++
	a.mu.Unlock()
	if a.mfaFails {
		http.Error(w, "unavailable", http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]bool{"mfaEnabled": a.mfa})
}

func (a *authServer) form(i int) url.Values {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.forms[i]
}

func (a *authServer) checks() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.mfaChecks
}

// grants returns the grant types requested so far.
func (a *authServer) grants() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	var grants []string
	for _, form := range a.forms {
		grants = append(grants, form.Get("grant_type"))
	}
	return grants
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// testOperation is an operation on the test service.
func testOperation(method, path string) *operation {
	return &operation{service: testService, method: method, path: path}
}
