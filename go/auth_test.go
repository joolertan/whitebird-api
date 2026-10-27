package whitebird

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

// withCode answers the TOTP code of accounts with two-factor authentication.
var withCode = WithCodeProvider(StaticCode("123456"))

// withSession gives a client a valid session, for tests that are not about signing in.
func withSession() Option {
	return WithTokenStore(&memoryStore{token: &Token{AccessToken: testJWT("session", time.Now().Add(time.Hour))}})
}

// bearerEcho answers with the access token it received.
func bearerEcho(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]string{"token": strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")})
}

func callEcho(t *testing.T, client *Client) string {
	t.Helper()
	var out struct{ Token string }
	if err := client.Request(context.Background(), testService, http.MethodGet, "/echo", nil, &out); err != nil {
		t.Fatal(err)
	}
	return out.Token
}

func TestLoginWhenCheckFails(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfaFails = true
	mux.HandleFunc("GET /echo", bearerEcho)
	client := newTestClient(t, mux, WithCredentials("Trader@Example.com", "secret"))

	token := callEcho(t, client)
	if jwtExpiry(token).IsZero() {
		t.Errorf("request sent token %q, want the issued JWT", token)
	}
	form := auth.firstForm()
	want := url.Values{"grant_type": {"password"}, "username": {"trader@example.com"}, "password": {"secret"}}
	if form.Encode() != want.Encode() {
		t.Errorf("login form = %v, want %v", form, want)
	}
	if checks := auth.checks(); checks != 1 {
		t.Errorf("MFA checks = %d, want 1", checks)
	}
	callEcho(t, client)
	if len(auth.grants()) != 1 {
		t.Errorf("token requests = %d, want 1: the token is reused", len(auth.forms))
	}
}

func TestLoginWhenCheckFailsSendsCode(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfaFails = true
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(StaticCode("123456")),
	)
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}
	if code := auth.firstForm().Get("code"); code != "123456" {
		t.Errorf("code = %q, want 123456", code)
	}
}

func TestLoginWithCode(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	mux.HandleFunc("GET /echo", bearerEcho)
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(CodeFunc(func(context.Context) (string, error) { return "123456", nil })),
	)
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}
	if code := auth.firstForm().Get("code"); code != "123456" {
		t.Errorf("code = %q, want 123456", code)
	}
	if checks := auth.checks(); checks != 1 {
		t.Errorf("MFA checks = %d, want 1", checks)
	}
}

func TestLoginWithoutTwoFactorNeedsSolver(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	asked := 0
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(CodeFunc(func(context.Context) (string, error) { asked++; return "123456", nil })),
	)
	err := client.Login(context.Background())
	if !errors.Is(err, ErrCaptchaRequired) || !strings.Contains(err.Error(), "WithCaptchaSolver") {
		t.Errorf("Login() = %v, want ErrCaptchaRequired", err)
	}
	if asked != 0 {
		t.Errorf("code provider called %d times for an account without 2FA", asked)
	}
	if len(auth.grants()) != 0 {
		t.Error("client tried the password without a captcha")
	}
}

func TestLoginMFARequired(t *testing.T) {
	t.Run("check", func(t *testing.T) {
		mux := http.NewServeMux()
		auth := newAuthServer(t, mux)
		auth.mfa = true
		client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"))
		err := client.Login(context.Background())
		if !errors.Is(err, ErrMFARequired) || !errors.Is(err, ErrAuthentication) {
			t.Errorf("Login() = %v, want ErrMFARequired", err)
		}
		if len(auth.grants()) != 0 {
			t.Error("client tried the password although MFA is enabled")
		}
	})
	t.Run("login answer", func(t *testing.T) {
		mux := http.NewServeMux()
		auth := newAuthServer(t, mux)
		auth.mfaFails = true
		auth.reply = func(url.Values) (int, string) { return http.StatusOK, `{"mfaEnabled":true}` }
		client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"))
		if err := client.Login(context.Background()); !errors.Is(err, ErrMFARequired) {
			t.Errorf("Login() = %v, want ErrMFARequired", err)
		}
	})
	t.Run("invalid grant after failed check", func(t *testing.T) {
		mux := http.NewServeMux()
		auth := newAuthServer(t, mux)
		auth.mfaFails = true
		auth.reply = func(url.Values) (int, string) {
			return http.StatusBadRequest, `{"error":"invalid_grant","error_description":"Bad credentials"}`
		}
		client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"))
		err := client.Login(context.Background())
		if !errors.Is(err, ErrMFARequired) || !strings.Contains(err.Error(), "Bad credentials") {
			t.Errorf("Login() = %v, want ErrMFARequired with the server's description", err)
		}
	})
}

