// Command login_totp signs in with a TOTP code computed from the 2FA secret, keeps the session
// in a file and prints the profile of the account. Run it twice: the second run reuses the
// saved session instead of signing in again.
package main

import (
	"context"
	"fmt"
	"log"
	"os"

	whitebird "github.com/joolertan/whitebird-api/go"
	"github.com/joolertan/whitebird-api/go/otp"
)

func main() {
	totp, err := otp.New(os.Getenv("WHITEBIRD_TOTP_SECRET"))
	if err != nil {
		log.Fatal(err)
	}
	client, err := whitebird.NewClient(
		whitebird.WithCredentials(os.Getenv("WHITEBIRD_USERNAME"), os.Getenv("WHITEBIRD_PASSWORD")),
		whitebird.WithCodeProvider(totp),
		whitebird.WithTokenStore(whitebird.FileTokenStore{Path: "whitebird-session.json"}),
	)
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.Background()

	if err := client.Login(ctx); err != nil {
		log.Fatal(err)
	}
	me, err := client.User.Me(ctx)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("Signed in as %s\n", me.Email)
	fmt.Printf("  status: %s\n  roles:  %v\n  2FA:    %t\n", me.UserStatus, me.Roles, me.MFAEnabled)
}
