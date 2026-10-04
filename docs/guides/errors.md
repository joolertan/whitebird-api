# Errors

Every failure is one of two kinds:

- a **transport error**: no response arrived (connection refused, TLS failure, timeout);
- an **API error**: Whitebird answered and refused the request.

Whitebird services describe errors as
`{"code": 400, "status": "INVALID_SMS_CODE", "subStatus": "...", "message": "...", "fields": [...]}`.
API errors carry these values: the HTTP status, `code`, `error_code` (the `status` string, the
most useful one to branch on), `sub_status`, `message`, the per-field validation `fields` and the
raw body. The OAuth endpoint's `{"error", "error_description"}` and market data's non-zero
`retCode` are mapped onto the same fields.

## Classes

```
WhitebirdError
├── TransportError
└── APIError
    ├── BadRequestError            400
    │   └── VerificationRequiredError   SMS or liveness check pending
    ├── AuthenticationError        401, rejected credentials or code
    │   ├── MFARequiredError       a 2FA code or an operation MFA token is needed
    │   └── CaptchaRequiredError   sign-in needs a captcha and no solver is configured
    ├── PermissionDeniedError      403
    ├── NotFoundError              404
    ├── ConflictError              409
    ├── RateLimitError             429, with retry_after
    └── ServerError                5xx
```

=== "Python"

    Exceptions in the `whitebird` package.

    ```python
    from whitebird import APIError, NotFoundError, TransportError

    try:
        order = client.exchange.orders.get(order_id)
    except NotFoundError:
        order = None
    except APIError as error:
        print(error.status_code, error.error_code, error.message, error.fields)
        raise
    except TransportError:
        ...  # network trouble; safe to retry later
    ```

=== "Node.js"

    Error classes exported by `whitebird-api`; check with `instanceof`.

    ```ts
    import { APIError, NotFoundError } from 'whitebird-api';

    try {
      await client.exchange.orders.get(orderId);
    } catch (error) {
      if (error instanceof NotFoundError) return undefined;
      if (error instanceof APIError) console.error(error.status, error.errorCode, error.message);
      throw error;
    }
    ```

=== "Go"

    API errors are `*whitebird.APIError`; every class is a sentinel matched with `errors.Is`
    (`ErrNotFound`, `ErrMFARequired`, ...), and subclasses also match their parent
    (`ErrMFARequired` is an `ErrAuthentication`).

    ```go
    order, err := client.Exchange.Orders.Get(ctx, orderID)
    var apiErr *whitebird.APIError
    switch {
    case errors.Is(err, whitebird.ErrNotFound):
        return nil, nil
    case errors.As(err, &apiErr):
        log.Printf("%d %s: %s", apiErr.StatusCode, apiErr.ErrorCode, apiErr.Message)
    }
    ```

=== "Rust"

    `whitebird::Error` is an enum: `Error::Api(ApiError)` for API errors, plus transport,
    decoding and configuration variants. `ApiError::kind()` gives the class as an `ErrorKind`, and
    `Error` has shortcuts such as `is_not_found()`.

    ```rust
    match client.exchange().orders().get(&order_id).await {
        Ok(order) => Some(order),
        Err(e) if e.is_not_found() => None,
        Err(e) => {
            if let Some(api) = e.api() {
                eprintln!("{} {:?}: {}", api.status(), api.error_code(), api.message());
            }
            return Err(e);
        }
    }
    ```

## Retries and timeouts

Idempotent requests (`GET`, `HEAD`, `PUT`, `DELETE`) are retried on connection errors and on
HTTP 429, 502, 503 and 504, with exponential backoff and jitter, honouring `Retry-After`. `POST`
requests, which create orders and move money, are never retried automatically. The defaults are
2 retries and a 30 second timeout per request; both can be changed on the client and for a single
request:

=== "Python"

    ```python
    client = Whitebird(username, password, max_retries=5, timeout=60)
    client.exchange.orders.history(size=500, timeout=120)
    ```

=== "Node.js"

    ```ts
    const client = new Whitebird({ username, password, maxRetries: 5, timeout: 60_000 });
    await client.exchange.orders.get(orderId, { timeout: 5_000, maxRetries: 0 });
    ```

=== "Go"

    ```go
    client, err := whitebird.NewClient(whitebird.WithMaxRetries(5), whitebird.WithTimeout(time.Minute))
    order, err := client.Exchange.Orders.Get(ctx, orderID, whitebird.WithRequestTimeout(5*time.Second))
    ```

=== "Rust"

    ```rust
    let client = Client::builder().max_retries(5).timeout(Duration::from_secs(60)).build()?;
    let quick = client.with_options(RequestOptions::new().timeout(Duration::from_secs(5)));
    let order = quick.exchange().orders().get(&order_id).await?;
    ```

Rate limiting answers `RateLimitError`, whose `retry_after` tells how long to wait when the server
said so.
