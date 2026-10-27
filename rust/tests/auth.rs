mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::*;
use serde_json::json;
use whitebird::{CodeProvider, Error, ErrorKind, Method, Service, TokenStore, Tokens};
use wiremock::matchers::{body_json, body_string_contains, header, method, path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

async fn me_responds(server: &MockServer, token: &str) {
    Mock::given(method("GET"))
        .and(path(ME_PATH))
        .and(header("authorization", format!("Bearer {token}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(subaccount("main")))
        .mount(server)
        .await;
}

#[tokio::test]
async fn logs_in_lazily_with_lowercased_username() {
    let server = MockServer::start().await;
    let access = jwt("me", 3600);
    Mock::given(method("POST"))
        .and(path(MFA_PATH))
        .and(body_json(json!({ "username": "trader@example.com" })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "mfaEnabled": true })))
        .expect(1)
        .mount(&server)
        .await;
    password_grant()
        .and(header(
            "authorization",
            "Basic YXV0aENsaWVudElkOlhZN2ttem9OemwxMDA=",
        ))
        .respond_with(grant(&access, "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;
    me_responds(&server, &access).await;

    let client = builder(&server)
        .credentials("Trader@Example.com", "secret")
        .otp("123456")
        .build()
        .unwrap();
    let me = client.brokerage().subaccounts().me().await.unwrap();
    client.brokerage().subaccounts().me().await.unwrap();

    assert_eq!(me.display_name.as_deref(), Some("main"));
    let requests = server.received_requests().await.unwrap();
    let login = requests
        .iter()
        .find(|r| r.url.path() == TOKEN_PATH)
        .unwrap();
    assert_eq!(
        form(login),
        [
            ("grant_type".into(), "password".into()),
            ("username".into(), "trader@example.com".into()),
            ("password".into(), "secret".into()),
            ("code".into(), "123456".into()),
        ]
    );
}

#[tokio::test]
async fn sends_the_one_time_code_when_mfa_is_on() {
    let server = MockServer::start().await;
    mfa_check(true).expect(1).mount(&server).await;
    password_grant()
        .and(body_string_contains("code=123456"))
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;

    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .build()
        .unwrap();
    let tokens = client.login().await.unwrap();

    assert_eq!(tokens.refresh_token.as_deref(), Some("refresh-1"));
    assert!(tokens.expires_at.is_some());
}

#[tokio::test]
async fn asks_the_code_provider_on_every_login() {
    let server = MockServer::start().await;
    password_grant()
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .mount(&server)
        .await;
    let calls = Arc::new(Mutex::new(0));
    let counter = Arc::clone(&calls);
    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .otp(CodeProvider::from_fn(move || {
            *counter.lock().unwrap() += 1;
            Ok("654321".into())
        }))
        .build()
        .unwrap();

    client.login().await.unwrap();
    client.login().await.unwrap();

    assert_eq!(*calls.lock().unwrap(), 2);
}

#[tokio::test]
async fn awaits_an_async_code_provider() {
    let server = MockServer::start().await;
    password_grant()
        .and(body_string_contains("code=246810"))
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;
    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .otp(CodeProvider::new(|| async {
            tokio::task::yield_now().await;
            Ok("246810".to_owned())
        }))
        .build()
        .unwrap();

    client.login().await.unwrap();
}

#[tokio::test]
async fn asks_for_a_captcha_instead_of_a_code_without_two_factor() {
    let server = MockServer::start().await;
    mfa_check(false).expect(1).mount(&server).await;
    password_grant()
        .respond_with(grant("unused", "unused"))
        .expect(0)
        .mount(&server)
        .await;
    let calls = Arc::new(Mutex::new(0));
    let counter = Arc::clone(&calls);
    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .otp(CodeProvider::from_fn(move || {
            *counter.lock().unwrap() += 1;
            Ok("123456".into())
        }))
        .build()
        .unwrap();

    let error = client.login().await.unwrap_err();

    assert!(error.is_captcha_required(), "{error:?}");
    assert!(
        error
            .api()
            .unwrap()
            .message()
            .contains("ClientBuilder::captcha")
    );
    assert_eq!(*calls.lock().unwrap(), 0);
}

#[tokio::test]
async fn sends_the_code_when_the_status_check_fails() {
    let server = MockServer::start().await;
    Mock::given(path(MFA_PATH))
        .respond_with(ResponseTemplate::new(500))
        .expect(1)
        .mount(&server)
        .await;
    password_grant()
        .and(body_string_contains("code=123456"))
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;
    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .build()
        .unwrap();

    client.login().await.unwrap();
}

#[tokio::test]
async fn reports_a_failing_code_provider() {
    let server = MockServer::start().await;
    password_grant()
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(0)
        .mount(&server)
        .await;
    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .otp(CodeProvider::from_fn(|| Err("no code entered".into())))
        .build()
        .unwrap();

    let error = client.login().await.unwrap_err();

    assert!(matches!(error, Error::CodeProvider(_)), "{error:?}");
}

#[cfg(feature = "otp")]
#[tokio::test]
async fn computes_totp_codes_from_the_secret() {
    let server = MockServer::start().await;
    password_grant()
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .mount(&server)
        .await;
    let totp = whitebird::Totp::new("JBSWY3DPEHPK3PXP").unwrap();
    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .otp(totp.clone())
        .build()
        .unwrap();

    let before = totp.code();
    client.login().await.unwrap();
    let after = totp.code();

    let requests = server.received_requests().await.unwrap();
    let login = requests
        .iter()
        .find(|r| r.url.path() == TOKEN_PATH)
        .unwrap();
    let fields = form(login);
    let (_, code) = fields.iter().find(|(name, _)| name == "code").unwrap();
    assert!(*code == before || *code == after);
}

#[tokio::test]
async fn reports_mfa_required_before_login_without_a_code_provider() {
    let server = MockServer::start().await;
    mfa_check(true).expect(1).mount(&server).await;
    password_grant()
        .respond_with(grant("unused", "unused"))
        .expect(0)
        .mount(&server)
        .await;

    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .build()
        .unwrap();
    let error = client.login().await.unwrap_err();

    assert!(error.is_mfa_required(), "{error:?}");
    assert!(error.is_authentication());
}

#[tokio::test]
async fn reports_mfa_required_when_the_server_wants_a_code() {
    for answer in [
        ResponseTemplate::new(200).set_body_json(json!({ "mfaEnabled": true })),
        ResponseTemplate::new(400).set_body_json(
            json!({ "error": "invalid_grant", "error_description": "Bad credentials" }),
        ),
    ] {
        let server = MockServer::start().await;
        Mock::given(path(MFA_PATH))
            .respond_with(ResponseTemplate::new(404))
            .mount(&server)
            .await;
        password_grant().respond_with(answer).mount(&server).await;

        let client = builder(&server)
            .credentials("me@example.com", "secret")
            .build()
            .unwrap();
        let error = client.login().await.unwrap_err();

        assert!(error.is_mfa_required(), "{error:?}");
    }
}

#[tokio::test]
async fn rejected_code_is_an_authentication_error() {
    for (answer, message) in [
        (
            ResponseTemplate::new(400).set_body_json(
                json!({ "error": "invalid_grant", "error_description": "Invalid code" }),
            ),
            "Invalid code",
        ),
        (
            ResponseTemplate::new(200).set_body_json(json!({ "mfaEnabled": true })),
            "the one-time code was not accepted",
        ),
    ] {
        let server = MockServer::start().await;
        password_grant().respond_with(answer).mount(&server).await;

        let client = builder(&server)
            .credentials("me@example.com", "secret")
            .otp("000000")
            .build()
            .unwrap();
        let error = client.login().await.unwrap_err();

        let api = error.api().unwrap();
        assert_eq!(api.kind(), ErrorKind::Authentication);
        assert_eq!(api.message(), message);
        assert!(!error.is_mfa_required());
    }
}

#[tokio::test]
async fn wrong_password_is_an_authentication_error() {
    let server = MockServer::start().await;
    mfa_check(true).mount(&server).await;
    password_grant()
        .respond_with(ResponseTemplate::new(400).set_body_json(
            json!({ "error": "invalid_grant", "error_description": "Bad credentials" }),
        ))
        .mount(&server)
        .await;

    let client = builder(&server)
        .credentials("me@example.com", "wrong")
        .otp("123456")
        .build()
        .unwrap();
    let error = client.login().await.unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::Authentication));
    assert_eq!(error.api().unwrap().error_code(), Some("invalid_grant"));
}

#[tokio::test]
async fn refreshes_proactively_before_the_token_expires() {
    let server = MockServer::start().await;
    let fresh = jwt("fresh", 3600);
    refresh_grant()
        .and(body_string_contains("refresh_token=refresh-1"))
        .respond_with(grant(&fresh, "refresh-2"))
        .expect(1)
        .mount(&server)
        .await;
    password_grant()
        .respond_with(ResponseTemplate::new(500))
        .expect(0)
        .mount(&server)
        .await;
    me_responds(&server, &fresh).await;

    let client = signed_in(&server, &jwt("expiring", 10));
    client.brokerage().subaccounts().me().await.unwrap();
}

#[tokio::test]
async fn refreshes_once_after_a_401() {
    let server = MockServer::start().await;
    let stale = jwt("stale", 3600);
    let fresh = jwt("fresh", 3600);
    Mock::given(path(ME_PATH))
        .and(header("authorization", format!("Bearer {stale}")))
        .respond_with(ResponseTemplate::new(401))
        .expect(1)
        .mount(&server)
        .await;
    refresh_grant()
        .respond_with(grant(&fresh, "refresh-2"))
        .expect(1)
        .mount(&server)
        .await;
    me_responds(&server, &fresh).await;

    let client = signed_in(&server, &stale);
    client.brokerage().subaccounts().me().await.unwrap();
}

#[tokio::test]
async fn gives_up_when_the_new_token_is_rejected_too() {
    let server = MockServer::start().await;
    Mock::given(path(ME_PATH))
        .respond_with(ResponseTemplate::new(401).set_body_json(json!({ "message": "Expired" })))
        .expect(2)
        .mount(&server)
        .await;
    refresh_grant()
        .respond_with(grant(&jwt("fresh", 3600), "refresh-2"))
        .expect(1)
        .mount(&server)
        .await;

    let client = signed_in(&server, &jwt("stale", 3600));
    let error = client.brokerage().subaccounts().me().await.unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::Authentication));
}

