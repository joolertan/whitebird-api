"""JSON encoding and decoding that keeps decimal numbers exact."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


def loads(content: str | bytes) -> Any:
    """Decodes JSON, reading every non-integer number as a `Decimal`."""
    return json.loads(content, parse_float=Decimal)


def dumps(value: object) -> str:
    """Encodes JSON, writing `Decimal` values as exact numbers.

    Models are written with their wire names: the fields that were set, and the fields left
    unset that have a default other than None (the web application always sends those).
    """
    return "".join(_encode(value))


def format_time(value: date) -> str:
    """Formats a date as `YYYY-MM-DD` and a datetime as `YYYY-MM-DDTHH:MM:SS+HHMM`.

    That is how the web application sends timestamps: whole seconds and a numeric offset. A
    naive datetime is taken to be in UTC.
    """
    if not isinstance(value, datetime):
        return value.isoformat()
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S%z")


def model_fields(model: BaseModel) -> dict[str, Any]:
    """Returns the fields of a model to send, by wire name (see `dumps`)."""
    fields = type(model).model_fields
    values: dict[str, Any] = {}
    for name, info in fields.items():
        unset = name not in model.model_fields_set
        if unset and (info.is_required() or info.get_default() is None):
            continue
        values[info.serialization_alias or info.alias or name] = getattr(model, name)
    values.update(model.model_extra or {})
    return values


def _encode(value: object) -> Iterator[str]:
    if isinstance(value, BaseModel):
        value = model_fields(value)
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{value} cannot be encoded as a JSON number")
        yield format(value, "f")
    elif isinstance(value, date):
        yield json.dumps(format_time(value))
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
