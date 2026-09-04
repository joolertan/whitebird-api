package whitebird

import "context"

// A CodeProvider supplies the TOTP code sent with the password when the account has two-factor
// authentication enabled. It is asked once per login.
//
// [StaticCode] and [CodeFunc] adapt a fixed code and a function; [otp.TOTP] computes codes from
// the account secret.
//
// [otp.TOTP]: https://pkg.go.dev/github.com/joolertan/whitebird-api/go/otp#TOTP
type CodeProvider interface {
	Code(ctx context.Context) (string, error)
}

// StaticCode is a [CodeProvider] that always returns the same code. As TOTP codes expire within a
// minute, it is only useful for a client that logs in right away.
type StaticCode string

// Code returns c.
func (c StaticCode) Code(context.Context) (string, error) {
	return string(c), nil
}

// CodeFunc adapts a function, for example one that prompts the user, to a [CodeProvider].
type CodeFunc func(ctx context.Context) (string, error)

// Code returns f(ctx).
func (f CodeFunc) Code(ctx context.Context) (string, error) {
	return f(ctx)
}
