"""Python emitter: models, enums and resource classes in ``python/src/whitebird/_generated/``."""

from __future__ import annotations

import keyword
import re
import textwrap
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import jinja2

from .. import naming
from ..ir import IR, Enum, Field, Operation, Param, Resource, TypeRef
from . import _util

PACKAGE = "python/src/whitebird/_generated"
OWNED = [f"{PACKAGE}/*.py", f"{PACKAGE}/**/*.py"]
PROJECT = Path(__file__).resolve().parents[5] / "python"
MODELS = "whitebird._generated.models"
WIDTH = 100
FLAVORS = ("sync", "async")

Role = Literal["field", "param", "return", "cast"]
Entries = Sequence[tuple[str | None, str]]

KEYWORDS = frozenset(keyword.kwlist)
# BaseModel attributes that a field must not shadow.
MODEL_ATTRIBUTES = frozenset(
    {
        "construct",
        "copy",
        "dict",
        "from_orm",
        "json",
        "parse_file",
        "parse_obj",
        "parse_raw",
        "schema",
        "schema_json",
        "update_forward_refs",
        "validate",
    }
)
# Keyword arguments that every method already takes through **options.
OPTIONS = frozenset({"extra_headers", "idempotent", "max_retries", "options", "timeout"})
CLIENT_MEMBERS = frozenset({"aclose", "close", "login", "mfa_token", "request"})
# Names imported by generated modules, which model and enum classes must not take.
HELPERS = frozenset(
    {
        "Any",
        "AsyncEventStream",
        "AsyncPager",
        "AsyncResource",
        "AsyncResources",
        "Date",
        "DateTime",
        "Decimal",
        "EventStream",
        "Field",
        "FileContent",
        "IntEnum",
        "Mapping",
        "Page",
        "RequestOptions",
        "Resources",
        "Sequence",
        "StrEnum",
        "SyncPager",
        "SyncResource",
        "Unpack",
        "WhitebirdModel",
    }
)
BUILTIN_TYPES = frozenset({"bool", "bytes", "dict", "float", "int", "list", "str"})
SCALARS = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}
BODY_LABELS = {"json": "body field", "form": "form field", "multipart": "multipart field"}
REFERENCES = 3
LIST_ITEM = r"\s*([-*+]|\d+\.)\s"
STRUCTURED = rf"{LIST_ITEM}|\s{{4}}|```"  # lines that are not reflowed


def emit(ir: IR) -> dict[str, str]:
    env = _util.environment("python", docstring=render_docstring, literal=repr)
    context = Context(ir)
    tops = [ir.root.children[key] for key in sorted(ir.root.children)]
    files = {
        "__init__.py": env.get_template("package.py.j2").render(),
        "services.py": env.get_template("services.py.j2").render(ir=ir),
        "models.py": _models(context, env),
        "resources/__init__.py": env.get_template("root.py.j2").render(
            resources=[_child(top) for top in tops]
        ),
    }
    for top in tops:
        files[f"resources/{identifier(top.name)}.py"] = _resources(context, top, env)
    return {f"{PACKAGE}/{path}": _format(path, source) for path, source in files.items()}


def _format(path: str, source: str) -> str:
    cwd = PROJECT if PROJECT.is_dir() else None
    stdin = ["--stdin-filename", f"src/whitebird/_generated/{path}", "-"]
    sort_imports = ["ruff", "check", "--select", "I", "--fix", "--exit-zero", "--quiet", *stdin]
    return _util.pipe(["ruff", "format", *stdin], _util.pipe(sort_imports, source, cwd), cwd)


# Names


def identifier(name: str, reserved: frozenset[str] = frozenset()) -> str:
    """Makes a snake_case name a valid identifier that shadows nothing in `reserved`."""
    return naming.escape(name, KEYWORDS | reserved, digit_prefix="n")


def wire_name(name: str) -> str:
    """Mirror of ``whitebird._models.wire_name``: the alias a model derives from an attribute."""
    head, *rest = name.rstrip("_").split("_")
    return head + "".join(part.capitalize() for part in rest)


def _field_name(item: Field) -> str:
    return identifier(item.name, MODEL_ATTRIBUTES)


def _resource_class(resource: Resource, flavor: str) -> str:
    name = "".join(naming.pascal(part) for part in resource.path) + "Resource"
    return f"Async{name}" if flavor == "async" else name


