"""Emits the generated layer of the Node.js SDK: models, resource classes and service URLs."""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import jinja2

from .. import naming
from ..ir import IR, Body, Enum, Field, Model, Operation, Param, Resource, Service, TypeRef
from ._util import docstring_lines, environment, pipe

PACKAGE = Path(__file__).resolve().parents[5] / "node"
BIOME = PACKAGE / "node_modules" / ".bin" / "biome"
GENERATED = "node/src/generated"
OWNED = [f"{GENERATED}/*.ts", f"{GENERATED}/**/*.ts"]

WIDTH = 100
IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
# Words that cannot name a parameter or a local variable in strict mode, plus the names of the
# locals in generated methods.
RESERVED = frozenset(
    {
        "arguments",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "eval",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "let",
        "new",
        "null",
        "package",
        "private",
        "protected",
        "public",
        "return",
        "static",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "undefined",
        "var",
        "void",
        "while",
        "with",
        "yield",
        "body",
        "headers",
        "options",
        "params",
        "query",
    }
)
# Members that every resource class inherits.
RESERVED_MEMBERS = frozenset({"constructor", "_client"})
# Names the handwritten core exports from the package root.
CORE_EXPORTS = frozenset(
    {
        "APIError",
        "APIErrorDetails",
        "APIResource",
        "ApiRequest",
        "AuthenticationError",
        "BadRequestError",
        "CodeProvider",
        "ConflictError",
        "MFARequiredError",
        "MemoryTokenStore",
        "NotFoundError",
        "Page",
        "Paginator",
        "PermissionDeniedError",
        "RateLimitError",
        "RawRequest",
        "RequestOptions",
        "Resources",
        "ServerError",
        "ServerSentEvent",
        "StreamOptions",
        "TokenStore",
        "Tokens",
        "TransportError",
        "VERSION",
        "VerificationRequiredError",
        "Whitebird",
        "WhitebirdError",
        "WhitebirdOptions",
    }
)
SCALARS = {
    "string": "string",
    "datetime": "string",
    "date": "string",
    "integer": "number",
    "number": "number",
    "decimal": "number",
    "boolean": "boolean",
    "binary": "Blob",
    "any": "unknown",
}
RESPONSE_TYPES = {"text": "string", "binary": "Uint8Array", "empty": "void"}


def emit(ir: IR) -> dict[str, str]:
    env = environment("node", doc=_doc_block, quote=_quote, key=_key)
    tops = [_ResourceView.of(ir.root.children[name]) for name in sorted(ir.root.children)]
    _check_names(ir, tops)
    files = {
        "index.ts": env.get_template("index.ts.j2").render(resources=tops),
        "models.ts": _models(env, ir),
        "services.ts": env.get_template("services.ts.j2").render(
            environments=ir.environments,
            services=[_service_view(service, ir.environments) for service in ir.services.values()],
            oauth=ir.oauth,
            operation_mfa=ir.operation_mfa,
        ),
    }
    for top in tops:
        files[f"resources/{top.module}.ts"] = _resource_module(env, ir, top)
    return {
        f"{GENERATED}/{path}": _format(f"src/generated/{path}", content)
        for path, content in sorted(files.items())
    }


def _models(env: jinja2.Environment, ir: IR) -> str:
    uses = _Uses()
    for model in ir.models.values():
        for item in model.fields:
            uses.add(item.type)
    return env.get_template("models.ts.j2").render(
        enums=[_enum_view(enum) for enum in ir.enums.values()],
        models=[_model_view(model) for model in ir.models.values()],
        uses=uses,
    )


def _resource_module(env: jinja2.Environment, ir: IR, top: _ResourceView) -> str:
    resources = top.walk()
    methods = [method for resource in resources for method in resource.methods]
    uses = _Uses()
    for method in methods:
        uses.merge(method.uses)
    pagination = [
        name for name, used in (("Page", uses.page), ("Paginator", uses.paginator)) if used
    ]
    sse = ["ServerSentEvent", "StreamOptions"] if uses.stream else []
    imports = [
        ("../../http.js", ["RequestOptions"] if uses.options else []),
        ("../../pagination.js", pagination),
        ("../../sse.js", sse),
        ("../models.js", sorted(uses.types & (ir.models.keys() | ir.enums.keys()))),
    ]
    return env.get_template("resource.ts.j2").render(
        imports=[
            "import { APIResource } from '../../resource.js';",
            *(
                f"import type {{ {', '.join(names)} }} from '{module}';"
                for module, names in imports
                if names
            ),
        ],
        resources=resources,
        params=[method.params for method in methods if method.params],
    )