func TestLoginBadCredentials(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	auth.reply = func(url.Values) (int, string) {
		return http.StatusBadRequest, `{"error":"invalid_grant","error_description":"Bad credentials"}`
	}
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "wrong"),
		WithCodeProvider(StaticCode("123456")),
	)
	err := client.Login(context.Background())
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Message != "Bad credentials" {
		t.Fatalf("Login() = %v, want the server's description", err)
	}
	if !errors.Is(err, ErrAuthentication) || errors.Is(err, ErrMFARequired) || errors.Is(err, ErrBadRequest) {
		t.Errorf("Login() = %v, want ErrAuthentication only", err)
	}
}

func TestLoginWrongCode(t *testing.T) {
	answers := map[string]func(url.Values) (int, string){
		"invalid grant": func(url.Values) (int, string) {
			return http.StatusBadRequest, `{"error":"invalid_grant","error_description":"Invalid code"}`
		},
		"mfa answer": func(url.Values) (int, string) { return http.StatusOK, `{"mfaEnabled":true}` },
	}
	for name, reply := range answers {
		t.Run(name, func(t *testing.T) {
			mux := http.NewServeMux()
			auth := newAuthServer(t, mux)
			auth.mfa, auth.reply = true, reply
			client := newTestClient(t, mux,
				WithCredentials("trader@example.com", "secret"),
				WithCodeProvider(StaticCode("000000")),
			)
			err := client.Login(context.Background())
			if !errors.Is(err, ErrAuthentication) || errors.Is(err, ErrMFARequired) {
				t.Errorf("Login() = %v, want ErrAuthentication but not ErrMFARequired", err)
			}
		})
	}
}

func TestCodeProviderError(t *testing.T) {
	mux := http.NewServeMux()
	newAuthServer(t, mux).mfa = true
	failure := errors.New("no terminal")
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(CodeFunc(func(context.Context) (string, error) { return "", failure })),
	)
	if err := client.Login(context.Background()); !errors.Is(err, failure) {
		t.Errorf("Login() = %v, want the provider's error", err)
	}
}

func TestNoCredentials(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	client := newTestClient(t, mux)
	if err := client.Login(context.Background()); !errors.Is(err, ErrAuthentication) {
		t.Errorf("Login() = %v, want ErrAuthentication", err)
	}
	if len(auth.grants()) != 0 {
		t.Error("client called the token endpoint without credentials")
	}
}

func TestProactiveRefresh(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	mux.HandleFunc("GET /echo", bearerEcho)
	store := &memoryStore{token: &Token{
		AccessToken:  testJWT("old", time.Now().Add(10*time.Second)),
		RefreshToken: "refresh-old",
	}}
	old := store.token.AccessToken
	client := newTestClient(t, mux, WithTokenStore(store))

	token := callEcho(t, client)
	if got := auth.grants(); !slices.Equal(got, []string{"refresh_token"}) {
		t.Fatalf("grants = %v, want a single refresh", got)
	}
	if form := auth.firstForm(); form.Get("refresh_token") != "refresh-old" {
		t.Errorf("refresh form = %v", form)
	}
	if token == old {
		t.Error("request sent the token that was about to expire")
	}
	saved, _ := store.Load(context.Background())
	if saved.AccessToken != token || saved.RefreshToken != "refresh-1" {
		t.Errorf("stored token = %+v, want the refreshed one", saved)
	}
}

func TestReactiveRefresh(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	var mu sync.Mutex
	var seen []string
	mux.HandleFunc("GET /echo", func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		mu.Lock()
		seen = append(seen, token)
		mu.Unlock()
		if token == "revoked" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		bearerEcho(w, r)
	})
	store := &memoryStore{token: &Token{AccessToken: "revoked", RefreshToken: "refresh-old"}}
	client := newTestClient(t, mux, WithTokenStore(store))

	token := callEcho(t, client)
	if got := auth.grants(); !slices.Equal(got, []string{"refresh_token"}) {
		t.Fatalf("grants = %v, want a single refresh", got)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 2 || seen[0] != "revoked" || seen[1] != token {
		t.Errorf("tokens sent = %v, want the revoked one then the new one", seen)
	}
}

