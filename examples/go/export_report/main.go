// Command export_report downloads the PDF statement of the exchange orders completed in the
// last days.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/joolertan/whitebird-api/examples/go/internal/envclient"
	whitebird "github.com/joolertan/whitebird-api/go"
)

// dateFrameLayout is the timestamp format of report date frames.
const dateFrameLayout = "2006-01-02T15:04:05-0700"

func main() {
	days := flag.Int("days", 30, "number of days to cover")
	out := flag.String("o", "exchange-report.pdf", "file to write")
	flag.Parse()

	client, err := envclient.New()
	if err != nil {
		log.Fatal(err)
	}
	end := time.Now()
	start := end.AddDate(0, 0, -*days)
	report, err := client.Reports.DownloadExchange(context.Background(), whitebird.ReportsDownloadExchangeParams{
		CompletionDateFrame: &whitebird.ReportDateFrame{
			Start: whitebird.Ptr(start.Format(dateFrameLayout)),
			End:   whitebird.Ptr(end.Format(dateFrameLayout)),
		},
	})
	if err != nil {
		log.Fatal(err)
	}
	if err := os.WriteFile(*out, report, 0o600); err != nil {
		log.Fatal(err)
	}
	fmt.Printf("Saved %d bytes to %s\n", len(report), *out)
}
