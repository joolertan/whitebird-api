"""Go emitter: renders the generated layer of the Go module as ``go/*_gen.go``.

The handwritten core (the other files in ``go/``) provides the types and helpers the generated
code relies on: ``core``, ``operation``, ``call``, ``stream``, ``paginate``, the body encoders,
``Page``, ``StreamEvent``, ``Decimal``, ``Time``, ``Date``, ``Environment`` and ``Service``.

Type rules (documented for users in ``go/doc.go``):

* request fields (parameters and models reachable from request bodies): required fields are
  values, optional ones are pointers, except slices, maps, ``any`` and the ``Decimal``, ``Time``
  and ``Date`` types, whose zero value already means "absent";
* response fields are values that hold the zero value when absent or null, except nested
  models, which are pointers unless required.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .. import naming
from ..ir import IR, Body, Field, Operation, Param, Resource, TypeRef
from . import _util

OWNED = ["go/*_gen.go"]
ROOT = PurePosixPath("go")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


INITIALISMS = _words(
    """
    acl aml api ascii bic cpu css csv dns eof guid html http https iban id ip json jwt kyc mfa
    otc otp pdf qr rpc sms smtp sql sse ssh tcp tls totp ttl udp ui uid uri url utf8 uuid xlsx
    xml xsrf xss
    """
)

KEYWORDS = _words(
    """
    break case chan const continue default defer else fallthrough for func go goto if import
    interface map package range return select struct switch type var
    """
)
PREDECLARED = _words(
    """
    any append bool byte cap clear close comparable complex complex64 complex128 copy delete
    error false float32 float64 imag int int8 int16 int32 int64 iota len make max min new nil
    panic print println real recover rune string true uint uint8 uint16 uint32 uint64 uintptr
    """
)
# Names a generated method body uses besides its path parameters.
LOCALS = _words("ctx params opts r p page context http iter url strconv time fmt io json")

# Exported identifiers declared by the handwritten core.
CORE_NAMES = _words(
    """
    APIError Captcha Client CodeFunc CodeProvider Date DateOf Decimal DecimalFromFloat
    DecimalFromInt Environment ErrAuthentication ErrBadRequest ErrCaptchaRequired ErrConflict
    ErrMFARequired ErrNotFound ErrPermissionDenied ErrRateLimited ErrServer
    ErrVerificationRequired File FileTokenStore MustDecimal NewClient NewDecimal Option Page Ptr
    RequestOption Service StaticCode StreamEvent Time Token TokenStore TransportError Version
    WithBaseURL WithCaptchaSolver WithCodeProvider WithCredentials WithEnvironment
    WithHTTPClient WithHeader WithIdempotent WithLanguage WithMaxRetries WithQuery
    WithRequestMaxRetries WithRequestTimeout WithRetcodeEnvelope WithTimeout WithTokenStore
    WithUserAgent WithoutAuth
    """
)

# File names the generator uses for something other than a resource.
RESERVED_FILES = frozenset({"client", "environments", "models", "enums"})

SCALARS = {
    "string": "string",
    "number": "float64",
    "decimal": "Decimal",
    "boolean": "bool",
    "datetime": "Time",
    "date": "Date",
    "binary": "[]byte",
    "any": "any",
}
INTEGERS = {"int32": "int32", "int64": "int64"}
# Types whose zero value stands for "absent", so optional fields of these types need no pointer.
ZERO_IS_ABSENT = frozenset({"decimal", "datetime", "date", "any", "array", "map", "binary"})
# Value types whose zero value is never sent; used for "omitempty" in query, header and form tags.
OMIT_ZERO_TYPES = frozenset({"Decimal", "Time", "Date"})
PATH_KINDS = frozenset(
    {"string", "integer", "number", "decimal", "boolean", "datetime", "date", "enum"}
)
CLIENT_METHODS = frozenset({"ConfirmOperation", "Login", "Request", "SetMFAToken"})
# The core constants for the effects an operation has on the session (x-wb-session).
SESSION_EFFECTS = {"replace": "sessionReplace", "end": "sessionEnd"}

PACKAGES = {
    "context": "context",
    "fmt": "fmt",
    "http": "net/http",
    "io": "io",
    "iter": "iter",
    "json": "encoding/json",
    "strconv": "strconv",
    "time": "time",
    "url": "net/url",
}

VERBS = _words(
    """
    accept activate add approve block buy calculate cancel change check close confirm connect
    convert count create deactivate decline delete deposit disable disconnect download edit
    enable estimate exchange export fetch find generate get import issue link list load lock
    mark modify open place preview read refresh register reject remove request resend reset
    retrieve return revoke save search sell send set sign start stop submit subscribe transfer
    unblock unlink unlock unsubscribe update upload validate verify withdraw
    """
)

WRAP = 96


class EmitError(Exception):
    pass


def emit(ir: IR) -> dict[str, str]:
    return Emitter(ir).files()


# Naming


def exported(value: str) -> str:
    name = "".join(_word(word) for word in naming.words(value))
    if not name:
        raise EmitError(f"cannot derive a Go identifier from {value!r}")
    return "N" + name if name[0].isdigit() else name


def unexported(value: str) -> str:
    words = naming.words(value)
    if not words:
        raise EmitError(f"cannot derive a Go identifier from {value!r}")
    name = words[0] + "".join(_word(word) for word in words[1:])
    if name[0].isdigit():
        name = "n" + name
    if name in KEYWORDS or name in PREDECLARED or name in LOCALS:
        name += "Param"
    return name


def _word(word: str) -> str:
    if word in INITIALISMS:
        return word.upper()
    if word.endswith("s") and word[:-1] in INITIALISMS:
        return word[:-1].upper() + "s"
    return word[:1].upper() + word[1:]


# Doc comments


def comment(paragraphs: Iterable[str]) -> list[str]:
    """Formats paragraphs as // comment lines."""
    lines: list[str] = []
    for paragraph in paragraphs:
        if lines:
            lines.append("//")
        lines.extend(f"// {line}".rstrip() for line in _wrap(paragraph))
    return lines


