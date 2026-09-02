"""Client settings shared by the engine and the authenticator."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import get_args

import httpx

from ._generated.services import SERVICE_URLS, Environment
from ._version import __version__

USER_AGENT = f"whitebird-api-python/{__version__}"
FALLBACK_ENVIRONMENT = "production"


@dataclass(frozen=True)
class Config:
    """Resolved client settings."""

    base_urls: Mapping[str, str]
    timeout: float | httpx.Timeout | None
    max_retries: int

    @classmethod
    def create(
        cls,
        environment: Environment,
        overrides: Mapping[str, str] | None,
        timeout: float | httpx.Timeout | None,
        max_retries: int,
    ) -> Config:
        """Resolves the base URL of every service for `environment`, applying `overrides`."""
        if environment not in get_args(Environment):
            known = ", ".join(get_args(Environment))
            raise ValueError(f"unknown environment {environment!r}, expected one of {known}")
        urls = {
            service: hosts.get(environment) or hosts[FALLBACK_ENVIRONMENT]
            for service, hosts in SERVICE_URLS.items()
        }
        urls.update(overrides or {})
        return cls(
            base_urls={service: url.rstrip("/") for service, url in urls.items()},
            timeout=timeout,
            max_retries=max_retries,
        )

    def url(self, service: str, path: str) -> str:
        """Returns the absolute URL of `path` on `service`."""
        try:
            base = self.base_urls[service]
        except KeyError:
            known = ", ".join(sorted(self.base_urls))
            raise ValueError(f"unknown service {service!r}, expected one of {known}") from None
        return base + path
