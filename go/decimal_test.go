package whitebird

import (
	"encoding/json"
	"math/big"
	"testing"
)

func TestNewDecimal(t *testing.T) {
	valid := map[string]string{
		"0":                      "0",
		"-12.50":                 "-12.50",
		"+3":                     "3",
		".5":                     "0.5",
		"5.":                     "5",
		"007.10":                 "7.10",
		"1e-8":                   "1e-8",
		"-2.5E+3":                "-2.5e+3",
		"123456789.123456789012": "123456789.123456789012",
	}
	for in, want := range valid {
		d, err := NewDecimal(in)
		if err != nil || d.String() != want {
			t.Errorf("NewDecimal(%q) = %q, %v; want %q", in, d, err, want)
		}
	}
	if d, err := NewDecimal("\t42 "); err != nil || d.String() != "42" {
		t.Errorf("NewDecimal with spaces = %q, %v", d, err)
	}
	for _, in := range []string{"", ".", "-", "1.2.3", "abc", "1e", "0x10", "NaN"} {
		if _, err := NewDecimal(in); err == nil {
			t.Errorf("NewDecimal(%q) accepted an invalid number", in)
		}
	}
}

func TestDecimalJSON(t *testing.T) {
	var v struct {
		Number Decimal
		String Decimal
		Null   Decimal
		Empty  Decimal
		Absent Decimal
	}
	in := `{"Number": 0.1000000000000000055511151231257827, "String": "97000.50", "Null": null, "Empty": ""}`
	if err := json.Unmarshal([]byte(in), &v); err != nil {
		t.Fatal(err)
	}
	if v.Number.String() != "0.1000000000000000055511151231257827" || v.String.String() != "97000.50" {
		t.Errorf("decoded %+v", v)
	}
	if v.Null != (Decimal{}) || v.Empty != (Decimal{}) || v.Absent != (Decimal{}) {
		t.Errorf("null, empty and absent decimals = %+v, want zero values", v)
	}
	out, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"Number":0.1000000000000000055511151231257827,"String":97000.50,"Null":null,"Empty":null,"Absent":null}`
	if string(out) != want {
		t.Errorf("Marshal() = %s, want %s", out, want)
	}
	// One odd value does not fail a whole response.
	for _, in := range []string{`"12abc"`, `"N/A"`, `true`, `{}`} {
		invalid := MustDecimal("1")
		if err := json.Unmarshal([]byte(in), &invalid); err != nil || invalid != (Decimal{}) {
			t.Errorf("Unmarshal(%s) = %v, %v; want the zero Decimal", in, invalid, err)
		}
	}
	if !MustDecimal("1.0").Equal(MustDecimal("1")) || MustDecimal("1e-8").Cmp(MustDecimal("0.00000002")) != -1 || !(Decimal{}).Equal(MustDecimal("0")) {
		t.Error("Equal or Cmp compare the digits, not the numbers")
	}
	omitted, _ := json.Marshal(struct {
		A Decimal `json:"a,omitzero"`
		B Decimal `json:"b,omitzero"`
	}{B: MustDecimal("0")})
	if string(omitted) != `{"b":0}` {
		t.Errorf("omitzero: %s, want only the zero amount", omitted)
	}
}

func TestDecimalConversions(t *testing.T) {
	d := MustDecimal("-1.25")
	if d.Float64() != -1.25 {
		t.Errorf("Float64() = %v", d.Float64())
	}
	if d.Rat().Cmp(big.NewRat(-5, 4)) != 0 {
		t.Errorf("Rat() = %v", d.Rat())
	}
	if (Decimal{}).Rat() != nil || (Decimal{}).Float64() != 0 {
		t.Error("zero Decimal converts to a number")
	}
	if got := DecimalFromFloat(0.1).String(); got != "0.1" {
		t.Errorf("DecimalFromFloat(0.1) = %s", got)
	}
	if got := DecimalFromInt(-42).String(); got != "-42" {
		t.Errorf("DecimalFromInt(-42) = %s", got)
	}
	text, _ := d.MarshalText()
	var back Decimal
	if err := back.UnmarshalText(text); err != nil || back != d {
		t.Errorf("text round trip = %v, %v", back, err)
	}
}
