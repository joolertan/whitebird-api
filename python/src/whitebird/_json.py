"""JSON encoding and decoding that keeps decimal numbers exact."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


def loads(content: str | bytes) -> Any:
    """Decodes JSON, reading every non-integer number as a `Decimal`."""
    return json.loads(content, parse_float=Decimal)


def dumps(value: object) -> str:
    """Encodes JSON, writing `Decimal` values as exact numbers.

    Models are written with their wire names and only the fields that were set.
    """
    return "".join(_encode(value))


def _encode(value: object) -> Iterator[str]:
    if isinstance(value, BaseModel):
        value = value.model_dump(by_alias=True, exclude_unset=True)
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{value} cannot be encoded as a JSON number")
        yield format(value, "f")
    elif isinstance(value, datetime | date):
        yield json.dumps(value.isoformat())
    elif isinstance(value, Mapping):
        yield "{"
        for index, (key, item) in enumerate(value.items()):
            yield f"{',' if index else ''}{json.dumps(str(key))}:"
            yield from _encode(item)
        yield "}"
    elif isinstance(value, list | tuple):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _encode(item)
        yield "]"
    else:
        yield json.dumps(value, allow_nan=False)
