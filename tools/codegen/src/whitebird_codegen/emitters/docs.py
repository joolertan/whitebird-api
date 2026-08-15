"""Emits the endpoint catalog for the documentation site."""

from __future__ import annotations

from ..ir import IR, Operation
from ._util import environment

OWNED = ["docs/reference/endpoints.md"]

AUTH_LABELS = {"none": "public", "user": "user", "mfa": "user + MFA"}


def emit(ir: IR) -> dict[str, str]:
    env = environment("docs")
    sections = [
        (resource, sorted(resource.all_operations(), key=_order))
        for resource in (ir.root.children[name] for name in sorted(ir.root.children))
    ]
    page = env.get_template("endpoints.md.j2").render(
        sections=sections,
        services=ir.services,
        total=len(ir.operations),
        auth_labels=AUTH_LABELS,
        call=_call,
    )
    return {OWNED[0]: page}


def _order(op: Operation) -> tuple[tuple[str, ...], str]:
    return op.resource, op.name


def _call(op: Operation) -> str:
    return ".".join(["client", *op.resource, op.name])
