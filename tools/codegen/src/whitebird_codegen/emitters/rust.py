"""Rust emitter: generates ``rust/src/generated/`` from the IR.

``models.rs`` holds models and open enums, ``params.rs`` one parameter struct per method (query,
header and flattened body fields), ``resources.rs`` the resource handles reached from ``Client``
accessors and ``blocking.rs`` their blocking counterparts. Output is piped through rustfmt.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass, replace

from .. import naming
from ..ir import IR, Enum, Field, Model, Operation, Pagination, Resource, TypeRef
from . import _util

OWNED = ["rust/src/generated/*.rs"]

TARGET = "rust/src/generated"
FILES = ("mod", "services", "models", "params", "resources", "blocking")

KEYWORDS = frozenset(
    {
        *("as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum"),
        *("extern", "false", "fn", "for", "gen", "if", "impl", "in", "let", "loop", "match"),
        *("mod", "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct"),
        *("super", "trait", "true", "type", "unsafe", "use", "where", "while", "abstract"),
        *("become", "box", "do", "final", "macro", "override", "priv", "try", "typeof"),
        *("unsized", "virtual", "yield"),
    }
)
NOT_RAW = frozenset({"crate", "self", "Self", "super"})
SCALARS = {
    "string": "String",
    "number": "f64",
    "decimal": "Decimal",
    "boolean": "bool",
    "datetime": "Timestamp",
    "date": "NaiveDate",
    # Binary values only appear in parameters: multipart file parts.
    "binary": "File",
    "any": "Value",
}
# Paths of the non-prelude types that generated signatures may mention.
TYPE_PATHS = {
    "HashMap": "std::collections::HashMap",
    "NaiveDate": "chrono::NaiveDate",
    "Value": "serde_json::Value",
    "Bytes": "crate::Bytes",
    "Decimal": "crate::Decimal",
    "File": "crate::File",
    "Timestamp": "crate::Timestamp",
    "Page": "crate::Page",
    "Multipart": "crate::Multipart",
}
# Models named like these would shadow types the generated modules use.
RESERVED_TYPES = frozenset(
    {*TYPE_PATHS, "Box", "Client", "Envelope", "EventStream", "Method", "Option", "PageParam"}
    | {"Paginator", "Result", "Service", "String", "Vec"}
)
# Methods of the async and blocking clients, next to which top-level accessors are generated.
CLIENT_METHODS = frozenset(
    {
        "as_async",
        "base_url",
        "block_on",
        "builder",
        "clear_mfa_token",
        "confirm_operation",
        "environment",
        "login",
        "mfa_token",
        "request",
        "set_mfa_token",
        "with_options",
    }
)
COPY_TYPES = frozenset({"i32", "i64", "u32", "f64", "bool", "Decimal", "Timestamp", "NaiveDate"})
PAGE_DOC = "Page to start from, 0-based; the following pages are fetched as items are consumed."
DOC_WIDTH = 88
JSON = "application/json"
TEXT = "text/plain"


def emit(ir: IR) -> dict[str, str]:
    view = View(ir)
    env = _util.environment("rust")
    return {
        f"{TARGET}/{name}.rs": _util.pipe(
            ["rustfmt", "--edition", "2024"], env.get_template(f"{name}.rs.j2").render(view=view)
        )
        for name in FILES
    }


# Naming


def ident(name: str) -> str:
    """Rust identifier for a snake_case name."""
    name = name or "value"
    if name[0].isdigit():
        name = f"n{name}"
    if name in NOT_RAW:
        return f"{name}_"
    return f"r#{name}" if name in KEYWORDS else name


def type_name(name: str) -> str:
    return f"{name}Model" if name in RESERVED_TYPES else name


def resource_type(path: tuple[str, ...]) -> str:
    return type_name("".join(naming.pascal(segment) for segment in path))


def accessor(path: tuple[str, ...]) -> str:
    """Accessor of the resource at ``path``; top-level ones must not shadow ``Client`` methods."""
    name = ident(path[-1])
    return f"{name}_resource" if len(path) == 1 and name in CLIENT_METHODS else name


def accessor_chain(path: tuple[str, ...]) -> str:
    return "".join(f".{accessor(path[: depth + 1])}()" for depth in range(len(path)))


def serde_camel(rust_ident: str) -> str:
    """Field name produced by serde's ``rename_all = "camelCase"``."""
    head, *rest = rust_ident.removeprefix("r#").split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def literal(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    for char, escape in (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        value = value.replace(char, escape)
    return f'"{value}"'


def default_literal(value: object) -> str | None:
    """Rust literal of a scalar spec default, or None for no or a structured default."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if "." in repr(value) or "e" in repr(value) else f"{value}.0"
    if isinstance(value, str):
        return literal(value)
    return None


# Documentation

_LIST_ITEM = re.compile(r"(?:[-*+]|\d{1,9}[.)]) +")
_BLOCK_START = re.compile(r"(?:[-*+]|\d{1,9}[.)])(?: |$)|#|>")
_URL = re.compile(r"\bhttps?://[^\s<>\\]*[^\s<>\\.,;:)'\"]")


def doc_lines(*parts: str | None) -> list[str]:
    """Spec text as rustdoc lines that cannot turn into links, HTML or doctests.

    Paragraphs and list items are joined into single lines for [`comment`] to wrap.
    """
    lines: list[str] = []
    for line in _util.docstring_lines(*parts):
        text = re.sub(r"([\[\]<>])", r"\\\1", line.strip().replace("```", "'''"))
        text = _URL.sub(lambda match: f"<{match.group(0)}>", text)
        previous = lines[-1] if lines else ""
        if text and previous and not _BLOCK_START.match(text) and "|" not in (text[0], previous[0]):
            lines[-1] = f"{previous} {text}"
        else:
            lines.append(text)
    return lines


def comment(*blocks: list[str]) -> list[str]:
    """``///`` lines of the non-empty blocks, separated by blank doc lines and wrapped."""
    lines: list[str] = []
    for block in filter(None, blocks):
        lines.extend(["///"] if lines else [])
        for line in block:
            item = _LIST_ITEM.match(line)
            wrapped: list[str] = []
            for part in textwrap.wrap(
                line,
                DOC_WIDTH,
                subsequent_indent=" " * (item.end() if item else 0),
                break_long_words=False,
                break_on_hyphens=False,
            ):
                if wrapped and _BLOCK_START.match(part.lstrip()):
                    # Wrapping must not start a list, heading or quote: keep the line longer.
                    wrapped[-1] += " " + part.lstrip()
                else:
                    wrapped.append(part)
            lines.extend(f"/// {part}" for part in wrapped) if wrapped else lines.append("///")
    return lines


# Views rendered by the templates


@dataclass
class FieldView:
    ident: str
    type: str
    docs: list[str]
    attrs: list[str]


@dataclass
class StructView:
    name: str
    docs: list[str]
    derives: list[str]
    attrs: list[str]
    fields: list[FieldView]


@dataclass
class VariantView:
    ident: str
    literal: str
    docs: list[str]


@dataclass
class EnumView:
    name: str
    docs: list[str]
    macro: str
    variants: list[VariantView]
    fallback: str


@dataclass
class AccessorView:
    name: str
    type: str
    docs: list[str]


@dataclass
class MethodView:
    name: str
    docs: list[str]
    blocking_docs: list[str]
    attrs: list[str]
    args: str
    call_args: str
    returns: str
    is_async: bool
    blocking_stream: str | None
    request: str
    chain: list[str]
    tail: list[str]


@dataclass
class ResourceView:
    type: str
    docs: list[str]
    chain: str
    children: list[AccessorView]
    methods: list[MethodView]


@dataclass
class ServiceView:
    variant: str
    key: str
    docs: list[str]
    urls: list[tuple[str, str]]
    default_url: str


@dataclass
class EnvironmentView:
    variant: str
    docs: list[str]
    default: bool


class Imports:
    """``use`` declarations, grouped std / external / crate like handwritten code."""

    def __init__(self, *paths: str, module: str = "") -> None:
        self.paths = set(paths)
        self.module = module

    def add(self, *paths: str) -> None:
        self.paths.update(paths)

    def type(self, ref: TypeRef | None, rust: str) -> None:
        """Imports what the Rust type ``rust`` of ``ref`` mentions."""
        for part in ref.walk() if ref else []:
            if part.kind in ("model", "enum") and part.name:
                self.add(f"crate::models::{type_name(part.name)}")
        self.add(*(path for name, path in TYPE_PATHS.items() if re.search(rf"\b{name}\b", rust)))

    def lines(self) -> list[str]:
        groups: dict[str, dict[str, list[str]]] = {"std": {}, "external": {}, "crate": {}}
        for path in sorted(self.paths):
            module, _, name = path.rpartition("::")
            if module == self.module:
                continue
            group = "crate" if path.startswith("crate::") else path.split("::")[0]
            groups.get(group, groups["external"]).setdefault(module, []).append(name)
        lines: list[str] = []
        for modules in groups.values():
            if modules and lines:
                lines.append("")
            for module, names in modules.items():
                imported = names[0] if len(names) == 1 else "{" + ", ".join(names) + "}"
                lines.append(f"use {module}::{imported};")
        return lines


class View:
    """Everything the templates render, computed once from the IR."""

    def __init__(self, ir: IR) -> None:
        self.ir = ir
        self.header = f"// {_util.HEADER}"
        self.environments = [
            self._environment(index, env) for index, env in enumerate(ir.environments)
        ]
        self.services = [self._service(key) for key in ir.services]
        self.oauth_service = naming.pascal(ir.oauth.service)
        self.oauth_token_path = literal(ir.oauth.token_path)
        self.oauth_basic = literal(ir.oauth.basic)
        self.oauth_mfa_status_path = literal(ir.oauth.mfa_status_path)
        self.oauth_captcha_path = literal(ir.oauth.captcha_path)
        self.operation_mfa_service = naming.pascal(ir.operation_mfa.service)
        self.operation_mfa_token_path = literal(ir.operation_mfa.token_path)

        self.inputs = self._closure(self._input_types())
        reachable = self.inputs | self._closure(
            [op.response.type for op in ir.operations if op.response.type]
        )
        self.model_imports = Imports(
            "serde::Deserialize", "serde::Serialize", module="crate::models"
        )
        self.structs = [self._model(m) for m in ir.models.values() if m.name in reachable]
        self.enums = [self._enum(e) for e in ir.enums.values() if e.name in reachable]
        self.model_imports.add(*(f"crate::enums::{enum.macro}" for enum in self.enums))
        if not self.structs:
            self.model_imports.paths -= {"serde::Deserialize", "serde::Serialize"}

        # Resource structs are imported next to models, so they avoid model names.
        self.model_names = {type_name(name) for name in (*ir.models, *ir.enums)}
        self.param_imports = Imports()
        self.params = {op.id: params for op in ir.operations if (params := self._params(op))}

        self.resource_imports = Imports(
            "crate::Client", "crate::Method", "crate::Result", "crate::Service"
        )
        self.blocking_imports = Imports("crate::Result", "crate::blocking::Client")
        self.accessors = [self._accessor(child) for _, child in sorted(ir.root.children.items())]
        self.resources = [self._resource(resource) for resource in ir.root.walk()[1:]]

    # Environments and services

    def _environment(self, index: int, env: str) -> EnvironmentView:
        default = self.ir.environments[0]
        fallback = (
            f" Services without a `{env}` deployment use the `{default}` one." if index else ""
        )
        return EnvironmentView(
            naming.pascal(env), comment([f"The `{env}` environment.{fallback}"]), index == 0
        )

    def _service(self, key: str) -> ServiceView:
        service = self.ir.services[key]
        default = service.urls.get(self.ir.environments[0]) or next(iter(service.urls.values()))
        overrides = [
            (naming.pascal(env), literal(url))
            for env, url in service.urls.items()
            if url != default
        ]
        return ServiceView(
            naming.pascal(key),
            literal(key),
            comment(doc_lines(f"{service.title}.")),
            overrides,
            literal(default),
        )

    # Types

    def _type(self, ref: TypeRef) -> str:
        if ref.kind in SCALARS:
            return SCALARS[ref.kind]
        if ref.kind == "integer":
            return "i32" if ref.format == "int32" else "i64"
        if ref.item and ref.kind in ("array", "map", "page"):
            item = self._type(ref.item)
            return {
                "array": f"Vec<{item}>",
                "map": f"HashMap<String, {item}>",
                "page": f"Page<{item}>",
            }[ref.kind]
        if ref.kind in ("model", "enum") and ref.name:
            return type_name(ref.name)
        return "Value"

    def _input_types(self) -> list[TypeRef]:
        types: list[TypeRef] = []
        for op in self.ir.operations:
            types.extend(param.type for param in op.params)
            if op.body and op.body.flattened:
                types.extend(f.type for f in op.body.fields)
            elif op.body:
                types.append(op.body.type)
        return types

    def _closure(self, roots: Iterable[TypeRef]) -> set[str]:
        """Names of the models and enums reachable from ``roots``."""
        seen: set[str] = set()
        pending = [part for ref in roots for part in ref.walk()]
        while pending:
            ref = pending.pop()
            if ref.kind in ("model", "enum") and ref.name and ref.name not in seen:
                seen.add(ref.name)
                if ref.kind == "model":
                    pending.extend(
                        p for f in self.ir.models[ref.name].fields for p in f.type.walk()
                    )
        return seen

    def _cyclic(self, owner: str, ref: TypeRef) -> bool:
        """True if a field of type ``ref`` in ``owner`` needs a ``Box`` to have a finite size."""
        pending, seen = [ref.name] if ref.kind == "model" else [], set()
        while pending:
            name = pending.pop()
            if name == owner:
                return True
            if name and name not in seen:
                seen.add(name)
                pending.extend(
                    f.type.name for f in self.ir.models[name].fields if f.type.kind == "model"
                )
        return False

    # Structs

    def _model(self, model: Model) -> StructView:
        fields = []
        for f in model.fields:
            rust = self._type(f.type)
            self.model_imports.type(f.type, rust)
            if self._cyclic(model.name, f.type):
                rust = f"Box<{rust}>"
            fields.append((f, rust))
        return self._struct(
            name=type_name(model.name),
            docs=doc_lines(model.description),
            fields=fields,
            derives=["Serialize", "Deserialize"],
            builder=model.name in self.inputs,
        )

    def _struct(
        self,
        *,
        name: str,
        docs: list[str],
        fields: list[tuple[Field, str]],
        derives: list[str],
        builder: bool,
        skipped: frozenset[str] = frozenset(),
    ) -> StructView:
        serialized = bool(derives)
        renamed = [(ident(f.name), f.wire) for f, _ in fields if f.name not in skipped]
        rename_all = serialized and any(
            i.removeprefix("r#") != w and serde_camel(i) == w for i, w in renamed
        )
        views = []
        for f, rust in fields:
            rust_ident = ident(f.name)
            optional = not f.required or f.nullable
            serde = []
            if f.name in skipped:
                serde.append("skip")
            elif serialized:
                if f.wire != (
                    serde_camel(rust_ident) if rename_all else rust_ident.removeprefix("r#")
                ):
                    serde.append(f"rename = {literal(f.wire)}")
                if not f.required:
                    serde.append('skip_serializing_if = "Option::is_none"')
            attrs = ["#[deprecated]"] if f.deprecated else []
            attrs += [f"#[serde({', '.join(serde)})]"] if serde else []
            field_docs = comment(
                doc_lines(f.description),
                ["Inferred from how the web application uses the response; may be absent."]
                if f.inferred
                else [],
                [f"Defaults to `{json.dumps(f.default)}`."] if f.default is not None else [],
            )
            views.append(
                FieldView(rust_ident, f"Option<{rust}>" if optional else rust, field_docs, attrs)
            )

        all_derives = ["Debug", "Clone"]
        all_derives += ["Default"] if all(view.type.startswith("Option<") for view in views) else []
        all_derives += ["PartialEq", *derives]
        attrs = []
        if builder:
            all_derives.append("bon::Builder")
            converted = [
                rust
                for rust in ("String", "File")
                if any(view.type in (rust, f"Option<{rust}>") for view in views)
            ]
            if converted:
                attrs.append(f"#[builder({', '.join(f'on({rust}, into)' for rust in converted)})]")
        if rename_all:
            attrs.append('#[serde(rename_all = "camelCase")]')
        return StructView(name, comment(docs), all_derives, attrs, views)

    def _enum(self, enum: Enum) -> EnumView:
        used: set[str] = set()
        variants = []
        for value in enum.values:
            name = naming.pascal(value.name) or "Empty"
            name = f"V{name}" if name[0].isdigit() else name
            name = f"{name}_" if name == "Self" else name
            base, counter = name, 2
            while name in used:
                name, counter = f"{base}{counter}", counter + 1
            used.add(name)
            variants.append(
                VariantView(name, literal(value.wire), comment(doc_lines(value.description)))
            )
        return EnumView(
            name=type_name(enum.name),
            docs=comment(doc_lines(enum.description)),
            macro="integer_enum" if enum.base == "integer" else "string_enum",
            variants=variants,
            fallback="Unrecognized" if "Unknown" in used else "Unknown",
        )

    # Parameters

    @staticmethod
    def _page_names(op: Operation, location: str) -> tuple[str, ...]:
        pagination = op.pagination
        if pagination and pagination.location == location:
            return pagination.page_param, pagination.size_param
        return ()

    def _param_field_type(self, op: Operation, f: Field, location: str) -> str:
        if f.type.kind == "integer" and f.wire in self._page_names(op, location):
            return "u32"
        return self._type(f.type)

    def _params(self, op: Operation) -> StructView | None:
        query = [
            (
                Field(
                    p.name, p.wire, p.type, p.required, description=p.description, default=p.default
                ),
                "query",
            )
            for p in [*op.query_params, *op.header_params]
        ]
        body = op.body
        flattened = [(f, "body") for f in body.fields] if body and body.flattened else []
        if not query and not flattened:
            return None
        fields = []
        for f, location in [*query, *flattened]:
            rust = self._param_field_type(op, f, location)
            self.param_imports.type(f.type, rust)
            if (
                op.pagination
                and location == op.pagination.location
                and f.wire == op.pagination.page_param
            ):
                f = replace(f, description=PAGE_DOC, default=None)
            fields.append((f, rust))
        serialized = bool(flattened) and body is not None and body.kind in ("json", "form")
        if serialized:
            self.param_imports.add("serde::Serialize")
        method = f"{self._resource_type(op.resource)}::{ident(op.name)}"
        return self._struct(
            name=self._params_name(op),
            docs=[f"Parameters of [`{method}`](crate::resources::{method})."],
            fields=fields,
            derives=["Serialize"] if serialized else [],
            builder=True,
            skipped=frozenset(f.name for f, _ in query) if serialized else frozenset(),
        )

    @staticmethod
    def _params_name(op: Operation) -> str:
        return f"{naming.pascal(op.id)}Params"

    # Resources

    def _resource_type(self, path: tuple[str, ...]) -> str:
        name = resource_type(path)
        return f"{name}Resource" if name in self.model_names else name

    def _accessor(self, resource: Resource) -> AccessorView:
        return AccessorView(
            accessor(resource.path),
            self._resource_type(resource.path),
            comment([f"Endpoints of `client{accessor_chain(resource.path)}`."]),
        )

    def _resource(self, resource: Resource) -> ResourceView:
        children = [self._accessor(child) for _, child in sorted(resource.children.items())]
        clashes = {ident(op.name) for op in resource.operations} & {
            child.name for child in children
        }
        if clashes:
            path = ".".join(resource.path)
            raise ValueError(
                f"{path}: {', '.join(sorted(clashes))} is both an operation and a resource"
            )
        return ResourceView(
            type=self._resource_type(resource.path),
            docs=comment([f"Endpoints of `client{accessor_chain(resource.path)}`."]),
            chain=accessor_chain(resource.path),
            children=children,
            methods=[self._method(op) for op in resource.operations],
        )

    def _use(self, ref: TypeRef | None, rust: str) -> None:
        self.resource_imports.type(ref, rust)
        self.blocking_imports.type(ref, rust)

    def _arguments(self, op: Operation) -> list[tuple[str, str]]:
        args = []
        for param in op.path_params:
            scalar = param.type.kind in ("integer", "number", "decimal", "boolean", "enum")
            rust = self._type(param.type) if scalar else "&str"
            self._use(param.type if scalar else None, rust)
            args.append((ident(param.name), rust))
        body = op.body
        if body and not body.flattened:
            rust = "Multipart" if body.kind == "multipart" else self._type(body.type)
            self._use(body.type if body.kind != "multipart" else None, rust)
            args.append(("body", rust if body.required else f"Option<{rust}>"))
        if op.id in self.params:
            name = self._params_name(op)
            self.resource_imports.add(f"crate::params::{name}")
            self.blocking_imports.add(f"crate::params::{name}")
            args.append(("params", name))
        return args

    def _method(self, op: Operation) -> MethodView:
        args = self._arguments(op)
        response = op.response
        if op.pagination:
            item = self._type(op.pagination.item)
            self._use(op.pagination.item, item)
            self.resource_imports.add("crate::Paginator", "crate::PageParam")
            self.blocking_imports.add("crate::blocking::Paginator")
            returns = f"Paginator<{item}>"
            tail = [self._paginate(op.pagination)]
            blocking_stream = "Paginator"
        elif response.kind == "sse":
            payload = self._type(response.type) if response.type else "Value"
            self._use(response.type, payload)
            self.resource_imports.add("crate::sse::EventStream")
            self.blocking_imports.add("crate::blocking::EventStream")
            returns = f"EventStream<{payload}>"
            tail = [".events()"]
            blocking_stream = "EventStream"
        else:
            if response.kind == "json":
                payload = self._type(response.type) if response.type else "Value"
                self._use(response.type, payload)
            else:
                payload = {"binary": "Bytes", "text": "String", "empty": "()"}[response.kind]
                self._use(None, payload)
            returns = f"Result<{payload}>"
            # Payloads without a fixed shape are taken as text when they are not JSON.
            lenient = response.kind == "json" and payload in ("Value", "String")
            read = {
                "json": ".json_or_text()" if lenient else ".json()",
                "binary": ".bytes()",
                "text": ".text()",
            }.get(response.kind)
            tail = (
                [".send()", ".await?", read, ".await"]
                if read
                else [".send()", ".await?;", "Ok(())"]
            )
            blocking_stream = None

        return MethodView(
            name=ident(op.name),
            docs=self._method_docs(op, blocking=False),
            blocking_docs=self._method_docs(op, blocking=True),
            attrs=["#[deprecated]"] if op.deprecated else [],
            args="".join(f", {name}: {rust}" for name, rust in args),
            call_args=", ".join(name for name, _ in args),
            returns=returns,
            is_async=blocking_stream is None,
            blocking_stream=blocking_stream,
            request=self._request_call(op),
            chain=self._request_chain(op),
            tail=tail,
        )

    @staticmethod
    def _method_docs(op: Operation, *, blocking: bool) -> list[str]:
        notes = []
        if op.status == "experimental":
            notes.append("**Experimental:** the endpoint may change without notice.")
        if op.auth == "mfa" and op.mfa_operation:
            notes.append(
                f"Needs an operation MFA token for `{op.mfa_operation}`, which the client "
                "obtains with [`Client::confirm_operation`](crate::Client::confirm_operation) "
                "when a one-time code provider is configured."
            )
        elif op.auth == "mfa":
            notes.append(
                "Needs an operation MFA token; see "
                "[`Client::set_mfa_token`](crate::Client::set_mfa_token)."
            )
        if op.auth == "none":
            notes.append("Public: works without signing in.")
        kind = "iterator" if blocking else "stream"
        if op.pagination:
            errors = [
                f"Returns a lazy {kind} over the items of every page; it yields errors as items."
            ]
        elif op.response.kind == "sse":
            article = "an" if blocking else "a"
            errors = [
                f"Returns {article} {kind} of events that reconnects automatically; "
                "it yields errors as items."
            ]
        else:
            decoded = " or the response cannot be decoded" if op.response.kind == "json" else ""
            errors = ["# Errors", "", f"Returns an error if the request fails{decoded}."]
        return comment(
            doc_lines(op.summary, op.description),
            [f"`{op.method} {op.path}`"],
            [line for note in notes for line in ("", note)][1:],
            errors,
        )

    def _request_call(self, op: Operation) -> str:
        """``request(..)`` call starting the chain; path parameters are encoded and checked."""
        target = f"Service::{naming.pascal(op.service)}, Method::{op.method}"
        if not op.path_params:
            return f"request({target}, {literal(op.path)})"
        template = re.sub(r"{[^}]+}", "{}", op.path)
        if re.sub(r"{}", "", template).count("{") or "}" in re.sub(r"{}", "", template):
            raise ValueError(f"{op.id}: unexpected brace in path {op.path}")
        params = ", ".join(f"&{ident(p.name)}" for p in op.path_params)
        return f"request_path({target}, {literal(template)}, &[{params}])"

    def _request_chain(self, op: Operation) -> list[str]:
        chain = [".public()"] if op.auth == "none" else []
        if op.envelope == "retcode":
            chain.append(".envelope(Envelope::Retcode)")
            self.resource_imports.add("crate::Envelope")
        params = [*op.query_params, *op.header_params]
        for param in params:
            field = Field(param.name, param.wire, param.type)
            method = "query" if param.location == "query" else "header"
            chain.append(f".{method}({literal(param.wire)}, {self._argument(op, field, 'query')})")
        # Like the web app, values the spec gives a default for are always sent.
        for param in params:
            if (default := default_literal(param.default)) is not None:
                method = "default_query" if param.location == "query" else "default_header"
                chain.append(f".{method}({literal(param.wire)}, {default})")
        if op.auth == "mfa" and op.mfa_operation:
            chain.append(f".mfa_operation({literal(op.mfa_operation)})")
        body = op.body
        if body and body.flattened and body.kind == "multipart":
            self.resource_imports.add("crate::Multipart")
            parts = "".join(self._part(op, f) for f in body.fields)
            chain.append(f".multipart(Multipart::new(){parts})")
        elif body and body.flattened and not body.fields:
            self.resource_imports.add("serde_json::Map")
            chain.append(f".{body.kind}(Map::new())")
        elif body:
            chain.append(f".{body.kind}({'params' if body.flattened else 'body'})")
        for f in self._body_fields(op):
            if (default := default_literal(f.default)) is not None:
                chain.append(f".default_field({literal(f.wire)}, {default})")
        if op.session == "end":
            chain.append(".ends_session()")
        elif op.session == "replace":
            chain.append(".replaces_session()")
        return chain

    def _body_fields(self, op: Operation) -> list[Field]:
        """Top-level fields of the request body."""
        body = op.body
        if body is None:
            return []
        if body.flattened:
            return body.fields
        if body.type.kind == "model" and body.type.name and body.kind in ("json", "form"):
            return self.ir.models[body.type.name].fields
        return []

    def _argument(self, op: Operation, f: Field, location: str) -> str:
        """``params.<field>``, borrowed unless the field is ``Copy``."""
        copy = self._param_field_type(op, f, location) in COPY_TYPES or (
            f.type.kind == "enum"
            and f.type.name is not None
            and self.ir.enums[f.type.name].base == "integer"
        )
        return f"{'' if copy else '&'}params.{ident(f.name)}"

    def _part(self, op: Operation, f: Field) -> str:
        name, value = literal(f.wire), f"params.{ident(f.name)}"
        item = f.type.item if f.type.kind == "array" and f.type.item else f.type
        if f.type.kind == "binary":
            # An optional file is an Option, which `files` takes as zero or one file.
            return f".{'file' if f.required else 'files'}({name}, {value})"
        if item.kind == "binary":
            files = value if f.required else f"{value}.into_iter().flatten()"
            return f".files({name}, {files})"
        argument = self._argument(op, f, "body")
        if item.kind in ("model", "map", "any"):
            if f.content_type == TEXT:
                # Sent like the web app's `JSON.stringify` part: plain text without a type.
                return f".json_text({name}, {argument})"
            part = f".json({name}, {argument})"
            if f.content_type and f.content_type != JSON:
                part += f".content_type({name}, {literal(f.content_type)})"
            return part
        part = f".text({name}, {argument})"
        if f.content_type and f.content_type != TEXT:
            part += f".content_type({name}, {literal(f.content_type)})"
        return part

    @staticmethod
    def _paginate(pagination: Pagination) -> str:
        location = "Query" if pagination.location == "query" else "Body"
        return f".paginate(PageParam::{location}({literal(pagination.page_param)}))"
