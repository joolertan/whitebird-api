// Command login signs in, keeps the session in whitebird-session.json and prints the profile of
// the account. Run it twice: the second run reuses the saved session instead of signing in again.
package main

import (
	"context"
	"fmt"
	"log"

	"github.com/joolertan/whitebird-api/examples/go/internal/envclient"
)

func main() {
	client, err := envclient.New()
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
