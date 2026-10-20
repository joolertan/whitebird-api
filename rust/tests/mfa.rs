mod common;

use common::*;
use serde_json::json;
use whitebird::models::BalanceAccountType;
use whitebird::params::BalanceWithdrawalsCreateCryptoParams;
use whitebird::{Client, CodeProvider, Error, MemoryTokenStore, Tokens};
use wiremock::matchers::{body_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const WITHDRAWAL_PATH: &str = "/api/v2/exchange/client/balance/crypto/withdrawal";

fn client(server: &MockServer, otp: Option<&str>) -> Client {
    let mut builder =
        builder(server).token_store(MemoryTokenStore::from(Tokens::new(jwt("me", 3600), None)));
    if let Some(code) = otp {
        builder = builder.otp(code);
    }
    builder.build().unwrap()
}

fn withdrawal() -> BalanceWithdrawalsCreateCryptoParams {
    BalanceWithdrawalsCreateCryptoParams::builder()
        .account_type(BalanceAccountType::Wallet)
        .calculation_id("calc-1")
        .build()
}

fn mfa_denied() -> ResponseTemplate {
    ResponseTemplate::new(403).set_body_json(json!({
        "status": "FORBIDDEN",
        "message": "Operation denied, requires mfa checking",
    }))
}

async fn issue_token(server: &MockServer, answer: ResponseTemplate) {
    Mock::given(method("POST"))
        .and(path(MFA_TOKEN_PATH))
        .respond_with(answer)
        .mount(server)
        .await;
}

#[tokio::test]
async fn confirm_operation_stores_the_issued_token() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(MFA_TOKEN_PATH))
        .and(header(
            "authorization",
            format!("Bearer {}", jwt("me", 3600)),
        ))
        .and(body_json(
            json!({ "operation": "WITHDRAWAL", "mfaCode": "123456" }),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "mfaToken": "mfa-1" })))
        .expect(1)
        .mount(&server)
        .await;
    let client = client(&server, None);

    let token = client
        .confirm_operation("WITHDRAWAL", Some("123456"))
        .await
        .unwrap();

    assert_eq!(token, "mfa-1");
    assert_eq!(client.mfa_token().as_deref(), Some("mfa-1"));
}

#[tokio::test]
async fn confirm_operation_asks_the_code_provider() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(MFA_TOKEN_PATH))
        .and(body_json(
            json!({ "operation": "WITHDRAWAL", "mfaCode": "135790" }),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "mfaToken": "mfa-1" })))
        .expect(1)
        .mount(&server)
        .await;
    let client = builder(&server)
        .token_store(MemoryTokenStore::from(Tokens::new(jwt("me", 3600), None)))
        .otp(CodeProvider::new(|| async { Ok("135790".to_owned()) }))
        .build()
        .unwrap();

    let token = client.confirm_operation("WITHDRAWAL", None).await.unwrap();

    assert_eq!(token, "mfa-1");
}

#[tokio::test]
async fn reads_the_token_wherever_the_server_puts_it() {
    let answers = [
        ResponseTemplate::new(200).set_body_json("mfa-json"),
        ResponseTemplate::new(200).set_body_string("mfa-text\n"),
        ResponseTemplate::new(200)
            .insert_header("x-mfa-token", "mfa-header")
            .set_body_json(json!({})),
    ];
    for (answer, expected) in answers
        .into_iter()
        .zip(["mfa-json", "mfa-text", "mfa-header"])
    {
        let server = MockServer::start().await;
        issue_token(&server, answer).await;

        let token = client(&server, Some("123456"))
            .confirm_operation("WITHDRAWAL", None)
            .await
            .unwrap();

        assert_eq!(token, expected);
    }
}

#[tokio::test]
async fn confirm_operation_needs_a_code() {
    let server = MockServer::start().await;
    issue_token(&server, ResponseTemplate::new(200).set_body_json(json!({}))).await;
    let client = client(&server, None);

    let missing = client
        .confirm_operation("WITHDRAWAL", None)
        .await
        .unwrap_err();
    let empty = client
        .confirm_operation("WITHDRAWAL", Some("123456"))
        .await
        .unwrap_err();

    assert!(matches!(missing, Error::Config(_)), "{missing:?}");
    assert_eq!(
        empty.api().unwrap().message(),
        "the response carries no MFA token"
    );
}

#[tokio::test]
async fn obtains_a_token_before_the_first_sensitive_operation() {
    let server = MockServer::start().await;
    Mock::given(path(MFA_TOKEN_PATH))
        .and(body_json(
            json!({ "operation": "WITHDRAWAL", "mfaCode": "654321" }),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "mfaToken": "mfa-1" })))
        .expect(1)
        .mount(&server)
        .await;
    Mock::given(path(WITHDRAWAL_PATH))
        .and(header("x-mfa-token", "mfa-1"))
        .and(body_json(
            json!({ "accountType": "WALLET", "calculationId": "calc-1" }),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "id": 1 })))
        .expect(2)
        .mount(&server)
        .await;
    let client = client(&server, Some("654321"));

    let withdrawals = client.balance().withdrawals();
    withdrawals.create_crypto(withdrawal()).await.unwrap();
    withdrawals.create_crypto(withdrawal()).await.unwrap();
}

async fn withdraw_with_stale_token(
    server: &MockServer,
    fresh: ResponseTemplate,
) -> Result<(), Error> {
    Mock::given(method("POST"))
        .and(path(MFA_TOKEN_PATH))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "mfaToken": "fresh" })))
        .expect(1)
        .mount(server)
        .await;
    Mock::given(path(WITHDRAWAL_PATH))
        .and(header("x-mfa-token", "stale"))
        .respond_with(mfa_denied())
        .expect(1)
        .mount(server)
        .await;
    Mock::given(path(WITHDRAWAL_PATH))
        .and(header("x-mfa-token", "fresh"))
        .respond_with(fresh)
        .expect(1)
        .mount(server)
        .await;
    let client = client(server, Some("654321"));
    client.set_mfa_token("stale");

    let result = client
        .balance()
        .withdrawals()
        .create_crypto(withdrawal())
        .await;
    assert_eq!(client.mfa_token().as_deref(), Some("fresh"));
    result.map(drop)
}

#[tokio::test]
async fn renews_a_refused_token() {
    let server = MockServer::start().await;
    let fresh = ResponseTemplate::new(200).set_body_json(json!({ "id": 1 }));

    withdraw_with_stale_token(&server, fresh).await.unwrap();
}

#[tokio::test]
async fn renews_a_refused_token_only_once() {
    let server = MockServer::start().await;

    let error = withdraw_with_stale_token(&server, mfa_denied())
        .await
        .unwrap_err();

    assert!(error.is_mfa_required(), "{error:?}");
}

#[tokio::test]
async fn without_a_code_provider_the_refusal_propagates() {
    let server = MockServer::start().await;
    issue_token(&server, ResponseTemplate::new(500)).await;
    Mock::given(path(WITHDRAWAL_PATH))
        .respond_with(mfa_denied())
        .expect(1)
        .mount(&server)
        .await;

    let error = client(&server, None)
        .balance()
        .withdrawals()
        .create_crypto(withdrawal())
        .await
        .unwrap_err();

    assert!(error.is_mfa_required(), "{error:?}");
    let requests = server.received_requests().await.unwrap();
    assert!(requests.iter().all(|r| r.url.path() != MFA_TOKEN_PATH));
}
