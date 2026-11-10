package whitebird_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/http/httptest"
	"strconv"
	"time"

	whitebird "github.com/joolertan/whitebird-api/go"
	"github.com/joolertan/whitebird-api/go/otp"
)

// exampleSecret is the TOTP secret of the account served by fakeWhitebird.
const exampleSecret = "JBSWY3DPEHPK3PXP"

// fakeWhitebird stands in for the Whitebird services in the examples. It serves the token
// endpoint, which wants a TOTP code, and a few brokerage endpoints.
func fakeWhitebird() *httptest.Server {
	totp, _ := otp.New(exampleSecret)
	mux := http.NewServeMux()
	reply := func(w http.ResponseWriter, status int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(v)
	}
	mux.HandleFunc("POST /oauth/token", func(w http.ResponseWriter, r *http.Request) {
		// Like the real server, accept the previous code too, in case a period just ended.
		current, _ := totp.At(time.Now())
		previous, _ := totp.At(time.Now().Add(-30 * time.Second))
		if code := r.FormValue("code"); code != current && code != previous {
			reply(w, http.StatusOK, map[string]bool{"mfaEnabled": true})
			return
		}
		reply(w, http.StatusOK, map[string]any{"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
	})
	mux.HandleFunc("GET /api/v1/brokerage/subaccount/me", func(w http.ResponseWriter, _ *http.Request) {
		reply(w, http.StatusOK, map[string]any{
			"uuid": "4d1c", "displayName": "Main", "status": "ACTIVE",
			"balances": map[string]any{"BTC": 0.5, "USDT": 1200},
		})
	})
	mux.HandleFunc("GET /api/v1/brokerage/subaccount/{id}", func(w http.ResponseWriter, _ *http.Request) {
		reply(w, http.StatusNotFound, map[string]any{
			"code": 404, "status": "SUBACCOUNT_NOT_FOUND", "message": "Subaccount not found",
		})
	})
	mux.HandleFunc("POST /api/v1/brokerage/otc-order/page", func(w http.ResponseWriter, r *http.Request) {
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		orders := [][]map[string]any{
			{{"uuid": "o-1", "fromAsset": "USDT", "toAsset": "BTC", "status": "COMPLETED"},
				{"uuid": "o-2", "fromAsset": "BTC", "toAsset": "USDT", "status": "NEW"}},
			{{"uuid": "o-3", "fromAsset": "USDT", "toAsset": "ETH", "status": "REJECTED"}},
		}
		reply(w, http.StatusOK, map[string]any{
			"content": orders[page], "number": page, "size": 2,
			"totalElements": 3, "totalPages": 2, "last": page == 1,
		})
	})
	mux.HandleFunc("GET /api/v1/brokerage/broker/fees", func(w http.ResponseWriter, _ *http.Request) {
		reply(w, http.StatusOK, map[string]any{"USDT": "0.1"})
	})
	return httptest.NewServer(mux)
}

// newExampleClient returns a client logged in to fakeWhitebird.
func newExampleClient(url string) *whitebird.Client {
	totp, err := otp.New(exampleSecret)
	if err != nil {
		log.Fatal(err)
	}
	client, err := whitebird.NewClient(
		whitebird.WithCredentials("trader@example.com", "password"),
		whitebird.WithCodeProvider(totp),
		whitebird.WithBaseURL(whitebird.ServiceAuth, url),
		whitebird.WithBaseURL(whitebird.ServiceBrokerage, url),
	)
	if err != nil {
		log.Fatal(err)
	}
	return client
}

func Example() {
	srv := fakeWhitebird()
	defer srv.Close()

	// The TOTP secret is the one shown next to the QR code when two-factor authentication is
	// turned on.
	totp, err := otp.New(exampleSecret)
	if err != nil {
		log.Fatal(err)
	}
	client, err := whitebird.NewClient(
		whitebird.WithCredentials("trader@example.com", "password"),
		whitebird.WithCodeProvider(totp),
		// Real code leaves the base URLs out.
		whitebird.WithBaseURL(whitebird.ServiceAuth, srv.URL),
		whitebird.WithBaseURL(whitebird.ServiceBrokerage, srv.URL),
	)
	if err != nil {
		log.Fatal(err)
	}

	me, err := client.Brokerage.Subaccounts.Me(context.Background())
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(me.DisplayName, me.Status, me.Balances["BTC"])
	// Output: Main ACTIVE 0.5
}

func ExampleBrokerageOTCOrdersResource_List() {
	srv := fakeWhitebird()
	defer srv.Close()
	client := newExampleClient(srv.URL)

	params := whitebird.BrokerageOTCOrdersListParams{Size: 2}
	for order, err := range client.Brokerage.OTCOrders.List(context.Background(), params) {
		if err != nil {
			log.Fatal(err)
		}
		fmt.Println(order.UUID, order.FromAsset, "->", order.ToAsset, order.Status)
	}
	// Output:
	// o-1 USDT -> BTC COMPLETED
	// o-2 BTC -> USDT NEW
	// o-3 USDT -> ETH REJECTED
}

func ExampleBrokerageOTCOrdersResource_ListPage() {
	srv := fakeWhitebird()
	defer srv.Close()
	client := newExampleClient(srv.URL)

	page, err := client.Brokerage.OTCOrders.ListPage(context.Background(), whitebird.BrokerageOTCOrdersListParams{Size: 2})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("%d of %d orders, more: %v\n", len(page.Content), page.TotalElements, page.HasNext())
	// Output: 2 of 3 orders, more: true
}

func ExampleAPIError() {
	srv := fakeWhitebird()
	defer srv.Close()
	client := newExampleClient(srv.URL)

	_, err := client.Brokerage.Subaccounts.Get(context.Background(), "missing")
	if errors.Is(err, whitebird.ErrNotFound) {
		fmt.Println("no such subaccount")
	}
	var apiErr *whitebird.APIError
	if errors.As(err, &apiErr) {
		fmt.Println(apiErr.StatusCode, apiErr.ErrorCode, apiErr.Message)
	}
	// Output:
	// no such subaccount
	// 404 SUBACCOUNT_NOT_FOUND Subaccount not found
}

func ExampleClient_Request() {
	srv := fakeWhitebird()
	defer srv.Close()
	client := newExampleClient(srv.URL)

	// An endpoint this package does not cover yet.
	var fees map[string]whitebird.Decimal
	err := client.Request(context.Background(), whitebird.ServiceBrokerage, http.MethodGet, "/api/v1/brokerage/broker/fees", nil, &fees)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(fees["USDT"])
	// Output: 0.1
}

func ExampleNewClient_mfaRequired() {
	srv := fakeWhitebird()
	defer srv.Close()

	client, err := whitebird.NewClient(
		whitebird.WithCredentials("trader@example.com", "password"),
		whitebird.WithBaseURL(whitebird.ServiceAuth, srv.URL),
	)
	if err != nil {
		log.Fatal(err)
	}
	if err := client.Login(context.Background()); errors.Is(err, whitebird.ErrMFARequired) {
		fmt.Println("this account needs a TOTP code: use WithCodeProvider")
	}
	// Output: this account needs a TOTP code: use WithCodeProvider
}

func ExampleFileTokenStore() {
	// Keep the session across restarts, so that the program does not log in every time it
	// starts. The file holds credentials: keep it private.
	client, err := whitebird.NewClient(
		whitebird.WithCredentials("trader@example.com", "password"),
		whitebird.WithTokenStore(whitebird.FileTokenStore{Path: "whitebird-token.json"}),
		whitebird.WithTimeout(10*time.Second),
	)
	if err != nil {
		log.Fatal(err)
	}
	_ = client
}
