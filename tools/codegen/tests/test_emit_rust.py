import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from whitebird_codegen.build import load
from whitebird_codegen.emitters import rust
from whitebird_codegen.emitters._util import HEADER
from whitebird_codegen.ir import IR

FIXTURE = Path(__file__).parent / "fixtures" / "spec"
CRATE = Path(__file__).resolve().parents[3] / "rust"


@pytest.fixture(scope="module")
def ir() -> IR:
    return load(FIXTURE)


@pytest.fixture(scope="module")
def files(ir: IR) -> dict[str, str]:
    if shutil.which("rustfmt") is None:
        pytest.skip("rustfmt is not installed")
    return rust.emit(ir)


def test_files_are_owned_and_marked(files: dict[str, str]) -> None:
    assert sorted(files) == [
        f"rust/src/generated/{name}.rs"
        for name in ("blocking", "mod", "models", "params", "resources", "services")
    ]
    for path, content in files.items():
        assert any(fnmatch.fnmatch(path, pattern) for pattern in rust.OWNED)
        assert content.startswith(f"// {HEADER}\n")


def test_output_is_deterministic(ir: IR, files: dict[str, str]) -> None:
    assert rust.emit(ir) == files


def test_names() -> None:
    assert rust.ident("type") == "r#type"
    assert rust.ident("self") == "self_"
    assert rust.ident("2fa") == "n2fa"
    assert rust.ident("order_id") == "order_id"
    assert rust.type_name("Result") == "ResultModel"
    assert rust.resource_type(("brokerage", "otc_orders")) == "BrokerageOtcOrders"
    assert rust.accessor_chain(("brokerage", "otc_orders")) == ".brokerage().otc_orders()"
    assert rust.accessor_chain(("request", "login")) == ".request_resource().login()"


def test_doc_lines_escape_markdown() -> None:
    assert rust.doc_lines("Returns List<Order> [paged]", "See https://example.test/docs.") == [
        r"Returns List\<Order\> \[paged\]",
        "",
        "See <https://example.test/docs>.",
    ]


def test_docs_are_reflowed_without_breaking_lists() -> None:
    lines = rust.doc_lines("Messages are\ndispatched on `type`:\n\n- `A`: first\n  kind\n2. second")
    assert lines == ["Messages are dispatched on `type`:", "", "- `A`: first kind", "2. second"]
    item = "- " + " ".join(["word"] * 30)
    assert rust.comment([item]) == [
        "/// - " + " ".join(["word"] * 17),
        "///   " + " ".join(["word"] * 13),
    ]
    assert rust.comment(["x" * 87 + " - y"]) == ["/// " + "x" * 87 + " - y"]


def test_operations(files: dict[str, str]) -> None:
    resources = files["rust/src/generated/resources.rs"]
    assert (
        "    #[deprecated]\n    pub async fn delete(&self, product_id: i64) -> Result<()> {"
        in resources
    )
    assert "**Experimental:** the endpoint may change without notice." in resources
    assert "pub fn search(&self, params: OrdersSearchParams) -> Paginator<Order> {" in resources
    assert '.paginate(PageParam::Body("page"))' in resources
    assert '.paginate(PageParam::Query("page"))' in resources
    assert ".public()\n            .envelope(Envelope::Retcode)" in resources
    assert "pub fn stream(&self) -> EventStream<Event> {" in resources
    assert "Needs an operation MFA token for `WITHDRAWAL`" in resources
    assert '.mfa_operation("WITHDRAWAL")\n            .form(params)' in resources
    assert (
        ".request_path(\n                Service::Shop,\n                Method::POST,\n"
        '                "/api/v1/orders/{}/withdraw",\n                &[&order_id],\n'
        "            )\n"
    ) in resources
    # Path parameters are encoded by the runtime, never pasted into the path.
    assert '"/api/v1/orders/{orderId}' not in resources
    # A page response without page parameters is a single call, not a paginator.
    assert "pub async fn featured(&self) -> Result<Page<Product>> {" in resources


def test_defaults_are_sent(files: dict[str, str]) -> None:
    resources = files["rust/src/generated/resources.rs"]
    params = files["rust/src/generated/params.rs"]
    assert (
        '.query("labels[]", &params.labels)\n'
        '            .query("sort", &params.sort)\n'
        '            .default_query("page", 0)\n'
        '            .default_query("size", 20)\n'
        '            .default_query("sort", "name,asc")\n'
    ) in resources
    assert '.json(params)\n            .default_field("channel", "web")\n' in resources
    assert '    /// Defaults to `"name,asc"`.\n    pub sort: Option<String>,' in params
    assert "    pub labels: Option<Vec<String>>," in params


def test_session_hooks(files: dict[str, str]) -> None:
    resources = files["rust/src/generated/resources.rs"]
    assert '"/api/v1/session/logout")\n            .ends_session()\n' in resources
    assert ".json(params)\n            .replaces_session()\n" in resources


def test_request_bodies(files: dict[str, str]) -> None:
    resources = files["rust/src/generated/resources.rs"]
    assert (
        '.request(Service::Shop, Method::POST, "/api/v1/cart/clear")\n'
        "            .json(Map::new())\n"
    ) in resources
    # Files keep their name and media type; a text/plain part carries the JSON as text.
    assert (
        '.files("file", params.file)\n'
        '                    .json("crop", &params.crop)\n'
        '                    .json_text("caption", &params.caption),'
    ) in resources
    params = files["rust/src/generated/params.rs"]
    assert "#[builder(on(File, into))]" in params
    assert "    pub file: Option<File>," in params
    assert "# Errors" in resources


def test_models_are_tolerant(files: dict[str, str]) -> None:
    models = files["rust/src/generated/models.rs"]
    assert "string_enum! {" in models
    assert "_ => Unknown," in models
    assert "deny_unknown_fields" not in models


def test_blocking_mirrors_async(files: dict[str, str]) -> None:
    blocking = files["rust/src/generated/blocking.rs"]
    assert (
        "    #[deprecated]\n    #[allow(deprecated)]\n"
        "    pub fn delete(&self, product_id: i64) -> Result<()> {\n"
        "        self.client\n"
        "            .block_on(self.client.as_async().products().delete(product_id))\n"
    ) in blocking
    assert "Paginator::new(self.client, self.client.as_async().orders().search(params))" in blocking
    assert "EventStream::new(self.client, self.client.as_async().events().stream())" in blocking


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_fixture_crate_compiles(files: dict[str, str], tmp_path: Path) -> None:
    crate = tmp_path / "rust"
    shutil.copytree(
        CRATE,
        crate,
        ignore=shutil.ignore_patterns("target", "generated", "tests", "examples"),
    )
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    env = {
        **os.environ,
        "CARGO_TARGET_DIR": str(CRATE / "target" / "codegen-check"),
        "RUSTFLAGS": "-D warnings",
    }
    result = subprocess.run(
        ["cargo", "clippy", "--all-features", "--lib", "--quiet"],
        cwd=crate,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
