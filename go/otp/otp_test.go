package otp

import (
	"context"
	"testing"
	"time"
)

// Test vectors from RFC 6238, appendix B.
func TestRFC6238(t *testing.T) {
	keys := map[Algorithm][]byte{
		SHA1:   []byte("12345678901234567890"),
		SHA256: []byte("12345678901234567890123456789012"),
		SHA512: []byte("1234567890123456789012345678901234567890123456789012345678901234"),
	}
	vectors := []struct {
		unix      int64
		algorithm Algorithm
		code      string
	}{
		{59, SHA1, "94287082"},
		{59, SHA256, "46119246"},
		{59, SHA512, "90693936"},
		{1111111109, SHA1, "07081804"},
		{1111111109, SHA256, "68084774"},
		{1111111109, SHA512, "25091201"},
		{1111111111, SHA1, "14050471"},
		{1111111111, SHA256, "67062674"},
		{1111111111, SHA512, "99943326"},
		{1234567890, SHA1, "89005924"},
		{1234567890, SHA256, "91819424"},
		{1234567890, SHA512, "93441116"},
		{2000000000, SHA1, "69279037"},
		{2000000000, SHA256, "90698825"},
		{2000000000, SHA512, "38618901"},
		{20000000000, SHA1, "65353130"},
		{20000000000, SHA256, "77737706"},
		{20000000000, SHA512, "47863826"},
	}
	for _, v := range vectors {
		totp := &TOTP{Key: keys[v.algorithm], Digits: 8, Algorithm: v.algorithm}
		got, err := totp.At(time.Unix(v.unix, 0))
		if err != nil {
			t.Fatal(err)
		}
		if got != v.code {
			t.Errorf("At(%d) with algorithm %d = %s, want %s", v.unix, v.algorithm, got, v.code)
		}
	}
}

func TestNewDecodesLenientBase32(t *testing.T) {
	totp, err := New("gezd gnbv gy3t qojq gezd gnbv gy3t qojq")
	if err != nil {
		t.Fatal(err)
	}
	totp.Now = func() time.Time { return time.Unix(59, 0) }
	got, err := totp.Code(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got != "287082" {
		t.Errorf("Code() = %s, want 287082", got)
	}
}

func TestSevenDigits(t *testing.T) {
	totp := &TOTP{Key: []byte("12345678901234567890"), Digits: 7}
	got, err := totp.At(time.Unix(59, 0))
	if err != nil {
		t.Fatal(err)
	}
	if got != "4287082" {
		t.Errorf("At(59) = %s, want 4287082", got)
	}
}

func TestInvalidSettings(t *testing.T) {
	key := []byte("12345678901234567890")
	for name, totp := range map[string]*TOTP{
		"too few digits":  {Key: key, Digits: 5},
		"too many digits": {Key: key, Digits: 9},
		"short period":    {Key: key, Period: time.Millisecond},
		"bad algorithm":   {Key: key, Algorithm: 7},
		"empty key":       {},
	} {
		if _, err := totp.At(time.Now()); err == nil {
			t.Errorf("%s: no error", name)
		}
	}
	for _, secret := range []string{"", "not base32!", "A"} {
		if _, err := New(secret); err == nil {
			t.Errorf("New(%q): no error", secret)
		}
	}
}