#[tokio::test]
async fn logs_in_again_when_the_refresh_token_is_rejected() {
    let server = MockServer::start().await;
    let fresh = jwt("fresh", 3600);
    refresh_grant()
        .respond_with(ResponseTemplate::new(400).set_body_json(json!({ "error": "invalid_grant" })))
        .expect(1)
        .mount(&server)
        .await;
    mfa_check(true).mount(&server).await;
    password_grant()
        .respond_with(grant(&fresh, "refresh-2"))
        .expect(1)
        .mount(&server)
        .await;
    me_responds(&server, &fresh).await;

    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .token_store(whitebird::MemoryTokenStore::from(Tokens::new(
            jwt("expired", -60),
            Some("revoked".into()),
        )))
        .build()
        .unwrap();
    client.brokerage().subaccounts().me().await.unwrap();
}

#[tokio::test]
async fn concurrent_requests_share_one_refresh() {
    let server = MockServer::start().await;
    let fresh = jwt("fresh", 3600);
    refresh_grant()
        .respond_with(grant(&fresh, "refresh-2").set_delay(Duration::from_millis(200)))
        .expect(1)
        .mount(&server)
        .await;
    me_responds(&server, &fresh).await;

    let client = signed_in(&server, &jwt("expired", -1));
    let calls = (0..10).map(|_| {
        let client = client.clone();
        tokio::spawn(async move { client.brokerage().subaccounts().me().await })
    });
    for result in futures::future::join_all(calls).await {
        result.unwrap().unwrap();
    }
}