def _wrap(paragraph: str) -> list[str]:
    raw = paragraph.splitlines()
    if any(re.match(r"\s*([-*+]|\d+\.)\s", line) for line in raw):
        return raw
    return textwrap.wrap(
        " ".join(line.strip() for line in raw),
        WRAP,
        break_long_words=False,
        break_on_hyphens=False,
    )


def paragraphs(text: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]


def sentence(text: str) -> str:
    return text + "." if re.search(r"[\w)`'\"]$", text) else text


def links(names: list[str], limit: int = 4) -> str:
    """Formats names as doc links: "[A]", "[A] and [B]", "[A], [B], [C], [D] and 2 others"."""
    names = list(dict.fromkeys(names))
    shown = [f"[{name}]" for name in names[:limit]]
    if len(names) > limit:
        hidden = len(names) - limit
        shown.append(f"{hidden} other" if hidden == 1 else f"{hidden} others")
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def third_person(verb: str) -> str:
    if verb.endswith(("s", "x", "z", "ch", "sh")):
        return verb + "es"
    if verb.endswith("y") and verb[-2:-1] not in ("a", "e", "i", "o", "u"):
        return verb[:-1] + "ies"
    return verb + "s"


def lead(name: str, text: str) -> str | None:
    """Turns "Approve or reject an order" into "Confirm approves or rejects an order." when text
    starts with a verb in the imperative or the third person."""
    words = text.split(" ")
    verb = words[0].lower()
    if verb in VERBS:
        words[0] = third_person(verb)
        if len(words) > 2 and words[1] in ("and", "or") and words[2].lower() in VERBS:
            words[2] = third_person(words[2].lower())
    elif verb.endswith("s") and (verb[:-1] in VERBS or verb[:-2] in VERBS):
        words[0] = verb
    else:
        return None
    return sentence(f"{name} {' '.join(words)}")


# View models handed to the templates


@dataclass
class GoField:
    name: str
    type: str
    tag: str
    doc: list[str]

    @property
    def suffix(self) -> str:
        return f" `{self.tag}`" if self.tag else ""


@dataclass
class GoStruct:
    name: str
    doc: list[str]
    fields: list[GoField]
    # Statements that give unset fields their default value before the request is sent.
    defaults: list[str] = field(default_factory=list)


@dataclass
class GoModel:
    name: str
    doc: list[str]
    fields: list[GoField]
    map_value: str | None = None


@dataclass
class GoConst:
    name: str
    value: str
    doc: list[str]


@dataclass
class GoEnum:
    name: str
    base: str
    doc: list[str]
    values: list[GoConst]


