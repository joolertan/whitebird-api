// Command balances prints the balances of the Whitebird wallet.
package main

import (
	"context"
	"fmt"
	"log"

	"github.com/joolertan/whitebird-api/examples/go/internal/envclient"
	whitebird "github.com/joolertan/whitebird-api/go"
)

func main() {
	client, err := envclient.New()
	if err != nil {
		log.Fatal(err)
	}
	account, err := client.Accounting.Accounts.Get(context.Background(), whitebird.AccountingAccountsGetParams{
		Type: whitebird.AccountingAccountTypeUserBalance,
	})
	if err != nil {
		log.Fatal(err)
	}
	for _, b := range account.Balances {
		fmt.Printf("%-10s %s\n", b.Currency, b.Amount)
	}
}
