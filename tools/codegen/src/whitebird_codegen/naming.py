import re

_WORD = re.compile(r"[A-Z]?[a-z]+\d*|[A-Z]+\d*(?=[A-Z][a-z]|$)|[A-Z]+\d*|\d+[a-z]*")


def words(value: str) -> list[str]:
    parts: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", value):
        parts.extend(match.group(0).lower() for match in _WORD.finditer(chunk))
    return parts


def snake(value: str) -> str:
    return "_".join(words(value))


def upper_snake(value: str) -> str:
    return snake(value).upper()


def pascal(value: str) -> str:
    return "".join(word.capitalize() for word in words(value))


def camel(value: str) -> str:
    result = pascal(value)
    return result[:1].lower() + result[1:]


def escape(
    name: str, reserved: frozenset[str], *, suffix: str = "_", digit_prefix: str = "n"
) -> str:
    if name[:1].isdigit():
        name = digit_prefix + name
    if name in reserved:
        name += suffix
    return name