@dataclass
class GoService:
    name: str
    key: str
    title: str
    urls: list[tuple[str, str]]


@dataclass
class GoArg:
    name: str
    type: str


@dataclass
class GoMethod:
    name: str
    doc: list[str]
    args: list[GoArg]
    returns: str
    kind: str
    target: str = ""
    operation: list[tuple[str, str]] = field(default_factory=list)
    defaults: list[str] = field(default_factory=list)
    # Iterators over a paginated operation: the page method, its arguments and the page field.
    page_method: str = ""
    page_args: str = ""
    page_field: str = ""
    item: str = ""

    @property
    def signature(self) -> str:
        args = ", ".join(f"{a.name} {a.type}" for a in self.args)
        return f"{self.name}({args}) {self.returns}"

    @property
    def invoke(self) -> str:
        if self.kind in ("call", "stream"):
            return f"{self.kind}[{self.target}](ctx, r.core, "
        return f"r.core.{self.kind}(ctx, "


@dataclass
class GoResource:
    name: str
    type: str
    doc: list[str]
    children: list[GoResource]
    methods: list[GoMethod]
    params: list[GoStruct]

    @property
    def has_core(self) -> bool:
        return bool(self.methods)

    def walk(self) -> list[GoResource]:
        return [self, *(r for child in self.children for r in child.walk())]


@dataclass
class Usages:
    returned_by: dict[str, list[str]] = field(default_factory=dict)
    used_in: dict[str, list[str]] = field(default_factory=dict)

    def add(self, kind: str, type_ref: TypeRef, link: str) -> None:
        target = self.returned_by if kind == "returned" else self.used_in
        for ref in type_ref.walk():
            if ref.kind in ("model", "enum") and ref.name:
                target.setdefault(ref.name, []).append(link)


