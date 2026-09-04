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

// refreshMargin is how long before expiry an access token is replaced.
const refreshMargin = 30 * time.Second

func (t *Token) fresh(now time.Time) bool {
	return t != nil && t.AccessToken != "" && (t.Expiry.IsZero() || now.Add(refreshMargin).Before(t.Expiry))
}

// A TokenStore keeps tokens between runs, so that a restarted program does not log in again.
// Implementations must be safe for concurrent use.
type TokenStore interface {
	// Load returns the saved token, or nil if there is none.
	Load(ctx context.Context) (*Token, error)
	// Save replaces the saved token.
	Save(ctx context.Context, token *Token) error
}

// FileTokenStore is a [TokenStore] that keeps the token in a JSON file readable by the current
// user only. The file holds credentials: keep it out of version control.
type FileTokenStore struct {
	Path string
}

// Load reads the token from the file. A missing file means no token.
func (s FileTokenStore) Load(context.Context) (*Token, error) {
	data, err := os.ReadFile(s.Path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var token Token
	if err := json.Unmarshal(data, &token); err != nil {
		return nil, err
	}
	return &token, nil
}

// Save writes the token to the file, replacing it atomically.
func (s FileTokenStore) Save(_ context.Context, token *Token) error {
	data, err := json.Marshal(token)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(s.Path), filepath.Base(s.Path)+".*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), s.Path)
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
	if err != nil || exp <= 0 {
		return time.Time{}
	}
	return time.Unix(int64(exp), 0)
}
