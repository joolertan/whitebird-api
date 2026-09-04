// Package otp computes time-based one-time passwords (TOTP, RFC 6238) such as those shown by
// authenticator apps.
//
// A [TOTP] can serve as the code provider of a Whitebird client, which then logs in to accounts
// with two-factor authentication on its own:
//
//	totp, err := otp.New(secret)
//	if err != nil {
//		return err
//	}
//	client, err := whitebird.NewClient(
//		whitebird.WithCredentials(email, password),
//		whitebird.WithCodeProvider(totp),
//	)
package otp

import (
	"cmp"
	"context"
	"crypto/hmac"
	"crypto/sha1"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base32"
	"encoding/binary"
	"errors"
	"fmt"
	"hash"
	"strings"
	"time"
)

// Algorithm is the HMAC hash function of a [TOTP].
type Algorithm int

// Hash functions allowed by RFC 6238.
const (
	SHA1 Algorithm = iota
	SHA256
	SHA512
)

func (a Algorithm) hash() (func() hash.Hash, error) {
	switch a {
	case SHA1:
		return sha1.New, nil
	case SHA256:
		return sha256.New, nil
	case SHA512:
		return sha512.New, nil
	}
	return nil, fmt.Errorf("otp: unknown algorithm %d", a)
}

// TOTP generates time-based one-time passwords. The zero values of Digits, Period and
// Algorithm stand for the usual 6 digits, 30 seconds and SHA-1.
type TOTP struct {
	// Key is the shared secret.
	Key []byte
	// Digits is the length of a code, from 6 to 8.
	Digits int
	// Period is how long a code stays valid.
	Period time.Duration
	// Algorithm is the HMAC hash function.
	Algorithm Algorithm
	// Now returns the current time. It defaults to [time.Now].
	Now func() time.Time
}

// New returns a TOTP with the default settings for a base32-encoded secret, as shown by the
// account settings next to the QR code. Spaces, lowercase letters and missing padding are
// accepted.
func New(secret string) (*TOTP, error) {
	key, err := DecodeSecret(secret)
	if err != nil {
		return nil, err
	}
	return &TOTP{Key: key}, nil
}

// DecodeSecret decodes a base32-encoded secret, ignoring spaces, hyphens, case and padding.
func DecodeSecret(secret string) ([]byte, error) {
	cleaned := strings.ToUpper(strings.NewReplacer(" ", "", "-", "", "=", "").Replace(secret))
	key, err := base32.StdEncoding.WithPadding(base32.NoPadding).DecodeString(cleaned)
	if err != nil || len(key) == 0 {
		return nil, errors.New("otp: invalid base32 secret")
	}
	return key, nil
}

// Code returns the code for the current time. It makes TOTP a code provider for
// whitebird.WithCodeProvider.
func (t *TOTP) Code(context.Context) (string, error) {
	now := time.Now
	if t.Now != nil {
		now = t.Now
	}
	return t.At(now())
}

// At returns the code valid at the given time.
func (t *TOTP) At(at time.Time) (string, error) {
	digits := cmp.Or(t.Digits, 6)
	if digits < 6 || digits > 8 {
		return "", fmt.Errorf("otp: %d digits; want 6 to 8", digits)
	}
	period := cmp.Or(t.Period, 30*time.Second)
	if period < time.Second {
		return "", fmt.Errorf("otp: period %v is shorter than a second", period)
	}
	newHash, err := t.Algorithm.hash()
	if err != nil {
		return "", err
	}
	if len(t.Key) == 0 {
		return "", errors.New("otp: empty key")
	}
	counter := uint64(at.Unix() / int64(period/time.Second))
	return hotp(newHash, t.Key, counter, digits), nil
}

// hotp computes an HMAC-based one-time password (RFC 4226).
func hotp(newHash func() hash.Hash, key []byte, counter uint64, digits int) string {
	mac := hmac.New(newHash, key)
	mac.Write(binary.BigEndian.AppendUint64(nil, counter))
	sum := mac.Sum(nil)
	offset := sum[len(sum)-1] & 0x0f
	value := binary.BigEndian.Uint32(sum[offset:]) & 0x7fffffff
	modulo := uint32(1)
	for range digits {
		modulo *= 10
	}
	return fmt.Sprintf("%0*d", digits, value%modulo)
}
