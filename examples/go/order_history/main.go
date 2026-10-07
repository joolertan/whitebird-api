// Command order_history prints the most recent exchange orders. The iterator fetches further
// pages as the loop goes on; breaking out of it stops fetching.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"time"

	"github.com/joolertan/whitebird-api/examples/go/internal/envclient"
	whitebird "github.com/joolertan/whitebird-api/go"
)

func main() {
	limit := flag.Int("n", 30, "number of orders to print")
	flag.Parse()

	client, err := envclient.New()
	if err != nil {
		log.Fatal(err)
	}
	params := whitebird.ExchangeOrdersHistoryParams{Size: 20, Sort: []string{"creationDate,desc"}}
	printed := 0
	for order, err := range client.Exchange.Orders.History(context.Background(), params) {
		if err != nil {
			log.Fatal(err)
		}
		fmt.Printf("#%-8d %s  %-12s %s -> %s\n", order.Number, order.CreationDate.Format(time.DateTime),
			order.Status, leg(order.Input), leg(order.Output))
		if printed++; printed == *limit {
			break
		}
	}
}

func leg(l *whitebird.ExchangeLeg) string {
	if l == nil {
		return "?"
	}
	return fmt.Sprintf("%s %s", l.Amount, l.Asset)
}
