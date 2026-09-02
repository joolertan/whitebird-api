"""Models and enums of the Whitebird API.

Responses are parsed into these pydantic models. Attributes are snake_case versions of the wire
names, fields unknown to this release are kept in `model_extra`, and enum fields hold the raw
value when Whitebird sends one this release does not know.
"""

from whitebird._generated import models as _generated
from whitebird._generated.models import *  # noqa: F403
from whitebird._models import WhitebirdModel

__all__ = ["WhitebirdModel"]
__all__ += _generated.__all__
