# Authentication

Whitebird uses the OAuth 2 password grant of its web application. The client keeps the session for
you:

- it signs in on the first call that needs an account, or when you call `login`;
- it refreshes the access token shortly before it expires, and once more if a request is
  rejected with HTTP 401;
- if the refresh token is no longer valid and the password is known, it signs in again.

Public endpoints (market data, ICO showcase, payment provider lists) work without credentials.

## Two-factor authentication

Accounts with 2FA send a TOTP code with the password. Configure a code provider: a fixed code, a
function that returns one, or a TOTP generator built from the authenticator secret (the base32
string behind the QR code).

=== "Python"

    ```python
    from whitebird import Whitebird
    from whitebird.otp import TOTP  # pip install "whitebird-api[otp]"

    client = Whitebird(username, password, otp=TOTP(secret))
    client = Whitebird(username, password, otp=lambda: input("2FA code: "))
    ```

=== "Node.js"

    ```ts
    import { totp } from 'whitebird-api/otp';

    const client = new Whitebird({ username, password, otp: totp(secret) });
    ```

=== "Go"

    ```go
    totp, err := otp.New(secret)
    client, err := whitebird.NewClient(
        whitebird.WithCredentials(username, password),
        whitebird.WithCodeProvider(totp),
    )
    ```

=== "Rust"

    ```rust
    let client = Client::builder()
        .credentials(username, password)
        .otp(Totp::new(&secret)?) // feature "otp"
        .build()?;
    ```

Without a code provider, signing in to a 2FA account raises `MFARequiredError`.

## Keeping the session between runs

Tokens live in a token store, in memory by default. Plug in your own store (a file, a database,
a secret manager) so a restarted program reuses the session instead of signing in again. A store
has three operations: load, save and clear.

=== "Python"

    ```python
    from whitebird import FileTokenStore, Whitebird

    client = Whitebird(username, password, token_store=FileTokenStore("~/.whitebird/session.json"))
    ```

=== "Node.js"

    ```ts
    import { readFile, writeFile, rm } from 'node:fs/promises';
    import type { TokenStore, Tokens } from 'whitebird-api';

    const file = 'session.json';
    const tokenStore: TokenStore = {
      load: () => readFile(file, 'utf8').then((text) => JSON.parse(text) as Tokens, () => undefined),
      save: (tokens) => writeFile(file, JSON.stringify(tokens), { mode: 0o600 }),
      clear: () => rm(file, { force: true }),
    };
    const client = new Whitebird({ username, password, tokenStore });
    ```

=== "Go"

    Implement `whitebird.TokenStore` and pass it with `whitebird.WithTokenStore(store)`.

=== "Rust"

    Implement `whitebird::TokenStore` and pass it with `ClientBuilder::token_store`.

`logout` signs out and clears the store.

## Captcha

The sign-in form makes accounts **without** 2FA solve a slider captcha: a background image with a
cut-out and a puzzle piece that has to be slid into place. The SDKs never solve captchas
themselves. If the server asks for one and no solver is configured, signing in raises
`CaptchaRequiredError`. Enabling 2FA on the account avoids the captcha altogether.

A solver receives the captcha (request id, both PNG images, the piece's vertical offset) and
returns the horizontal offset of the piece. The client sends the answer with the sign-in request
and retries with a new captcha, up to three attempts, when the answer is rejected.

=== "Python"

    ```python
    from whitebird.types import Captcha

    def solve(captcha: Captcha) -> int:
        Path("captcha.png").write_bytes(captcha.background_image)
        return int(input("Slider position (0-250): "))

    client = Whitebird(username, password, captcha=solve)
    ```

=== "Node.js"

    ```ts
    const client = new Whitebird({
      username,
      password,
      captcha: async (captcha) => {
        await writeFile('captcha.png', captcha.backgroundImage);
        return Number(await rl.question('Slider position (0-250): '));
      },
    });
    ```

=== "Go"

    ```go
    solve := func(ctx context.Context, c whitebird.Captcha) (int, error) {
        if err := os.WriteFile("captcha.png", c.BackgroundImage, 0o600); err != nil {
            return 0, err
        }
        var position int
        _, err := fmt.Scan(&position)
        return position, err
    }
    client, err := whitebird.NewClient(whitebird.WithCredentials(username, password), whitebird.WithCaptchaSolver(solve))
    ```

=== "Rust"

    See `ClientBuilder::captcha` in the [Rust reference](../reference/rust.md).

## Confirming withdrawals (operation MFA)

Withdrawals are refused until the request carries an operation MFA token, obtained by exchanging a
fresh TOTP code. With a code provider configured the client does this by itself: before a
withdrawal it obtains a token if it has none, and if the token is rejected it gets a new one and
retries once. The token is then sent with every request.

To do it by hand, for example with a code typed by the user, call `confirm_operation`:

=== "Python"

    ```python
    client.confirm_operation("WITHDRAWAL", code="123456")
    client.balance.withdrawals.create_crypto(...)
    ```

=== "Node.js"

    ```ts
    await client.confirmOperation('WITHDRAWAL', '123456');
    ```

=== "Go"

    ```go
    _, err := client.ConfirmOperation(ctx, "WITHDRAWAL", "123456")
    ```

=== "Rust"

    ```rust
    client.confirm_operation("WITHDRAWAL", Some("123456")).await?;
    ```

The token can also be read and set directly (`mfa_token` / `mfaToken` / `SetMFAToken` /
`set_mfa_token`), for example to reuse one across processes.

## Anti-fraud verification

Any request may be refused with `VerificationRequiredError` when Whitebird wants an extra check:

- **SMS**: call `user.antifraud.send_code()`, then `user.antifraud.verify_code(code=...)` with the
  code the user received, and repeat the refused request unchanged;
- **liveness**: the user has to pass a liveness check in the Whitebird app (see `kyc.liveness`).

=== "Python"

    ```python
    from whitebird import VerificationRequiredError

    try:
        order = client.exchange.orders.create(...)
    except VerificationRequiredError as error:
        if error.kind != "sms":
            raise
        client.user.antifraud.send_code()
        client.user.antifraud.verify_code(code=input("SMS code: "))
        order = client.exchange.orders.create(...)
    ```

=== "Node.js"

    ```ts
    try {
      await client.exchange.orders.create(params);
    } catch (error) {
      if (!(error instanceof VerificationRequiredError) || error.kind !== 'sms') throw error;
      await client.user.antifraud.sendCode();
      await client.user.antifraud.verifyCode({ code: await ask('SMS code: ') });
      await client.exchange.orders.create(params);
    }
    ```

=== "Go"

    ```go
    order, err := client.Exchange.Orders.Create(ctx, params)
    if errors.Is(err, whitebird.ErrVerificationRequired) {
        // send_code, verify_code, then repeat the call
    }
    ```

=== "Rust"

    ```rust
    match client.exchange().orders().create(params.clone()).await {
        Err(e) if e.is_verification_required() => { /* send_code, verify_code, retry */ }
        other => other?,
    };
    ```
