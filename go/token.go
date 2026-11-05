package whitebird

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Token is an OAuth token pair issued by the authorization server.
type Token struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token,omitempty"`
	// Expiry is when the access token expires, taken from its JWT "exp" claim. It is zero when
	// unknown, in which case the token is used until the API rejects it.
	Expiry time.Time `json:"expiry,omitzero"`
}

// refreshMargin is how long before expiry an access token is replaced, as in the web
// application.
const refreshMargin = 60 * time.Second

// fresh reports whether t can be used without renewing it first.
func (t *Token) fresh(now time.Time) bool {
	return t != nil && t.AccessToken != "" && (t.Expiry.IsZero() || now.Add(refreshMargin).Before(t.Expiry))
}

// valid reports whether the access token of t has not expired yet.
func (t *Token) valid(now time.Time) bool {
	return t != nil && t.AccessToken != "" && (t.Expiry.IsZero() || now.Before(t.Expiry))
}

// refreshable reports whether t has a refresh token that has not expired, as far as its JWT
// "exp" claim tells.
func (t *Token) refreshable(now time.Time) bool {
	if t == nil || t.RefreshToken == "" {
		return false
	}
	exp := jwtExpiry(t.RefreshToken)
	return exp.IsZero() || now.Before(exp)
}

// withExpiry returns t with its expiry read from the access token if it is missing.
func (t *Token) withExpiry() *Token {
	if t == nil || !t.Expiry.IsZero() {
		return t
	}
	return &Token{AccessToken: t.AccessToken, RefreshToken: t.RefreshToken, Expiry: jwtExpiry(t.AccessToken)}
}

// A TokenStore keeps tokens between runs, so that a restarted program does not log in again.
// Implementations must be safe for concurrent use.
//
// The client treats the store as a cache: a token that fails to save is still used, and
// [FileTokenStore] reports a damaged file as no token rather than an error.
type TokenStore interface {
	// Load returns the saved token, or nil if there is none.
	Load(ctx context.Context) (*Token, error)
	// Save replaces the saved token.
	Save(ctx context.Context, token *Token) error
	// Clear removes the saved token, after a logout or when the session is no longer valid.
	Clear(ctx context.Context) error
}

// FileTokenStore is a [TokenStore] that keeps the token in a JSON file readable by the current
// user only. The file holds credentials: keep it out of version control.
type FileTokenStore struct {
	Path string
}

// Load reads the token from the file. A missing file, or one that does not hold a token, means
// no token.
func (s FileTokenStore) Load(context.Context) (*Token, error) {
	data, err := os.ReadFile(s.Path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var token Token
	if err := json.Unmarshal(data, &token); err != nil || token.AccessToken == "" {
		return nil, nil //nolint:nilerr // A damaged file holds no session.
	}
	return &token, nil
}

// Save writes the token to the file, replacing it atomically. It creates the directory of the
// file if needed.
func (s FileTokenStore) Save(_ context.Context, token *Token) error {
	data, err := json.Marshal(token)
	if err != nil {
		return err
	}
	dir := filepath.Dir(s.Path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, filepath.Base(s.Path)+".*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), s.Path)
}

// Clear deletes the file. A missing file is not an error.
func (s FileTokenStore) Clear(context.Context) error {
	if err := os.Remove(s.Path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return nil
}

// jwtExpiry returns the "exp" claim of a JWT, or the zero time if the token is not a JWT.
func jwtExpiry(token string) time.Time {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return time.Time{}
	}
	payload, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return time.Time{}
	}
	var claims struct {
		Exp json.Number `json:"exp"`
	}
	if json.Unmarshal(payload, &claims) != nil {
		return time.Time{}
	}
	exp, err := claims.Exp.Float64()
	if err != nil || exp <= 0 || exp > 1e12 {
		return time.Time{}
	}
	return time.Unix(int64(exp), 0)
}
