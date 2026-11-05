package whitebird

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
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

func TestLoginWithCode(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	mux.HandleFunc("GET /echo", bearerEcho)
	client := newTestClient(t, mux,
		WithCredentials("Trader@Example.com", "secret"),
		WithCodeProvider(CodeFunc(func(context.Context) (string, error) { return " 123456\n", nil })),
	)
	if token := callEcho(t, client); jwtExpiry(token).IsZero() {
		t.Errorf("request sent token %q, want the issued JWT", token)
	}
	// As in the web application: the password alone first, then with the code.
	want := url.Values{"grant_type": {"password"}, "username": {"trader@example.com"}, "password": {"secret"}}
	if form := auth.form(0); form.Encode() != want.Encode() {
		t.Errorf("first grant = %v, want %v", form, want)
	}
	want.Set("code", "123456")
	if form := auth.form(1); form.Encode() != want.Encode() {
		t.Errorf("second grant = %v, want %v", form, want)
	}
	if auth.checks() != 1 || auth.usernames[0] != "Trader@Example.com" {
		t.Errorf("MFA checks for %q, want one for the username as given", auth.usernames)
	}
	callEcho(t, client)
	if len(auth.grants()) != 2 {
		t.Errorf("grants = %v, want the token to be reused", auth.grants())
	}
}

func TestLoginWithTwoFactorWithoutCodeStep(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	// The server issues a token for the password alone.
	auth.reply = func(url.Values) (int, string) {
		return http.StatusOK, `{"access_token":"issued","refresh_token":"r"}`
	}
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(CodeFunc(func(context.Context) (string, error) {
			t.Error("code asked although the server issued a token")
			return "", nil
		})),
	)
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := auth.grants(); len(got) != 1 {
		t.Errorf("grants = %v, want one", got)
	}
}

func TestLoginStatusCheckFailure(t *testing.T) {
	t.Run("server error", func(t *testing.T) {
		mux := http.NewServeMux()
		auth := newAuthServer(t, mux)
		auth.mfaFails = true
		client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithMaxRetries(0))
		if err := client.Login(context.Background()); !errors.Is(err, ErrServer) {
			t.Errorf("Login() = %v, want the server error", err)
		}
		if len(auth.grants()) != 0 {
			t.Error("password sent although the two-factor check failed")
		}
	})
	t.Run("unknown username", func(t *testing.T) {
		mux := http.NewServeMux()
		auth := newAuthServer(t, mux)
		auth.unknown = true
		client := newTestClient(t, mux, WithCredentials("nobody@example.com", "secret"), withCode)
		err := client.Login(context.Background())
		if !errors.Is(err, ErrAuthentication) || errors.Is(err, ErrNotFound) || !strings.Contains(err.Error(), unknownUserHint) {
			t.Errorf("Login() = %v, want ErrAuthentication naming the unknown account", err)
		}
		if len(auth.grants()) != 0 {
			t.Error("password sent for an unknown account")
		}
	})
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
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"))
	err := client.Login(context.Background())
	if !errors.Is(err, ErrMFARequired) || !errors.Is(err, ErrAuthentication) {
		t.Errorf("Login() = %v, want ErrMFARequired", err)
	}
	if got := auth.grants(); len(got) != 1 || auth.form(0).Has("code") {
		t.Errorf("grants = %v, want the password alone", got)
	}
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
		WithCodeProvider(CodeFunc(func(context.Context) (string, error) {
			t.Error("code asked after the password was refused")
			return "", nil
		})),
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

func TestLoginRefusedForAWhile(t *testing.T) {
	tests := map[string]error{
		messageUserBlocked:  ErrPermissionDenied,
		messageManyRequests: ErrRateLimited,
	}
	for message, want := range tests {
		t.Run(message, func(t *testing.T) {
			mux := http.NewServeMux()
			auth := newAuthServer(t, mux)
			auth.mfa = true
			auth.reply = func(url.Values) (int, string) {
				return http.StatusBadRequest, `{"message":"` + message + `"}`
			}
			client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithMaxRetries(0))
			if err := client.Login(context.Background()); !errors.Is(err, want) {
				t.Errorf("Login() = %v, want %v", err, want)
			}
		})
	}
}

