import builtins
import importlib
import sys
from datetime import UTC, datetime
from typing import Any

import pytest

from whitebird.otp import TOTP

# RFC 6238, appendix B: HMAC-SHA1 with the ASCII secret "12345678901234567890".
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize(
    ("timestamp", "code"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc_6238_vectors(timestamp: int, code: str) -> None:
    assert TOTP(RFC_SECRET, digits=8).at(timestamp) == code


def test_six_digit_codes_and_datetimes() -> None:
    otp = TOTP("gezd gnbv gy3t qojq gezd gnbv gy3t qojq")
    assert otp.at(datetime.fromtimestamp(59, UTC)) == "287082"
    assert len(otp()) == 6
    assert otp() == otp.at(datetime.now(UTC))
    assert "GEZD" not in repr(otp)


def test_custom_period() -> None:
    assert TOTP(RFC_SECRET, digits=8, period=60).at(119) == TOTP(RFC_SECRET, digits=8).at(59)


def test_missing_pyotp_explains_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def without_pyotp(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pyotp":
            raise ImportError("No module named 'pyotp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pyotp)
    monkeypatch.delitem(sys.modules, "whitebird.otp")

    with pytest.raises(ImportError, match=r'pip install "whitebird-api\[otp\]"'):
        importlib.import_module("whitebird.otp")