class Emitter:
    def __init__(self, ir: IR) -> None:
        self.ir = ir
        self.declared: dict[str, str] = {}
        self.usages = Usages()
        self.referenced = self._referenced()
        self.request_models = self._request_models()
        self.model_names = {
            name: self._type_name(name, model.service)
            for name, model in ir.models.items()
            if name in self.referenced
        }
        self.enum_names = {
            name: self._type_name(name, None) for name in ir.enums if name in self.referenced
        }
        for name in [*self.model_names.values(), *self.enum_names.values()]:
            self._declare(name, "a schema")

    # Analysis

    def _referenced(self) -> set[str]:
        """Names of the models and enums the generated code uses.

        Models that only describe a flattened request body are not among them: their fields
        become fields of the parameter struct instead.
        """
        seen: set[str] = set()

        def visit(type_ref: TypeRef) -> None:
            for ref in type_ref.walk():
                if ref.name and ref.name not in seen and ref.kind in ("model", "enum"):
                    seen.add(ref.name)
                    if ref.kind == "model":
                        for f in self.ir.models[ref.name].fields:
                            visit(f.type)

        for op in self.ir.operations:
            for param in op.params:
                visit(param.type)
            for type_ref in _body_types(op.body):
                visit(type_ref)
            if op.response.type:
                visit(op.response.type)
        return seen

    def _request_models(self) -> set[str]:
        seen: set[str] = set()

        def visit(type_ref: TypeRef) -> None:
            for ref in type_ref.walk():
                if ref.kind == "model" and ref.name and ref.name not in seen:
                    seen.add(ref.name)
                    for f in self.ir.models[ref.name].fields:
                        visit(f.type)

        for op in self.ir.operations:
            for param in op.params:
                visit(param.type)
            for type_ref in _body_types(op.body):
                visit(type_ref)
        return seen

    def _type_name(self, name: str, service: str | None) -> str:
        go_name = exported(name)
        if go_name not in CORE_NAMES:
            return go_name
        candidates = [exported(f"{service} {name}")] if service else []
        candidates.append(go_name + ("Model" if service else "Enum"))
        return next(c for c in candidates if c not in CORE_NAMES)

    def _declare(self, name: str, what: str) -> None:
        if name in CORE_NAMES:
            raise EmitError(f"{what} would be named {name}, which the Go core already declares")
        if name in self.declared:
            raise EmitError(f"{what} and {self.declared[name]} would both be named {name}")
        self.declared[name] = what

    # Types

    def go_type(self, type_ref: TypeRef, *, request: bool, form: str | None = None) -> str:
        kind = type_ref.kind
        if kind == "binary" and form:
            return "io.Reader" if form == "multipart" else "string"
        if kind in SCALARS:
            return SCALARS[kind]
        if kind == "integer":
            return INTEGERS.get(type_ref.format or "", "int")
        if kind == "array" and type_ref.item:
            return "[]" + self.go_type(type_ref.item, request=request, form=form)
        if kind == "map" and type_ref.item:
            return "map[string]" + self.go_type(type_ref.item, request=request)
        if kind == "page" and type_ref.item:
            return f"Page[{self.go_type(type_ref.item, request=request)}]"
        if kind == "model" and type_ref.name:
            return self.model_names[type_ref.name]
        if kind == "enum" and type_ref.name:
            return self.enum_names[type_ref.name]
        raise EmitError(f"unsupported type {type_ref}")

    def _is_map_model(self, type_ref: TypeRef) -> bool:
        return type_ref.kind == "model" and not self.ir.models[type_ref.name or ""].fields

    def field_type(
        self,
        type_ref: TypeRef,
        *,
        required: bool,
        nullable: bool,
        request: bool,
        form: str | None = None,
    ) -> str:
        base = self.go_type(type_ref, request=request, form=form)
        if base == "io.Reader" or type_ref.kind in ZERO_IS_ABSENT or self._is_map_model(type_ref):
            return base
        if request:
            return base if required else "*" + base
        if type_ref.kind == "model" and (nullable or not required):
            return "*" + base
        return base

    # Models and enums

    def _model_usages(self) -> None:
        for name, model in self.ir.models.items():
            if name in self.referenced:
                for f in model.fields:
                    self.usages.add("used", f.type, self.model_names[name])

    def models(self) -> list[GoModel]:
        result = []
        for name, model in self.ir.models.items():
            if name not in self.referenced:
                continue
            go_name = self.model_names[name]
            doc = comment([*self._usage(go_name, name), *paragraphs(model.description)])
            if not model.fields:
                result.append(GoModel(go_name, doc, [], map_value="any"))
                continue
            request = name in self.request_models
            fields = [self._model_field(f, request=request) for f in model.fields]
            result.append(GoModel(go_name, doc, fields))
        return result

    def _model_field(self, f: Field, *, request: bool) -> GoField:
        type_ = self.field_type(f.type, required=f.required, nullable=f.nullable, request=request)
        tag = f'json:"{f.wire}{"" if f.required else ",omitzero"}"'
        return GoField(exported(f.wire), type_, tag, comment(_field_doc(f.description, f)))

    def enums(self) -> list[GoEnum]:
        result = []
        for name, enum in self.ir.enums.items():
            if name not in self.referenced:
                continue
            go_name = self.enum_names[name]
            values = []
            used: set[str] = set()
            # The type name prefix makes a leading digit acceptable: Priority1.
            names = [go_name + "".join(map(_word, naming.words(v.name))) for v in enum.values]
            if len(set(names)) < len(names):
                # Values that differ only in case, such as 1m and 1M, keep their spelling.
                names = [go_name + re.sub(r"\W", "", str(v.wire)) for v in enum.values]
            for index, value in enumerate(enum.values):
                const = names[index]
                if const == go_name or const in used or not const.isidentifier():
                    const = f"{go_name}Value{index}"
                used.add(const)
                self._declare(const, f"value {value.wire!r} of {name}")
                literal = json.dumps(value.wire)
                values.append(GoConst(const, literal, comment(paragraphs(value.description))))
            base = "int" if enum.base == "integer" else "string"
            doc = comment([*self._usage(go_name, name), *paragraphs(enum.description)])
            result.append(GoEnum(go_name, base, doc, values))
        return result

    def _usage(self, go_name: str, name: str) -> list[str]:
        returned = self.usages.returned_by.get(name)
        if returned:
            return [f"{go_name} is returned by {links(returned)}."]
        used = self.usages.used_in.get(name)
        return [f"{go_name} is used in {links(used)}."] if used else []

    # Resources

    def resource(self, node: Resource) -> GoResource:
        type_name = exported("_".join(node.path)) + "Resource"
        self._declare(type_name, f"resource {'.'.join(node.path)}")
        accessor = ".".join(exported(segment) for segment in node.path)
        children = [self.resource(node.children[key]) for key in sorted(node.children)]
        methods: list[GoMethod] = []
        params: list[GoStruct] = []
        for op in node.operations:
            op_methods, op_params = self.operation(op, type_name)
            methods.extend(op_methods)
            params.extend(op_params)
        members = [m.name for m in methods] + [c.name for c in children]
        for duplicate in sorted({m for m in members if members.count(m) > 1}):
            raise EmitError(f"{type_name} would have two members named {duplicate}")
        doc = comment([f"{type_name} holds the operations available as Client.{accessor}."])
        return GoResource(exported(node.name), type_name, doc, children, methods, params)

    def operation(self, op: Operation, resource: str) -> tuple[list[GoMethod], list[GoStruct]]:
        name = exported(op.name)
        struct = self.params(op, resource, name)
        args = [GoArg("ctx", "context.Context")]
        path_args: dict[str, tuple[str, TypeRef]] = {}
        for param in op.path_params:
            arg = unexported(param.wire)
            type_ref = param.type if param.type.kind in PATH_KINDS else TypeRef("string")
            path_args[param.wire] = (arg, type_ref)
            args.append(GoArg(arg, self.go_type(type_ref, request=True)))
        if struct:
            args.append(GoArg("params", struct.name))
        args.append(GoArg("opts", "...RequestOption"))

        fields = [
            ("service", "Service" + exported(op.service)),
            ("method", "http.Method" + op.method.capitalize()),
            ("path", json.dumps(op.path)),
        ]
        if op.path_params:
            values = [self._path_value(*path_args[w]) for w in _placeholders(op.path)]
            fields.append(("pathParams", f"[]string{{{', '.join(values)}}}"))
        if op.auth == "none":
            fields.append(("public", "true"))
        if struct and any(f.tag.startswith(("query:", "header:")) for f in struct.fields):
            fields.append(("params", "params"))
        if op.body:
            fields.append(("body", self._body_expr(op.body, struct)))
        if op.envelope:
            fields.append(("envelope", "true"))
        if op.auth == "mfa" and op.mfa_operation:
            fields.append(("mfaOperation", json.dumps(op.mfa_operation)))
        if op.session:
            fields.append(("session", SESSION_EFFECTS[op.session]))

        response = op.response
        paged = op.pagination is not None and response.type is not None
        defaults = struct.defaults if struct else []
        method = GoMethod(name, [], args, "", "", operation=fields, defaults=defaults)
        if response.kind == "sse":
            item = "json.RawMessage"
            if response.type:
                item = self.go_type(response.type, request=False)
            method.kind, method.target = "stream", item
            method.returns = f"iter.Seq2[StreamEvent[{item}], error]"
        elif response.kind == "empty":
            method.kind, method.returns = "exec", "error"
        elif response.kind == "binary":
            method.kind, method.returns = "binary", "([]byte, error)"
        elif response.kind == "text":
            method.kind, method.returns = "text", "(string, error)"
        else:
            result = self._result(response.type)
            method.kind, method.target, method.returns = "call", result, f"({result}, error)"

        link = f"{resource}.{name}"
        if response.type:
            self.usages.add("returned", response.type, link)
        method.doc = comment(self._operation_doc(op, name, paged, resource))
        if not paged:
            return [method], [struct] if struct else []

        assert struct and op.pagination and response.type and response.type.item
        item = self.go_type(response.type.item, request=False)
        forwarded = [a.name for a in args[1:-2]]
        page = GoMethod(
            name + "Page",
            comment(
                [
                    f"{name}Page is like [{resource}.{name}] but returns a single page, the one "
                    "selected by params.Page, along with the pagination metadata."
                ]
            ),
            args,
            method.returns,
            "call",
            target=method.target,
            operation=fields,
            defaults=defaults,
        )
        iterator = GoMethod(
            name,
            method.doc,
            args,
            f"iter.Seq2[{item}, error]",
            "paginate",
            page_method=page.name,
            page_args=", ".join(["ctx", *forwarded, "p", "opts..."]),
            page_field=exported(op.pagination.page_param),
            item=item,
        )
        return [iterator, page], [struct]

    def _result(self, type_ref: TypeRef | None) -> str:
        if type_ref is None or type_ref.kind == "any":
            return "json.RawMessage"
        go_type = self.go_type(type_ref, request=False)
        if type_ref.kind in ("model", "page") and not self._is_map_model(type_ref):
            return "*" + go_type
        return go_type

    def _path_value(self, arg: str, type_ref: TypeRef) -> str:
        """Formats a path argument as a string; the core escapes it."""
        kind = type_ref.kind
        if kind == "enum":
            enum = self.ir.enums[type_ref.name or ""]
            if enum.base == "integer":
                return f"strconv.Itoa(int({arg}))"
            return f"string({arg})"
        if kind == "integer":
            return {
                "int64": f"strconv.FormatInt({arg}, 10)",
                "int32": f"strconv.FormatInt(int64({arg}), 10)",
            }.get(type_ref.format or "", f"strconv.Itoa({arg})")
        if kind == "boolean":
            return f"strconv.FormatBool({arg})"
        if kind == "number":
            return f"strconv.FormatFloat({arg}, 'f', -1, 64)"
        if kind == "datetime":
            return f"{arg}.Format(wireLayout)"
        if kind in ("decimal", "date"):
            return f"{arg}.String()"
        return arg

    def _body_expr(self, body: Body, struct: GoStruct | None) -> str:
        encoder = {"json": "jsonBody", "form": "formBody", "multipart": "multipartBody"}[body.kind]
        if not body.flattened:
            return f"{encoder}(params.Body)"
        # A flattened body is always sent, as an empty object when the method has no parameters.
        return f"{encoder}(params)" if struct else f"{encoder}(struct{{}}{{}})"

    def params(self, op: Operation, resource: str, method: str) -> GoStruct | None:
        name = exported("_".join([*op.resource, op.name])) + "Params"
        fields: list[GoField] = []
        pagination = op.pagination
        flat_json = op.body is not None and op.body.kind == "json" and op.body.flattened
        skip_json = ' json:"-"' if flat_json else ""

        def paging(wire: str) -> bool:
            return pagination is not None and wire in (pagination.page_param, pagination.size_param)

        defaults: list[str] = []

        def default(go_name: str, go_type: str, item: Field | Param, *, paged: bool) -> bool:
            """Adds the statement that sends the default of item when it is unset, and reports
            whether the field must be sent even when it holds its zero value."""
            statement, always = _default(go_name, go_type, item, paged=paged)
            defaults.extend(statement)
            return always

        for param in [*op.query_params, *op.header_params]:
            go_name = exported(param.wire)
            if paging(param.wire):
                type_ = "int"
                option = "" if default(go_name, type_, param, paged=True) else ",omitempty"
            else:
                type_ = self.field_type(
                    param.type, required=param.required, nullable=False, request=True
                )
                default(go_name, type_, param, paged=False)
                option = _omitempty(param.required, type_)
            tag = f'{param.location}:"{param.wire}{option}"{skip_json}'
            # The iterators start from params.Page, so its default goes without saying.
            starts = pagination is not None and param.wire == pagination.page_param
            doc = _field_doc(param.description, param, default=not starts, sent=True)
            fields.append(GoField(go_name, type_, tag, comment(doc)))
            self.usages.add("used", param.type, name)

        if pagination and pagination.location == "query":
            present = {f.tag.split('"')[1].split(",")[0] for f in fields}
            synthetic = (
                (pagination.page_param, "Zero-based page index."),
                (pagination.size_param, "Page size."),
            )
            for wire, text in synthetic:
                if wire not in present:
                    tag = f'query:"{wire},omitempty"{skip_json}'
                    fields.append(GoField(exported(wire), "int", tag, comment([text])))

        body = op.body
        if body and body.flattened:
            form = body.kind if body.kind != "json" else None
            for f in body.fields:
                go_name = exported(f.wire)
                required = f.required and not paging(f.wire)
                if paging(f.wire):
                    type_ = "int"
                else:
                    type_ = self.field_type(
                        f.type, required=required, nullable=f.nullable, request=True, form=form
                    )
                always = default(go_name, type_, f, paged=paging(f.wire)) or required
                if form:
                    option = ",omitempty" if paging(f.wire) else _omitempty(required, type_)
                    if always:
                        option = ""
                    tag = f'form:"{f.wire}{option}"'
                    if form == "multipart" and f.content_type:
                        tag += f' mime:"{f.content_type}"'
                else:
                    tag = f'json:"{f.wire}{"" if always else ",omitzero"}"'
                doc = comment(_field_doc(f.description, f, sent=True))
                fields.append(GoField(go_name, type_, tag, doc))
                self.usages.add("used", f.type, name)
        elif body:
            type_ = self.go_type(body.type, request=True)
            doc = comment(["Body is the request body."])
            fields.append(GoField("Body", type_, "", doc))
            self.usages.add("used", body.type, name)

        if not fields:
            return None
        names = [f.name for f in fields]
        for duplicate in sorted({n for n in names if names.count(n) > 1}):
            raise EmitError(f"{name} would have two fields named {duplicate}")
        self._declare(name, f"parameters of {op.id}")
        targets = f"[{resource}.{method}]"
        if pagination:
            targets += f" and [{resource}.{method}Page]"
        doc = comment([f"{name} holds the parameters of {targets}."])
        return GoStruct(name, doc, fields, defaults)

    def _operation_doc(self, op: Operation, name: str, paged: bool, resource: str) -> list[str]:
        texts = [t for t in (op.description, op.summary) if t and t.strip()]
        first = None
        rest: list[str] = []
        for index, text in enumerate(texts):
            parts = paragraphs(text)
            first = lead(name, parts[0])
            if first:
                rest = parts[1:] + [p for t in texts[:index] for p in paragraphs(t)]
                break
        endpoint = f"{op.method} {op.path}"
        if op.auth == "none":
            endpoint += " without authentication"
        elif op.auth == "mfa":
            what = f" for {op.mfa_operation}" if op.mfa_operation else ""
            endpoint += f" with an operation MFA token{what} (see [Client.ConfirmOperation])"
        if first:
            result = [first, *map(sentence, rest), f"It calls {endpoint}."]
        else:
            body = paragraphs(texts[0]) if texts else []
            result = [f"{name} calls {endpoint}.", *map(sentence, body)]
        if paged:
            result.append(
                "The iterator fetches pages as the loop advances, starting with params.Page, and "
                f"stops after the last one. [{resource}.{name}Page] fetches a single page."
            )
        if op.response.kind == "sse":
            result.append(
                "The stream reconnects when the connection drops, resuming after the last event "
                "received. It ends when ctx is done, when the loop breaks or on an error that "
                "reconnecting cannot fix."
            )
        if op.status == "experimental":
            result.append("This endpoint is experimental and may change without notice.")
        if op.deprecated:
            result.append("Deprecated: Whitebird has deprecated this endpoint.")
        return result

    # Files

    def files(self) -> dict[str, str]:
        env = _util.environment("go")
        resources = [self.resource(self.ir.root.children[k]) for k in sorted(self.ir.root.children)]
        environments = self.environments()
        services = self.services()
        clashes = sorted(CLIENT_METHODS & {r.name for r in resources})
        if clashes:
            raise EmitError(f"resource {clashes[0]} clashes with the Client method of that name")
        self._model_usages()
        models, enums = self.models(), self.enums()

        oauth, mfa = self.ir.oauth, self.ir.operation_mfa
        rendered = {
            "client_gen.go": env.get_template("client.go.j2").render(resources=resources),
            "environments_gen.go": env.get_template("environments.go.j2").render(
                environments=environments,
                services=services,
                oauth_service=self._service(oauth.service, "oauth"),
                token_path=json.dumps(oauth.token_path),
                mfa_status_path=json.dumps(oauth.mfa_status_path),
                captcha_path=json.dumps(oauth.captcha_path),
                basic=json.dumps(oauth.basic),
                operation_mfa_service=self._service(mfa.service, "operation_mfa"),
                operation_mfa_path=json.dumps(mfa.token_path),
            ),
        }
        if models:
            code = [f.type for m in models for f in m.fields]
            rendered["models_gen.go"] = env.get_template("models.go.j2").render(
                models=models, imports=_imports(code)
            )
        if enums:
            rendered["enums_gen.go"] = env.get_template("enums.go.j2").render(enums=enums)
        for resource in resources:
            stem = naming.snake(resource.name)
            if stem in RESERVED_FILES:
                stem += "_resource"
            code = ["context.Context", "http.Method"]
            for node in resource.walk():
                code += [f.type for s in node.params for f in s.fields]
                code += [m.returns for m in node.methods]
                code += [a.type for m in node.methods for a in m.args]
                code += [value for m in node.methods for _, value in m.operation]
            rendered[f"{stem}_gen.go"] = env.get_template("resource.go.j2").render(
                resources=resource.walk(), imports=_imports(code)
            )
        return {
            str(ROOT / name): _util.pipe(["gofmt"], source) for name, source in rendered.items()
        }

    def _service(self, key: str, where: str) -> str:
        if key not in self.ir.services:
            raise EmitError(f"{where} refers to the unknown service {key}")
        return "Service" + exported(key)

    def environments(self) -> list[GoConst]:
        result = []
        for env in self.ir.environments:
            name = exported(env)
            self._declare(name, f"environment {env}")
            result.append(GoConst(name, json.dumps(env), []))
        return result

    def services(self) -> list[GoService]:
        result = []
        default = exported(self.ir.environments[0])
        for key, service in self.ir.services.items():
            name = "Service" + exported(key)
            self._declare(name, f"service {key}")
            urls = [(exported(env), json.dumps(url)) for env, url in service.urls.items()]
            if not any(env == default for env, _ in urls):
                raise EmitError(f"service {key} has no {self.ir.environments[0]} URL")
            result.append(GoService(name, json.dumps(key), service.title, urls))
        return result


