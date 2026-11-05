package whitebird

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"
)

type sampleParams struct {
	Page      int       `query:"page,omitempty" json:"-"`
	Sort      []string  `query:"sort" json:"-"`
	From      Time      `query:"from,omitempty" json:"-"`
	Min       Decimal   `query:"min,omitempty" json:"-"`
	Status    *string   `query:"status" json:"-"`
	RequestID string    `header:"X-Request-Id" json:"-"`
	Amount    Decimal   `json:"amount"`
	Note      *string   `json:"note,omitzero"`
	Tags      []string  `json:"tags,omitzero"`
	Day       Date      `json:"day,omitzero"`
	Kind      orderKind `json:"kind,omitzero"`
}

type orderKind string

func TestOperationEncoding(t *testing.T) {
	mux := http.NewServeMux()
	var got *http.Request
	var body []byte
	mux.HandleFunc("POST /orders/{id}", func(w http.ResponseWriter, r *http.Request) {
		got = r
		body, _ = io.ReadAll(r.Body)
		writeJSON(w, map[string]string{"id": r.PathValue("id")})
	})
	client := newTestClient(t, mux)
	params := sampleParams{
		Sort:      []string{"created,desc", "id"},
		From:      Time{Time: time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)},
		RequestID: "req-1",
		Amount:    MustDecimal("0.10"),
		Note:      Ptr(""),
	}
	op := testOperation(http.MethodPost, "/orders/{id}")
	op.pathParams = []string{"a/b"}
	op.public, op.params, op.body = true, params, jsonBody(params)

	out, err := call[*struct{ ID string }](context.Background(), client.core, op, []RequestOption{WithHeader("X-Extra", "1"), WithQuery("extra", "2")})
	if err != nil {
		t.Fatal(err)
	}
	if out.ID != "a/b" {
		t.Errorf("path value = %q, want a/b", out.ID)
	}
	if want := "extra=2&from=2026-01-02T03%3A04%3A05%2B0000&sort=created%2Cdesc&sort=id"; got.URL.RawQuery != want {
		t.Errorf("query = %s, want %s", got.URL.RawQuery, want)
	}
	if want := `{"amount":0.10,"note":""}`; string(body) != want {
		t.Errorf("body = %s, want %s", body, want)
	}
	for key, want := range map[string]string{
		"X-Request-Id":  "req-1",
		"X-Extra":       "1",
		"Content-Type":  "application/json",
		"Accept":        "application/json",
		"User-Agent":    "whitebird-api-go/" + Version,
		"Authorization": "",
	} {
		if value := got.Header.Get(key); value != want {
			t.Errorf("header %s = %q, want %q", key, value, want)
		}
	}
}

func TestFormAndMultipartBodies(t *testing.T) {
	type params struct {
		Address string    `form:"address"`
		Memo    *string   `form:"memo"`
		File    io.Reader `form:"file"`
		Skipped io.Reader `form:"skipped"`
	}
	contentType, data, err := formBody(params{Address: "addr", Memo: Ptr("hi")})()
	if err != nil || contentType != "application/x-www-form-urlencoded" || string(data) != "address=addr&memo=hi" {
		t.Errorf("formBody() = %q, %q, %v", contentType, data, err)
	}

	contentType, data, err = multipartBody(params{Address: "addr", File: strings.NewReader("png")})()
	if err != nil {
		t.Fatal(err)
	}
	req := &http.Request{Header: http.Header{"Content-Type": {contentType}}, Body: io.NopCloser(strings.NewReader(string(data)))}
	if err := req.ParseMultipartForm(1 << 20); err != nil {
		t.Fatal(err)
	}
	if req.FormValue("address") != "addr" || req.MultipartForm.File["file"][0].Filename != "file" || len(req.MultipartForm.File["skipped"]) != 0 {
		t.Errorf("multipart form = %+v", req.MultipartForm)
	}
}

