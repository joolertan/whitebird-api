"""Generates the Python SDK from the fixture spec into a copy of the package and exercises it."""

import fnmatch
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from whitebird_codegen.build import load
from whitebird_codegen.emitters import _util
from whitebird_codegen.emitters import python as emitter

FIXTURE = Path(__file__).parent / "fixtures" / "spec"
PROJECT = Path(__file__).resolve().parents[3] / "python"
INTERPRETER = PROJECT / ".venv" / "bin" / "python"

BEHAVIOUR = """
import asyncio
import json
import warnings
from decimal import Decimal

import httpx
import pytest
import respx

from whitebird import AsyncWhitebird, MemoryTokenStore, ServerSentEvent, Tokens, Whitebird
from whitebird.models import Category, Event, Order, OrderStatus, Priority, Product, Ticker

SHOP = "https://shop.example.test"


def client() -> Whitebird:
    return Whitebird(token_store=MemoryTokenStore(Tokens("token")))


def test_query_pagination_on_a_public_endpoint(respx_mock: respx.MockRouter) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        content = [{"id": page, "name": f"p{page}", "category": "GAMES", "price": 0.1}]
        return httpx.Response(
            200, json={"content": content, "totalElements": 2, "totalPages": 2, "number": page}
        )

    route = respx_mock.get(f"{SHOP}/api/v1/products").mock(side_effect=respond)

    products = list(client().products.list(size=1, category=Category.GAMES, tags=["a", "b"]))

    assert [p.id for p in products] == [0, 1]
    assert products[0].category is Category.GAMES
    assert products[0].price == Decimal("0.1")
    request = route.calls.last.request
    assert request.url.query == b"page=1&size=1&category=GAMES&tags=a&tags=b"
    assert "authorization" not in request.headers


def test_body_pagination(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{SHOP}/api/v1/orders/search").respond(
        json={"content": [{"id": "o", "status": "NEW"}], "totalElements": 1, "last": True}
    )

    orders = list(client().orders.search(size=10, statuses=[OrderStatus.NEW, "OTHER"]))

    assert orders == [Order(id="o", status=OrderStatus.NEW)]
    assert json.loads(route.calls.last.request.content) == {
        "page": 0,
        "size": 10,
        "statuses": ["NEW", "OTHER"],
    }


def test_json_body_and_header(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{SHOP}/api/v1/orders").respond(json={"id": "o", "status": "X"})

    order = client().orders.create(
        product_id=1, quantity=2, priority=Priority.VALUE_2, x_request_id="r"
    )

    assert order.status == "X"
    request = route.calls.last.request
    assert json.loads(request.content) == {"productId": 1, "quantity": 2, "priority": 2}
    assert request.headers["x-request-id"] == "r"
    assert request.headers["authorization"] == "Bearer token"


def test_form_body_with_mfa_token(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{SHOP}/api/v1/orders/o%201/withdraw").respond(200)
    shop = client()
    shop.mfa_token = "mfa"

    assert shop.orders.withdraw("o 1", address="addr") is None

    request = route.calls.last.request
    assert request.content == b"address=addr"
    assert request.headers["x-mfa-token"] == "mfa"


def test_binary_text_map_and_envelope(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{SHOP}/api/v1/orders/o/receipt", params={"format": "PDF"}).respond(
        content=b"%PDF", headers={"content-type": "application/pdf"}
    )
    upload = respx_mock.post(f"{SHOP}/api/v1/avatar").respond(text="ok")
    respx_mock.get(f"{SHOP}/api/v1/balances").respond(
        content=b'{"BTC": 0.000000000000000001}', headers={"content-type": "application/json"}
    )
    respx_mock.get(f"{SHOP}/api/v1/tickers").respond(
        json={"retCode": 0, "retMsg": "OK", "result": [{"symbol": "BTCUSDT", "lastPrice": "1"}]}
    )
    shop = client()

    assert shop.orders.documents.receipt("o", format="PDF") == b"%PDF"
    assert shop.profile.upload_avatar(file=("a.png", b"img", "image/png")) == "ok"
    assert b'filename="a.png"' in upload.calls.last.request.content
    assert shop.balances.get() == {"BTC": Decimal("0.000000000000000001")}
    assert shop.tickers.list() == [Ticker(symbol="BTCUSDT", last_price="1")]


def test_path_parameter_and_lenient_model(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{SHOP}/api/v1/products/7").respond(
        json={"id": 7, "name": "n", "createdAt": 1714566600000, "releaseDate": "2024-05-01", "x": 1}
    )

    product = client().products.get(7)

    assert product.created_at is not None and product.created_at.year == 2024
    assert product.model_extra == {"x": 1}


def test_deprecated_operation_warns(respx_mock: respx.MockRouter) -> None:
    respx_mock.delete(f"{SHOP}/api/v1/products/7").respond(204)

    with pytest.warns(DeprecationWarning, match="products.delete"):
        client().products.delete(7)


def test_event_stream(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{SHOP}/api/v1/events").respond(
        content=b'event: update\\nid: 1\\ndata: {"type": "t", "payload": {"a": 1}}\\n\\n',
        headers={"content-type": "text/event-stream"},
    )

    with client().events.stream() as events:
        event = next(events)

    assert event == ServerSentEvent(Event(type="t", payload={"a": 1}), "update", "1")


def test_async_client(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{SHOP}/api/v1/products").respond(
        json={"content": [{"id": 1, "name": "n"}], "totalElements": 1, "totalPages": 1}
    )
    respx_mock.get(f"{SHOP}/api/v1/products/1").respond(json={"id": 1, "name": "n"})

    async def run() -> tuple[list[Product], Product]:
        async with AsyncWhitebird(token_store=MemoryTokenStore(Tokens("token"))) as shop:
            return [p async for p in shop.products.list()], await shop.products.get(1)

    products, product = asyncio.run(run())

    assert products == [product]
"""