def _service_view(service: Service, environments: list[str]) -> dict[str, object]:
    """Lists the URL of the service in every environment; missing ones fall back to production."""
    fallback = service.urls.get("production") or next(iter(service.urls.values()))
    return {
        "key": service.key,
        "title": service.title,
        "urls": [
            (environment, service.urls.get(environment, fallback)) for environment in environments
        ],
    }


@dataclass
class _Uses:
    """What a generated module refers to, so it imports exactly that."""

    types: set[str] = field(default_factory=set)
    page: bool = False
    paginator: bool = False
    stream: bool = False
    options: bool = False

    def add(self, ref: TypeRef | None) -> None:
        for part in ref.walk() if ref else []:
            if part.name:
                self.types.add(part.name)
            self.page |= part.kind == "page"

    def merge(self, other: _Uses) -> None:
        self.types |= other.types
        self.page |= other.page
        self.paginator |= other.paginator
        self.stream |= other.stream
        self.options |= other.options


@dataclass
class _Member:
    key: str
    type: str
    optional: bool
    doc: list[str]


@dataclass
class _ParamsView:
    name: str
    extends: str | None
    members: list[_Member]
    doc: list[str]


@dataclass
class _Groups:
    """Members of the params object, by where they are sent."""

    query: list[Param]
    headers: list[Param]
    body: Body | None

    @property
    def body_fields(self) -> list[Field]:
        return self.body.fields if self.body and self.body.flattened else []

    @property
    def raw_body(self) -> bool:
        """True when the body is passed whole as `params.body`."""
        return self.body is not None and not self.body.flattened

    @property
    def empty_body(self) -> bool:
        """True when the body is an object without fields, sent as `{}`."""
        return self.body is not None and self.body.flattened and not self.body.fields

    @property
    def rest(self) -> str | None:
        """The location that receives the members not picked out by name."""
        if self.body_fields:
            return "body"
        if self.query:
            return "query"
        return "headers" if self.headers else None

    @property
    def required(self) -> bool:
        body = self.body
        if any(p.required for p in [*self.query, *self.headers]):
            return True
        return bool(
            body and body.required and (self.raw_body or any(f.required for f in body.fields))
        )


@dataclass
class _MethodView:
    op: Operation
    name: str
    args: list[str]
    returns: str
    prelude: str | None
    call: str
    request: list[str]
    doc: list[str]
    params: _ParamsView | None
    uses: _Uses


@dataclass
class _ResourceView:
    resource: Resource
    cls: str
    prop: str
    children: list[_ResourceView]
    methods: list[_MethodView]

    @classmethod
    def of(cls, resource: Resource) -> _ResourceView:
        children = [cls.of(resource.children[name]) for name in sorted(resource.children)]
        methods = [_method(op) for op in resource.operations]
        names = [child.prop for child in children] + [method.name for method in methods]
        for duplicate in sorted({name for name in names if names.count(name) > 1}):
            where = ".".join(resource.path)
            raise ValueError(f"{where}: {duplicate} names both a resource and a method")
        return cls(
            resource=resource,
            cls=_class_name(resource.path),
            prop=_member(naming.camel(resource.name)),
            children=children,
            methods=methods,
        )

    @property
    def module(self) -> str:
        return "-".join(naming.words(self.resource.name))

    @property
    def doc(self) -> list[str]:
        path = ".".join(_member(naming.camel(segment)) for segment in self.resource.path)
        return [f"Endpoints under `client.{path}`."]

    def walk(self) -> list[_ResourceView]:
        return [self, *(view for child in self.children for view in child.walk())]