func TestMultipartParts(t *testing.T) {
	type crop struct {
		Size int `json:"size"`
	}
	type params struct {
		Request string      `form:"request" mime:"application/json"`
		Crop    *crop       `form:"crop"`
		Note    string      `form:"note"`
		Amount  Decimal     `form:"amount"`
		Photo   io.Reader   `form:"photo" mime:"image/png"`
		Files   []io.Reader `form:"files"`
	}
	contentType, data, err := multipartBody(params{
		Request: `{"criteria":["a"]}`,
		Crop:    &crop{Size: 64},
		Note:    "hi",
		Amount:  MustDecimal("1.5"),
		Photo:   strings.NewReader("png"),
		Files:   []io.Reader{strings.NewReader("one"), strings.NewReader("two")},
	})()
	if err != nil {
		t.Fatal(err)
	}
	_, mediaParams, err := mime.ParseMediaType(contentType)
	if err != nil {
		t.Fatal(err)
	}
	reader := multipart.NewReader(bytes.NewReader(data), mediaParams["boundary"])
	var parts []string
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(part)
		parts = append(parts, fmt.Sprintf("%s %q %s %s", part.FormName(), part.FileName(), part.Header.Get("Content-Type"), body))
	}
	want := []string{
		`request "" application/json {"criteria":["a"]}`,
		`crop "" application/json {"size":64}`,
		`note ""  hi`,
		`amount ""  1.5`,
		`photo "photo" image/png png`,
		`files "files" text/plain; charset=utf-8 one`,
		`files "files" text/plain; charset=utf-8 two`,
	}
	if !slices.Equal(parts, want) {
		t.Errorf("parts = %q\nwant %q", parts, want)
	}
}

func TestEnvelope(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /tickers", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{"retCode":0,"retMsg":"OK","result":[{"symbol":"BTCUSDT","lastPrice":"97000.5"}]}`))
	})
	client := newTestClient(t, mux)
	op := testOperation(http.MethodGet, "/tickers")
	op.public, op.envelope = true, true

	tickers, err := call[[]struct {
		Symbol    string
		LastPrice Decimal
	}](context.Background(), client.core, op, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(tickers) != 1 || tickers[0].Symbol != "BTCUSDT" || tickers[0].LastPrice.String() != "97000.5" {
		t.Errorf("tickers = %+v", tickers)
	}
}

func TestUnwrapEnvelope(t *testing.T) {
	tests := []struct {
		body, result string
		code         int
	}{
		{`{"retCode":0,"result":[1]}`, `[1]`, 0},
		{`{"retCode":200,"retMsg":"OK","result":{"a":1}}`, `{"a":1}`, 0},
		{`{"code":0,"result":"x"}`, `"x"`, 0},
		{`{"result":{"a":1}}`, `{"a":1}`, 0},
		{`{"a":1}`, `{"a":1}`, 0},
		{`{"retCode":"0","result":2}`, `2`, 0},
		{`{"retCode":10001,"retMsg":"params error"}`, "", 10001},
		{`{"code":5,"message":"failed"}`, "", 5},
	}
	for _, tt := range tests {
		result, err := unwrapEnvelope([]byte(tt.body))
		var apiErr *APIError
		switch {
		case tt.code != 0 && (!errors.As(err, &apiErr) || apiErr.Code != tt.code || apiErr.Message == ""):
			t.Errorf("unwrapEnvelope(%s) = %v, want an error with code %d", tt.body, err, tt.code)
		case tt.code == 0 && (err != nil || string(result) != tt.result):
			t.Errorf("unwrapEnvelope(%s) = %s, %v; want %s", tt.body, result, err, tt.result)
		}
	}
}

func TestDecodeResultAcceptsText(t *testing.T) {
	var s string
	if err := decodeResult([]byte("created"), &s); err != nil || s != "created" {
		t.Errorf("string from text = %q, %v", s, err)
	}
	if err := decodeResult([]byte(`"quoted"`), &s); err != nil || s != "quoted" {
		t.Errorf("string from JSON = %q, %v", s, err)
	}
	var raw json.RawMessage
	if err := decodeResult([]byte("OK"), &raw); err != nil || string(raw) != `"OK"` {
		t.Errorf("untyped from text = %s, %v; want it as a JSON string", raw, err)
	}
	if err := decodeResult([]byte(" {\"a\":1}\n"), &raw); err != nil || string(raw) != `{"a":1}` {
		t.Errorf("untyped from JSON = %s, %v", raw, err)
	}
	if err := decodeResult(nil, &raw); err != nil || raw != nil {
		t.Errorf("untyped from nothing = %s, %v", raw, err)
	}
	var typed struct{ A int }
	if err := decodeResult([]byte("OK"), &typed); err == nil {
		t.Error("a typed result accepted text")
	}
}

func TestPathParameters(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /things/{id}/parts/{part}", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(r.URL.EscapedPath()))
	})
	client := newTestClient(t, mux)
	op := testOperation(http.MethodGet, "/things/{id}/parts/{part}")
	op.public = true
	op.pathParams = []string{`a/b\c?d`, "é 1"}
	path, err := client.core.text(context.Background(), op, nil)
	if want := "/things/a%2Fb%5Cc%3Fd/parts/%C3%A9%201"; err != nil || path != want {
		t.Errorf("path = %q, %v; want %q", path, err, want)
	}
	for _, value := range []string{"", ".", ".."} {
		op.pathParams = []string{value, "x"}
		if _, err := client.core.text(context.Background(), op, nil); err == nil || !strings.Contains(err.Error(), "path parameter id") {
			t.Errorf("path parameter %q: %v, want it refused", value, err)
		}
	}
}

