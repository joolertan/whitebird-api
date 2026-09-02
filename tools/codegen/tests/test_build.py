from pathlib import Path

import pytest

from whitebird_codegen.build import SpecError, load
from whitebird_codegen.ir import IR, Operation, TypeRef

FIXTURE = Path(__file__).parent / "fixtures" / "spec"


@pytest.fixture(scope="module")
def ir() -> IR:
    return load(FIXTURE)


def operation(ir: IR, op_id: str) -> Operation:
    return next(op for op in ir.operations if op.id == op_id)


def test_resources_follow_x_wb_resource(ir: IR) -> None:
    assert sorted(ir.root.children) == [
        "balances",
        "events",
        "orders",
        "products",
        "profile",
        "tickers",
    ]
    assert [op.name for op in ir.root.children["orders"].children["documents"].operations] == [
        "receipt"
    ]


def test_services_and_environments(ir: IR) -> None:
    assert ir.services["shop"].urls == {
        "production": "https://shop.example.test",
        "demo": "https://shop-demo.example.test",
    }
    assert ir.oauth.service == "auth"


def test_pagination_in_query(ir: IR) -> None:
    op = operation(ir, "productsList")
    assert op.auth == "none"
    assert op.response.type == TypeRef.page(TypeRef.model("Product"))
    assert op.pagination is not None and op.pagination.location == "query"
    assert [p.name for p in op.query_params] == ["page", "size", "category", "tags"]


def test_pagination_in_body(ir: IR) -> None:
    op = operation(ir, "ordersSearch")
    assert op.pagination is not None and op.pagination.location == "body"
    assert op.status == "experimental"
    assert op.body is not None and [f.name for f in op.body.fields] == ["page", "size", "statuses"]
    assert "OrdersSearchRequest" in ir.models


def test_page_wrappers_are_not_models(ir: IR) -> None:
    assert "PageProduct" not in ir.models
    assert "PageOrder" not in ir.models


def test_path_params_and_headers(ir: IR) -> None:
    op = operation(ir, "ordersCreate")
    assert [p.wire for p in op.header_params] == ["X-Request-Id"]
    assert op.body is not None and op.body.type == TypeRef.model("OrderRequest")
    receipt = operation(ir, "ordersReceipt")
    assert [p.name for p in receipt.path_params] == ["order_id"]
    assert receipt.response.kind == "binary"
    assert receipt.query_params[0].type == TypeRef.enum("Format")


def test_body_kinds(ir: IR) -> None:
    withdraw = operation(ir, "ordersWithdraw")
    assert withdraw.body is not None and withdraw.body.kind == "form"
    assert withdraw.auth == "mfa" and withdraw.mfa_operation == "WITHDRAWAL"
    assert withdraw.response.kind == "empty"
    upload = operation(ir, "profileUploadAvatar")
    assert upload.body is not None and upload.body.kind == "multipart"
    assert upload.response.kind == "text"


def test_response_kinds(ir: IR) -> None:
    assert operation(ir, "balancesGet").response.type == TypeRef.map(TypeRef("decimal"))
    events = operation(ir, "eventsStream")
    assert events.response.kind == "sse" and events.response.type == TypeRef.model("Event")
    assert operation(ir, "productsDelete").deprecated


def test_model_fields(ir: IR) -> None:
    fields = {f.name: f for f in ir.models["Product"].fields}
    assert fields["id"].required and fields["id"].type == TypeRef("integer", format="int64")
    assert fields["created_at"].wire == "createdAt" and fields["created_at"].type.kind == "datetime"
    assert fields["release_date"].type.kind == "date"
    assert fields["price"].type.kind == "decimal"
    assert fields["discount"].nullable and fields["discount"].type.kind == "number"
    assert fields["attributes"].type == TypeRef.map(TypeRef("string"))
    assert fields["dimensions"].type == TypeRef.model("ProductDimensions")
    assert fields["legacy_code"].deprecated
    assert fields["popularity"].inferred and not fields["popularity"].required
    assert fields["metadata"].type.kind == "any"


def test_enums(ir: IR) -> None:
    category = ir.enums["Category"]
    assert [v.name for v in category.values] == ["BOOKS", "GAMES", "IN_PROGRESS"]
    assert ir.enums["Priority"].base == "integer"


def test_invalid_spec_reports_every_problem(tmp_path: Path) -> None:
    (tmp_path / "services").mkdir()
    (tmp_path / "environments.yaml").write_text((FIXTURE / "environments.yaml").read_text())
    (tmp_path / "services" / "bad.yaml").write_text(
        "x-wb-service: shop\n"
        "paths:\n"
        "  /a:\n    get: {responses: {}}\n"
        "  /b:\n    get: {operationId: x}\n"
    )
    with pytest.raises(SpecError) as error:
        load(tmp_path)
    assert len(error.value.problems) == 2


def test_envelope(ir: IR) -> None:
    op = operation(ir, "tickersList")
    assert op.envelope == "retcode"
    assert op.response.type == TypeRef.array(TypeRef.model("Ticker"))
    assert operation(ir, "productsList").envelope is None


def test_bare_number_is_decimal(tmp_path: Path) -> None:
    (tmp_path / "services").mkdir()
    (tmp_path / "environments.yaml").write_text((FIXTURE / "environments.yaml").read_text())
    (tmp_path / "services" / "shop.yaml").write_text(
        "x-wb-service: shop\n"
        "paths:\n"
        "  /a:\n"
        "    get:\n"
        "      operationId: a\n"
        "      x-wb-resource: a\n"
        "      x-wb-name: get\n"
        "      responses:\n"
        "        '200':\n"
        "          content:\n"
        "            application/json:\n"
        "              schema:\n"
        "                properties:\n"
        "                  amount: {type: number}\n"
        "                  ratio: {type: number, format: double}\n"
    )
    fields = {f.name: f.type for f in load(tmp_path).models["AResponse"].fields}
    assert fields == {"amount": TypeRef("decimal"), "ratio": TypeRef("number", format="double")}


def test_operation_and_resource_names_must_differ(tmp_path: Path) -> None:
    (tmp_path / "services").mkdir()
    (tmp_path / "environments.yaml").write_text((FIXTURE / "environments.yaml").read_text())
    operation = (
        "    get: {{operationId: {0}, x-wb-resource: {1}, x-wb-name: {2}, responses: {{}}}}\n"
    )
    (tmp_path / "services" / "shop.yaml").write_text(
        "x-wb-service: shop\npaths:\n"
        "  /a:\n"
        + operation.format("aOrders", "a", "orders")
        + "  /b:\n"
        + operation.format("aOrdersList", "a.orders", "list")
    )
    with pytest.raises(SpecError, match="both an operation and a resource"):
        load(tmp_path)