def _method(op: Operation) -> _MethodView:
    path_args = [_local(naming.camel(p.name)) for p in op.path_params]
    args = [f"{name}: {_ts(p.type)}" for name, p in zip(path_args, op.path_params, strict=True)]
    groups = _Groups(op.query_params, op.header_params, op.body)
    params = None
    if groups.query or groups.headers or groups.body_fields or groups.raw_body:
        params = _params_view(op, groups)
        args.append(_params_arg(params, groups))
    args.append(f"options?: {'StreamOptions' if op.response.kind == 'sse' else 'RequestOptions'}")
    prelude, request = _request(op, groups)
    if op.pagination and op.pagination.location == "body":
        request.append("pageIn: 'body'")
    call, returns = _returns(op)
    return _MethodView(
        op=op,
        name=_member(naming.camel(op.name)),
        args=args,
        returns=returns,
        prelude=prelude,
        call=call,
        request=request,
        doc=_method_doc(op, path_args),
        params=params,
        uses=_method_uses(op),
    )


def _returns(op: Operation) -> tuple[str, str]:
    """Returns the core method that performs the call, and the return type."""
    if op.pagination:
        return "paginate", f"Paginator<{_ts(op.pagination.item)}>"
    payload = _ts(op.response.type) if op.response.type else "unknown"
    if op.response.kind == "sse":
        return "stream", f"AsyncIterable<ServerSentEvent<{payload}>>"
    return "call", f"Promise<{RESPONSE_TYPES.get(op.response.kind, payload)}>"


def _method_uses(op: Operation) -> _Uses:
    sse = op.response.kind == "sse"
    uses = _Uses(paginator=op.pagination is not None, stream=sse, options=not sse)
    result = op.pagination.item if op.pagination else op.response.type
    body = op.body.type if op.body and (op.body.fields or not op.body.flattened) else None
    for ref in [*(p.type for p in op.params), body, result]:
        uses.add(ref)
    return uses


def _params_arg(params: _ParamsView | None, groups: _Groups) -> str:
    if params:
        return f"params: {params.name}" + ("" if groups.required else " = {}")
    # The body model itself is the params type. The body is sent even when no field is set.
    assert groups.body
    model = _ts(groups.body.type)
    if groups.required:
        return f"params: {model}"
    if any(f.required for f in groups.body.fields):
        model = f"Partial<{model}>"
    return f"params: {model} = {{}}"


def _params_view(op: Operation, groups: _Groups) -> _ParamsView | None:
    """Declares the params interface, or returns None when the body model serves as one."""
    body = groups.body
    if groups.body_fields and not (groups.query or groups.headers):
        return None
    extends = None
    if body and groups.body_fields:
        extends = _ts(body.type)
        if not body.required and any(f.required for f in body.fields):
            extends = f"Partial<{extends}>"
    members = [
        _Member(_key(p.wire), _ts(p.type), not p.required, _text(p.description))
        for p in [*groups.query, *groups.headers]
    ]
    if body and groups.raw_body:
        members.append(_Member("body", _ts(body.type), not body.required, ["Request body."]))
    return _ParamsView(
        name=naming.pascal(op.id) + "Params",
        extends=extends,
        members=members,
        doc=[
            f"Parameters of {{@link {_class_name(op.resource)}.{_member(naming.camel(op.name))}}}."
        ],
    )


