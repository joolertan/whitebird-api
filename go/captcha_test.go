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
	answer, _ := base64.StdEncoding.DecodeString(answers[0])
	if want := fmt.Sprintf(`{"sliderPosition":117,"requestId":%q}`, c.RequestID); string(answer) != want {
		t.Errorf("captcha header = %s, want %s", answer, want)
	}
	if auth.firstForm().Has("code") {
		t.Error("login sent a TOTP code for an account without 2FA")
	}
}

func TestLoginRetriesInvalidCaptcha(t *testing.T) {
	for _, rejections := range []int{2, 3} {
		t.Run(fmt.Sprint(rejections, " rejections"), func(t *testing.T) {
			mux := http.NewServeMux()
			auth := newAuthServer(t, mux)
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
	auth.mfaFails = true
	auth.reply = func(url.Values) (int, string) {
		return http.StatusBadRequest, `{"status":"CAPTCHA_REQUIRED","message":"Solve the captcha"}`
	}
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"))

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

func TestLoginWithTwoFactorSkipsCaptcha(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	auth.mfa = true
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCodeProvider(StaticCode("123456")),
		WithCaptchaSolver(func(context.Context, Captcha) (int, error) {
			t.Error("solver called for an account with 2FA")
			return 0, nil
		}),
	)
	if err := client.Login(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, answers := auth.captchas(); auth.firstForm().Get("code") != "123456" || answers[0] != "" {
		t.Errorf("token request = %v with captcha %q, want the code and no captcha", auth.firstForm(), answers[0])
	}
}

func TestCaptchaSolverError(t *testing.T) {
	mux := http.NewServeMux()
	auth := newAuthServer(t, mux)
	errGiveUp := errors.New("no idea")
	client := newTestClient(t, mux,
		WithCredentials("trader@example.com", "secret"),
		WithCaptchaSolver(func(context.Context, Captcha) (int, error) { return 0, errGiveUp }),
	)
	if err := client.Login(context.Background()); !errors.Is(err, errGiveUp) {
		t.Errorf("Login() = %v, want the solver error", err)
	}
	if len(auth.grants()) != 0 {
		t.Error("token requested without a captcha answer")
	}
}
