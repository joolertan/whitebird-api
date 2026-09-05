import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from whitebird_codegen.build import load
from whitebird_codegen.emitters import go
from whitebird_codegen.emitters._util import HEADER
from whitebird_codegen.ir import IR

FIXTURE = Path(__file__).parent / "fixtures" / "spec"
MODULE = Path(__file__).resolve().parents[3] / "go"

needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="Go is not installed")


@pytest.fixture(scope="module")
def ir() -> IR:
    return load(FIXTURE)


@pytest.fixture(scope="module")
def files(ir: IR) -> dict[str, str]:
    if shutil.which("gofmt") is None:
        pytest.skip("gofmt is not installed")
    return go.emit(ir)


def test_files_are_owned_and_marked(files: dict[str, str]) -> None:
    assert sorted(files) == [
        "go/balances_gen.go",
        "go/client_gen.go",
        "go/enums_gen.go",
        "go/environments_gen.go",
        "go/events_gen.go",
        "go/models_gen.go",
        "go/orders_gen.go",
        "go/products_gen.go",
        "go/profile_gen.go",
        "go/tickers_gen.go",
    ]
    for path, content in files.items():
        assert any(fnmatch.fnmatch(path, pattern) for pattern in go.OWNED)
        assert content.startswith(f"// {HEADER}\n\npackage whitebird\n")


def test_output_is_deterministic(ir: IR, files: dict[str, str]) -> None:
    assert go.emit(ir) == files


def test_names() -> None:
    assert go.exported("otc_orders") == "OTCOrders"
    assert go.exported("brokerUuid") == "BrokerUUID"
    assert go.exported("subaccountIds") == "SubaccountIDs"
    assert go.exported("X-Request-Id") == "XRequestID"
    assert go.unexported("productId") == "productID"
    assert go.unexported("type") == "typeParam"
    assert go.unexported("url") == "urlParam"


def test_doc_comments() -> None:
    assert go.lead("Create", "Create a deposit") == "Create creates a deposit."
    assert go.lead("Search", "search orders") == "Search searches orders."
    assert go.lead("Confirm", "Approve or reject it") == "Confirm approves or rejects it."
    assert go.lead("Balances", "Returns balances.") == "Balances returns balances."
    assert go.lead("Confirm", "Client confirmation step") is None
    assert go.links(["A", "B", "C", "D", "E"]) == "[A], [B], [C], [D] and 1 other"


def test_operation_docs(files: dict[str, str]) -> None:
    products = files["go/products_gen.go"]
    assert (
        "// Get gets a product.\n//\n"
        "// It calls GET /api/v1/products/{productId} without authentication.\n"
    ) in products
    assert (
        "// Deprecated: Whitebird has deprecated this endpoint.\nfunc (r *ProductsResource) Delete("
    ) in products
    assert "This endpoint is experimental" in files["go/orders_gen.go"]
    assert "operation MFA token for\n// WITHDRAWAL" in files["go/orders_gen.go"]


def test_type_rules(files: dict[str, str]) -> None:
    models, orders = files["go/models_gen.go"], files["go/orders_gen.go"]
    # Responses: values, nested models as pointers.
    assert 'Dimensions  *ProductDimensions `json:"dimensions,omitzero"`' in models
    assert 'Discount    float64            `json:"discount,omitzero"`' in models
    # Requests: optional fields as pointers, flattened bodies, header and query tags.
    assert 'Priority   *Priority `json:"priority,omitzero"`' in orders
    assert 'XRequestID *string   `header:"X-Request-Id" json:"-"`' in orders
    assert 'Page     int           `json:"page,omitzero"`' in orders
    assert 'File io.Reader `form:"file"`' in files["go/profile_gen.go"]
    # Flattened request bodies are not models.
    assert "type OrderRequest " not in models
    assert "Priority1 Priority = 1" in files["go/enums_gen.go"]


