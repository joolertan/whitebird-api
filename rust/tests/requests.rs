mod common;

use std::time::Duration;

use common::*;
use serde_json::{Value, json};
use whitebird::models::OtcOrderStatus;
use whitebird::params::BrokerageOtcOrdersCreateParams;
use whitebird::{
    Client, Decimal, Envelope, Error, ErrorKind, Method, Multipart, RequestOptions, Service,
};
use wiremock::matchers::{body_json, header, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn unavailable() -> ResponseTemplate {
    ResponseTemplate::new(503).insert_header("retry-after", "0")
}

fn client(server: &MockServer, max_retries: u32) -> Client {
    builder(server)
        .max_retries(max_retries)
        .credentials("me@example.com", "secret")
        .token_store(whitebird::MemoryTokenStore::from(whitebird::Tokens::new(
            jwt("me", 3600),
            None,
        )))
        .build()
        .unwrap()
}

fn create_order() -> BrokerageOtcOrdersCreateParams {
    BrokerageOtcOrdersCreateParams::builder()
        .subaccount_id("sub-1")
        .from_asset("USDT")
        .from_amount("100.50".parse().unwrap())
        .to_asset("BTC")
        .exchange_rate_pair("BTC/USDT")
        .exchange_rate_value("65000.123456789012345678".parse().unwrap())
        .build()
}

async fn get_public(server: &MockServer, answer: ResponseTemplate) -> Error {
    Mock::given(path("/api/v1/thing"))
        .respond_with(answer)
        .mount(server)
        .await;
    builder(server)
        .build()
        .unwrap()
        .request(Service::Brokerage, Method::GET, "/api/v1/thing")
        .public()
        .send()
        .await
        .unwrap_err()
}

#[tokio::test]
async fn retries_idempotent_requests_honouring_retry_after() {
    let server = MockServer::start().await;
    Mock::given(path(ME_PATH))
        .respond_with(unavailable())
        .up_to_n_times(2)
        .with_priority(1)
        .mount(&server)
        .await;
    Mock::given(path(ME_PATH))
        .respond_with(ResponseTemplate::new(200).set_body_json(subaccount("main")))
        .mount(&server)
        .await;

    let me = client(&server, 2)
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap();

    assert_eq!(me.name, None);
    assert_eq!(server.received_requests().await.unwrap().len(), 3);
}

#[tokio::test]
async fn stops_after_max_retries() {
    let server = MockServer::start().await;
    Mock::given(path(ME_PATH))
        .respond_with(unavailable().set_body_json(json!({ "message": "Maintenance" })))
        .expect(2)
        .mount(&server)
        .await;

    let error = client(&server, 1)
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::Server));
    assert_eq!(error.api().unwrap().message(), "Maintenance");
}

#[tokio::test]
async fn never_retries_post_unless_marked_idempotent() {
    let server = MockServer::start().await;
    Mock::given(path("/api/v1/brokerage/otc-order"))
        .respond_with(unavailable())
        .mount(&server)
        .await;
    let client = client(&server, 2);

    let error = client
        .brokerage()
        .otc_orders()
        .create(create_order())
        .await
        .unwrap_err();
    assert_eq!(error.kind(), Some(ErrorKind::Server));
    assert_eq!(server.received_requests().await.unwrap().len(), 1);

    client
        .with_options(RequestOptions::new().idempotent(true))
        .brokerage()
        .otc_orders()
        .create(create_order())
        .await
        .unwrap_err();
    assert_eq!(server.received_requests().await.unwrap().len(), 4);
}

#[tokio::test]
async fn does_not_wait_out_a_long_retry_after() {
    let server = MockServer::start().await;
    Mock::given(path(ME_PATH))
        .respond_with(ResponseTemplate::new(429).insert_header("retry-after", "120"))
        .expect(1)
        .mount(&server)
        .await;

    let error = client(&server, 2)
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap_err();

    assert!(error.is_rate_limited());
    assert_eq!(
        error.api().unwrap().retry_after(),
        Some(Duration::from_secs(120))
    );
}

#[tokio::test]
async fn classifies_every_error_kind() {
    let cases = [
        (
            400,
            json!({ "status": "BAD_REQUEST", "message": "Invalid amount" }),
            ErrorKind::BadRequest,
        ),
        (
            400,
            json!({ "status": "SMS_VERIFICATION_REQUIRED", "message": "Confirm by SMS" }),
            ErrorKind::VerificationRequired,
        ),
        (
            400,
            json!({ "status": "LIVENESS_VERIFICATION_REQUIRED", "message": "Selfie" }),
            ErrorKind::VerificationRequired,
        ),
        (
            400,
            json!({ "status": "MFA_CHECKING_REQUIRED", "message": "Enter code" }),
            ErrorKind::MfaRequired,
        ),
        (
            403,
            json!({ "message": "Operation denied, requires mfa checking" }),
            ErrorKind::MfaRequired,
        ),
        (
            401,
            json!({ "error": "unauthorized", "error_description": "Full authentication is required" }),
            ErrorKind::Authentication,
        ),
        (
            403,
            json!({ "status": "FORBIDDEN", "message": "Access denied" }),
            ErrorKind::PermissionDenied,
        ),
        (
            404,
            json!({ "status": "NOT_FOUND", "message": "Order not found" }),
            ErrorKind::NotFound,
        ),
        (
            409,
            json!({ "status": "CONFLICT", "message": "Already exists" }),
            ErrorKind::Conflict,
        ),
        (
            429,
            json!({ "message": "Slow down" }),
            ErrorKind::RateLimited,
        ),
        (
            500,
            json!({ "message": "Internal error" }),
            ErrorKind::Server,
        ),
        (418, json!({ "message": "Teapot" }), ErrorKind::Other),
    ];
    for (status, body, kind) in cases {
        let server = MockServer::start().await;
        let error = get_public(&server, ResponseTemplate::new(status).set_body_json(&body)).await;

        let api = error.api().unwrap();
        assert_eq!(api.kind(), kind, "{body}");
        assert_eq!(api.status().as_u16(), status);
        assert_eq!(api.body(), body.to_string());
    }
}

