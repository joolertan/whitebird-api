package whitebird

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"time"
)

// Time is a point in time exchanged with the API. It embeds [time.Time], so all of its methods
// are available.
//
// Decoding is lenient: it accepts RFC 3339, ISO 8601 with a "+0300" offset or without a time
// zone (taken as UTC) and milliseconds since the Unix epoch, as a number or a string. Time
// encodes as the web application sends times, to the second and with the offset of its
// location: "2026-01-02T15:04:05+0300". The zero Time encodes as null.
type Time struct {
	time.Time
}

var (
	zonedLayouts = []string{
		time.RFC3339Nano,
		"2006-01-02T15:04:05.999999999Z0700",
		"2006-01-02T15:04:05.999999999Z07",
		"2006-01-02 15:04:05.999999999Z07:00",
		"2006-01-02 15:04:05.999999999Z0700",
	}
	naiveLayouts = []string{
		"2006-01-02T15:04:05.999999999",
		"2006-01-02 15:04:05.999999999",
		"2006-01-02T15:04",
		time.DateOnly,
	}
)

func parseTime(s string) (time.Time, error) {
	if ms, err := strconv.ParseInt(s, 10, 64); err == nil {
		return time.UnixMilli(ms).UTC(), nil
	}
	for _, layout := range zonedLayouts {
		if t, err := time.Parse(layout, s); err == nil {
			return t, nil
		}
	}
	for _, layout := range naiveLayouts {
		if t, err := time.ParseInLocation(layout, s, time.UTC); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("whitebird: cannot parse time %q", s)
}

// unquoteTime returns the content of a JSON string or number holding a time. It returns "" for
// null and the empty string.
func unquoteTime(data []byte) (string, error) {
	if bytes.Equal(data, []byte("null")) {
		return "", nil
	}
	if len(data) > 0 && data[0] == '"' {
		var s string
		err := json.Unmarshal(data, &s)
		return s, err
	}
	var n json.Number
	if err := json.Unmarshal(data, &n); err != nil {
		return "", fmt.Errorf("whitebird: cannot parse time %s", data)
	}
	if _, err := n.Int64(); err == nil {
		return n.String(), nil
	}
	f, err := n.Float64()
	return strconv.FormatInt(int64(f), 10), err
}

// wireLayout is the format of the times the web application sends (dayjs
// "YYYY-MM-DDTHH:mm:ssZZ").
const wireLayout = "2006-01-02T15:04:05-0700"

// MarshalJSON encodes t as a "2006-01-02T15:04:05-0700" string, or null for the zero Time.
func (t Time) MarshalJSON() ([]byte, error) {
	if t.IsZero() {
		return []byte("null"), nil
	}
	return []byte(`"` + t.Format(wireLayout) + `"`), nil
}

// MarshalText formats t as "2006-01-02T15:04:05-0700", for query parameters.
func (t Time) MarshalText() ([]byte, error) {
	return []byte(t.Format(wireLayout)), nil
}

// UnmarshalJSON decodes a timestamp in any of the formats described on [Time].
func (t *Time) UnmarshalJSON(data []byte) error {
	s, err := unquoteTime(data)
	if err != nil || s == "" {
		*t = Time{}
		return err
	}
	parsed, err := parseTime(s)
	*t = Time{Time: parsed}
	return err
}

// UnmarshalText decodes a timestamp in any of the formats described on [Time].
func (t *Time) UnmarshalText(text []byte) error {
	parsed, err := parseTime(string(text))
	*t = Time{Time: parsed}
	return err
}

// Date is a calendar date, exchanged with the API as "2006-01-02". The zero Date encodes as null.
type Date struct {
	Year  int
	Month time.Month
	Day   int
}

// DateOf returns the date of t in its location.
func DateOf(t time.Time) Date {
	y, m, d := t.Date()
	return Date{Year: y, Month: m, Day: d}
}

// In returns the start of the date in loc.
func (d Date) In(loc *time.Location) time.Time {
	return time.Date(d.Year, d.Month, d.Day, 0, 0, 0, 0, loc)
}

// IsZero reports whether d is the zero Date.
func (d Date) IsZero() bool {
	return d == Date{}
}

// String formats d as "2006-01-02".
func (d Date) String() string {
	return fmt.Sprintf("%04d-%02d-%02d", d.Year, d.Month, d.Day)
}

// MarshalText formats d as "2006-01-02".
func (d Date) MarshalText() ([]byte, error) {
	return []byte(d.String()), nil
}

// UnmarshalText parses a date, or a timestamp in any of the formats accepted by [Time], whose
// date is taken.
func (d *Date) UnmarshalText(text []byte) error {
	t, err := parseTime(string(text))
	if err != nil {
		return err
	}
	*d = DateOf(t)
	return nil
}

// MarshalJSON encodes d as a "2006-01-02" string, or null for the zero Date.
func (d Date) MarshalJSON() ([]byte, error) {
	if d.IsZero() {
		return []byte("null"), nil
	}
	return []byte(`"` + d.String() + `"`), nil
}

// UnmarshalJSON decodes a date, or a timestamp whose date is taken.
func (d *Date) UnmarshalJSON(data []byte) error {
	s, err := unquoteTime(data)
	if err != nil || s == "" {
		*d = Date{}
		return err
	}
	return d.UnmarshalText([]byte(s))
}
