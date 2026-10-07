// Command notifications prints account notifications as they arrive, until interrupted with
// Ctrl+C. The stream reconnects on its own when the connection drops.
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"time"

	"github.com/joolertan/whitebird-api/examples/go/internal/envclient"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	client, err := envclient.New()
	if err != nil {
		return err
	}
	fmt.Println("Waiting for notifications; press Ctrl+C to stop.")
	for event, err := range client.Notifications.Stream(ctx) {
		if ctx.Err() != nil {
			return nil
		}
		if err != nil {
			return err
		}
		n := event.Data
		fmt.Printf("%s %s: %s\n", n.CreatedAt.Format(time.TimeOnly), n.Type, event.Raw)
	}
	return nil
}