def _resource_path(resource: Resource) -> str:
    return ".".join(identifier(part) for part in resource.path)


def _client_path(op: Operation) -> str:
    return "client." + ".".join(identifier(part) for part in op.resource) + f".{op.name}()"


def _enum_members(enum: Enum) -> list[tuple[str, str | int]]:
    members: list[tuple[str, str | int]] = []
    for value in enum.values:
        name = re.sub(r"\W", "_", value.name).strip("_") or "EMPTY"
        if name[0].isdigit():
            name = f"VALUE_{name}"
        unique, counter = name, 2
        while any(unique == taken for taken, _ in members):
            unique, counter = f"{name}_{counter}", counter + 1
        members.append((unique, value.wire))
    return members


def _enumerate(items: list[str]) -> str:
    """Joins references as prose: "a, b and c", "a, b, c and 2 more"."""
    items = list(dict.fromkeys(items))
    if len(items) > REFERENCES:
        items = [*items[:REFERENCES], f"{len(items) - REFERENCES} more"]
    return " and ".join([", ".join(items[:-1]), items[-1]]) if len(items) > 1 else items[0]


class Context:
    """What declarations need to know about the whole API: class names and usages."""

    def __init__(self, ir: IR) -> None:
        self.ir = ir
        taken = HELPERS | {
            _resource_class(resource, flavor)
            for resource in ir.root.walk()[1:]
            for flavor in FLAVORS
        }
        self.names = {
            name: f"{name}{suffix}" if name in taken or keyword.iskeyword(name) else name
            for names, suffix in ((ir.models, "Model"), (ir.enums, "Enum"))
            for name in names
        }
        self.returned_by: dict[str, list[str]] = defaultdict(list)
        self.sent_by: dict[str, list[str]] = defaultdict(list)
        self.used_by: dict[str, list[str]] = defaultdict(list)
        for model in ir.models.values():
            for item in model.fields:
                where = f"`{self.names[model.name]}.{_field_name(item)}`"
                self._record(self.used_by, item.type, where)
        for op in ir.operations:
            where = f"`{_client_path(op)}`"
            if op.response.type:
                self._record(self.returned_by, op.response.type, where)
            if op.body:
                self._record(self.sent_by, op.body.type, where)
            for param in op.params:
                parameter = f"the `{identifier(param.name)}` parameter of {where}"
                self._record(self.used_by, param.type, parameter)

    @staticmethod
    def _record(usages: dict[str, list[str]], ref: TypeRef, where: str) -> None:
        for nested in ref.walk():
            if nested.name:
                usages[nested.name].append(where)

    def describe(self, name: str, description: str | None) -> Doc:
        """Documents a model or enum by its description, or else by where the API uses it."""
        if description:
            return Doc(description)
        sentences = []
        if self.returned_by[name]:
            sentences.append(f"Returned by {_enumerate(self.returned_by[name])}.")
        if self.sent_by[name]:
            sentences.append(f"Request body of {_enumerate(self.sent_by[name])}.")
        if self.used_by[name]:
            verb = "Values of" if name in self.ir.enums else "Used in"
            sentences.append(f"{verb} {_enumerate(self.used_by[name])}.")
        text = " ".join(sentences)
        if text and len(text) <= WIDTH - len('    """"""'):
            return Doc(text)
        return Doc(" ".join(naming.words(name)).capitalize(), [text] if text else [])


# Types


@dataclass
class Imports:
    modules: set[str] = field(default_factory=set)
    names: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add(self, module: str, name: str | None = None) -> None:
        if name is None:
            self.modules.add(module)
        else:
            self.names[module].add(name)

    def lines(self) -> list[str]:
        return [f"import {module}" for module in sorted(self.modules)] + [
            f"from {module} import {', '.join(sorted(names))}"
            for module, names in sorted(self.names.items())
        ]


