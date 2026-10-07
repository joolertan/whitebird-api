// Command exchange quotes a conversion between two assets of the Whitebird wallet and, with
// -confirm, places the exchange order.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"

	"github.com/joolertan/whitebird-api/examples/go/internal/envclient"
	whitebird "github.com/joolertan/whitebird-api/go"
)

func main() {
	from := flag.String("from", "USDT", "asset to sell")
	to := flag.String("to", "BTC", "asset to buy")
	amount := flag.String("amount", "10", "amount to sell")
	confirm := flag.Bool("confirm", false, "place the order instead of only quoting it")
	flag.Parse()

	sell, err := whitebird.NewDecimal(*amount)
	if err != nil {
		log.Fatal(err)
	}
	client, err := envclient.New()
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.Background()

	quote, err := client.Exchange.Quotes.Create(ctx, whitebird.ExchangeQuotesCreateParams{
		Input:  whitebird.ExchangeQuoteLegRequest{Asset: *from, Type: whitebird.ExchangeLegTypeInternalBalance, Amount: sell},
		Output: whitebird.ExchangeQuoteLegRequest{Asset: *to, Type: whitebird.ExchangeLegTypeInternalBalance},
	})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("Quote %s: %s %s -> %s %s at %s, valid until %s\n", quote.ID,
		quote.Input.Amount, quote.Input.Asset, quote.Output.Amount, quote.Output.Asset,
		quote.ExchangeRateValue, quote.ExpirationDate)
	if !*confirm {
		fmt.Println("Dry run: pass -confirm to place the order.")
		return
	}

	order, err := client.Exchange.Orders.Create(ctx, whitebird.ExchangeOrdersCreateParams{
		QuoteID:     quote.ID,
		Destination: whitebird.ExchangeDestinationAccounting,
	})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("Order #%d placed: %s\n", order.Number, order.Status)
}