func TestRefreshFailureFallsBackToLogin(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.reply = func(form url.Values) (int, string) {
		if form.Get("grant_type") == "refresh_token" {
			return http.StatusBadRequest, `{"error":"invalid_grant","error_description":"Invalid refresh token"}`
		}
		return http.StatusOK, `{"access_token":"fresh","refresh_token":"r"}`
	}
	mux.HandleFunc("GET /echo", bearerEcho)
	auth.mfa = true
	store := &memoryStore{token: &Token{AccessToken: "expired", RefreshToken: "r0", Expiry: time.Now().Add(-time.Minute)}}
	client := newTestClient(t, mux, WithTokenStore(store), WithCredentials("trader@example.com", "secret"), withCode)

	if token := callEcho(t, client); token != "fresh" {
		t.Errorf("token = %q, want the one from the new login", token)
	}
	if got := auth.grants(); !slices.Equal(got, []string{"refresh_token", "password"}) {
		t.Errorf("grants = %v, want a refresh, then a login", got)
	}
}

func TestRefreshFailureWithoutPassword(t *testing.T) {
	mux := http.NewServeMux()
	newAuthServer(t, mux).reply = func(url.Values) (int, string) {
		return http.StatusBadRequest, `{"error":"invalid_grant"}`
	}
	store := &memoryStore{token: &Token{AccessToken: "expired", RefreshToken: "r0", Expiry: time.Now().Add(-time.Minute)}}
	client := newTestClient(t, mux, WithTokenStore(store))
	if err := client.Login(context.Background()); !errors.Is(err, ErrAuthentication) {
		t.Errorf("Login() = %v, want ErrAuthentication", err)
	}
}

func TestConcurrentCallsShareOneRefresh(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.delay = 50 * time.Millisecond
	mux.HandleFunc("GET /echo", bearerEcho)
	store := &memoryStore{token: &Token{AccessToken: "expired", RefreshToken: "r0", Expiry: time.Now().Add(-time.Minute)}}
	client := newTestClient(t, mux, WithTokenStore(store))

	const callers = 20
	tokens := make([]string, callers)
	var wg sync.WaitGroup
	for i := range callers {
		wg.Go(func() { tokens[i] = callEcho(t, client) })
	}
	wg.Wait()
	if got := auth.grants(); len(got) != 1 {
		t.Fatalf("grants = %v, want a single refresh", got)
	}
	for _, token := range tokens {
		if token != tokens[0] {
			t.Fatalf("callers used different tokens: %v", tokens)
		}
	}
}

func TestRefreshOutlivesCanceledCaller(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.delay, auth.mfa = 50*time.Millisecond, true
	mux.HandleFunc("GET /echo", bearerEcho)
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode)

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := client.Login(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("Login() = %v, want context.Canceled", err)
	}
	callEcho(t, client)
	if got := auth.grants(); len(got) != 1 {
		t.Errorf("grants = %v, want the canceled caller's login to be reused", got)
	}
}

func TestFileTokenStore(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	mux.HandleFunc("GET /echo", bearerEcho)
	store := FileTokenStore{Path: filepath.Join(t.TempDir(), "token.json")}

	if token, err := store.Load(context.Background()); token != nil || err != nil {
		t.Fatalf("Load() on a missing file = %v, %v; want nil, nil", token, err)
	}
	auth.mfa = true
	first := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithTokenStore(store))
	token := callEcho(t, first)
	second := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithTokenStore(store))
	if got := callEcho(t, second); got != token {
		t.Errorf("second client sent %q, want the stored token", got)
	}
	if grants := auth.grants(); len(grants) != 1 {
		t.Errorf("grants = %v, want a single login", grants)
	}
	saved, err := store.Load(context.Background())
	if err != nil || saved.AccessToken != token || saved.Expiry.IsZero() {
		t.Errorf("Load() = %+v, %v", saved, err)
	}
}

func TestMFAToken(t *testing.T) {
	mux := http.NewServeMux()
	headers := make(chan string, 3)
	mux.HandleFunc("GET /public", func(_ http.ResponseWriter, r *http.Request) {
		headers <- r.Header.Get("x-mfa-token")
	})
	client := newTestClient(t, mux)
	call := func() {
		if err := client.Request(context.Background(), testService, http.MethodGet, "/public", nil, nil, WithoutAuth()); err != nil {
			t.Fatal(err)
		}
	}
	call()
	client.SetMFAToken("mfa-1")
	call()
	client.SetMFAToken("")
	call()
	if got := []string{<-headers, <-headers, <-headers}; !slices.Equal(got, []string{"", "mfa-1", ""}) {
		t.Errorf("x-mfa-token headers = %q", got)
	}
}

type memoryStore struct {
	mu    sync.Mutex
	token *Token
	saves int
}

func (s *memoryStore) Load(context.Context) (*Token, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.token, nil
}

func (s *memoryStore) Save(_ context.Context, token *Token) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.token = token
	s.saves++
	return nil
}
