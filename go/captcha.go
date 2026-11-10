package whitebird

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// Captcha is the slider captcha that Whitebird may ask for at sign-in: the puzzle piece must be
// slid horizontally into the cut-out of the background. See [WithCaptchaSolver].
type Captcha struct {
	// RequestID identifies the captcha.
	RequestID string
	// BackgroundImage is a PNG image with the cut-out.
	BackgroundImage []byte
	// PuzzlePiece is a PNG image of the piece to slide.
	PuzzlePiece []byte
	// PositionY is the vertical position of the piece in background pixels.
	PositionY int
}

// captchaAttempts limits the captchas solved for one login.
const captchaAttempts = 3

// solveCaptcha gets a captcha, has the configured solver solve it and returns the answer as the
// value of the captcha header of the token request.
func (c *core) solveCaptcha(ctx context.Context) (string, error) {
	id := newRequestID()
	p := &prepared{
		method:     http.MethodGet,
		url:        c.baseURLs[tokenService] + captchaPath + "?" + url.Values{"requestId": {id}}.Encode(),
		header:     http.Header{"Accept": {"application/json"}},
		public:     true,
		timeout:    c.cfg.timeout,
		maxRetries: c.cfg.maxRetries,
		idempotent: true,
	}
	r, err := c.do(ctx, p, false)
	if err != nil {
		return "", err
	}
	var resp struct {
		BackgroundImage string `json:"backgroundImage"`
		PuzzlePiece     string `json:"puzzlePiece"`
		PositionY       int    `json:"positionY"`
	}
	if err := json.Unmarshal(r.body, &resp); err != nil {
		return "", fmt.Errorf("whitebird: decode captcha: %w", err)
	}
	captcha := Captcha{RequestID: id, PositionY: resp.PositionY}
	if captcha.BackgroundImage, err = decodeImage(resp.BackgroundImage); err != nil {
		return "", fmt.Errorf("whitebird: decode captcha background: %w", err)
	}
	if captcha.PuzzlePiece, err = decodeImage(resp.PuzzlePiece); err != nil {
		return "", fmt.Errorf("whitebird: decode captcha puzzle piece: %w", err)
	}
	x, err := c.cfg.captcha(ctx, captcha)
	if err != nil {
		return "", fmt.Errorf("whitebird: solve captcha: %w", err)
	}
	// The web application sends the value of its range input, a string.
	answer, err := json.Marshal(struct {
		SliderPosition string `json:"sliderPosition"`
		RequestID      string `json:"requestId"`
	}{strconv.Itoa(x), id})
	return base64.StdEncoding.EncodeToString(answer), err
}

// decodeImage decodes a base64 image, with or without a data URL prefix.
func decodeImage(s string) ([]byte, error) {
	if rest, ok := strings.CutPrefix(s, "data:"); ok {
		_, s, _ = strings.Cut(rest, ",")
	}
	return base64.StdEncoding.DecodeString(s)
}

// newRequestID returns a random UUID (version 4).
func newRequestID() string {
	var b [16]byte
	rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}
