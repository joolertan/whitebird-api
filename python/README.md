# whitebird-api

[![PyPI](https://img.shields.io/pypi/v/whitebird-api?label=PyPI)](https://pypi.org/project/whitebird-api/)
[![Python](https://img.shields.io/pypi/pyversions/whitebird-api)](https://pypi.org/project/whitebird-api/)
[![Docs](https://img.shields.io/readthedocs/whitebird-api)](https://whitebird-api.readthedocs.io)

Unofficial Python client for the [Whitebird](https://whitebird.io) crypto exchange API, sync and
async, fully typed.

```sh
pip install whitebird-api
pip install "whitebird-api[otp]"   # TOTP codes for accounts with two-factor authentication
```

```python
import os

from whitebird import Whitebird
from whitebird.otp import TOTP

client = Whitebird(
    os.environ["WHITEBIRD_USERNAME"],
    os.environ["WHITEBIRD_PASSWORD"],
    otp=TOTP(os.environ["WHITEBIRD_TOTP_SECRET"]),
)

book = client.market.orderbook(symbol="BTCUSDT", limit=5)

# Paginated methods are iterable; pages are fetched as the loop goes.
for order in client.exchange.orders.history(size=50):
    print(order.number, order.status)

# Real-time notifications, reconnecting on their own.
with client.notifications.stream() as events:
    for event in events:
        print(event.data.type, event.data.payload)
```

`AsyncWhitebird` offers the same methods as coroutines, with `async for` over listings and
streams.

- **Sessions:** the client signs in on the first call that needs it, refreshes the token before it
  expires and keeps it in a token store (`FileTokenStore` survives restarts).
- **Captcha:** accounts without two-factor authentication may get a slider captcha; pass
  `captcha=` a function that returns the slider position.
- **Withdrawals:** with `otp` configured the client obtains the operation MFA token on its own;
  `client.confirm_operation("WITHDRAWAL", code)` does it explicitly.
- **Errors:** `APIError` subclasses carry `status_code`, `error_code`, `sub_status`, `message` and
  `fields`; network failures raise `TransportError`.
- **Escape hatch:** `client.request(service, method, path, ...)` calls endpoints the SDK does not
  cover yet.

Documentation: <https://whitebird-api.readthedocs.io> ·
Examples: [`examples/python`](https://github.com/joolertan/whitebird-api/tree/main/examples/python)

This project is not affiliated with Whitebird.