def _body_types(body: Body | None) -> list[TypeRef]:
    if body is None:
        return []
    return [f.type for f in body.fields] if body.flattened else [body.type]


def _field_doc(
    description: str | None, item: Field | Param, *, default: bool = True, sent: bool = False
) -> list[str]:
    """Documents a field. With sent, the client sends its default when the field is unset."""
    result = [sentence(p) for p in paragraphs(description)]
    notes = []
    if default and _scalar_default(item) is not None:
        value = json.dumps(item.default)
        notes.append(f"Defaults to {value}." if sent else f"The server defaults to {value}.")
    if isinstance(item, Field) and item.inferred:
        notes.append("Inferred from the web application; not yet observed in responses.")
    if notes:
        result[-1:] = [" ".join(result[-1:] + notes)]
    if isinstance(item, Field) and item.deprecated:
        result.append("Deprecated: Whitebird has deprecated this field.")
    return result


def _scalar_default(item: Field | Param) -> str | int | float | bool | None:
    value = item.default
    return value if isinstance(value, (str, int, float, bool)) else None


def _default(
    go_name: str, go_type: str, item: Field | Param, *, paged: bool
) -> tuple[list[str], bool]:
    """Returns the statements that set the field go_name of params to the default of item when
    it is unset, and whether the field must be sent even when it holds its zero value."""
    value = _scalar_default(item)
    if value is None or not _compatible(value, item.type.kind):
        return [], False
    literal = json.dumps(value)
    target = f"params.{go_name}"
    if go_type.startswith("*"):
        return [f"if {target} == nil {{", f"{target} = Ptr[{go_type[1:]}]({literal})", "}"], False
    if isinstance(value, bool) or go_type in OMIT_ZERO_TYPES or go_type in ("any", "io.Reader"):
        # Required booleans are always sent; other value types have no default to give.
        return [], False
    if value in ("", 0):
        # The default is the zero value: send it.
        return [], True
    if isinstance(value, str) or paged:
        # An empty string or a page size of 0 means the field is unset.
        zero = '""' if isinstance(value, str) else "0"
        return [f"if {target} == {zero} {{", f"{target} = {literal}", "}"], True
    # Zero may be meant for a required number.
    return [], False


def _compatible(value: str | int | float | bool, kind: str) -> bool:
    if isinstance(value, bool):
        return kind == "boolean"
    if isinstance(value, int):
        return kind in ("integer", "number", "enum")
    if isinstance(value, float):
        return kind == "number"
    return kind in ("string", "enum")


def _placeholders(path: str) -> list[str]:
    return re.findall(r"\{([^}]+)\}", path)


def _omitempty(required: bool, go_type: str) -> str:
    return ",omitempty" if not required and go_type in OMIT_ZERO_TYPES else ""


def _imports(code: Iterable[str]) -> list[str]:
    """Returns the import paths of the packages referenced in snippets of generated code."""
    used: set[str] = set()
    for text in code:
        text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
        used.update(re.findall(r"\b([a-z]+)\.", text))
    return sorted(PACKAGES[name] for name in used if name in PACKAGES)
