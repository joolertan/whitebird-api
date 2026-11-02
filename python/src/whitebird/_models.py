"""Base classes and field types shared by the generated models."""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AliasGenerator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    GetCoreSchemaHandler,
)
from pydantic_core import CoreSchema, core_schema


def wire_name(name: str) -> str:
    """Returns the camelCase wire name of a snake_case attribute (`from_` becomes `from`)."""
    head, *rest = name.rstrip("_").split("_")
    return head + "".join(part.capitalize() for part in rest)


class WhitebirdModel(BaseModel):
    """Base class of every API model.

    Attributes are snake_case and map to the camelCase names used on the wire; both spellings
    are accepted when validating. Fields this version of the SDK does not know about are kept
    in `model_extra` instead of being dropped.
    """

    model_config = ConfigDict(
        alias_generator=AliasGenerator(validation_alias=wire_name, serialization_alias=wire_name),
        validate_by_name=True,
        validate_by_alias=True,
        extra="allow",
        use_attribute_docstrings=True,
    )


class _OpenEnum:
    # Known values become members before validation; unknown ones fail here and fall through
    # to the plain `str` / `int` side of the `X | str` unions used for every enum field.
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        members: dict[object, object] = getattr(cls, "_value2member_map_", {})

        def to_member(value: object) -> object:
            try:
                return members.get(value, value)
            except TypeError:  # unhashable, e.g. an object where a value is expected
                return value

        return core_schema.no_info_before_validator_function(to_member, handler(source))


class StrEnum(_OpenEnum, enum.StrEnum):
    """String enum of the API.

    Fields typed `X | str` hold an `X` member for known values and the raw string for values
    added by Whitebird after this release.
    """


class IntEnum(_OpenEnum, enum.IntEnum):
    """Integer enum of the API.

    Fields typed `X | int` hold an `X` member for known values and the raw integer otherwise.
    """


def _from_epoch_millis(value: object) -> object:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value) / 1000, UTC)
    return value


def _assume_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _to_date(value: object) -> object:
    value = _from_epoch_millis(value)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and len(value) > 10 and value[10] in "T ":
        return value[:10]
    return value


DateTime = Annotated[datetime, BeforeValidator(_from_epoch_millis), AfterValidator(_assume_utc)]
"""A timestamp: RFC 3339, ISO 8601 with a `+HHMM` offset or none (UTC), or epoch milliseconds."""

Date = Annotated[date, BeforeValidator(_to_date)]
"""A calendar date: ISO 8601 date, the date part of a timestamp, or epoch milliseconds."""
