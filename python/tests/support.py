"""Constants and helpers shared by the tests."""

import base64
import email.parser
import email.policy
import json
import time
from collections.abc import Callable
from typing import Any, cast

import httpx

from whitebird import AsyncWhitebird, Tokens, Whitebird
from whitebird._generated.services import (
    OAUTH_CAPTCHA_PATH,
    OAUTH_MFA_STATUS_PATH,
    OAUTH_SERVICE,
    OAUTH_TOKEN_PATH,
    OPERATION_MFA_SERVICE,
    OPERATION_MFA_TOKEN_PATH,
)

AUTH = "https://auth.test"
API = "https://api.test"
TOKEN_URL = f"{AUTH}{OAUTH_TOKEN_PATH}"
MFA_URL = f"{AUTH}{OAUTH_MFA_STATUS_PATH}"
CAPTCHA_URL = f"{AUTH}{OAUTH_CAPTCHA_PATH}"
BASE_URLS = {OAUTH_SERVICE: AUTH, "api": API}
CONFIRM_URL = f"{BASE_URLS[OPERATION_MFA_SERVICE]}{OPERATION_MFA_TOKEN_PATH}"

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


def multipart(request: httpx.Request) -> list[tuple[str, str | None, str | None, bytes]]:
    """Returns the name, content type, filename and content of every part of a request."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + request.content)
    return [
        (
            str(part.get_param("name", header="content-disposition")),
            part["content-type"],
            part.get_filename(),
            cast(bytes, part.get_payload(decode=True)),
        )
        for part in message.iter_parts()
    ]