def _request(op: Operation, groups: _Groups) -> tuple[str | None, list[str]]:
    """Returns the statement splitting `params` by location, and the request descriptor."""
    rest = groups.rest
    picked: dict[str, list[tuple[str, str]]] = {
        location: [(p.wire, _local(naming.camel(p.name))) for p in members]
        for location, members in (("query", groups.query), ("headers", groups.headers))
        if location != rest
    }
    if groups.raw_body:
        picked["body"] = [("body", "body")]
    bindings = [_binding(wire, local) for pairs in picked.values() for wire, local in pairs]
    # A lone raw body reads better as `params.body` than as a destructuring.
    prelude = None
    if bindings and (rest or not groups.raw_body or len(bindings) > 1):
        prelude = (
            "const { " + ", ".join([*bindings, *([f"...{rest}"] if rest else [])]) + " } = params;"
        )

    request = [
        f"service: {_quote(op.service)}",
        f"method: {_quote(op.method)}",
        f"path: {_path(op)}",
    ]
    for location in ("query", "headers", "body"):
        if location == rest:
            request.append(location if prelude else f"{location}: params")
        elif location == "body" and groups.raw_body:
            request.append("body" if prelude else "body: params.body")
        elif location == "body" and groups.empty_body:
            request.append("body: {}")
        elif picked.get(location):
            pairs = ", ".join(_binding(wire, local) for wire, local in picked[location])
            request.append(f"{location}: {{ {pairs} }}")
    if op.body and op.body.kind != "json":
        request.append(f"bodyType: {_quote(op.body.kind)}")
    part_types = (
        [(f.wire, f.content_type) for f in op.body.fields if f.content_type] if op.body else []
    )
    if part_types:
        pairs = ", ".join(f"{_key(wire)}: {_quote(media)}" for wire, media in part_types)
        request.append(f"partTypes: {{ {pairs} }}")
    if op.response.kind in RESPONSE_TYPES:
        request.append(f"responseType: {_quote(op.response.kind)}")
    if op.auth == "none":
        request.append("auth: false")
    if op.envelope:
        request.append(f"envelope: {_quote(op.envelope)}")
    if op.mfa_operation:
        request.append(f"mfa: {_quote(op.mfa_operation)}")
    return prelude, request


def _binding(wire: str, local: str) -> str:
    return local if wire == local else f"{_key(wire)}: {local}"


def _path(op: Operation) -> str:
    if not op.path_params:
        return _quote(op.path)
    names = {p.wire: _local(naming.camel(p.name)) for p in op.path_params}
    escaped = op.path.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    substituted = re.sub(
        r"{([^}]+)}", lambda m: f"${{encodeURIComponent({names[m.group(1)]})}}", escaped
    )
    return f"`{substituted}`"


def _method_doc(op: Operation, path_args: list[str]) -> list[str]:
    lines = [_escape(line) for line in docstring_lines(op.summary, op.description)]
    lines += [""] if lines else []
    lines.append(f"`{op.method} {op.path}`")
    if op.auth == "none":
        lines += ["", "Works without logging in."]
    elif op.mfa_operation:
        lines += [
            "",
            f"Needs an operation MFA token for `{op.mfa_operation}`: the client gets one with the "
            "configured `otp`, or call {@link Whitebird.confirmOperation} first.",
        ]
    elif op.auth == "mfa":
        lines += ["", "Needs an operation MFA token: set {@link Whitebird.mfaToken} first."]
    tags = [
        f"@param {name} - {' '.join(_text(p.description))}"
        for name, p in zip(path_args, op.path_params, strict=True)
        if p.description
    ]
    if op.pagination:
        tags.append(
            "@returns A {@link Paginator}: await it for the first page, or iterate it with "
            "`for await` for every item."
        )
    elif op.response.kind == "sse":
        tags.append(
            "@returns Events as they arrive. The stream reconnects on its own and ends when "
            "`options.signal` aborts."
        )
    if op.auth == "mfa":
        tags.append(
            "@throws {@link MFARequiredError} if the operation MFA token is missing and no `otp` "
            "is configured."
        )
    if op.status == "experimental":
        tags.append("@experimental")
    if op.deprecated:
        tags.append("@deprecated")
    return [*lines, *([""] if tags else []), *tags]


def _enum_view(enum: Enum) -> dict[str, object]:
    base = "string" if enum.base == "string" else "number"
    literals = [_quote(v.wire) if isinstance(v.wire, str) else str(v.wire) for v in enum.values]
    keys = [_enum_key(v.name) for v in enum.values]
    for duplicate in sorted({key for key in keys if keys.count(key) > 1}):
        raise ValueError(f"enum {enum.name}: two values map to the key {duplicate}")
    return {
        "name": enum.name,
        "doc": _text(enum.description),
        "union": " | ".join([*literals, f"({base} & {{}})"]),
        "members": [
            {"key": key, "literal": literal, "doc": _text(value.description)}
            for key, literal, value in zip(keys, literals, enum.values, strict=True)
        ],
    }