func TestLanguage(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /language", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(r.Header.Get("Accept-Language")))
	})
	client := newTestClient(t, mux, WithLanguage("ru"))
	op := testOperation(http.MethodGet, "/language")
	op.public = true
	if got, err := client.core.text(context.Background(), op, nil); err != nil || got != "ru" {
		t.Errorf("Accept-Language = %q, %v", got, err)
	}
	if got, err := client.core.text(context.Background(), op, []RequestOption{WithHeader("Accept-Language", "en")}); err != nil || got != "en" {
		t.Errorf("Accept-Language with WithHeader = %q, %v", got, err)
	}
}

func TestMultipartFilesAndText(t *testing.T) {
	type caption struct {
		Text string `json:"text"`
	}
	type params struct {
		Named   io.Reader `form:"named"`
		Typed   io.Reader `form:"typed"`
		Sniffed io.Reader `form:"sniffed"`
		Caption *caption  `form:"caption" mime:"text/plain"`
	}
	contentType, data, err := multipartBody(params{
		Named:   File{Name: "passport.pdf", Body: strings.NewReader("%PDF-1.7")},
		Typed:   &File{Name: "scan", ContentType: "image/heic", Body: strings.NewReader("heic")},
		Sniffed: strings.NewReader("\x89PNG\r\n\x1a\n...."),
		Caption: &caption{Text: "hi"},
	})()
	if err != nil {
		t.Fatal(err)
	}
	_, mediaParams, _ := mime.ParseMediaType(contentType)
	reader := multipart.NewReader(bytes.NewReader(data), mediaParams["boundary"])
	var parts []string
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(part)
		parts = append(parts, fmt.Sprintf("%s %q %q %q", part.FormName(), part.FileName(), part.Header.Get("Content-Type"), body))
	}
	want := []string{
		`named "passport.pdf" "application/pdf" "%PDF-1.7"`,
		`typed "scan" "image/heic" "heic"`,
		`sniffed "sniffed" "image/png" "\x89PNG\r\n\x1a\n...."`,
		// A plain field, as the web application sends the JSON text.
		`caption "" "" "{\"text\":\"hi\"}"`,
	}
	if !slices.Equal(parts, want) {
		t.Errorf("parts = %q\nwant %q", parts, want)
	}
}

func TestRequestRetcodeEnvelope(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /instruments", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("category") == "bad" {
			w.Write([]byte(`{"retCode":10001,"retMsg":"params error","result":{}}`))
			return
		}
		w.Write([]byte(`{"retCode":0,"retMsg":"OK","result":{"list":[{"symbol":"BTCUSDT"}]}}`))
	})
	client := newTestClient(t, mux)
	ctx := context.Background()

	var out struct {
		List []struct{ Symbol string }
	}
	if err := client.Request(ctx, testService, http.MethodGet, "/instruments", nil, &out, WithoutAuth(), WithRetcodeEnvelope()); err != nil {
		t.Fatal(err)
	}
	if len(out.List) != 1 || out.List[0].Symbol != "BTCUSDT" {
		t.Errorf("result = %+v", out)
	}

	err := client.Request(ctx, testService, http.MethodGet, "/instruments", nil, &out, WithoutAuth(), WithRetcodeEnvelope(), WithQuery("category", "bad"))
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Code != 10001 {
		t.Errorf("Request() = %v, want the retCode error", err)
	}

	var raw []byte
	if err := client.Request(ctx, testService, http.MethodGet, "/instruments", nil, &raw, WithoutAuth()); err != nil || !bytes.HasPrefix(raw, []byte(`{"retCode":0`)) {
		t.Errorf("without the option, body = %s, %v; want the envelope", raw, err)
	}
}

func TestResponseKinds(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /report", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Accept") != "*/*" {
			t.Errorf("Accept = %q", r.Header.Get("Accept"))
		}
		w.Write([]byte{0x25, 0x50, 0x44, 0x46})
	})
	mux.HandleFunc("GET /text", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte("uploaded"))
	})
	mux.HandleFunc("DELETE /thing", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	client := newTestClient(t, mux)
	ctx := context.Background()
	public := func(method, path string) *operation {
		op := testOperation(method, path)
		op.public = true
		return op
	}

	if data, err := client.core.binary(ctx, public(http.MethodGet, "/report"), nil); err != nil || string(data) != "%PDF" {
		t.Errorf("binary() = %q, %v", data, err)
	}
	if text, err := client.core.text(ctx, public(http.MethodGet, "/text"), nil); err != nil || text != "uploaded" {
		t.Errorf("text() = %q, %v", text, err)
	}
	if err := client.core.exec(ctx, public(http.MethodDelete, "/thing"), nil); err != nil {
		t.Errorf("exec() = %v", err)
	}
}

