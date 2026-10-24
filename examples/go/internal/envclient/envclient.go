// Package envclient builds the client the examples share from environment variables.
package envclient

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"strings"

	whitebird "github.com/joolertan/whitebird-api/go"
	"github.com/joolertan/whitebird-api/go/otp"
)

// SessionFile keeps the session between runs, so that only the first one signs in.
const SessionFile = "whitebird-session.json"

// New returns a client that signs in with WHITEBIRD_USERNAME and WHITEBIRD_PASSWORD and keeps
// its session in SessionFile. Two-factor codes are asked for in the terminal, or computed from
// WHITEBIRD_TOTP_SECRET when it is set.
func New(opts ...whitebird.Option) (*whitebird.Client, error) {
	codes, err := codeProvider()
	if err != nil {
		return nil, err
	}
	opts = append([]whitebird.Option{
		whitebird.WithCredentials(os.Getenv("WHITEBIRD_USERNAME"), os.Getenv("WHITEBIRD_PASSWORD")),
		whitebird.WithCodeProvider(codes),
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

// askCode reads a two-factor code from the terminal.
func askCode(context.Context) (string, error) {
	fmt.Print("Two-factor code: ")
	line, err := bufio.NewReader(os.Stdin).ReadString('\n')
	return strings.TrimSpace(line), err
}