@pytest.fixture(scope="module")
def files() -> dict[str, str]:
    return emitter.emit(load(FIXTURE))


@pytest.fixture(scope="module")
def package(files: dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not INTERPRETER.exists():
        pytest.skip("python/.venv is missing: run `uv sync` in python/")
    source = tmp_path_factory.mktemp("sdk") / "src"
    shutil.copytree(
        PROJECT / "src" / "whitebird",
        source / "whitebird",
        ignore=shutil.ignore_patterns("_generated", "__pycache__"),
    )
    for path, content in files.items():
        target = source / Path(path).relative_to("python/src")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return source


def run(*args: str | Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(cwd), "MYPYPATH": str(cwd)}
    return subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def test_files_are_owned_and_marked(files: dict[str, str]) -> None:
    assert {path.removeprefix(f"{emitter.PACKAGE}/") for path in files} == {
        "__init__.py",
        "models.py",
        "services.py",
        "resources/__init__.py",
        "resources/balances.py",
        "resources/events.py",
        "resources/orders.py",
        "resources/products.py",
        "resources/profile.py",
        "resources/tickers.py",
    }
    for path, content in files.items():
        assert any(fnmatch.fnmatch(path, pattern) for pattern in emitter.OWNED)
        assert content.startswith(f"# {_util.HEADER}\n")


def test_output_is_deterministic(files: dict[str, str]) -> None:
    assert emitter.emit(load(FIXTURE)) == files


def test_generated_code_reads_like_the_spec(files: dict[str, str]) -> None:
    models = files[f"{emitter.PACKAGE}/models.py"]
    assert "class Priority(IntEnum):" in models
    assert '    IN_PROGRESS = "in-progress"' in models
    assert "    category: Category | str | None = None\n" in models
    assert "    discount: float | None = None\n" in models
    assert "    legacy_code: str | None = Field(default=None, deprecated=True)\n" in models
    assert '"""Seen in the web app only. Inferred from the web application' in models
    products = files[f"{emitter.PACKAGE}/resources/products.py"]
    assert "    ) -> SyncPager[Product]:" in products
    assert "@deprecated(" in products
    tickers = files[f"{emitter.PACKAGE}/resources/tickers.py"]
    assert "    ) -> builtins.list[Ticker]:" in tickers
    assert "cast_to=list[Ticker]," in tickers


def test_generated_package_type_checks(package: Path) -> None:
    result = run(
        INTERPRETER,
        "-m",
        "mypy",
        "--config-file",
        PROJECT / "pyproject.toml",
        "--no-incremental",
        "-p",
        "whitebird",
        cwd=package,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_package_lints(package: Path) -> None:
    result = run(
        INTERPRETER.parent / "ruff",
        "check",
        "--config",
        PROJECT / "pyproject.toml",
        package / "whitebird" / "_generated",
        cwd=package,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_package_works(package: Path) -> None:
    tests = package.parent / "test_fixture_sdk.py"
    tests.write_text(textwrap.dedent(BEHAVIOUR))
    plugins = ["-p", "no:cacheprovider", "-p", "no:asyncio"]
    result = run(INTERPRETER, "-m", "pytest", "-q", *plugins, "-W", "error", tests, cwd=package)
    assert result.returncode == 0, result.stdout + result.stderr