func TestLoginWrongCode(t *testing.T) {
	answers := map[string]func(url.Values) (int, string){
		"invalid grant": func(form url.Values) (int, string) {
			if form.Has("code") {
				return http.StatusBadRequest, `{"error":"invalid_grant","error_description":"Invalid code"}`
			}
			return 0, ""
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
			if got := auth.grants(); len(got) != 2 {
				t.Errorf("grants = %v, want two", got)
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
	mux = http.NewServeMux()
	newAuthServer(t, mux).mfa = true
	client = newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), WithCodeProvider(StaticCode(" ")))
	if err := client.Login(context.Background()); err == nil || !strings.Contains(err.Error(), "no code") {
		t.Errorf("Login() with an empty code = %v", err)
	}
}

func TestNoCredentials(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	solved := false
	client := newTestClient(t, mux, WithCaptchaSolver(func(context.Context, Captcha) (int, error) {
		solved = true
		return 0, nil
	}))
	if err := client.Login(context.Background()); !errors.Is(err, ErrAuthentication) {
		t.Errorf("Login() = %v, want ErrAuthentication", err)
	}
	if len(auth.grants()) != 0 || auth.checks() != 0 || solved {
		t.Error("client signed in without credentials")
	}
}

func TestProactiveRefresh(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	mux.HandleFunc("GET /echo", bearerEcho)
	// The web application renews tokens with less than a minute left.
	store := &memoryStore{token: &Token{
		AccessToken:  testJWT("old", time.Now().Add(50*time.Second)),
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

func TestRefreshKeepsRefreshToken(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.reply = func(url.Values) (int, string) {
		return http.StatusOK, `{"access_token":"new","expires_in":3600}`
	}
	store := &memoryStore{token: &Token{AccessToken: "old", RefreshToken: "keep", Expiry: time.Now().Add(-time.Minute)}}
	client := newTestClient(t, mux, WithTokenStore(store))
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}
	saved, _ := store.Load(context.Background())
	if saved.AccessToken != "new" || saved.RefreshToken != "keep" || time.Until(saved.Expiry) < 59*time.Minute {
		t.Errorf("stored token = %+v, want the new access token with the old refresh token", saved)
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

func TestRefreshRejectedFallsBackToLogin(t *testing.T) {
	for _, status := range []int{http.StatusBadRequest, http.StatusUnauthorized} {
		mux := http.NewServeMux()
		auth := newAuthServer(t, mux)
		auth.mfa = true
		auth.reply = func(form url.Values) (int, string) {
			if form.Get("grant_type") == "refresh_token" {
				return status, `{"error":"invalid_grant","error_description":"Invalid refresh token"}`
			}
			return 0, ""
		}
		mux.HandleFunc("GET /echo", bearerEcho)
		store := &memoryStore{token: &Token{AccessToken: "expired", RefreshToken: "r0", Expiry: time.Now().Add(-time.Minute)}}
		client := newTestClient(t, mux, WithTokenStore(store), WithCredentials("trader@example.com", "secret"), withCode)
		client.SetMFAToken("mfa")

		if token := callEcho(t, client); jwtExpiry(token).IsZero() {
			t.Errorf("status %d: token = %q, want the one from the new login", status, token)
		}
		if got := auth.grants(); !slices.Equal(got, []string{"refresh_token", "password", "password"}) {
			t.Errorf("status %d: grants = %v, want a refresh, then a login", status, got)
		}
		if store.clears != 1 || client.core.mfaToken.Load() != nil {
			t.Errorf("status %d: store cleared %d times, MFA token %v; want the old session forgotten", status, store.clears, client.core.mfaToken.Load())
		}
	}
}

func TestEarlyRefreshFailureKeepsValidToken(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.reply = func(url.Values) (int, string) { return http.StatusServiceUnavailable, `{"message":"down"}` }
	mux.HandleFunc("GET /echo", bearerEcho)
	access := testJWT("expiring", time.Now().Add(30*time.Second))
	client := newTestClient(t, mux, WithTokenStore(&memoryStore{token: &Token{AccessToken: access, RefreshToken: "r0"}}), WithMaxRetries(0))
	if token := callEcho(t, client); token != access {
		t.Errorf("token = %q, want the one still valid", token)
	}
	if got := auth.grants(); !slices.Equal(got, []string{"refresh_token"}) {
		t.Errorf("grants = %v, want a refresh attempt", got)
	}
}

func TestRefreshServerErrorPropagates(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.reply = func(url.Values) (int, string) { return http.StatusServiceUnavailable, `{"message":"down"}` }
	store := &memoryStore{token: &Token{AccessToken: "expired", RefreshToken: "r0", Expiry: time.Now().Add(-time.Minute)}}
	client := newTestClient(t, mux, WithTokenStore(store), WithCredentials("trader@example.com", "secret"), withCode, WithMaxRetries(0))
	if err := client.Login(context.Background()); !errors.Is(err, ErrServer) {
		t.Errorf("Login() = %v, want ErrServer", err)
	}
	if got := auth.grants(); !slices.Equal(got, []string{"refresh_token"}) || store.clears != 0 {
		t.Errorf("grants = %v, store cleared %d times; want the refresh alone and the session kept", got, store.clears)
	}
}

func TestRefreshRejectedWithoutPassword(t *testing.T) {
	mux := http.NewServeMux()
	newAuthServer(t, mux).reply = func(url.Values) (int, string) {
		return http.StatusUnauthorized, `{"error":"unauthorized"}`
	}
	store := &memoryStore{token: &Token{AccessToken: "expired", RefreshToken: "r0", Expiry: time.Now().Add(-time.Minute)}}
	client := newTestClient(t, mux, WithTokenStore(store))
	if err := client.Login(context.Background()); !errors.Is(err, ErrAuthentication) {
		t.Errorf("Login() = %v, want ErrAuthentication", err)
	}
	if store.token != nil {
		t.Errorf("stored token = %+v, want none", store.token)
	}
}

func TestExpiredRefreshTokenIsNotSent(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	store := &memoryStore{token: &Token{
		AccessToken:  "expired",
		RefreshToken: testJWT("refresh", time.Now().Add(-time.Minute)),
		Expiry:       time.Now().Add(-time.Minute),
	}}
	client := newTestClient(t, mux, WithTokenStore(store), WithCredentials("trader@example.com", "secret"), withCode)
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := auth.grants(); !slices.Equal(got, []string{"password", "password"}) {
		t.Errorf("grants = %v, want a login without a refresh", got)
	}
}

func TestTokenAboutToExpireWithoutRenewal(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	mux.HandleFunc("GET /echo", bearerEcho)
	access := testJWT("last", time.Now().Add(30*time.Second))
	client := newTestClient(t, mux, WithTokenStore(&memoryStore{token: &Token{AccessToken: access}}))
	if token := callEcho(t, client); token != access {
		t.Errorf("token = %q, want the one that cannot be renewed", token)
	}
	if len(auth.grants()) != 0 {
		t.Errorf("grants = %v, want none", auth.grants())
	}
}

func TestRefreshAdoptsTokenOfAnotherProcess(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	mux.HandleFunc("GET /echo", bearerEcho)
	store := &memoryStore{token: &Token{AccessToken: "old", RefreshToken: "r0", Expiry: time.Now().Add(-time.Minute)}}
	client := newTestClient(t, mux, WithTokenStore(store))
	if _, err := client.core.currentToken(context.Background()); err != nil {
		t.Fatal(err)
	}
	// Another process renews the session meanwhile.
	renewed := testJWT("other", time.Now().Add(time.Hour))
	store.Save(context.Background(), &Token{AccessToken: renewed, RefreshToken: "r1"})

	if token := callEcho(t, client); token != renewed {
		t.Errorf("token = %q, want the one the other process saved", token)
	}
	if len(auth.grants()) != 0 {
		t.Errorf("grants = %v, want none", auth.grants())
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

func TestLoginOutlivesCanceledCaller(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	mux.HandleFunc("GET /echo", bearerEcho)
	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(CodeFunc(func(ctx context.Context) (string, error) {
			once.Do(func() { close(started) })
			<-release
			return "123456", ctx.Err()
		})))

	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error)
	go func() { first <- client.Login(ctx) }()
	<-started
	second := make(chan string)
	go func() { second <- callEcho(t, client) }()
	// Wait until the second caller waits for the login too.
	for waiters := 0; waiters < 2; {
		time.Sleep(time.Millisecond)
		client.core.session.mu.Lock()
		if f := client.core.session.flight; f != nil {
			waiters = f.waiters
		}
		client.core.session.mu.Unlock()
	}
	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatalf("Login() = %v, want context.Canceled", err)
	}
	close(release)
	if token := <-second; jwtExpiry(token).IsZero() {
		t.Errorf("second caller got %q, want the token of the shared login", token)
	}
	if got := auth.grants(); len(got) != 2 {
		t.Errorf("grants = %v, want a single login", got)
	}
}

func TestAbandonedLoginIsCanceled(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	var prompts atomic.Int32
	stuck := make(chan error, 1)
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(CodeFunc(func(ctx context.Context) (string, error) {
			if prompts.Add(1) == 1 {
				// Nobody answers the first prompt.
				<-ctx.Done()
				stuck <- ctx.Err()
				return "", ctx.Err()
			}
			return "123456", nil
		})))

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := client.Login(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Login() = %v, want context.DeadlineExceeded", err)
	}
	if err := <-stuck; !errors.Is(err, context.Canceled) {
		t.Errorf("prompt context = %v, want it canceled once nobody waits", err)
	}
	if err := client.Login(context.Background()); err != nil {
		t.Fatalf("Login() after an abandoned one = %v", err)
	}
	if prompts.Load() != 2 {
		t.Errorf("prompts = %d, want a new one", prompts.Load())
	}
}

func TestFileTokenStore(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	mux.HandleFunc("GET /echo", bearerEcho)
	store := FileTokenStore{Path: filepath.Join(t.TempDir(), "config", "token.json")}
	ctx := context.Background()

	if token, err := store.Load(ctx); token != nil || err != nil {
		t.Fatalf("Load() on a missing file = %v, %v; want nil, nil", token, err)
	}
	auth.mfa = true
	first := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithTokenStore(store))
	token := callEcho(t, first)
	second := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithTokenStore(store))
	if got := callEcho(t, second); got != token {
		t.Errorf("second client sent %q, want the stored token", got)
	}
	if grants := auth.grants(); len(grants) != 2 {
		t.Errorf("grants = %v, want a single login", grants)
	}
	saved, err := store.Load(ctx)
	if err != nil || saved.AccessToken != token || saved.Expiry.IsZero() {
		t.Errorf("Load() = %+v, %v", saved, err)
	}
	if info, err := os.Stat(store.Path); err != nil || info.Mode().Perm() != 0o600 {
		t.Errorf("token file mode = %v, %v; want 0600", info.Mode(), err)
	}

	for _, content := range []string{"{", `{"access_token":7}`, `[]`, `{}`} {
		os.WriteFile(store.Path, []byte(content), 0o600)
		if token, err := store.Load(ctx); token != nil || err != nil {
			t.Errorf("Load() of %s = %v, %v; want no token", content, token, err)
		}
	}
	if err := store.Clear(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(store.Path); !os.IsNotExist(err) || store.Clear(ctx) != nil {
		t.Errorf("Clear() left the file: %v", err)
	}
}

func TestFailingTokenStore(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	mux.HandleFunc("GET /echo", bearerEcho)
	store := &failingStore{loadErr: errors.New("disk on fire")}
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithTokenStore(store))

	if err := client.Request(context.Background(), testService, http.MethodGet, "/echo", nil, nil); err == nil {
		t.Fatal("Request() succeeded although the token store failed to load")
	}
	store.loadErr = nil
	if token := callEcho(t, client); token == "" {
		t.Error("no token sent")
	}
	if store.saves.Load() != 1 {
		t.Errorf("saves = %d, want an attempt", store.saves.Load())
	}
}

func TestSessionEnd(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	var logouts atomic.Int32
	mux.HandleFunc("POST /logout", func(w http.ResponseWriter, r *http.Request) {
		logouts.Add(1)
		if r.Header.Get("Authorization") == "" {
			t.Error("logout sent without the token")
		}
		w.WriteHeader(http.StatusInternalServerError)
	})
	mux.HandleFunc("GET /echo", bearerEcho)
	store := &memoryStore{}
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode, WithTokenStore(store), WithMaxRetries(0))
	logout := func() error {
		op := testOperation(http.MethodPost, "/logout")
		op.session = sessionEnd
		return client.core.exec(context.Background(), op, nil)
	}

	// Without a session, nothing is sent: the client does not sign in to sign out.
	if err := logout(); err != nil || logouts.Load() != 0 || len(auth.grants()) != 0 {
		t.Fatalf("logout without a session = %v after %d requests and grants %v", err, logouts.Load(), auth.grants())
	}
	first := callEcho(t, client)
	client.SetMFAToken("mfa")
	if err := logout(); !errors.Is(err, ErrServer) {
		t.Errorf("logout() = %v, want the server's error", err)
	}
	if logouts.Load() != 1 || store.token != nil || store.clears != 2 || client.core.mfaToken.Load() != nil {
		t.Errorf("after logout: %d requests, stored %+v, %d clears, MFA token %v", logouts.Load(), store.token, store.clears, client.core.mfaToken.Load())
	}
	if second := callEcho(t, client); second == first || len(auth.grants()) != 4 {
		t.Errorf("after logout the client sent %q with grants %v, want a new login", second, auth.grants())
	}
}