@dataclass
class Types:
    """Renders IR types as Python types and records the imports they need.

    Roles: `field` (model attributes), `param` (method arguments, which accept more), `return`
    (method results) and `cast` (the type responses are validated against, an expression).
    """

    context: Context
    imports: Imports
    in_models_module: bool = False
    shadowed: frozenset[str] = frozenset()

    def render(self, ref: TypeRef, role: Role, *, qualify: bool = True) -> str:
        """Renders `ref`; shadowed builtins are spelled `builtins.x` in annotations only."""
        if self.shadowed and (role == "cast" or not qualify):
            return Types(self.context, self.imports, self.in_models_module).render(ref, role)
        kind, item = ref.kind, ref.item
        if kind in SCALARS:
            return self.builtin(SCALARS[kind])
        if kind == "decimal":
            self.imports.add("decimal", "Decimal")
            return "Decimal | float" if role == "param" else "Decimal"
        if kind in ("datetime", "date") and role in ("field", "cast"):
            alias = "DateTime" if kind == "datetime" else "Date"
            self.imports.add("whitebird._models", alias)
            return alias
        if kind in ("datetime", "date"):
            self.imports.add("datetime", kind)
            return kind
        if kind == "binary" and role == "param":
            self.imports.add("whitebird.types", "FileContent")
            return "FileContent"
        if kind == "binary":
            return self.builtin("bytes")
        if kind == "array" and item and role == "param":
            self.imports.add("collections.abc", "Sequence")
            return f"Sequence[{self.render(item, role)}]"
        if kind == "array" and item:
            return f"{self.builtin('list')}[{self.render(item, role)}]"
        if kind == "map" and item and role == "param":
            self.imports.add("collections.abc", "Mapping")
            return f"Mapping[str, {self.render(item, role)}]"
        if kind == "map" and item:
            return f"{self.builtin('dict')}[str, {self.render(item, role)}]"
        if kind == "page" and item:
            self.imports.add("whitebird.pagination", "Page")
            return f"Page[{self.render(item, role)}]"
        if kind == "model" and ref.name:
            return self.named(ref.name)
        if kind == "enum" and ref.name:
            base = "int" if self.context.ir.enums[ref.name].base == "integer" else "str"
            return f"{self.named(ref.name)} | {self.builtin(base)}"
        self.imports.add("typing", "Any")
        return "Any"

    def named(self, name: str) -> str:
        python_name = self.context.names[name]
        if not self.in_models_module:
            self.imports.add(MODELS, python_name)
        return python_name

    def builtin(self, name: str) -> str:
        if name in self.shadowed:
            self.imports.add("builtins")
            return f"builtins.{name}"
        return name

    def default(self, ref: TypeRef, value: Any) -> str | None:
        """Renders a spec default as a literal, or None when it has no simple literal form."""
        if ref.kind == "enum" and ref.name:
            for member, wire in _enum_members(self.context.ir.enums[ref.name]):
                if wire == value:
                    return f"{self.named(ref.name)}.{member}"
        return repr(value) if isinstance(value, bool | int | float | str) else None


def _optional(annotation: str) -> str:
    return annotation if annotation == "Any" else f"{annotation} | None"


# Docstrings


@dataclass
class Doc:
    summary: str
    body: list[str] = field(default_factory=list)
    sections: list[tuple[str, Entries]] = field(default_factory=list)

    def section(self, title: str, entries: Entries) -> None:
        if entries:
            self.sections.append((title, entries))


def render_docstring(doc: Doc | str, indent: int) -> str:
    """Renders a Google-style docstring literal whose continuation lines start at `indent`."""
    if isinstance(doc, str):
        doc = Doc(doc)
    width = WIDTH - indent
    summary, *rest = _split_summary(_sentence(doc.summary), width - 6)
    lines = _wrap(summary, width - 3)
    for paragraph in [*rest, *doc.body]:
        lines += ["", *_wrap(_punctuate(paragraph), width)]
    for title, entries in doc.sections:
        lines += ["", f"{title}:"]
        for name, text in entries:
            entry = _wrap(f"{name}: {text}", width - 4, 4) if name else _wrap(text, width - 4)
            lines += [f"    {line}" for line in entry]
    text = "\n".join(lines).replace('"""', r"\"\"\"")
    prefix = "r" if "\\" in text else ""
    if len(lines) == 1 and len(text) + indent + 6 <= WIDTH:
        return f'{prefix}"""{text}"""'
    pad = " " * indent
    body = "\n".join(pad + line if line else "" for line in text.splitlines())
    return f'{prefix}"""{body.lstrip()}\n{pad}"""'


def _split_summary(text: str, width: int) -> list[str]:
    """Moves what follows the first sentence to the body when the summary is too long."""
    first, _, rest = text.partition(". ")
    if len(text) <= width or not rest or len(first) + 1 > width:
        return [text]
    return [f"{first}.", rest]


