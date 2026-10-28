"""Imports a service's public OpenAPI document into ``spec/services``.

Annotations added by hand survive re-imports: ``operationId``, ``x-wb-*``, summaries and
descriptions of operations (matched by method and path) and descriptions of schema properties
(matched by schema and property name).
"""

from __future__ import annotations

import copy
import datetime
import json
import re
import urllib.request
from typing import Any

from . import naming

Schema = dict[str, Any]

EXCLUDED_PATH = re.compile(r"/management/|/admin(/|$)")
EXCLUDED_OPERATION = re.compile(r"admin", re.IGNORECASE)
STEM_SUFFIXES = frozenset({"request", "response", "filter", "details", "data", "calculation"})
PRESERVED = (
    "operationId",
    "summary",
    "description",
    "x-wb-resource",
    "x-wb-name",
    "x-wb-auth",
    "x-wb-status",
    "x-wb-first-seen",
    "x-wb-response",
    "x-wb-envelope",
    "x-wb-session",
)
PAGEABLE = [
    {
        "name": "page",
        "in": "query",
        "description": "Zero-based page index.",
        "schema": {"type": "integer", "minimum": 0, "default": 0},
    },
    {
        "name": "size",
        "in": "query",
        "description": "Page size.",
        "schema": {"type": "integer", "minimum": 1, "default": 20},
    },
    {
        "name": "sort",
        "in": "query",
        "description": "Sort criteria in the format `property,(asc|desc)`.",
        "schema": {"type": "array", "items": {"type": "string"}},
    },
]


def fetch(source: str) -> Schema:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "whitebird-codegen"})
        with urllib.request.urlopen(request, timeout=30) as response:
            document: Schema = json.load(response)
            return document
    with open(source, encoding="utf-8") as handle:
        document = json.load(handle)
        return document


def convert(source: Schema, service: str, url: str, existing: Schema | None) -> Schema:
    previous = _annotations(existing or {})
    today = datetime.date.today().isoformat()
    paths: dict[str, Schema] = {}
    for path, item in sorted(source.get("paths", {}).items()):
        if EXCLUDED_PATH.search(path):
            continue
        for method, raw in item.items():
            if EXCLUDED_OPERATION.search(raw.get("operationId", "")):
                continue
            operation = _operation(raw)
            known = previous.get((method, path), {})
            tag = (raw.get("tags") or [service])[0]
            operation.update(
                {
                    "operationId": naming.camel(f"{service} {raw['operationId']}"),
                    "x-wb-resource": f"{service}.{_plural(naming.snake(tag.removesuffix(' API')))}",
                    "x-wb-name": naming.snake(raw["operationId"]),
                    "x-wb-source": "openapi",
                    "x-wb-first-seen": today,
                    **known,
                }
            )
            paths.setdefault(path, {})[method] = operation

    schemas = _hoist_enums(copy.deepcopy(source.get("components", {}).get("schemas", {})))
    _keep_descriptions(schemas, (existing or {}).get("components", {}).get("schemas", {}))
    _exclusive_bounds(paths)
    _exclusive_bounds(schemas)
    document: Schema = {
        "openapi": "3.1.0",
        "info": {
            "title": source.get("info", {}).get("title", service),
            "version": source.get("info", {}).get("version", ""),
        },
        "x-wb-service": service,
        "servers": [{"url": url}],
        "paths": paths,
        "components": {
            "schemas": {name: schemas[name] for name in sorted(_reachable(paths, schemas))}
        },
    }
    return document


def _annotations(document: Schema) -> dict[tuple[str, str], Schema]:
    found: dict[tuple[str, str], Schema] = {}
    for path, item in document.get("paths", {}).items():
        for method, operation in item.items():
            found[(method, path)] = {key: operation[key] for key in PRESERVED if key in operation}
    return found


