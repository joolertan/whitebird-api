# Authentication

Whitebird uses the OAuth 2 password grant. The client keeps the session for you:

- it signs in on the first call that needs an account, or when you call `login`;
- it refreshes the access token when less than a minute of it is left, and once more if a request
  is rejected with HTTP 401;
- if Whitebird rejects the refresh (the session expired or was revoked), it forgets the session
  and signs in again when the password is known. Network and server errors during a refresh are
  raised as they are and keep the session for the next call.

Public endpoints (market data, ICO showcase, payment provider lists) work without credentials.

## Two-factor authentication

Accounts with 2FA send a TOTP code with the password, and the same kind of code confirms
withdrawals. Configure a code provider:

- a function that returns the current code: it can ask the user, wait for a chat bot or read a
  password manager, so the authenticator secret never reaches the program;
- a TOTP generator built from the authenticator secret (the base32 string behind the QR code), for
  programs that run unattended;
- a fixed code, which only suits a single sign-in.

The client asks the provider only when Whitebird wants a code: after the password of a 2FA
account is accepted, and when an operation is refused until it is confirmed. A function therefore
keeps working when the client signs in again hours later.

=== "Python"

    ```python
    import asyncio

    from whitebird import AsyncWhitebird, Whitebird
    from whitebird.otp import TOTP  # pip install "whitebird-api[otp]"

    client = Whitebird(username, password, otp=lambda: input("2FA code: "))
    client = Whitebird(username, password, otp=TOTP(secret))


    async def ask() -> str:
        return await asyncio.to_thread(input, "2FA code: ")


    async_client = AsyncWhitebird(username, password, otp=ask)  # functions or coroutines
    ```

=== "Node.js"

    ```ts
    import { createInterface } from 'node:readline/promises';
    import { Whitebird } from 'whitebird-api';
    import { totp } from 'whitebird-api/otp';

    const rl = createInterface({ input: process.stdin, output: process.stdout });

    // The function may return the code or a promise of it.
    const client = new Whitebird({ username, password, otp: () => rl.question('2FA code: ') });
    const unattended = new Whitebird({ username, password, otp: totp(secret) });
    ```

=== "Go"

    ```go
    ask := whitebird.CodeFunc(func(ctx context.Context) (string, error) {
        fmt.Print("2FA code: ")
        var code string
        _, err := fmt.Scanln(&code)
        return code, err
    })
    client, err := whitebird.NewClient(
        whitebird.WithCredentials(username, password),
        whitebird.WithCodeProvider(ask), // or an *otp.TOTP from otp.New(secret)
    )
    ```

=== "Rust"

    ```rust
    use whitebird::{Client, CodeProvider};

    // Runs on a blocking thread; CodeProvider::new takes an async function instead.
    let ask = CodeProvider::from_fn(|| {
        let mut code = String::new();
        std::io::stdin().read_line(&mut code)?;
        Ok(code.trim().to_owned())
    });
    let client = Client::builder()
        .credentials(username, password)
        .otp(ask) // or Totp::new(&secret)? with the "otp" feature
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

`user.logout()` ends the session on the server, and the client forgets it whatever the server
answers: its tokens and operation MFA token are dropped and the store is cleared, so the next call
signs in again. In Node.js, `client.logout()` does the same and ignores server errors.

`user.password.change(...)` answers with a new token pair, which the client stores in place of
the old session; the new password replaces the old one for later sign-ins.

## Captcha

Whitebird may ask for a slider captcha when signing in: a background image with a cut-out and a
puzzle piece that has to be slid into place. The SDKs never solve captchas themselves. Like the
sign-in form, the client first sends the password without a captcha; only when the server answers
that it wants one (status `REQUIRED_CAPTCHA`) does it ask the solver, and from then on every
request of that sign-in, including the one with the 2FA code, carries a newly solved captcha.
Without a solver, signing in then raises `CaptchaRequiredError`. If Whitebird does not know the
username, signing in raises `AuthenticationError`.

A solver receives the captcha (request id, both PNG images, the piece's vertical offset) and
returns the horizontal offset of the piece. The client sends the answer with the sign-in request
and retries with a new captcha, up to three attempts, when the answer is rejected.

=== "Python"

    ```python
    from pathlib import Path

    from whitebird import Whitebird
    from whitebird.types import Captcha


    def solve(captcha: Captcha) -> int:
        Path("captcha.png").write_bytes(captcha.background_image)
        return int(input("Slider position (0-250): "))

    client = Whitebird(username, password, captcha=solve)
    ```

=== "Node.js"

    ```ts
    import { writeFile } from 'node:fs/promises';

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

    ```rust
    use whitebird::{CaptchaSolver, Client};

    // Runs on a blocking thread; CaptchaSolver::new takes an async function instead.
    let solve = CaptchaSolver::from_fn(|captcha| {
        std::fs::write("captcha.png", &captcha.background_image)?;
        let mut position = String::new();
        std::io::stdin().read_line(&mut position)?;
        Ok(position.trim().parse()?)
    });
    let client = Client::builder().credentials(username, password).captcha(solve).build()?;
    ```

## Confirming withdrawals (operation MFA)

Withdrawals are refused until the request carries an operation MFA token, obtained by exchanging a
fresh 2FA code. With a code provider configured the client does this by itself, like the web
application: when an operation is refused for lack of confirmation, it asks the provider for a
code, exchanges it for a token and repeats the request once. Operations refused at the same time
share one confirmation. The token is then sent with every request. Without a code provider the
refusal is raised as `MFARequiredError`.

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
    let order = match client.exchange().orders().create(params.clone()).await {
        Err(e) if e.is_verification_required() => {
            // send_code, verify_code, then repeat the call
            client.exchange().orders().create(params).await?
        }
        other => other?,
    };
    ```
