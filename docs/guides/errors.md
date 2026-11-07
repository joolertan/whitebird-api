# Errors

Every failure is one of two kinds:

- a **transport error**: no response arrived (connection refused, TLS failure, timeout);
- an **API error**: Whitebird answered and refused the request.

Whitebird services describe errors as
`{"code": 400, "status": "INVALID_SMS_CODE", "subStatus": "...", "message": "...", "fields": [...]}`.
The OAuth endpoint's `{"error", "error_description"}` and the `{"retCode", "retMsg"}` of market
data and trading are mapped onto the same values. When the body is not JSON, its text becomes the
message.

| Value | Python | Node.js | Go | Rust |
|---|---|---|---|---|
| HTTP status | `status_code` | `status` | `StatusCode` | `status()` |
| numeric `code` or `retCode` | `code` | `code` | `Code` | `code()` |
| `status` string, the one to branch on | `error_code` | `errorCode` | `ErrorCode` | `error_code()` |
| `subStatus` | `sub_status` | `subStatus` | `SubStatus` | `sub_status()` |
| message | `message` | `message` | `Message` | `message()` |
| per-field validation errors | `fields` | `fields` | `Fields` | `fields()` |
| raw body | `body` | `body` | `Body` | `body()` |

## Classes

The class names below are the Python and Node.js ones. Go has a sentinel for each class
(`ErrBadRequest`, `ErrVerificationRequired`, `ErrAuthentication`, ...), Rust an `ErrorKind`
variant (`BadRequest`, `VerificationRequired`, `Authentication`, ...).

```
WhitebirdError
├── TransportError
└── APIError
    ├── BadRequestError            400
    │   └── VerificationRequiredError   SMS or liveness check pending
    ├── AuthenticationError        401, rejected credentials or code, unknown username
    │   ├── MFARequiredError       a 2FA code or an operation confirmation is missing
    │   └── CaptchaRequiredError   sign-in needs a captcha and no solver is configured
    ├── PermissionDeniedError      403
    ├── NotFoundError              404
    ├── ConflictError              409
    ├── RateLimitError             429, with the server's Retry-After
    └── ServerError                5xx
```

=== "Python"

    Exceptions in the `whitebird` package.

    ```python
    from whitebird import APIError, NotFoundError, TransportError

    try:
        order = client.exchange.orders.get(order_id)
    except NotFoundError:
        print("No such order")
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

The web application never repeats a request, so the SDKs repeat only those that are safe to
repeat:

- requests that only read (`GET`, `HEAD`, `OPTIONS`) are retried on network errors, timeouts and
  HTTP 429, 502, 503 and 504;
- all other requests, which place orders, cancel them and move money, are retried only when they
  certainly did not reach Whitebird: the connection could not be established, or the server
  answered 429 (too many requests). A timeout or a server error after the request was sent is
  raised, because the operation may have gone through.

Retries wait with exponential backoff and jitter, or as long as `Retry-After` asks. The defaults
are 2 retries and a 30 second timeout per request; both can be changed on the client and for a
single request:

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

Rate limiting answers `RateLimitError` once the retries are used up; its retry-after value
(`retry_after`, `retryAfter`, `RetryAfter`, `retry_after()`) tells how long to wait when the server
said so.
