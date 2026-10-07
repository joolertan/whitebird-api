// Package envclient builds the client the examples share from environment variables.
package envclient

import (
	"os"

	whitebird "github.com/joolertan/whitebird-api/go"
	"github.com/joolertan/whitebird-api/go/otp"
)

// New returns a client that signs in with WHITEBIRD_USERNAME and WHITEBIRD_PASSWORD and, for
// accounts with two-factor authentication, codes computed from WHITEBIRD_TOTP_SECRET.
func New(opts ...whitebird.Option) (*whitebird.Client, error) {
	opts = append([]whitebird.Option{
		whitebird.WithCredentials(os.Getenv("WHITEBIRD_USERNAME"), os.Getenv("WHITEBIRD_PASSWORD")),
	}, opts...)
	if secret := os.Getenv("WHITEBIRD_TOTP_SECRET"); secret != "" {
		totp, err := otp.New(secret)
		if err != nil {
			return nil, err
		}
		opts = append(opts, whitebird.WithCodeProvider(totp))
	}
	return whitebird.NewClient(opts...)
}
