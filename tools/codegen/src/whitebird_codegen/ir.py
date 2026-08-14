"""Language-neutral description of the API consumed by the emitters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TypeKind = Literal[
    "string",
    "integer",
    "number",
    "decimal",
    "boolean",
    "datetime",
    "date",
    "binary",
    "any",
    "array",
    "map",
    "model",
    "enum",
    "page",
]
Location = Literal["path", "query", "header"]
BodyKind = Literal["json", "form", "multipart"]
ResponseKind = Literal["json", "binary", "text", "empty", "sse"]
Auth = Literal["none", "user", "mfa"]
Status = Literal["stable", "experimental", "deprecated"]
Envelope = Literal["retcode"]


@dataclass(frozen=True)
class TypeRef:
    kind: TypeKind
    item: TypeRef | None = None
    name: str | None = None
    format: str | None = None

    @classmethod
    def array(cls, item: TypeRef) -> TypeRef:
        return cls("array", item=item)

    @classmethod
    def map(cls, item: TypeRef) -> TypeRef:
        return cls("map", item=item)

    @classmethod
    def model(cls, name: str) -> TypeRef:
        return cls("model", name=name)

    @classmethod
    def enum(cls, name: str) -> TypeRef:
        return cls("enum", name=name)

    @classmethod
    def page(cls, item: TypeRef) -> TypeRef:
        return cls("page", item=item)

    def walk(self) -> list[TypeRef]:
        """Returns this type and every type nested in it."""
        return [self, *(self.item.walk() if self.item else [])]


@dataclass
class Field:
    name: str
    wire: str
    type: TypeRef
    required: bool = False
    nullable: bool = False
    description: str | None = None
    inferred: bool = False
    deprecated: bool = False
    default: Any = None


@dataclass
class Model:
    name: str
    fields: list[Field]
    description: str | None = None
    service: str | None = None


@dataclass
class EnumValue:
    wire: str | int
    name: str
    description: str | None = None


@dataclass
class Enum:
    name: str
    values: list[EnumValue]
    base: Literal["string", "integer"] = "string"
    description: str | None = None


@dataclass
class Param:
    name: str
    wire: str
    location: Location
    type: TypeRef
    required: bool = False
    description: str | None = None
    default: Any = None


@dataclass
class Body:
    kind: BodyKind
    type: TypeRef
    fields: list[Field]
    required: bool = True

    @property
    def flattened(self) -> bool:
        """True when the body is an object whose fields become method parameters."""
        return bool(self.fields)


@dataclass
class Response:
    kind: ResponseKind
    type: TypeRef | None = None


@dataclass
class Pagination:
    item: TypeRef
    page_param: str
    size_param: str
    location: Literal["query", "body"]


@dataclass
class Operation:
    id: str
    name: str
    resource: tuple[str, ...]
    service: str
    method: str
    path: str
    summary: str | None
    description: str | None
    path_params: list[Param]
    query_params: list[Param]
    header_params: list[Param]
    body: Body | None
    response: Response
    auth: Auth = "user"
    status: Status = "stable"
    source: str | None = None
    pagination: Pagination | None = None
    mfa_operation: str | None = None
    envelope: Envelope | None = None

    @property
    def deprecated(self) -> bool:
        return self.status == "deprecated"

    @property
    def params(self) -> list[Param]:
        return [*self.path_params, *self.query_params, *self.header_params]


@dataclass
class Resource:
    path: tuple[str, ...]
    operations: list[Operation] = field(default_factory=list)
    children: dict[str, Resource] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path[-1] if self.path else ""

    def child(self, segment: str) -> Resource:
        if segment not in self.children:
            self.children[segment] = Resource((*self.path, segment))
        return self.children[segment]

    def walk(self) -> list[Resource]:
        """Returns this resource and all descendants, parents first."""
        result = [self]
        for key in sorted(self.children):
            result.extend(self.children[key].walk())
        return result

    def all_operations(self) -> list[Operation]:
        return [op for resource in self.walk() for op in resource.operations]


@dataclass
class Service:
    key: str
    title: str
    urls: dict[str, str]


@dataclass
class OAuth:
    service: str
    token_path: str
    basic: str


@dataclass
class IR:
    environments: list[str]
    services: dict[str, Service]
    oauth: OAuth
    root: Resource
    models: dict[str, Model]
    enums: dict[str, Enum]

    @property
    def operations(self) -> list[Operation]:
        return self.root.all_operations()
