package whitebird

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"sync"
	"testing"
	"time"
)

// testService is served by the test server, alongside the authorization server and the
// operation MFA endpoint.
const testService Service = "test"

func newTestClient(t *testing.T, handler http.Handler, opts ...Option) *Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	opts = append([]Option{
		WithBaseURL(tokenService, srv.URL),
		WithBaseURL(operationMFAService, srv.URL),
		WithBaseURL(testService, srv.URL),
	}, opts...)
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

// authServer imitates the authorization server. Like the real one, it answers a password
// grant without a code with {"mfaEnabled": true} for accounts with two-factor authentication.
type authServer struct {
	t   *testing.T
	ttl time.Duration
	// mfa is the answer of the MFA check; mfaFails makes the check fail and unknown answers
	// that the account does not exist.
	mfa      bool
	mfaFails bool
	unknown  bool
	// reply, if set, answers token requests instead of issuing a token, unless it returns
	// status 0.
	reply func(form url.Values) (status int, body string)
	delay time.Duration

	mu         sync.Mutex
	forms      []url.Values
	answers    []string // captcha headers of the token requests
	captchaIDs []string
	usernames  []string // usernames of the MFA checks
	issued     int
}

func newAuthServer(t *testing.T, mux *http.ServeMux) *authServer {
	a := &authServer{t: t, ttl: time.Hour}
	mux.HandleFunc("POST "+tokenPath, a.token)
	mux.HandleFunc("POST "+mfaStatusPath, a.mfaCheck)
	mux.HandleFunc("GET "+captchaPath, a.captcha)
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
	a.answers = append(a.answers, r.Header.Get("Captcha"))
	reply := a.reply
	a.issued++
	n := a.issued
	a.mu.Unlock()
	if reply != nil {
		if status, body := reply(r.PostForm); status != 0 {
			w.WriteHeader(status)
			fmt.Fprint(w, body)
			return
		}
	}
	if a.mfa && r.PostForm.Get("grant_type") == "password" && !r.PostForm.Has("code") {
		writeJSON(w, map[string]bool{"mfaEnabled": true})
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
	a.usernames = append(a.usernames, body.Username)
	a.mu.Unlock()
	switch {
	case a.mfaFails:
		http.Error(w, "unavailable", http.StatusInternalServerError)
	case a.unknown:
		w.WriteHeader(http.StatusNotFound)
		writeJSON(w, map[string]string{"status": "NOT_FOUND", "message": "User not found"})
	default:
		writeJSON(w, map[string]bool{"mfaEnabled": a.mfa})
	}
}

func (a *authServer) captcha(w http.ResponseWriter, r *http.Request) {
	a.mu.Lock()
	a.captchaIDs = append(a.captchaIDs, r.URL.Query().Get("requestId"))
	a.mu.Unlock()
	enc := base64.StdEncoding
	writeJSON(w, map[string]any{
		"backgroundImage": enc.EncodeToString([]byte("background")),
		"puzzlePiece":     "data:image/png;base64," + enc.EncodeToString([]byte("piece")),
		"positionY":       42,
	})
}

// firstForm returns the form of the first token request.
func (a *authServer) firstForm() url.Values {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.forms[0]
}

// captchas returns the request IDs of the captchas generated and the captcha headers of the
// token requests.
func (a *authServer) captchas() (ids, answers []string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return slices.Clone(a.captchaIDs), slices.Clone(a.answers)
}

func (a *authServer) checks() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.usernames)
}

// form returns the form of token request i.
func (a *authServer) form(i int) url.Values {
	a.mu.Lock()
	defer a.mu.Unlock()
	if i >= len(a.forms) {
		a.t.Fatalf("token request %d of %d", i, len(a.forms))
	}
	return a.forms[i]
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
