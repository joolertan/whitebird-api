from __future__ import annotations

from whitebird_codegen import importer

SOURCE = {
    "info": {"title": "Shop", "version": "1"},
    "paths": {
        "/api/v1/orders": {
            "post": {
                "operationId": "createOrder",
                "tags": ["Order API"],
                "summary": "Create order",
                "requestBody": {
                    "content": {"*/*": {"schema": {"$ref": "#/components/schemas/Order"}}}
                },
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
    "components": {
        "schemas": {
            "Order": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "minimum": 0, "exclusiveMinimum": True},
                    "note": {"type": "string", "description": "Upstream text."},
                    "limit": {"type": "integer", "maximum": 10, "exclusiveMaximum": False},
                },
            }
        }
    },
}

EXISTING = {
    "paths": {
        "/api/v1/orders": {
            "post": {
                "operationId": "shopPlaceOrder",
                "summary": "Place an order",
                "description": "Hand-written notes.",
                "x-wb-resource": "shop.orders",
                "x-wb-name": "place",
                "x-wb-session": "end",
            }
        }
    },
    "components": {
        "schemas": {
            "Order": {
                "properties": {"note": {"type": "string", "description": "Shown to the buyer."}}
            }
        }
    },
}


def test_reimport_keeps_hand_written_annotations() -> None:
    document = importer.convert(SOURCE, "shop", "https://shop.example", EXISTING)

    operation = document["paths"]["/api/v1/orders"]["post"]
    assert operation["operationId"] == "shopPlaceOrder"
    assert operation["summary"] == "Place an order"
    assert operation["description"] == "Hand-written notes."
    assert operation["x-wb-name"] == "place"
    assert operation["x-wb-session"] == "end"
    assert "application/json" in operation["requestBody"]["content"]
    properties = document["components"]["schemas"]["Order"]["properties"]
    assert properties["note"]["description"] == "Shown to the buyer."


def test_first_import_takes_upstream_texts() -> None:
    document = importer.convert(SOURCE, "shop", "https://shop.example", None)

    operation = document["paths"]["/api/v1/orders"]["post"]
    assert operation["operationId"] == "shopCreateOrder"
    assert operation["summary"] == "Create order"
    assert operation["x-wb-resource"] == "shop.orders"
    properties = document["components"]["schemas"]["Order"]["properties"]
    assert properties["note"]["description"] == "Upstream text."


def test_exclusive_bounds_use_the_openapi_3_1_form() -> None:
    document = importer.convert(SOURCE, "shop", "https://shop.example", None)

    properties = document["components"]["schemas"]["Order"]["properties"]
    assert properties["amount"] == {"type": "number", "exclusiveMinimum": 0}
    assert properties["limit"] == {"type": "integer", "maximum": 10}