def _model_view(model: Model) -> dict[str, object]:
    return {
        "name": model.name,
        "doc": _text(model.description),
        "fields": [
            _Member(
                key=_key(item.wire),
                type=_ts(item.type) + (" | null" if item.nullable else ""),
                optional=not item.required,
                doc=_field_doc(item),
            )
            for item in model.fields
        ],
    }


def _field_doc(item: Field) -> list[str]:
    lines = _text(item.description)
    if item.inferred:
        lines.append("Inferred from the web application, not observed in API responses.")
    if item.deprecated:
        lines.append("@deprecated")
    return lines


def _ts(ref: TypeRef) -> str:
    if ref.kind in SCALARS:
        return SCALARS[ref.kind]
    if ref.kind in ("model", "enum"):
        assert ref.name
        return ref.name
    assert ref.item
    item = _ts(ref.item)
    if ref.kind == "array":
        return f"({item})[]" if " " in item else f"{item}[]"
    if ref.kind == "map":
        return f"Record<string, {item}>"
    return f"Page<{item}>"


def _class_name(path: tuple[str, ...]) -> str:
    return "".join(naming.pascal(segment) for segment in path) + "Resource"


def _enum_key(name: str) -> str:
    key = naming.escape(name, frozenset(), digit_prefix="N")
    return key if IDENTIFIER.match(key) else _quote(key)


def _key(wire: str) -> str:
    return wire if IDENTIFIER.match(wire) else _quote(wire)


def _local(name: str) -> str:
    return naming.escape(name or "value", RESERVED, digit_prefix="n")


def _member(name: str) -> str:
    return naming.escape(name, RESERVED_MEMBERS, digit_prefix="n")


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _escape(line: str) -> str:
    """Keeps spec text from ending the comment early or being read as a TSDoc tag."""
    return line.rstrip().replace("*/", "*\\/").replace("@", "\\@")


def _text(text: str | None) -> list[str]:
    return [_escape(line) for line in (text or "").strip().splitlines()]


def _doc_block(lines: list[str], indent: int = 0) -> str:
    if not lines:
        return ""
    pad = " " * indent
    width = WIDTH - indent - 3
    wrapped: list[str] = []
    for line in lines:
        if len(line) <= width or line.startswith("`"):
            wrapped.append(line)
        else:
            continuation = "  " if line.startswith("@") else ""
            wrapped += textwrap.wrap(
                line,
                width,
                subsequent_indent=continuation,
                break_long_words=False,
                break_on_hyphens=False,
            )
    if len(wrapped) == 1 and len(wrapped[0]) <= width - 4:
        return f"{pad}/** {wrapped[0]} */\n"
    body = "".join(f"{pad} *{' ' + line if line else ''}\n" for line in wrapped)
    return f"{pad}/**\n{body}{pad} */\n"


def _check_names(ir: IR, tops: list[_ResourceView]) -> None:
    """Fails on generated exports that would clash with each other or with the core."""
    owners = dict.fromkeys(CORE_EXPORTS, "the handwritten core")

    def claim(name: str, owner: str) -> None:
        if name in owners:
            raise ValueError(f"{owner} and {owners[name]} both export {name}; rename one of them")
        owners[name] = owner

    for name in ("Environment", "ServiceName", "SERVICE_URLS", "OAUTH", "OPERATION_MFA"):
        claim(name, "the services module")
    for name in [*ir.models, *ir.enums]:
        claim(name, f"schema {name}")
    for top in tops:
        for view in top.walk():
            claim(view.cls, f"resource {'.'.join(view.resource.path)}")
            for method in view.methods:
                if method.params:
                    claim(method.params.name, f"operation {method.op.id}")


def _format(path: str, source: str) -> str:
    if not BIOME.exists():
        raise RuntimeError(f"biome is missing: run `npm ci` in {PACKAGE}")
    return pipe([str(BIOME), "check", "--write", f"--stdin-file-path={path}"], source, cwd=PACKAGE)