func TestRequest(t *testing.T) {
	mux := http.NewServeMux()
	newAuthServer(t, mux).mfa = true
	mux.HandleFunc("PUT /things/{id}", func(w http.ResponseWriter, r *http.Request) {
		var in map[string]any
		if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
			t.Error(err)
		}
		writeJSON(w, map[string]any{
			"id":    r.PathValue("id"),
			"name":  in["name"],
			"auth":  strings.HasPrefix(r.Header.Get("Authorization"), "Bearer "),
			"query": r.URL.Query().Get("dry_run"),
		})
	})
	mux.HandleFunc("POST /form", func(w http.ResponseWriter, r *http.Request) {
		r.ParseForm()
		w.Write([]byte(r.PostForm.Get("a")))
	})
	client := newTestClient(t, mux, WithCredentials("trader@example.com", "secret"), withCode)
	ctx := context.Background()

	var thing struct {
		ID    string
		Name  string
		Auth  bool
		Query string
	}
	err := client.Request(ctx, testService, http.MethodPut, "/things/7", map[string]string{"name": "lamp"}, &thing, WithQuery("dry_run", "true"))
	if err != nil {
		t.Fatal(err)
	}
	if thing.ID != "7" || thing.Name != "lamp" || !thing.Auth || thing.Query != "true" {
		t.Errorf("thing = %+v", thing)
	}

	var raw []byte
	if err := client.Request(ctx, testService, http.MethodPost, "/form", url.Values{"a": {"b"}}, &raw, WithoutAuth()); err != nil {
		t.Fatal(err)
	}
	if string(raw) != "b" {
		t.Errorf("raw body = %q, want b", raw)
	}

	err = client.Request(ctx, Service("unknown"), http.MethodGet, "/", nil, nil)
	if err == nil || !strings.Contains(err.Error(), "unknown service") {
		t.Errorf("Request() to an unknown service = %v", err)
	}
}

func TestDecodeKeepsNumbers(t *testing.T) {
	var out map[string]any
	if err := decode([]byte(`{"amount":0.1000000000000000055511151231257827}`), &out); err != nil {
		t.Fatal(err)
	}
	if n, ok := out["amount"].(json.Number); !ok || n.String() != "0.1000000000000000055511151231257827" {
		t.Errorf("amount = %#v, want the exact json.Number", out["amount"])
	}
	var empty *struct{}
	if err := decode([]byte("  "), &empty); err != nil || empty != nil {
		t.Errorf("decode(empty) = %v, %v", empty, err)
	}
	if err := decode([]byte("{"), &out); err == nil || !strings.HasPrefix(err.Error(), "whitebird: decode response") {
		t.Errorf("decode(invalid) = %v", err)
	}
}

func TestNewClientValidation(t *testing.T) {
	if _, err := NewClient(WithEnvironment("staging")); err == nil {
		t.Error("NewClient accepted an unknown environment")
	}
	if _, err := NewClient(WithBaseURL(testService, "not a url")); err == nil {
		t.Error("NewClient accepted an invalid base URL")
	}
	client, err := NewClient(WithEnvironment(Demo), WithBaseURL(testService, "https://example.test/prefix/"))
	if err != nil {
		t.Fatal(err)
	}
	if got := client.core.baseURLs[testService]; got != "https://example.test/prefix" {
		t.Errorf("base URL = %q", got)
	}
	for service, hosts := range serviceURLs {
		want := hosts[Demo]
		if want == "" {
			want = hosts[Production]
		}
		if got := client.core.baseURLs[service]; got != want {
			t.Errorf("demo URL of %s = %q, want %q", service, got, want)
		}
	}
}

func TestUnsupportedParameter(t *testing.T) {
	_, err := encodeValues(struct {
		Filter map[string]string `query:"filter"`
	}{Filter: map[string]string{"a": "b"}}, "query")
	if err == nil || !strings.Contains(err.Error(), "query filter") {
		t.Errorf("encodeValues() = %v, want an error naming the parameter", err)
	}
}

func TestEmptyModelResponse(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /empty", func(http.ResponseWriter, *http.Request) {})
	mux.HandleFunc("GET /null", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("null")) })
	client := newTestClient(t, mux)
	for _, path := range []string{"/empty", "/null"} {
		op := testOperation(http.MethodGet, path)
		op.public = true
		out, err := call[*struct{ Name string }](context.Background(), client.core, op, nil)
		if err != nil || out == nil {
			t.Errorf("%s: call() = %v, %v; want an empty result", path, out, err)
		}
	}
}
