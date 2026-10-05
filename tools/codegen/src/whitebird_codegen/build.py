"""Builds the IR from the OpenAPI documents in ``spec/``."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, cast, get_args

import yaml

from . import naming
from .ir import (
    IR,
    Auth,
    Body,
    BodyKind,
    Enum,
    EnumValue,
    Envelope,
    Field,
    Model,
    OAuth,
    Operation,
    OperationMfa,
    Pagination,
    Param,
    Resource,
    Response,
    ResponseKind,
    Service,
    Status,
    TypeRef,
)

Schema = dict[str, Any]

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
STRING_FORMATS = {
    "date-time": TypeRef("datetime"),
    "date": TypeRef("date"),
    "binary": TypeRef("binary"),
}
CORE_HEADERS = frozenset({"authorization", "x-mfa-token", "accept-language", "content-type"})
BODY_MEDIA: tuple[tuple[str, BodyKind], ...] = (
    ("application/json", "json"),
    ("application/x-www-form-urlencoded", "form"),
    ("multipart/form-data", "multipart"),
    ("*/*", "json"),
)


class SpecError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


def load(spec_dir: Path) -> IR:
    environments = yaml.safe_load((spec_dir / "environments.yaml").read_text())
    documents = [
        (path.name, yaml.safe_load(path.read_text()))
        for path in sorted((spec_dir / "services").glob("*.yaml"))
    ]
    return Builder(environments).build(documents)


class Builder:
    def __init__(self, environments: dict[str, Any]) -> None:
        self.problems: list[str] = []
        self.environments: list[str] = environments["environments"]
        self.services = {
            key: Service(
                key=key,
                title=value["title"],
                urls={env: value[env] for env in self.environments if env in value},
            )
            for key, value in sorted(environments["services"].items())
        }
        oauth = environments["oauth"]
        self.oauth = OAuth(
            service=oauth["service"],
            token_path=oauth["token_path"],
            basic=oauth["basic"],
            mfa_status_path=oauth["mfa_status_path"],
            captcha_path=oauth["captcha_path"],
        )
        mfa = environments["operation_mfa"]
        self.operation_mfa = OperationMfa(service=mfa["service"], token_path=mfa["token_path"])
        self.root = Resource(())
        self.models: dict[str, Model] = {}
        self.enums: dict[str, Enum] = {}
        self.shapes: dict[str, tuple[str, str]] = {}
        self.operation_ids: set[str] = set()
        self.file = ""
        self.document: Schema = {}
        self.service = ""

    def build(self, documents: list[tuple[str, Schema]]) -> IR:
        for file, document in documents:
            self._document(file, document)
        self._check_resources()
        if self.problems:
            raise SpecError(self.problems)
        return IR(
            environments=self.environments,
            services=self.services,
            oauth=self.oauth,
            operation_mfa=self.operation_mfa,
            root=self.root,
            models=dict(sorted(self.models.items())),
            enums=dict(sorted(self.enums.items())),
        )

    def _problem(self, message: str) -> None:
        self.problems.append(f"{self.file}: {message}")

    def _document(self, file: str, document: Schema) -> None:
        self.file, self.document = file, document
        self.service = document.get("x-wb-service", "")
        if self.service not in self.services:
            self._problem(f"unknown x-wb-service {self.service!r}")
            return
        for path, item in document.get("paths", {}).items():
            shared = item.get("parameters", [])
            for method in HTTP_METHODS:
                if method in item:
                    self._operation(path, method, item[method], shared)

    def _operation(self, path: str, method: str, raw: Schema, shared: list[Schema]) -> None:
        where = f"{method.upper()} {path}"
        op_id, resource, name = (
            raw.get("operationId"),
            raw.get("x-wb-resource"),
            raw.get("x-wb-name"),
        )
        if not (op_id and resource and name):
            self._problem(f"{where}: operationId, x-wb-resource and x-wb-name are required")
            return
        if op_id in self.operation_ids:
            self._problem(f"{where}: duplicate operationId {op_id}")
        self.operation_ids.add(op_id)

        envelope = raw.get("x-wb-envelope", self.document.get("x-wb-envelope"))
        if envelope not in (None, *get_args(Envelope)):
            self._problem(f"{where}: invalid x-wb-envelope {envelope!r}")
            return
        auth = raw.get("x-wb-auth", "user")
        status = raw.get("x-wb-status", "deprecated" if raw.get("deprecated") else "stable")
        if auth not in get_args(Auth) or status not in get_args(Status):
            self._problem(f"{where}: invalid x-wb-auth {auth!r} or x-wb-status {status!r}")
            return

        hint = naming.pascal(op_id)
        params = [
            p for p in (self._param(item) for item in [*shared, *raw.get("parameters", [])]) if p
        ]
        declared = {p.wire: p for p in params if p.location == "path"}
        path_params = [
            declared.pop(wire)
            if wire in declared
            else Param(naming.snake(wire), wire, "path", TypeRef("string"), True)
            for wire in re.findall(r"{([^}]+)}", path)
        ]
        for wire in declared:
            self._problem(f"{where}: path parameter {wire} is not in the path")
        query = [p for p in params if p.location == "query"]
        headers = [p for p in params if p.location == "header"]
        body = self._body(raw.get("requestBody"), f"{hint}Request", where)
        response = self._response(raw, f"{hint}Response")
        self._check_collisions(where, [*path_params, *query, *headers], body)

        node = self.root
        for segment in resource.split("."):
            node = node.child(segment)
        if any(existing.name == name for existing in node.operations):
            self._problem(f"{where}: {resource}.{name} is defined twice")
            return
        node.operations.append(
            Operation(
                id=op_id,
                name=name,
                resource=node.path,
                service=self.service,
                method=method.upper(),
                path=path,
                summary=raw.get("summary"),
                description=raw.get("description"),
                path_params=path_params,
                query_params=query,
                header_params=headers,
                body=body,
                response=response,
                auth=cast(Auth, auth),
                status=cast(Status, status),
                source=raw.get("x-wb-source"),
                pagination=self._pagination(response, query, body),
                mfa_operation=raw.get("x-wb-mfa-operation"),
                envelope=cast(Envelope | None, envelope),
            )
        )

    def _check_resources(self) -> None:
        for resource in self.root.walk():
            for op in resource.operations:
                if op.name in resource.children:
                    path = ".".join((*resource.path, op.name))
                    self.problems.append(f"{path} is both an operation and a resource")

    def _check_collisions(self, where: str, params: list[Param], body: Body | None) -> None:
        names = [p.name for p in params] + [f.name for f in (body.fields if body else [])]
        for duplicate in sorted({n for n in names if names.count(n) > 1}):
            self._problem(f"{where}: parameter name {duplicate} is used twice")

    def _param(self, raw: Schema) -> Param | None:
        param = self._deref(raw)
        location = param.get("in")
        if location not in ("path", "query", "header"):
            return None
        if location == "header" and param["name"].lower() in CORE_HEADERS:
            return None
        schema = param.get("schema", {})
        return Param(
            name=naming.snake(param["name"]),
            wire=param["name"],
            location=location,
            type=self._type(schema, naming.pascal(param["name"])),
            required=param.get("required", location == "path"),
            description=param.get("description"),
            default=schema.get("default"),
        )

    def _body(self, raw: Schema | None, hint: str, where: str) -> Body | None:
        if not raw:
            return None
        request = self._deref(raw)
        content = request.get("content", {})
        match = next(((media, kind) for media, kind in BODY_MEDIA if media in content), None)
        if match is None:
            self._problem(f"{where}: unsupported request media {sorted(content)}")
            return None
        media, kind = match
        schema = content[media].get("schema", {})
        body_type = self._type(schema, hint)
        if body_type.kind == "model" and body_type.name:
            fields = self.models[body_type.name].fields
        else:
            fields = []
        encoding: dict[str, Schema] = content[media].get("encoding") or {}
        if encoding:
            fields = [
                replace(f, content_type=encoding.get(f.wire, {}).get("contentType")) for f in fields
            ]
        # An object without properties (`{}` is all the web app sends) still flattens.
        plain_object = self._deref(schema).get("type") == "object" and body_type.kind == "any"
        return Body(
            kind=kind,
            type=body_type,
            fields=fields,
            required=request.get("required", True),
            flattened=body_type.kind == "model" or plain_object,
        )

    def _response(self, raw: Schema, hint: str) -> Response:
        responses = raw.get("responses", {})
        success = next((code for code in sorted(map(str, responses)) if code.startswith("2")), None)
        override = raw.get("x-wb-response")
        if success is None:
            return Response(cast(ResponseKind, override or "empty"))
        response = self._deref(responses.get(success) or responses.get(int(success)) or {})
        content: dict[str, Schema] = response.get("content") or {}
        if not content:
            return Response(cast(ResponseKind, override or "empty"))
        media = next((m for m in content if "json" in m), next(iter(content)))
        kind = cast(ResponseKind, override or _response_kind(media))
        schema = content[media].get("schema")
        if kind in ("json", "sse") and schema:
            return Response(kind, self._type(schema, hint))
        return Response(kind, TypeRef("any") if kind == "json" else None)

    def _pagination(
        self, response: Response, query: list[Param], body: Body | None
    ) -> Pagination | None:
        if not response.type or response.type.kind != "page" or not response.type.item:
            return None
        body_names = {f.wire for f in body.fields} if body else set()
        query_names = {p.wire for p in query}
        in_body = "page" in body_names and "page" not in query_names
        return Pagination(response.type.item, "page", "size", "body" if in_body else "query")

    def _deref(self, schema: Schema) -> Schema:
        while "$ref" in schema:
            schema = self._lookup(schema["$ref"])[1]
        return schema

    def _lookup(self, ref: str) -> tuple[str, Schema]:
        if not ref.startswith("#/"):
            raise SpecError([f"{self.file}: only local $ref is supported, got {ref}"])
        node: Any = self.document
        for part in ref[2:].split("/"):
            node = node[part]
        return ref.rsplit("/", 1)[-1], node

    def _type(self, schema: Schema, hint: str) -> TypeRef:
        if "$ref" in schema:
            name, target = self._lookup(schema["$ref"])
            return self._named(naming.pascal(name), target)
        schema = _without_null(schema)
        if "$ref" in schema:
            return self._type(schema, hint)
        variants = schema.get("oneOf") or schema.get("anyOf")
        if variants:
            return TypeRef("any")
        if "allOf" in schema and len(schema["allOf"]) == 1 and "properties" not in schema:
            return self._type(schema["allOf"][0], hint)
        if "enum" in schema:
            return self._enum(naming.pascal(hint), schema)
        kind = schema.get("type")
        fmt = schema.get("format")
        # A JSON document carried in a string, e.g. the JSON part of a multipart body.
        if kind == "string" and "json" in schema.get("contentMediaType", ""):
            content = schema.get("contentSchema")
            if content:
                return self._type(content, hint)
        if kind == "string":
            return STRING_FORMATS.get(fmt or "", TypeRef("string"))
        if kind == "integer":
            return TypeRef("integer", format=fmt)
        if kind == "number":
            # Springdoc renders BigDecimal as a bare number; float and double carry a format.
            return TypeRef("decimal") if fmt in (None, "decimal") else TypeRef("number", format=fmt)
        if kind == "boolean":
            return TypeRef("boolean")
        if kind == "array":
            return TypeRef.array(self._type(schema.get("items", {}), f"{hint}Item"))
        if "properties" in schema or "allOf" in schema:
            return self._model(naming.pascal(hint), schema)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            return TypeRef.map(self._type(extra, f"{hint}Value"))
        return TypeRef("any")

    def _named(self, name: str, schema: Schema) -> TypeRef:
        item = self._page_item(schema)
        if item is not None:
            return TypeRef.page(self._type(item, f"{name}Item"))
        if "enum" in _without_null(schema):
            return self._enum(name, _without_null(schema))
        if "properties" in schema or "allOf" in schema or schema.get("type") == "object":
            return self._model(name, schema)
        return self._type(schema, name)

    def _page_item(self, schema: Schema) -> Schema | None:
        properties = self._properties(schema)[0]
        content = properties.get("content")
        if content is None or "totalElements" not in properties:
            return None
        content = self._deref(content)
        return cast(Schema, content.get("items", {})) if content.get("type") == "array" else None

    def _register(self, name: str, schema: Schema) -> bool:
        """Returns False when ``name`` is already registered with the same shape."""
        shape = json.dumps(schema, sort_keys=True, default=str)
        previous = self.shapes.get(name)
        if previous is None:
            self.shapes[name] = (self.file, shape)
            return True
        if previous[1] != shape:
            self._problem(
                f"schema {name} conflicts with the one in {previous[0]}; rename one of them"
            )
        return False

    def _enum(self, name: str, schema: Schema) -> TypeRef:
        if self._register(name, schema):
            values = [v for v in schema["enum"] if v is not None]
            self.enums[name] = Enum(
                name=name,
                values=[
                    EnumValue(wire=v, name=naming.upper_snake(str(v)) or str(v)) for v in values
                ],
                base="integer" if schema.get("type") == "integer" else "string",
                description=schema.get("description"),
            )
        return TypeRef.enum(name)

    def _model(self, name: str, schema: Schema) -> TypeRef:
        if not self._register(name, schema):
            return TypeRef.model(name)
        model = Model(
            name=name, fields=[], description=schema.get("description"), service=self.service
        )
        self.models[name] = model
        properties, required = self._properties(schema)
        for wire, prop in properties.items():
            field_name = naming.snake(wire) or wire
            if any(f.name == field_name for f in model.fields):
                self._problem(f"{name}: fields {wire} and another field both map to {field_name}")
                continue
            inferred = bool(prop.get("x-wb-inferred", False))
            model.fields.append(
                Field(
                    name=field_name,
                    wire=wire,
                    type=self._type(prop, f"{name}{naming.pascal(wire)}"),
                    required=wire in required and not inferred,
                    nullable=_is_nullable(prop),
                    description=prop.get("description"),
                    inferred=inferred,
                    deprecated=bool(prop.get("deprecated", False)),
                    default=prop.get("default"),
                )
            )
        return TypeRef.model(name)

    def _properties(self, schema: Schema) -> tuple[dict[str, Schema], set[str]]:
        schema = self._deref(schema)
        properties: dict[str, Schema] = {}
        required: set[str] = set(schema.get("required", []))
        for part in schema.get("allOf", []):
            nested, nested_required = self._properties(part)
            properties.update(nested)
            required |= nested_required
        properties.update(schema.get("properties", {}))
        return properties, required


def _response_kind(media: str) -> ResponseKind:
    if "json" in media or media == "*/*":
        return "json"
    if media == "text/event-stream":
        return "sse"
    if media.startswith("text/"):
        return "text"
    return "binary"


def _without_null(schema: Schema) -> Schema:
    kind = schema.get("type")
    if isinstance(kind, list):
        rest = [k for k in kind if k != "null"]
        return {**schema, "type": rest[0] if len(rest) == 1 else rest}
    for key in ("oneOf", "anyOf"):
        variants = schema.get(key)
        if variants:
            rest = [v for v in variants if v.get("type") != "null"]
            if len(rest) == 1:
                merged = {k: v for k, v in schema.items() if k != key}
                return {**merged, **rest[0]}
    return schema


def _is_nullable(schema: Schema) -> bool:
    kind = schema.get("type")
    if isinstance(kind, list) and "null" in kind:
        return True
    variants = schema.get("oneOf") or schema.get("anyOf") or []
    return any(v.get("type") == "null" for v in variants) or bool(schema.get("nullable"))
