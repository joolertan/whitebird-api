package whitebird

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
)

type pageParams struct {
	Page int `query:"page,omitempty"`
	Size int `query:"size,omitempty"`
}

// numbersServer serves the numbers 1 to total in pages, recording the pages requested.
func numbersServer(t *testing.T, total int) (*Client, func() []int) {
	var mu sync.Mutex
	var requested []int
	mux := http.NewServeMux()
	mux.HandleFunc("GET /numbers", func(w http.ResponseWriter, r *http.Request) {
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		size, _ := strconv.Atoi(r.URL.Query().Get("size"))
		mu.Lock()
		requested = append(requested, page)
		mu.Unlock()
		if page == 99 {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		content := []int{}
		for n := page*size + 1; n <= min((page+1)*size, total); n++ {
			content = append(content, n)
		}
		pages := (total + size - 1) / size
		writeJSON(w, map[string]any{
			"content": content, "totalElements": total, "totalPages": pages,
			"number": page, "size": size, "last": page+1 >= pages,
		})
	})
	client := newTestClient(t, mux, WithMaxRetries(0))
	return client, func() []int {
		mu.Lock()
		defer mu.Unlock()
		return slices.Clone(requested)
	}
}

func numbers(client *Client, params pageParams) func(func(int, error) bool) {
	return paginate(params.Page, func(page int) (*Page[int], error) {
		p := params
		p.Page = page
		op := testOperation(http.MethodGet, "/numbers")
		op.public, op.params = true, p
		return call[*Page[int]](context.Background(), client.core, op, nil)
	})
}

func TestPaginationAcrossPages(t *testing.T) {
	client, requested := numbersServer(t, 7)
	var got []int
	for n, err := range numbers(client, pageParams{Size: 3}) {
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, n)
	}
	if !slices.Equal(got, []int{1, 2, 3, 4, 5, 6, 7}) {
		t.Errorf("items = %v", got)
	}
	if !slices.Equal(requested(), []int{0, 1, 2}) {
		t.Errorf("pages requested = %v, want [0 1 2]", requested())
	}
}

func TestPaginationStartsAtParamsPage(t *testing.T) {
	client, requested := numbersServer(t, 7)
	var got []int
	for n, err := range numbers(client, pageParams{Page: 1, Size: 3}) {
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, n)
	}
	if !slices.Equal(got, []int{4, 5, 6, 7}) || !slices.Equal(requested(), []int{1, 2}) {
		t.Errorf("items = %v from pages %v", got, requested())
	}
}

func TestPaginationEarlyBreak(t *testing.T) {
	client, requested := numbersServer(t, 100)
	var got []int
	for n, err := range numbers(client, pageParams{Size: 2}) {
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, n)
		if n == 3 {
			break
		}
	}
	if !slices.Equal(got, []int{1, 2, 3}) || !slices.Equal(requested(), []int{0, 1}) {
		t.Errorf("items = %v from pages %v; want [1 2 3] from [0 1]", got, requested())
	}
}

func TestPaginationError(t *testing.T) {
	client, _ := numbersServer(t, 1000)
	var items int
	var errs []error
	for _, err := range numbers(client, pageParams{Page: 98, Size: 5}) {
		if err != nil {
			errs = append(errs, err)
			continue
		}
		items++
	}
	if items != 5 || len(errs) != 1 || !errors.Is(errs[0], ErrServer) {
		t.Errorf("got %d items and errors %v; want 5 items, then one ErrServer", items, errs)
	}
}

func TestPaginationStopsWhenPageIgnored(t *testing.T) {
	var requests atomic.Int32
	mux := http.NewServeMux()
	mux.HandleFunc("GET /numbers", func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		// A server that ignores the page parameter answers the first page every time.
		writeJSON(w, map[string]any{"content": []int{1, 2}, "totalPages": 5, "number": 0, "size": 2})
	})
	client := newTestClient(t, mux)
	var got []int
	for n, err := range numbers(client, pageParams{Size: 2}) {
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, n)
	}
	if !slices.Equal(got, []int{1, 2}) || requests.Load() != 2 {
		t.Errorf("items = %v after %d requests, want [1 2] after 2", got, requests.Load())
	}
}

func TestPageKeepsNumbers(t *testing.T) {
	var page Page[map[string]any]
	if err := decode([]byte(`{"content":[{"amount":0.1000000000000000055511151231257827}],"number":3}`), &page); err != nil {
		t.Fatal(err)
	}
	if n, ok := page.Content[0]["amount"].(json.Number); !ok || n.String() != "0.1000000000000000055511151231257827" || page.Number != 3 || !page.numbered {
		t.Errorf("page = %+v", page)
	}
}

func TestPageHasNext(t *testing.T) {
	tests := []struct {
		name string
		page Page[int]
		want bool
	}{
		{"empty", Page[int]{TotalPages: 5}, false},
		{"last", Page[int]{Content: []int{1}, Last: true, TotalPages: 5}, false},
		{"total pages", Page[int]{Content: []int{1}, Number: 3, TotalPages: 5}, true},
		{"final page by count", Page[int]{Content: []int{1}, Number: 4, TotalPages: 5}, false},
		{"total elements", Page[int]{Content: []int{1, 2}, Number: 0, Size: 2, TotalElements: 3}, true},
		{"all elements seen", Page[int]{Content: []int{3}, Number: 1, Size: 2, TotalElements: 3}, false},
		{"no metadata", Page[int]{Content: []int{1}}, true},
	}
	for _, tt := range tests {
		if got := tt.page.HasNext(); got != tt.want {
			t.Errorf("%s: HasNext() = %v, want %v", tt.name, got, tt.want)
		}
	}
}
