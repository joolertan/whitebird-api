package whitebird

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"sync/atomic"
	"testing"
)

var uuidV4 = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func TestLoginSolvesCaptcha(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.wantCaptcha = true
	var got []Captcha
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(StaticCode("123456")),
		WithCaptchaSolver(func(_ context.Context, c Captcha) (int, error) {
			got = append(got, c)
			return 117, nil
		}),
	)
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}

	if len(got) != 1 || !uuidV4.MatchString(got[0].RequestID) {
		t.Fatalf("solver got %+v, want one captcha with a UUID v4 request ID", got)
	}
	c := got[0]
	if string(c.BackgroundImage) != "background" || string(c.PuzzlePiece) != "piece" || c.PositionY != 42 {
		t.Errorf("captcha = %+v", c)
	}
	ids, answers := auth.captchas()
	if !slices.Equal(ids, []string{c.RequestID}) {
		t.Errorf("captchas requested = %v, want [%s]", ids, c.RequestID)
	}
	if len(answers) != 2 || answers[0] != "" {
		t.Fatalf("captcha headers = %q, want none, then the answer", answers)
	}
	answer, _ := base64.StdEncoding.DecodeString(answers[1])
	// The web application sends the position as the string value of its slider.
	if want := fmt.Sprintf(`{"sliderPosition":"117","requestId":%q}`, c.RequestID); string(answer) != want {
		t.Errorf("captcha header = %s, want %s", answer, want)
	}
	if auth.form(1).Has("code") || auth.form(1).Encode() != auth.firstForm().Encode() {
		t.Errorf("grant with the captcha = %v, want the first grant again", auth.form(1))
	}
}

func TestLoginRetriesInvalidCaptcha(t *testing.T) {
	for _, rejections := range []int{2, 3} {
		t.Run(fmt.Sprint(rejections, " rejections"), func(t *testing.T) {
			mux := http.NewServeMux()
			auth := newAuthServer(t, mux)
			auth.wantCaptcha = true
			var attempts atomic.Int32
			auth.reply = func(url.Values) (int, string) {
				if attempts.Add(1) <= int32(rejections) {
					return http.StatusBadRequest, `{"status":"INVALID_CAPTCHA","message":"Invalid captcha"}`
				}
				return 0, ""
			}
			solved := 0
			client := newTestClient(t, mux,
				WithCredentials("trader@example.com", "secret"),
				WithCaptchaSolver(func(context.Context, Captcha) (int, error) {
					solved++
					return 100, nil
				}),
			)

			err := client.Login(context.Background())
			ids, _ := auth.captchas()
			if solved != 3 || len(slices.Compact(slices.Sorted(slices.Values(ids)))) != 3 {
				t.Errorf("solved %d captchas with request IDs %v, want 3 different ones", solved, ids)
			}
			if rejections < captchaAttempts {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			var apiErr *APIError
			if !errors.As(err, &apiErr) || apiErr.ErrorCode != "INVALID_CAPTCHA" || errors.Is(err, ErrCaptchaRequired) {
				t.Errorf("Login() = %v, want the INVALID_CAPTCHA error", err)
			}
		})
	}
}

func TestLoginCaptchaRequired(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	auth.reply = func(url.Values) (int, string) {
		return http.StatusBadRequest, `{"status":"CAPTCHA_REQUIRED","message":"Solve the captcha"}`
	}
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode)

	err := client.Login(context.Background())
	if !errors.Is(err, ErrCaptchaRequired) || !errors.Is(err, ErrAuthentication) {
		t.Fatalf("Login() = %v, want ErrCaptchaRequired", err)
	}
	var apiErr *APIError
	if errors.As(err, &apiErr); apiErr.ErrorCode != "CAPTCHA_REQUIRED" {
		t.Errorf("ErrorCode = %q, want the one of the server", apiErr.ErrorCode)
	}
	if ids, answers := auth.captchas(); answers[0] != "" || len(ids) != 0 {
		t.Error("login without a solver requested a captcha")
	}
}

func TestLoginSolvesNoCaptchaUnlessAsked(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(StaticCode("123456")),
		WithCaptchaSolver(func(context.Context, Captcha) (int, error) {
			t.Error("solver called although the server asked for no captcha")
			return 0, nil
		}),
	)
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, answers := auth.captchas(); auth.form(1).Get("code") != "123456" || answers[0] != "" || answers[1] != "" {
		t.Errorf("token requests = %v with captchas %q, want the code and no captcha", auth.forms, answers)
	}
}

func TestLoginAfterCaptchaAsksForCode(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.wantCaptcha = true
	auth.reply = func(form url.Values) (int, string) {
		if !form.Has("code") {
			return http.StatusOK, `{"mfaEnabled":true}`
		}
		return 0, ""
	}
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(StaticCode("123456")),
		WithCaptchaSolver(func(context.Context, Captcha) (int, error) { return 7, nil }),
	)
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}
	// Once asked for, the captcha goes with every grant of the login, a new one each time.
	ids, answers := auth.captchas()
	if len(answers) != 3 || answers[0] != "" || answers[1] == "" || answers[2] == "" || answers[1] == answers[2] ||
		len(ids) != 2 || auth.form(1).Has("code") || auth.form(2).Get("code") != "123456" {
		t.Errorf("token requests = %v with captchas %q, want none, the captcha, then the code with a new one", auth.forms, answers)
	}
}

func TestCaptchaSolverError(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.wantCaptcha = true
	errGiveUp := errors.New("no idea")
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCaptchaSolver(func(context.Context, Captcha) (int, error) { return 0, errGiveUp }),
	)
	if err := client.Login(context.Background()); !errors.Is(err, errGiveUp) {
		t.Errorf("Login() = %v, want the solver error", err)
	}
	if _, answers := auth.captchas(); len(answers) != 1 || answers[0] != "" {
		t.Errorf("captcha headers = %q, want only the first grant, without one", answers)
	}
}