def _keep_descriptions(schemas: Schema, previous: Schema) -> None:
    """Keeps property descriptions written into the spec by hand."""
    for name, schema in schemas.items():
        old = previous.get(name, {}).get("properties", {})
        for prop_name, prop in schema.get("properties", {}).items():
            description = old.get(prop_name, {}).get("description")
            if description:
                prop["description"] = description


def _exclusive_bounds(node: Any) -> None:
    """Rewrites OpenAPI 3.0 boolean ``exclusiveMinimum``/``exclusiveMaximum`` in the 3.1 form."""
    if isinstance(node, dict):
        for exclusive, bound in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
            flag = node.get(exclusive)
            if not isinstance(flag, bool):
                continue
            del node[exclusive]
            if flag and bound in node:
                node[exclusive] = node.pop(bound)
        for value in node.values():
            _exclusive_bounds(value)
    elif isinstance(node, list):
        for value in node:
            _exclusive_bounds(value)


def _operation(raw: Schema) -> Schema:
    operation: Schema = {
        key: copy.deepcopy(raw[key]) for key in ("summary", "description") if raw.get(key)
    }
    parameters: list[Schema] = []
    for parameter in raw.get("parameters", []):
        if parameter.get("schema", {}).get("$ref", "").endswith("/Pageable"):
            parameters.extend(copy.deepcopy(PAGEABLE))
        else:
            parameters.append(copy.deepcopy(parameter))
    if parameters:
        operation["parameters"] = parameters
    if "requestBody" in raw:
        operation["requestBody"] = _json_media(copy.deepcopy(raw["requestBody"]))
    operation["responses"] = {
        code: _json_media(copy.deepcopy(response))
        for code, response in raw.get("responses", {}).items()
        if str(code).startswith("2")
    }
    return operation


def _json_media(container: Schema) -> Schema:
    content = container.get("content")
    if content and "*/*" in content:
        content["application/json"] = content.pop("*/*")
    return container


def _reachable(node: Any, schemas: Schema, found: set[str] | None = None) -> set[str]:
    found = set() if found is None else found
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            if name not in found:
                found.add(name)
                _reachable(schemas.get(name, {}), schemas, found)
        for value in node.values():
            _reachable(value, schemas, found)
    elif isinstance(node, list):
        for value in node:
            _reachable(value, schemas, found)
    return found


def _hoist_enums(schemas: Schema) -> Schema:
    """Replaces inline enums that appear more than once with a shared component."""
    usages: dict[tuple[str, ...], list[tuple[str, str, Schema]]] = {}
    for parent, schema in sorted(schemas.items()):
        for name, prop in schema.get("properties", {}).items():
            target = prop.get("items", prop) if prop.get("type") == "array" else prop
            if "enum" in target and target.get("type", "string") == "string":
                usages.setdefault(tuple(sorted(target["enum"])), []).append((parent, name, target))
    for values, places in sorted(usages.items()):
        if len(places) < 2:
            continue
        component = _enum_name(places)
        if component in schemas and schemas[component].get("enum") != list(values):
            component = naming.pascal(f"{_stem(places[0][0])} {places[0][1]}")
        schemas[component] = {"type": "string", "enum": list(values)}
        for _, _, target in places:
            target.clear()
            target["$ref"] = f"#/components/schemas/{component}"
    return schemas


def _enum_name(places: list[tuple[str, str, Schema]]) -> str:
    stems = [naming.words(_stem(parent)) for parent, _, _ in places]
    prefix: list[str] = []
    for column in zip(*stems, strict=False):
        if len(set(column)) != 1:
            break
        prefix.append(column[0])
    prop = min((name for _, name, _ in places), key=len)
    return naming.pascal(" ".join([*prefix, prop]))


def _stem(schema_name: str) -> str:
    parts = naming.words(schema_name)
    while len(parts) > 1 and parts[-1] in STEM_SUFFIXES:
        parts.pop()
    return " ".join(parts)


def _plural(word: str) -> str:
    if word.endswith("s") and not word.endswith("ss"):
        return word
    if word.endswith(("ss", "x", "sh", "ch")):
        return word + "es"
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"
