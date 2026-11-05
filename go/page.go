package whitebird

import (
	"bytes"
	"encoding/json"
	"iter"
)

// Page is one page of a paginated listing, as returned by the methods ending in Page. The
// methods without that suffix iterate over the items of all pages instead.
type Page[T any] struct {
	Content []T `json:"content"`
	// TotalElements is the number of items on all pages.
	TotalElements int64 `json:"totalElements"`
	TotalPages    int   `json:"totalPages"`
	// Number is the zero-based index of the page.
	Number int `json:"number"`
	// Size is the requested number of items per page.
	Size int `json:"size"`
	// NumberOfElements is the number of items on this page.
	NumberOfElements int  `json:"numberOfElements"`
	First            bool `json:"first"`
	Last             bool `json:"last"`
	Empty            bool `json:"empty"`

	// numbered is set when the response carried the page number.
	numbered bool
}

// pageFields has the fields of Page without its methods, to decode them.
type pageFields[T any] Page[T]

// UnmarshalJSON decodes a page, keeping numbers in untyped values as json.Number.
func (p *Page[T]) UnmarshalJSON(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode((*pageFields[T])(p)); err != nil {
		return err
	}
	var probe struct {
		Number json.RawMessage `json:"number"`
	}
	_ = json.Unmarshal(data, &probe)
	p.numbered = !isNull(probe.Number)
	return nil
}

// HasNext reports whether another page follows this one.
func (p *Page[T]) HasNext() bool {
	switch {
	case len(p.Content) == 0 || p.Last:
		return false
	case p.TotalPages > 0:
		return p.Number+1 < p.TotalPages
	case p.Size > 0:
		return int64(p.Number+1)*int64(p.Size) < p.TotalElements
	}
	return true
}

// paginate iterates over the items of the pages returned by fetch, starting from page first.
// Pages are fetched as the consumer advances; iteration stops after the last page, at the
// first error, when the server answers another page than the one asked for (it ignores the
// page parameter) or when the consumer breaks out of the loop.
func paginate[T any](first int, fetch func(page int) (*Page[T], error)) iter.Seq2[T, error] {
	return func(yield func(T, error) bool) {
		for n := first; ; n++ {
			page, err := fetch(n)
			if err != nil {
				var zero T
				yield(zero, err)
				return
			}
			if page == nil || page.numbered && page.Number != n {
				return
			}
			for _, item := range page.Content {
				if !yield(item, nil) {
					return
				}
			}
			if !page.HasNext() {
				return
			}
		}
	}
}
