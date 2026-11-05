package whitebird

import (
	"encoding/json"
	"testing"
	"time"
)

func TestTimeUnmarshal(t *testing.T) {
	want := time.Date(2026, 3, 4, 5, 6, 7, 123000000, time.UTC)
	inputs := []string{
		`"2026-03-04T05:06:07.123Z"`,
		`"2026-03-04T08:06:07.123+03:00"`,
		`"2026-03-04T08:06:07.123+0300"`,
		`"2026-03-04T05:06:07.123"`,
		`"2026-03-04 05:06:07.123"`,
		`1772600767123`,
		`"1772600767123"`,
		`1.772600767123e12`,
	}
	for _, in := range inputs {
		var got Time
		if err := json.Unmarshal([]byte(in), &got); err != nil {
			t.Errorf("Unmarshal(%s): %v", in, err)
			continue
		}
		if !got.Equal(want) {
			t.Errorf("Unmarshal(%s) = %v, want %v", in, got, want)
		}
	}
	var dateOnly Time
	if err := json.Unmarshal([]byte(`"2026-03-04"`), &dateOnly); err != nil || !dateOnly.Equal(time.Date(2026, 3, 4, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("Unmarshal(date) = %v, %v", dateOnly, err)
	}
	for _, in := range []string{`null`, `""`} {
		got := Time{Time: want}
		if err := json.Unmarshal([]byte(in), &got); err != nil || !got.IsZero() {
			t.Errorf("Unmarshal(%s) = %v, %v; want the zero Time", in, got, err)
		}
	}
	for _, in := range []string{`"yesterday"`, `true`, `{}`} {
		var got Time
		if err := json.Unmarshal([]byte(in), &got); err == nil {
			t.Errorf("Unmarshal(%s) accepted an invalid time", in)
		}
	}
}

func TestTimeMarshal(t *testing.T) {
	v := struct {
		At      Time
		Zero    Time
		Omitted Time `json:",omitzero"`
	}{At: Time{Time: time.Date(2026, 3, 4, 5, 6, 7, 890, time.FixedZone("MSK", 3*3600))}}
	out, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	// As the web application sends times: to the second, with a "+0300" offset.
	if want := `{"At":"2026-03-04T05:06:07+0300","Zero":null}`; string(out) != want {
		t.Errorf("Marshal() = %s, want %s", out, want)
	}
	text, _ := Time{Time: time.Date(2026, 3, 4, 5, 6, 7, 0, time.UTC)}.MarshalText()
	if string(text) != "2026-03-04T05:06:07+0000" {
		t.Errorf("MarshalText() = %s", text)
	}
}

func TestDate(t *testing.T) {
	var v struct {
		Date, FromTime, FromMillis, Null Date
	}
	in := `{"Date":"1990-05-17","FromTime":"1990-05-17T23:00:00Z","FromMillis":642902400000,"Null":null}`
	if err := json.Unmarshal([]byte(in), &v); err != nil {
		t.Fatal(err)
	}
	want := Date{Year: 1990, Month: time.May, Day: 17}
	if v.Date != want || v.FromTime != want || v.FromMillis != want || !v.Null.IsZero() {
		t.Errorf("decoded %+v", v)
	}
	out, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"Date":"1990-05-17","FromTime":"1990-05-17","FromMillis":"1990-05-17","Null":null}`; string(out) != want {
		t.Errorf("Marshal() = %s, want %s", out, want)
	}
	if got := DateOf(time.Date(2026, 1, 2, 23, 0, 0, 0, time.UTC)); got.String() != "2026-01-02" {
		t.Errorf("DateOf() = %v", got)
	}
	if got := want.In(time.UTC); !got.Equal(time.Date(1990, 5, 17, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("In() = %v", got)
	}
	var invalid Date
	if err := json.Unmarshal([]byte(`"17.05.1990"`), &invalid); err == nil {
		t.Error("Unmarshal accepted an invalid date")
	}
}