def _wrap(text: str, width: int, hang: int = 0) -> list[str]:
    """Wraps prose to `width`, keeping list items and indented code on their own lines."""
    lines: list[str] = []
    for index, paragraph in enumerate(re.split(r"\n\s*\n", text.strip())):
        if index:
            lines.append("")
        raw = [line.rstrip() for line in paragraph.splitlines()]
        if not any(re.match(STRUCTURED, line) for line in raw):
            lines += _fill(" ".join(line.strip() for line in raw), width, hang)
            continue
        for line in raw:
            item = re.match(LIST_ITEM, line)
            lines += _fill(line, width, hang + (len(item.group(0)) if item else 0))
    return lines


def _fill(text: str, width: int, hang: int) -> list[str]:
    indent = " " * hang
    wrapped = textwrap.wrap(
        text, width, subsequent_indent=indent, break_long_words=False, break_on_hyphens=False
    )
    return wrapped or [text]


def _punctuate(text: str) -> str:
    """Ends a prose paragraph with a period; lists and code are left alone."""
    text = text.rstrip()
    last = text.splitlines()[-1] if text else ""
    if not text or text.endswith((".", "!", "?", ":")) or re.match(STRUCTURED, last):
        return text
    return f"{text}."


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if text.endswith((".", "!", "?")) else f"{text}."


# Models


@dataclass
class FieldView:
    name: str
    annotation: str
    value: str | None
    doc: Doc | None


@dataclass
class ModelView:
    name: str
    doc: Doc
    fields: list[FieldView]


@dataclass
class EnumView:
    name: str
    base: str
    doc: Doc
    members: list[tuple[str, str, str | None]]


def _models(context: Context, env: jinja2.Environment) -> str:
    imports = Imports()
    enums = []
    for enum in context.ir.enums.values():
        base = "IntEnum" if enum.base == "integer" else "StrEnum"
        imports.add("whitebird._models", base)
        descriptions = {value.wire: value.description for value in enum.values}
        members = [(name, repr(wire), descriptions[wire]) for name, wire in _enum_members(enum)]
        doc = context.describe(enum.name, enum.description)
        enums.append(EnumView(context.names[enum.name], base, doc, members))
    models = []
    for model in context.ir.models.values():
        imports.add("whitebird._models", "WhitebirdModel")
        names = [_field_name(item) for item in model.fields]
        types = Types(context, imports, True, frozenset(names) & BUILTIN_TYPES)
        fields = [_field(item, name, types) for item, name in zip(model.fields, names, strict=True)]
        doc = context.describe(model.name, model.description)
        models.append(ModelView(context.names[model.name], doc, fields))
    exports = sorted([enum.name for enum in enums] + [model.name for model in models])
    return env.get_template("models.py.j2").render(
        imports=imports.lines(), exports=exports, enums=enums, models=models
    )


def _field(item: Field, name: str, types: Types) -> FieldView:
    annotation = types.render(item.type, "field")
    if item.nullable or not item.required:
        annotation = _optional(annotation)
    default = None if item.required else types.default(item.type, item.default) or "None"
    settings = []
    if wire_name(name) != item.wire:
        settings += [f"validation_alias={item.wire!r}", f"serialization_alias={item.wire!r}"]
    if item.deprecated:
        settings.append("deprecated=True")
    value = default
    if settings:
        types.imports.add("pydantic", "Field")
        value = f"Field({', '.join(([f'default={default}'] if default else []) + settings)})"
    notes = ["Deprecated." if item.deprecated else "", (item.description or "").strip()]
    if item.inferred:
        notes.append("Inferred from the web application; may be missing.")
    text = " ".join(note for note in notes if note)
    return FieldView(name, annotation, value, Doc(text) if text else None)


# Resources


@dataclass
class Argument:
    name: str
    annotation: str
    default: str | None
    doc: str


@dataclass
class MethodView:
    name: str
    doc: Doc
    decorators: list[str] = field(default_factory=list)
    positional: list[Argument] = field(default_factory=list)
    keyword: list[Argument] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)
    returns: dict[str, str] = field(default_factory=dict)
    call: str = "request"
    awaitable: bool = True
    returns_value: bool = True
    cast: str | None = None


@dataclass
class ChildView:
    attribute: str
    module: str
    classes: dict[str, str]
    doc: str


@dataclass
class ResourceView:
    classes: dict[str, str]
    doc: str
    children: list[ChildView]
    methods: list[MethodView]