#[derive(Default)]
struct RecordingStore {
    saved: Mutex<Vec<Tokens>>,
}

impl TokenStore for RecordingStore {
    fn load(&self) -> Option<Tokens> {
        self.saved.lock().unwrap().last().cloned()
    }

    fn save(&self, tokens: &Tokens) {
        self.saved.lock().unwrap().push(tokens.clone());
    }
}

#[tokio::test]
async fn token_store_keeps_the_session_across_clients() {
    let server = MockServer::start().await;
    let access = jwt("me", 3600);
    mfa_check(true).mount(&server).await;
    password_grant()
        .respond_with(grant(&access, "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;
    me_responds(&server, &access).await;
    let store = Arc::new(RecordingStore::default());

    let first = builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .token_store(Arc::clone(&store))
        .build()
        .unwrap();
    first.brokerage().subaccounts().me().await.unwrap();
    let second = builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .token_store(Arc::clone(&store))
        .build()
        .unwrap();
    second.brokerage().subaccounts().me().await.unwrap();

    let saved = store.saved.lock().unwrap();
    assert_eq!(saved.len(), 1);
    assert_eq!(saved[0].access_token, access);
}

#[tokio::test]
async fn authenticated_call_without_credentials_is_a_config_error() {
    let server = MockServer::start().await;
    let client = builder(&server).build().unwrap();

    let error = client.brokerage().subaccounts().me().await.unwrap_err();

    assert!(matches!(error, Error::Config(_)), "{error:?}");
    assert!(server.received_requests().await.unwrap().is_empty());
}

#[tokio::test]
async fn public_requests_carry_no_bearer_token() {
    let server = MockServer::start().await;
    Mock::given(path("/api/v1/public"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "ok": true })))
        .mount(&server)
        .await;

    let client = signed_in(&server, &jwt("me", 3600));
    let body: serde_json::Value = client
        .request(Service::Brokerage, Method::GET, "/api/v1/public")
        .public()
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();

    assert_eq!(body, json!({ "ok": true }));
    let requests = server.received_requests().await.unwrap();
    assert_eq!(bearer(&requests[0]), None);
}

#[tokio::test]
async fn sends_the_operation_mfa_token_while_it_is_set() {
    let server = MockServer::start().await;
    me_responds(&server, &jwt("me", 3600)).await;
    let client = signed_in(&server, &jwt("me", 3600));
    let header_of = |request: &Request| {
        request
            .headers
            .get("x-mfa-token")
            .map(|value| value.to_str().unwrap().to_owned())
    };

    client.set_mfa_token("mfa-1");
    client.brokerage().subaccounts().me().await.unwrap();
    client.clear_mfa_token();
    client.brokerage().subaccounts().me().await.unwrap();

    let requests = server.received_requests().await.unwrap();
    assert_eq!(header_of(&requests[0]).as_deref(), Some("mfa-1"));
    assert_eq!(header_of(&requests[1]), None);
}
