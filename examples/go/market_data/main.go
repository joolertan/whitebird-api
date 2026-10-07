// Command market_data prints spot instruments, an order book and recent hourly candles. Market
// data is public: no account is needed.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"time"

	whitebird "github.com/joolertan/whitebird-api/go"
)

func main() {
	demo := flag.Bool("demo", false, "use the demo trading contour")
	symbol := flag.String("symbol", "BTCUSDT", "instrument symbol")
	flag.Parse()

	env := whitebird.Production
	if *demo {
		env = whitebird.Demo
	}
	client, err := whitebird.NewClient(whitebird.WithEnvironment(env))
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.Background()

	instruments, err := client.Market.Instruments(ctx, whitebird.MarketInstrumentsParams{
		Category: whitebird.Ptr(whitebird.MarketCategorySpot),
	})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("%d spot instruments, for example:\n", len(instruments))
	for _, in := range instruments[:min(5, len(instruments))] {
		fmt.Printf("  %-10s %s/%s, tick size %s\n", in.Symbol, in.BaseCoin, in.QuoteCoin, in.TickSize)
	}

	book, err := client.Market.Orderbook(ctx, whitebird.MarketOrderbookParams{Symbol: *symbol, Limit: whitebird.Ptr(5)})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("\n%s order book:\n  %12s %10s   %-12s %s\n", book.Symbol, "bid size", "bid", "ask", "ask size")
	for i := range min(len(book.Bids), len(book.Asks)) {
		bid, ask := book.Bids[i], book.Asks[i]
		fmt.Printf("  %12s %10s   %-12s %s\n", bid[1], bid[0], ask[0], ask[1])
	}

	candles, err := client.Market.Klines(ctx, whitebird.MarketKlinesParams{
		Symbol:   *symbol,
		Interval: whitebird.MarketKlineInterval1h,
		Start:    whitebird.Ptr(time.Now().Add(-5 * time.Hour).UnixMilli()),
	})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("\n%s hourly candles:\n", *symbol)
	for _, c := range candles {
		start := time.UnixMilli(c.Start).UTC().Format(time.DateTime)
		fmt.Printf("  %s  open %s  high %s  low %s  close %s\n", start, c.Open, c.High, c.Low, c.Close)
	}
}
