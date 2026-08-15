"""Emitter registry.

An emitter is a module exposing:

* ``OWNED``: glob patterns (relative to the repository root) of the files it generates; files
  matching them that the emitter no longer produces are deleted.
* ``emit(ir) -> dict[str, str]``: generated files keyed by path relative to the repository root.
"""

from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType

LANGUAGES = ("python", "node", "go", "rust", "docs")


def load(language: str) -> ModuleType:
    if language not in LANGUAGES:
        raise ValueError(f"unknown target {language!r}, expected one of {', '.join(LANGUAGES)}")
    return importlib.import_module(f"{__name__}.{language}")


def available() -> list[str]:
    return [
        lang for lang in LANGUAGES if importlib.util.find_spec(f"{__name__}.{lang}") is not None
    ]
