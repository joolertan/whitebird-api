package whitebird

import (
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"
)

// Decimal is an exact decimal number such as an amount or a price. It keeps the digits it was
// given, so no precision is lost to binary floating point on the way to or from the API.
//
// It decodes from JSON numbers and strings and encodes as a JSON number. The zero value holds
// no number and encodes as null.
type Decimal struct {
	s string
}

var decimalPattern = regexp.MustCompile(`^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$`)

// NewDecimal parses a decimal number such as "-12.50" or "1e-8".
func NewDecimal(s string) (Decimal, error) {
	m := decimalPattern.FindStringSubmatch(strings.TrimSpace(s))
	if m == nil || m[2] == "" && m[3] == "" {
		return Decimal{}, fmt.Errorf("whitebird: invalid decimal %q", s)
	}
	sign, whole, frac, exp := m[1], strings.TrimLeft(m[2], "0"), m[3], m[4]
	var b strings.Builder
	if sign == "-" {
		b.WriteByte('-')
	}
	if whole == "" {
		whole = "0"
	}
	b.WriteString(whole)
	if frac != "" {
		b.WriteByte('.')
		b.WriteString(frac)
	}
	if exp != "" {
		b.WriteByte('e')
		b.WriteString(exp)
	}
	return Decimal{s: b.String()}, nil
}

// MustDecimal is like [NewDecimal] but panics if s is not a decimal number. It is meant for
// constants.
func MustDecimal(s string) Decimal {
	d, err := NewDecimal(s)
	if err != nil {
		panic(err)
	}
	return d
}

// DecimalFromFloat returns the shortest decimal that converts back to f exactly. It returns the
// zero Decimal for NaN and infinities.
func DecimalFromFloat(f float64) Decimal {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return Decimal{}
	}
	return Decimal{s: strconv.FormatFloat(f, 'f', -1, 64)}
}

// DecimalFromInt returns i as a decimal.
func DecimalFromInt(i int64) Decimal {
	return Decimal{s: strconv.FormatInt(i, 10)}
}

// String returns the number as given, or "" for the zero Decimal.
func (d Decimal) String() string {
	return d.s
}

// Float64 returns the float64 nearest to d, or 0 for the zero Decimal.
func (d Decimal) Float64() float64 {
	f, _ := strconv.ParseFloat(d.s, 64)
	return f
}

// Rat returns d as an exact rational number, or nil for the zero Decimal.
func (d Decimal) Rat() *big.Rat {
	r, ok := new(big.Rat).SetString(d.s)
	if !ok {
		return nil
	}
	return r
}

// MarshalJSON encodes d as a JSON number, or null for the zero Decimal.
func (d Decimal) MarshalJSON() ([]byte, error) {
	if d.s == "" {
		return []byte("null"), nil
	}
	return []byte(d.s), nil
}

// UnmarshalJSON decodes a JSON number or a string holding one. Null, "" and values that are not
// numbers give the zero Decimal, so that one odd value does not fail a whole response.
func (d *Decimal) UnmarshalJSON(data []byte) error {
	text := string(data)
	if len(data) > 0 && data[0] == '"' {
		if err := json.Unmarshal(data, &text); err != nil {
			return err
		}
	}
	parsed, err := NewDecimal(text)
	if err != nil {
		parsed = Decimal{}
	}
	*d = parsed
	return nil
}

// Cmp compares d and e numerically, returning -1, 0 or +1. The zero Decimal compares as 0.
func (d Decimal) Cmp(e Decimal) int {
	a, b := d.Rat(), e.Rat()
	if a == nil {
		a = new(big.Rat)
	}
	if b == nil {
		b = new(big.Rat)
	}
	return a.Cmp(b)
}

// Equal reports whether d and e are the same number, such as "1.0" and "1". Use it rather than
// ==, which compares the digits as written.
func (d Decimal) Equal(e Decimal) bool {
	return d.Cmp(e) == 0
}

// MarshalText encodes d as its string form, for query parameters.
func (d Decimal) MarshalText() ([]byte, error) {
	return []byte(d.s), nil
}

// UnmarshalText parses text with [NewDecimal]. Empty text gives the zero Decimal.
func (d *Decimal) UnmarshalText(text []byte) error {
	if len(text) == 0 {
		*d = Decimal{}
		return nil
	}
	parsed, err := NewDecimal(string(text))
	if err != nil {
		return err
	}
	*d = parsed
	return nil
}
