// Package envclient builds the client the examples share from environment variables.
package envclient

import (
	"bufio"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	whitebird "github.com/joolertan/whitebird-api/go"
	"github.com/joolertan/whitebird-api/go/otp"
)

// SessionFile keeps the session between runs, so that only the first one signs in.
const SessionFile = "whitebird-session.json"

// New returns a client that signs in with WHITEBIRD_USERNAME and WHITEBIRD_PASSWORD and keeps
// its session in SessionFile. Two-factor codes are asked for in the terminal, or computed from
// WHITEBIRD_TOTP_SECRET when it is set; accounts without two-factor authentication solve the
// sign-in captcha on a local page.
func New(opts ...whitebird.Option) (*whitebird.Client, error) {
	codes, err := codeProvider()
	if err != nil {
		return nil, err
	}
	opts = append([]whitebird.Option{
		whitebird.WithCredentials(os.Getenv("WHITEBIRD_USERNAME"), os.Getenv("WHITEBIRD_PASSWORD")),
		whitebird.WithCodeProvider(codes),
		whitebird.WithCaptchaSolver(solveCaptcha),
		whitebird.WithTokenStore(whitebird.FileTokenStore{Path: SessionFile}),
	}, opts...)
	return whitebird.NewClient(opts...)
}

func codeProvider() (whitebird.CodeProvider, error) {
	if secret := os.Getenv("WHITEBIRD_TOTP_SECRET"); secret != "" {
		return otp.New(secret)
	}
	return whitebird.CodeFunc(askCode), nil
}

// stdin is shared by the prompts, so that input one of them reads ahead is not lost.
var stdin = bufio.NewReader(os.Stdin)

func readLine() (string, error) {
	line, err := stdin.ReadString('\n')
	if errors.Is(err, io.EOF) && line != "" {
		err = nil // the last line of piped input may lack a newline
	}
	return strings.TrimSpace(line), err
}

// askCode reads a two-factor code from the terminal.
func askCode(context.Context) (string, error) {
	fmt.Print("Two-factor code: ")
	return readLine()
}

const captchaPage = `<!doctype html>
<meta charset="utf-8">
<title>Whitebird captcha</title>
<div style="position: relative; width: 290px; height: 192px">
  <img src="data:image/png;base64,%s">
  <img id="piece" src="data:image/png;base64,%s" style="position: absolute; left: 0; top: %dpx">
</div>
<input id="slider" type="range" min="0" max="250" value="0" style="width: 290px">
<p>Slider position: <b id="value">0</b></p>
<script>
  slider.oninput = () => {
    piece.style.left = slider.value + "px";
    value.textContent = slider.value;
  };
</script>
`

// solveCaptcha shows the captcha on a local page and reads the slider position found there.
func solveCaptcha(_ context.Context, c whitebird.Captcha) (int, error) {
	path := filepath.Join(os.TempDir(), "whitebird-captcha.html")
	enc := base64.StdEncoding
	page := fmt.Sprintf(captchaPage, enc.EncodeToString(c.BackgroundImage), enc.EncodeToString(c.PuzzlePiece), c.PositionY)
	if err := os.WriteFile(path, []byte(page), 0o600); err != nil {
		return 0, err
	}
	fmt.Printf("Open %s in a browser, slide the piece into the gap and enter the number shown: ", path)
	answer, err := readLine()
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(answer)
}