def _child(resource: Resource) -> ChildView:
    reserved = CLIENT_MEMBERS if len(resource.path) == 1 else frozenset()
    return ChildView(
        attribute=identifier(resource.name, reserved),
        module=identifier(resource.name),
        classes={flavor: _resource_class(resource, flavor) for flavor in FLAVORS},
        doc=f"Operations of `client.{_resource_path(resource)}`.",
    )


def _resources(context: Context, top: Resource, env: jinja2.Environment) -> str:
    imports = Imports()
    imports.add("whitebird._resource", "SyncResource")
    imports.add("whitebird._resource", "AsyncResource")
    views = []
    for resource in top.walk():
        children = [_child(resource.children[key]) for key in sorted(resource.children)]
        names = {identifier(op.name) for op in resource.operations}
        clashes = names & {child.attribute for child in children}
        if clashes:
            raise ValueError(
                f"{'.'.join(resource.path)}: {', '.join(sorted(clashes))} is both an operation "
                "and a nested resource"
            )
        if children:
            imports.add("functools", "cached_property")
        types = Types(context, imports, shadowed=frozenset(names) & BUILTIN_TYPES)
        view = _child(resource)
        methods = [_method(op, types) for op in resource.operations]
        views.append(ResourceView(view.classes, view.doc, children, methods))
    return env.get_template("resources.py.j2").render(
        path=_resource_path(top), imports=imports.lines(), resources=views
    )


def _method(op: Operation, types: Types) -> MethodView:
    types.imports.add("typing_extensions", "Unpack")
    types.imports.add("whitebird.types", "RequestOptions")
    method = MethodView(identifier(op.name), _operation_doc(op))
    method.arguments = [repr(op.service), repr(op.method), repr(op.path)]
    _inputs(op, types, method)
    if op.auth == "none":
        method.arguments.append("authenticated=False")
        method.doc.body.append("Public endpoint: works without credentials.")
    if op.envelope:
        method.arguments.append(f"envelope={op.envelope!r}")
    _notes(op, types, method)
    _output(op, types, method)
    if op.auth == "mfa":
        method.doc.section(
            "Raises", [("MFARequiredError", "The operation MFA token is missing or expired.")]
        )
    method.arguments.append("options=options")
    return method


def _operation_doc(op: Operation) -> Doc:
    text = "\n".join(_util.docstring_lines(op.summary, op.description))
    summary, *description = re.split(r"\n\s*\n", text or f"Call `{op.method} {op.path}`.")
    return Doc(summary, description)


def _inputs(op: Operation, types: Types, method: MethodView) -> None:
    """Adds the method arguments and the request parts they are sent in."""
    body = op.body
    reserved = OPTIONS | ({"body"} if body and not body.flattened else frozenset())

    def send(target: str, pairs: list[tuple[str, Argument]]) -> None:
        if pairs:
            mapping = ", ".join(f"{wire!r}: {argument.name}" for wire, argument in pairs)
            method.arguments.append(f"{target}={{{mapping}}}")

    path = [(p.wire, _argument(p, types, reserved, "path parameter")) for p in op.path_params]
    method.positional = [argument for _, argument in path]
    send("path_params", path)
    query = [(p.wire, _argument(p, types, reserved, "query parameter")) for p in op.query_params]
    send("params", query)
    keyword = [argument for _, argument in query]
    if body and body.flattened:
        label = BODY_LABELS[body.kind]
        fields = [(f.wire, _argument(f, types, reserved, label)) for f in body.fields]
        keyword += [argument for _, argument in fields]
        if body.kind == "multipart":
            binary = {f.wire for f in body.fields if f.type.kind == "binary"}
            send("data", [pair for pair in fields if pair[0] not in binary])
            send("files", [pair for pair in fields if pair[0] in binary])
        else:
            send("json" if body.kind == "json" else "data", fields)
    elif body:
        annotation = types.render(body.type, "param")
        optional = not body.required
        keyword.append(
            Argument(
                "body",
                _optional(annotation) if optional else annotation,
                "None" if optional else None,
                "Request body.",
            )
        )
        method.arguments.append("json=body" if body.kind == "json" else "data=body")
    headers = [(p.wire, _argument(p, types, reserved, "header")) for p in op.header_params]
    send("headers", headers)
    keyword += [argument for _, argument in headers]
    method.keyword = sorted(keyword, key=lambda argument: argument.default is not None)
    method.doc.section(
        "Args",
        [(a.name, a.doc) for a in [*method.positional, *method.keyword]]
        + [("**options", "Per-request options, see `RequestOptions`.")],
    )