def test_core_name_clashes_are_renamed(ir: IR) -> None:
    emitter = go.Emitter(ir)
    assert emitter._type_name("Page", "shop") == "ShopPage"
    assert emitter._type_name("Service", None) == "ServiceEnum"


@needs_go
def test_fixture_module_builds_and_works(tmp_path: Path, files: dict[str, str]) -> None:
    module = tmp_path / "go"
    shutil.copytree(
        MODULE,
        module,
        ignore=lambda _, names: [n for n in names if n.endswith(("_gen.go", "_test.go"))],
    )
    for path, content in files.items():
        (tmp_path / path).write_text(content)
    (module / "fixture_test.go").write_text(FIXTURE_TEST)
    env = {**os.environ, "GOFLAGS": "-mod=mod", "GOTOOLCHAIN": "local", "GOPROXY": "off"}
    commands = (["go", "build", "./..."], ["go", "vet", "./..."], ["go", "test", "-race", "./..."])
    for command in commands:
        result = subprocess.run(command, cwd=module, env=env, capture_output=True, text=True)
        assert result.returncode == 0, f"{' '.join(command)}:\n{result.stdout}{result.stderr}"


# Exercises every operation of the fixture spec against a fake server.
FIXTURE_TEST = r"""package whitebird

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

func fixtureClient(t *testing.T, mux *http.ServeMux) *Client {
	mux.HandleFunc("POST /oauth/token", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"access_token":"token","refresh_token":"refresh"}`)
	})
	mux.HandleFunc("POST /api/v1/client/mfa", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"mfaEnabled":false}`)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	client, err := NewClient(
		WithCredentials("user@example.test", "secret"),
		WithBaseURL(ServiceAuth, srv.URL),
		WithBaseURL(ServiceShop, srv.URL),
	)
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func expect(t *testing.T, what string, got, want any) {
	t.Helper()
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("%s = %v, want %v", what, got, want)
	}
}

func TestFixtureOperations(t *testing.T) {
	ctx := context.Background()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/products", func(w http.ResponseWriter, r *http.Request) {
		expect(t, "products auth", r.Header.Get("Authorization"), "")
		q := r.URL.Query()
		expect(t, "products query", q["tags"], []string{"a", "b"})
		expect(t, "products category", q.Get("category"), "in-progress")
		page, _ := strconv.Atoi(q.Get("page"))
		fmt.Fprintf(w, `{"content":[{"id":%d,"name":"p","price":"1.10","createdAt":"2026-01-02T03:04:05"}],"totalElements":2,"totalPages":2,"number":%d,"size":1}`, page+1, page)
	})
	mux.HandleFunc("GET /api/v1/products/{id}", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"id":%s,"name":"lamp","dimensions":{"width":2}}`, r.PathValue("id"))
	})
	mux.HandleFunc("DELETE /api/v1/products/{id}", func(w http.ResponseWriter, r *http.Request) {
		expect(t, "delete auth", r.Header.Get("Authorization"), "Bearer token")
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("POST /api/v1/orders", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		expect(t, "order body", string(body), `{"productId":7,"quantity":2,"priority":3}`)
		expect(t, "order header", r.Header.Get("X-Request-Id"), "req-1")
		fmt.Fprint(w, `{"id":"o1","status":"FILLED","total":12.5}`)
	})
	mux.HandleFunc("POST /api/v1/orders/search", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if strings.Contains(string(body), `"page":1`) {
			fmt.Fprint(w, `{"content":[],"totalElements":1}`)
			return
		}
		expect(t, "search body", string(body), `{"statuses":["NEW"]}`)
		fmt.Fprint(w, `{"content":[{"id":"o1","status":"NEW"}],"totalElements":1}`)
	})
	mux.HandleFunc("POST /api/v1/orders/{id}/withdraw", func(w http.ResponseWriter, r *http.Request) {
		r.ParseForm()
		expect(t, "withdraw form", r.PostForm.Encode(), "address=addr")
		expect(t, "withdraw MFA token", r.Header.Get("X-Mfa-Token"), "mfa")
		expect(t, "withdraw id", r.PathValue("id"), "a/b")
	})
	mux.HandleFunc("GET /api/v1/orders/{id}/receipt", func(w http.ResponseWriter, r *http.Request) {
		expect(t, "receipt format", r.URL.Query().Get("format"), "PDF")
		w.Write([]byte("%PDF"))
	})
	mux.HandleFunc("GET /api/v1/balances", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"BTC":0.100000000000000000001}`)
	})
	mux.HandleFunc("POST /api/v1/avatar", func(w http.ResponseWriter, r *http.Request) {
		file, header, err := r.FormFile("file")
		if err != nil {
			t.Error(err)
			return
		}
		data, _ := io.ReadAll(file)
		fmt.Fprintf(w, "%s:%s", header.Filename, data)
	})
	mux.HandleFunc("GET /api/v1/tickers", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"retCode":0,"retMsg":"OK","result":[{"symbol":"BTCUSDT"}]}`)
	})
	mux.HandleFunc("GET /api/v1/events", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "event: update\nid: 1\ndata: {\"type\":\"order\"}\n\n")
	})
	client := fixtureClient(t, mux)

	var ids []int64
	for product, err := range client.Products.List(ctx, ProductsListParams{Size: 1, Category: Ptr(CategoryInProgress), Tags: []string{"a", "b"}}) {
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, product.ID)
		expect(t, "price", product.Price, "1.10")
		expect(t, "created at", product.CreatedAt.Hour(), 3)
	}
	expect(t, "product IDs", ids, []int64{1, 2})

	product, err := client.Products.Get(ctx, 42)
	if err != nil {
		t.Fatal(err)
	}
	expect(t, "product", fmt.Sprintf("%d %s %v", product.ID, product.Name, product.Dimensions.Width), "42 lamp 2")
	if err := client.Products.Delete(ctx, 42); err != nil {
		t.Fatal(err)
	}

	order, err := client.Orders.Create(ctx, OrdersCreateParams{XRequestID: Ptr("req-1"), ProductID: 7, Quantity: 2, Priority: Ptr(Priority3)})
	if err != nil {
		t.Fatal(err)
	}
	expect(t, "order", fmt.Sprintf("%s %s", order.Status, order.Total), "FILLED 12.5")

	var found []string
	for order, err := range client.Orders.Search(ctx, OrdersSearchParams{Statuses: []OrderStatus{OrderStatusNew}}) {
		if err != nil {
			t.Fatal(err)
		}
		found = append(found, order.ID)
	}
	expect(t, "search", found, []string{"o1"})

	client.SetMFAToken("mfa")
	if err := client.Orders.Withdraw(ctx, "a/b", OrdersWithdrawParams{Address: "addr"}); err != nil {
		t.Fatal(err)
	}
	receipt, err := client.Orders.Documents.Receipt(ctx, "o1", OrdersDocumentsReceiptParams{Format: FormatPDF})
	if err != nil {
		t.Fatal(err)
	}
	expect(t, "receipt", string(receipt), "%PDF")

	balances, err := client.Balances.Get(ctx)
	if err != nil {
		t.Fatal(err)
	}
	expect(t, "balance", balances["BTC"], "0.100000000000000000001")

	text, err := client.Profile.UploadAvatar(ctx, ProfileUploadAvatarParams{File: strings.NewReader("png")})
	if err != nil {
		t.Fatal(err)
	}
	expect(t, "upload", text, "file:png")

	tickers, err := client.Tickers.List(ctx, TickersListParams{})
	if err != nil {
		t.Fatal(err)
	}
	expect(t, "tickers", tickers[0].Symbol, "BTCUSDT")

	for event, err := range client.Events.Stream(ctx) {
		if err != nil {
			t.Fatal(err)
		}
		expect(t, "event", fmt.Sprintf("%s %s %s", event.Name, event.ID, event.Data.Type), "update 1 order")
		break
	}
}
"""  # noqa: E501