func TestSessionEndDiscardsLoginUnderWay(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	asked := make(chan struct{})
	release := make(chan struct{})
	store := &memoryStore{}
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), WithTokenStore(store),
		WithCodeProvider(CodeFunc(func(context.Context) (string, error) {
			close(asked)
			<-release
			return "123456", nil
		})))
	done := make(chan error)
	go func() { done <- client.Login(context.Background()) }()
	<-asked
	if err := client.core.endSession(context.Background()); err != nil {
		t.Fatal(err)
	}
	close(release)
	if err := <-done; !errors.Is(err, ErrAuthentication) {
		t.Errorf("Login() = %v, want it to fail as the session ended", err)
	}
	if store.token != nil || client.core.session.token != nil {
		t.Errorf("the login finished after the logout was kept: %+v", store.token)
	}
}

func TestSessionReplace(t *testing.T) {
	mux := http.NewServeMux()
	newAuthServer(t, mux)
	mux.HandleFunc("GET /echo", bearerEcho)
	replaced := testJWT("replaced", time.Now().Add(time.Hour))
	mux.HandleFunc("PUT /password", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]any{"access_token": replaced, "refresh_token": "r2", "expires_in": 3600})
	})
	store := &memoryStore{token: &Token{AccessToken: testJWT("old", time.Now().Add(time.Hour)), RefreshToken: "r1"}}
	client := newTestClient(t, mux, WithTokenStore(store))

	op := testOperation(http.MethodPut, "/password")
	op.session = sessionReplace
	if err := client.core.exec(context.Background(), op, nil); err != nil {
		t.Fatal(err)
	}
	if token := callEcho(t, client); token != replaced {
		t.Errorf("token = %q, want the one of the new session", token)
	}
	if store.token.AccessToken != replaced || store.token.RefreshToken != "r2" {
		t.Errorf("stored token = %+v", store.token)
	}
}

