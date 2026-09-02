"""Constants and helpers shared by the tests."""

import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from whitebird import AsyncWhitebird, Tokens, Whitebird
from whitebird._generated.services import OAUTH_SERVICE, OAUTH_TOKEN_PATH

AUTH = "https://auth.test"
API = "https://api.test"
TOKEN_URL = f"{AUTH}{OAUTH_TOKEN_PATH}"
MFA_URL = f"{AUTH}/api/v1/client/mfa"
BASE_URLS = {OAUTH_SERVICE: AUTH, "api": API}

ClientFactory = Callable[..., Whitebird]
AsyncClientFactory = Callable[..., AsyncWhitebird]


def jwt(expires_in: float = 3600, subject: str = "user") -> str:
    def encode(part: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    claims = {"exp": int(time.time() + expires_in), "sub": subject}
    return f"{encode({'alg': 'HS256'})}.{encode(claims)}.signature"


def tokens(access_token: str | None = None, refresh_token: str = "refresh-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access_token or jwt(),
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 3600,
        },
    )


def session(expires_in: float = 3600, refresh_token: str | None = "refresh-0") -> Tokens:
    access_token = jwt(expires_in)
    return Tokens(access_token, refresh_token, time.time() + expires_in)


def form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))