#[tokio::test]
async fn exposes_the_service_error_body() {
    let server = MockServer::start().await;
    let body = json!({
        "code": 4001,
        "status": "SMS_VERIFICATION_REQUIRED",
        "message": "Confirm the operation by SMS",
        "fields": [{ "field": "amount", "message": "too small" }],
    });

    let error = get_public(&server, ResponseTemplate::new(400).set_body_json(&body)).await;

    assert!(error.is_verification_required());
    let api = error.api().unwrap();
    assert_eq!(api.code(), Some(4001));
    assert_eq!(api.error_code(), Some("SMS_VERIFICATION_REQUIRED"));
    assert_eq!(api.message(), "Confirm the operation by SMS");
    assert_eq!(
        api.fields(),
        [json!({ "field": "amount", "message": "too small" })]
    );
    assert_eq!(
        api.to_string(),
        "400 Bad Request SMS_VERIFICATION_REQUIRED: Confirm the operation by SMS"
    );
}

#[tokio::test]
async fn non_json_errors_fall_back_to_the_status() {
    let server = MockServer::start().await;

    let error = get_public(
        &server,
        ResponseTemplate::new(502).set_body_string("<html>Bad gateway</html>"),
    )
    .await;

    let api = error.api().unwrap();
    assert_eq!(api.kind(), ErrorKind::Server);
    assert_eq!(api.message(), "Bad Gateway");
    assert_eq!(api.body(), "<html>Bad gateway</html>");
}

#[tokio::test]
async fn unwraps_the_retcode_envelope() {
    let server = MockServer::start().await;
    Mock::given(path("/v5/market/tickers"))
        .and(query_param("symbol", "BTCUSDT"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "retCode": 0,
            "retMsg": "OK",
            "result": { "list": [{ "symbol": "BTCUSDT", "lastPrice": "65000.5" }] },
            "time": 1_714_554_000_000_i64,
        })))
        .mount(&server)
        .await;
    Mock::given(path("/v5/market/broken"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({ "retCode": 10001, "retMsg": "params error", "result": {} })),
        )
        .mount(&server)
        .await;
    let client = builder(&server)
        .base_url(Service::Exchange, server.uri())
        .build()
        .unwrap();
    let market = |path: &'static str| {
        client
            .request(Service::Exchange, Method::GET, path)
            .public()
            .envelope(Envelope::Retcode)
    };

    let result: Value = market("/v5/market/tickers")
        .query("symbol", "BTCUSDT")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let error = market("/v5/market/broken")
        .send()
        .await
        .unwrap()
        .json::<Value>()
        .await
        .unwrap_err();

    assert_eq!(result["list"][0]["lastPrice"], "65000.5");
    let api = error.api().unwrap();
    assert_eq!(api.kind(), ErrorKind::Other);
    assert_eq!(api.status().as_u16(), 200);
    assert_eq!(api.code(), Some(10001));
    assert_eq!(api.message(), "params error");
}

#[tokio::test]
async fn escape_hatch_sends_query_headers_and_json_with_auth() {
    let server = MockServer::start().await;
    let access = jwt("me", 3600);
    Mock::given(method("POST"))
        .and(path("/api/v2/brokerage/new-endpoint"))
        .and(query_param("limit", "5"))
        .and(header("x-request-source", "sdk"))
        .and(header("authorization", format!("Bearer {access}")))
        .and(body_json(json!({ "assets": ["BTC", "ETH"] })))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({ "id": 7 })))
        .expect(1)
        .mount(&server)
        .await;

    let response = signed_in(&server, &access)
        .request(
            Service::Brokerage,
            Method::POST,
            "/api/v2/brokerage/new-endpoint",
        )
        .query("limit", 5)
        .query("tag", ["a", "b"])
        .query("cursor", None::<String>)
        .header("x-request-source", "sdk")
        .json(json!({ "assets": ["BTC", "ETH"] }))
        .send()
        .await
        .unwrap();

    assert_eq!(response.status().as_u16(), 201);
    assert_eq!(response.json::<Value>().await.unwrap(), json!({ "id": 7 }));
    let requests = server.received_requests().await.unwrap();
    assert_eq!(requests[0].url.query(), Some("limit=5&tag=a&tag=b"));
}