func TestSessionReplaceUpdatesPassword(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	mux.HandleFunc("PUT /password", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]any{"access_token": testJWT("replaced", time.Now().Add(time.Hour))})
	})
	mux.HandleFunc("POST /logout", func(http.ResponseWriter, *http.Request) {})
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "old"), withCode)
	ctx := context.Background()
	if err := client.Login(ctx); err != nil {
		t.Fatal(err)
	}
	change := testOperation(http.MethodPut, "/password")
	change.session = sessionReplace
	change.body = jsonBody(map[string]string{"oldPassword": "old", "newPassword": "new"})
	if err := client.core.exec(ctx, change, nil); err != nil {
		t.Fatal(err)
	}
	if token := client.core.session.token; token.RefreshToken != "refresh-2" {
		t.Errorf("session = %+v, want the previous refresh token kept", token)
	}
	logout := testOperation(http.MethodPost, "/logout")
	logout.session = sessionEnd
	if err := client.core.exec(ctx, logout, nil); err != nil {
		t.Fatal(err)
	}
	if err := client.Login(ctx); err != nil {
		t.Fatal(err)
	}
	if password := auth.form(3).Get("password"); password != "new" {
		t.Errorf("login after the change sent password %q, want the new one", password)
	}
}

func TestSessionReplaceAfterEnd(t *testing.T) {
	mux := http.NewServeMux()
	arrived, release := make(chan struct{}), make(chan struct{})
	mux.HandleFunc("PUT /password", func(w http.ResponseWriter, _ *http.Request) {
		close(arrived)
		<-release
		writeJSON(w, map[string]any{"access_token": "replaced", "refresh_token": "r2"})
	})
	store := &memoryStore{token: &Token{AccessToken: testJWT("old", time.Now().Add(time.Hour))}}
	client := newTestClient(t, mux, WithTokenStore(store), WithCredentials("trader@example.com", "old"))
	change := testOperation(http.MethodPut, "/password")
	change.session = sessionReplace
	change.body = jsonBody(map[string]string{"newPassword": "new"})
	done := make(chan error)
	go func() { done <- client.core.exec(context.Background(), change, nil) }()
	<-arrived
	if err := client.core.endSession(context.Background()); err != nil {
		t.Fatal(err)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if client.core.session.token != nil || store.token != nil || *client.core.password.Load() != "old" {
		t.Errorf("the session ended during the change came back: %+v, %+v", client.core.session.token, store.token)
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
	client.SetMFAToken(" mfa-1\n")
	call()
	client.SetMFAToken("")
	call()
	if got := []string{<-headers, <-headers, <-headers}; !slices.Equal(got, []string{"", "mfa-1", ""}) {
		t.Errorf("x-mfa-token headers = %q", got)
	}
}

type memoryStore struct {
	mu     sync.Mutex
	token  *Token
	saves  int
	clears int
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

func (s *memoryStore) Clear(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.token = nil
	s.clears++
	return nil
}

// failingStore fails to load while loadErr is set, and always fails to save.
type failingStore struct {
	loadErr error
	saves   atomic.Int32
}

func (s *failingStore) Load(context.Context) (*Token, error) { return nil, s.loadErr }

func (s *failingStore) Save(context.Context, *Token) error {
	s.saves.Add(1)
	return errors.New("read-only")
}

func (s *failingStore) Clear(context.Context) error { return nil }
