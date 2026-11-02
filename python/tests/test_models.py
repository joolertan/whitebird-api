from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from whitebird._models import Date, DateTime, IntEnum, StrEnum, wire_name
from whitebird.models import WhitebirdModel


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Level(IntEnum):
    LOW = 1


class Trade(WhitebirdModel):
    trade_id: str
    side: Side | str | None = None
    sides: list[Side | str] | None = None
    level: Level | int | None = None
    amount: Decimal | None = None
    executed_at: DateTime | None = None
    settled_on: Date | None = None
    from_: str | None = None


def test_wire_names_and_snake_case_are_both_accepted() -> None:
    assert Trade.model_validate({"tradeId": "t", "from": "BTC"}) == Trade(trade_id="t", from_="BTC")
    assert Trade(trade_id="t").model_dump(by_alias=True, exclude_unset=True) == {"tradeId": "t"}


def test_unknown_fields_are_kept() -> None:
    trade = Trade.model_validate({"tradeId": "t", "newField": [1]})
    assert trade.model_extra == {"newField": [1]}


def test_enums_are_open() -> None:
    trade = Trade.model_validate(
        {"tradeId": "t", "side": "BUY", "sides": ["SELL", "SHORT"], "level": 7}
    )
    assert trade.side is Side.BUY
    assert trade.sides == [Side.SELL, "SHORT"]
    assert isinstance(trade.sides[0], Side)
    assert trade.level == 7
    assert not isinstance(trade.level, Level)
    assert Trade.model_validate({"tradeId": "t", "level": 1}).level is Level.LOW


@pytest.mark.parametrize("value", [{"a": 1}, ["BUY"]])
def test_enum_of_the_wrong_shape_is_a_validation_error(value: object) -> None:
    with pytest.raises(ValidationError):
        Trade.model_validate({"tradeId": "t", "side": value})


def test_unknown_values_of_a_closed_enum_type_fail() -> None:
    class Strict(WhitebirdModel):
        side: Side

    with pytest.raises(ValidationError):
        Strict.model_validate({"side": "SHORT"})


@pytest.mark.parametrize(
    "value",
    [
        "2024-05-01T12:30:00Z",
        "2024-05-01T15:30:00+03:00",
        "2024-05-01T15:30:00+0300",
        "2024-05-01T12:30:00",
        1714566600000,
        "1714566600000",
    ],
)
def test_lenient_timestamps(value: object) -> None:
    trade = Trade.model_validate({"tradeId": "t", "executedAt": value})
    assert trade.executed_at == datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
    assert trade.executed_at is not None
    assert trade.executed_at.tzinfo is not None


@pytest.mark.parametrize("value", ["2024-05-01", "2024-05-01T10:00:00", 1714566600000])
def test_lenient_dates(value: object) -> None:
    assert Trade.model_validate({"tradeId": "t", "settledOn": value}).settled_on == date(2024, 5, 1)


def test_decimals_stay_exact() -> None:
    trade = Trade.model_validate({"tradeId": "t", "amount": Decimal("0.123456789012345678")})
    assert trade.amount == Decimal("0.123456789012345678")


@pytest.mark.parametrize(
    ("name", "wire"),
    [("trade_id", "tradeId"), ("from_", "from"), ("address_line2", "addressLine2"), ("id", "id")],
)
def test_wire_name(name: str, wire: str) -> None:
    assert wire_name(name) == wire