def _argument(
    source: Param | Field, types: Types, reserved: frozenset[str], label: str
) -> Argument:
    annotation = types.render(source.type, "param")
    default = None
    if not source.required:
        default = types.default(source.type, source.default)
        if default is None:
            annotation, default = _optional(annotation), "None"
    elif isinstance(source, Field) and source.nullable:
        annotation = _optional(annotation)
    text = _sentence(source.description) if source.description else f"`{source.wire}` {label}."
    if isinstance(source, Field) and source.inferred:
        text += " Inferred from the web application."
    if isinstance(source, Field) and source.deprecated:
        text = f"Deprecated. {text}"
    return Argument(identifier(source.name, reserved), annotation, default, text)


def _notes(op: Operation, types: Types, method: MethodView) -> None:
    notes = []
    if op.status == "experimental":
        notes.append("Experimental: the endpoint may change or disappear without notice.")
    if op.source == "bundle":
        notes.append("Reconstructed from the Whitebird web application; fields may be missing.")
    if op.auth == "mfa":
        operation = f" for the `{op.mfa_operation}` operation" if op.mfa_operation else ""
        notes.append(f"Requires an operation MFA token{operation}, see `Whitebird.mfa_token`.")
    if notes:
        method.doc.section("Note", [(None, " ".join(notes))])
    if op.deprecated:
        warning = "{} is deprecated by Whitebird and may stop working."
        types.imports.add("typing_extensions", "deprecated")
        method.decorators.append(f"@deprecated({warning.format(_client_path(op))!r})")
        method.doc.section("Warning", [(None, warning.format(f"`{_client_path(op)}`"))])


def _output(op: Operation, types: Types, method: MethodView) -> None:
    """Sets how the method sends the call, what it returns and the Returns section."""
    response, pagination = op.response, op.pagination
    ref = response.type
    returns: str | None = None
    if pagination and ref and ref.item:
        item = types.render(ref.item, "return")
        types.imports.add("whitebird.pagination", "SyncPager")
        types.imports.add("whitebird.pagination", "AsyncPager")
        method.call, method.awaitable = "paginate", False
        method.returns = {"sync": f"SyncPager[{item}]", "async": f"AsyncPager[{item}]"}
        method.arguments.append(f"cast_to={types.render(ref, 'cast')}")
        if pagination.location == "body":
            method.arguments.append('page_in="body"')
        shown = types.render(ref.item, "return", qualify=False)
        returns = f"A pager over the `{shown}` items of every page."
    elif response.kind == "sse":
        event = shown = "Any"
        if ref and ref.kind != "any":
            event = types.render(ref, "return")
            shown = types.render(ref, "return", qualify=False)
            method.arguments.append(f"cast_to={types.render(ref, 'cast')}")
        else:
            types.imports.add("typing", "Any")
        types.imports.add("whitebird.streaming", "EventStream")
        types.imports.add("whitebird.streaming", "AsyncEventStream")
        method.call, method.awaitable = "stream", False
        method.returns = {"sync": f"EventStream[{event}]", "async": f"AsyncEventStream[{event}]"}
        returns = f"A stream of `{shown}` events that reconnects when the connection drops."
    elif response.kind == "empty":
        method.returns_value = False
        method.returns = dict.fromkeys(FLAVORS, "None")
    elif response.kind in ("binary", "text"):
        python_type = "bytes" if response.kind == "binary" else "str"
        method.returns = dict.fromkeys(FLAVORS, types.builtin(python_type))
        method.arguments.append(f"cast_to={python_type}")
        returns = "The response body." if response.kind == "binary" else "The response text."
    elif ref and ref.kind != "any":
        annotation = types.render(ref, "return")
        method.returns = dict.fromkeys(FLAVORS, annotation)
        method.arguments.append(f"cast_to={types.render(ref, 'cast')}")
        if ref.kind == "enum":
            # `X | str` is not a `type[T]`, so the engine returns Any for it.
            types.imports.add("typing", "cast")
            method.cast = annotation
        returns = f"The response as `{types.render(ref, 'return', qualify=False)}`."
    else:
        types.imports.add("typing", "Any")
        method.returns = dict.fromkeys(FLAVORS, "Any")
        returns = "The decoded JSON response."
    if returns:
        method.doc.section("Returns", [(None, returns)])