#[tokio::test]
async fn escape_hatch_sends_forms_and_multipart() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;
    let client = signed_in(&server, &jwt("me", 3600));

    client
        .request(Service::Brokerage, Method::POST, "/form")
        .form(json!({ "name": "main", "limit": 10, "skip": null }))
        .send()
        .await
        .unwrap();
    client
        .request(Service::Brokerage, Method::POST, "/upload")
        .multipart(
            Multipart::new()
                .text("documentType", "PASSPORT")
                .json("request", json!({ "criteria": ["certifiedExpert"] }))
                .file("file", b"%PDF".to_vec()),
        )
        .send()
        .await
        .unwrap();

    let requests = server.received_requests().await.unwrap();
    assert_eq!(
        form(&requests[0]),
        [
            ("limit".into(), "10".into()),
            ("name".into(), "main".into())
        ]
    );
    let content_type = requests[1].headers["content-type"].to_str().unwrap();
    assert!(content_type.starts_with("multipart/form-data; boundary="));
    let body = String::from_utf8_lossy(&requests[1].body);
    assert!(body.contains("name=\"documentType\"\r\n\r\nPASSPORT"));
    assert!(body.contains(
        "name=\"request\"\r\nContent-Type: application/json\r\n\r\n{\"criteria\":[\"certifiedExpert\"]}"
    ));
    assert!(body.contains("name=\"file\"; filename=\"file\""));
}

#[tokio::test]
async fn generated_methods_encode_params_and_tolerate_new_fields() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/v1/brokerage/otc-order"))
        .and(body_json(json!({
            "subaccountId": "sub-1",
            "fromAsset": "USDT",
            "fromAmount": "100.50",
            "toAsset": "BTC",
            "exchangeRatePair": "BTC/USDT",
            "exchangeRateValue": "65000.123456789012345678",
        })))
        .respond_with(ResponseTemplate::new(200).set_body_raw(
            r#"{
                "uuid": "order-1",
                "fromAmount": 100.50,
                "toAmount": 0.001546151234567890123,
                "status": "PARTIALLY_FILLED",
                "creationDate": "2024-05-01T09:00:00.123",
                "completionDate": null,
                "somethingNew": { "nested": true }
            }"#,
            "application/json",
        ))
        .mount(&server)
        .await;

    let order = signed_in(&server, &jwt("me", 3600))
        .brokerage()
        .otc_orders()
        .create(create_order())
        .await
        .unwrap();

    assert_eq!(order.uuid.as_deref(), Some("order-1"));
    assert_eq!(
        order.from_amount,
        Some("100.50".parse::<Decimal>().unwrap())
    );
    assert_eq!(
        order.to_amount.unwrap().to_string(),
        "0.001546151234567890123"
    );
    assert_eq!(
        order.status,
        Some(OtcOrderStatus::Unknown("PARTIALLY_FILLED".into()))
    );
    assert_eq!(
        order.creation_date.unwrap().to_string(),
        "2024-05-01T09:00:00.123Z"
    );
    assert_eq!(order.completion_date, None);
}

#[tokio::test]
async fn identifies_itself() {
    let server = MockServer::start().await;
    Mock::given(header(
        "user-agent",
        concat!("whitebird-api-rust/", env!("CARGO_PKG_VERSION")),
    ))
    .respond_with(ResponseTemplate::new(200).set_body_json(subaccount("main")))
    .expect(1)
    .mount(&server)
    .await;

    signed_in(&server, &jwt("me", 3600))
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap();
}

#[tokio::test]
async fn reports_timeouts_transport_and_decode_errors() {
    let server = MockServer::start().await;
    Mock::given(path("/slow"))
        .respond_with(ResponseTemplate::new(200).set_delay(Duration::from_secs(5)))
        .mount(&server)
        .await;
    Mock::given(path(ME_PATH))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "uuid": 42 })))
        .mount(&server)
        .await;
    let client = signed_in(&server, &jwt("me", 3600));

    let timeout = client
        .request(Service::Brokerage, Method::GET, "/slow")
        .options(RequestOptions::new().timeout(Duration::from_millis(50)))
        .send()
        .await
        .unwrap_err();
    let decode = client.brokerage().subaccounts().me().await.unwrap_err();
    let transport = Client::builder()
        .base_url(Service::Brokerage, "http://127.0.0.1:9")
        .max_retries(0)
        .build()
        .unwrap()
        .request(Service::Brokerage, Method::GET, "/")
        .public()
        .send()
        .await
        .unwrap_err();

    assert!(timeout.is_timeout(), "{timeout:?}");
    assert!(
        matches!(&decode, Error::Decode { body, .. } if body == r#"{"uuid":42}"#),
        "{decode:?}"
    );
    assert!(matches!(transport, Error::Transport(_)), "{transport:?}");
}

#[tokio::test]
async fn rejects_invalid_configuration() {
    let error = Client::builder()
        .base_url(Service::Brokerage, "not a url")
        .build()
        .unwrap_err();
    assert!(matches!(error, Error::Config(_)), "{error:?}");
}
