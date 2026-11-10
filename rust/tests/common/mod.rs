#![allow(dead_code, reason = "each test crate uses a different subset")]

use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde_json::{Value, json};
use whitebird::{Client, ClientBuilder, MemoryTokenStore, Service, Tokens};
use wiremock::matchers::{body_string_contains, method, path};
use wiremock::{Mock, MockBuilder, MockServer, Request, ResponseTemplate};

pub const TOKEN_PATH: &str = "/oauth/token";
pub const ME_PATH: &str = "/api/v1/brokerage/subaccount/me";
pub const CAPTCHA_PATH: &str = "/api/v1/captcha/generate";
pub const MFA_TOKEN_PATH: &str = "/api/v2/auth/mfa/merchant/client/token";

/// A JWT whose `exp` is `seconds` from now.
pub fn jwt(name: &str, seconds: i64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let exp = i64::try_from(now).unwrap() + seconds;
    let claims = URL_SAFE_NO_PAD.encode(json!({ "sub": name, "exp": exp }).to_string());
    format!("eyJhbGciOiJIUzI1NiJ9.{claims}.signature")
}

/// A client whose services all point at `server`.
pub fn builder(server: &MockServer) -> ClientBuilder {
    Client::builder()
        .base_url(Service::Api, server.uri())
        .base_url(Service::Auth, server.uri())
        .base_url(Service::Brokerage, server.uri())
        .base_url(Service::Exchange, server.uri())
        .base_url(Service::Notification, server.uri())
        .max_retries(0)
}

pub fn signed_in(server: &MockServer, access_token: &str) -> Client {
    builder(server)
        .token_store(MemoryTokenStore::from(Tokens::new(
            access_token,
            Some("refresh-1".into()),
        )))
        .build()
        .unwrap()
}

pub fn grant(access_token: &str, refresh_token: &str) -> ResponseTemplate {
    ResponseTemplate::new(200).set_body_json(json!({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": 3600,
    }))
}

pub fn password_grant() -> MockBuilder {
    Mock::given(method("POST"))
        .and(path(TOKEN_PATH))
        .and(body_string_contains("grant_type=password"))
}

pub fn refresh_grant() -> MockBuilder {
    Mock::given(method("POST"))
        .and(path(TOKEN_PATH))
        .and(body_string_contains("grant_type=refresh_token"))
}

/// The password grant without a captcha, answered like the server does when it wants one. Its
/// priority makes it win over the other mocks of the token endpoint.
pub fn captcha_required() -> Mock {
    password_grant()
        .and(|request: &Request| !request.headers.contains_key("captcha"))
        .respond_with(ResponseTemplate::new(400).set_body_json(json!({
            "code": 400,
            "status": "REQUIRED_CAPTCHA",
            "message": "Captcha required",
        })))
        .with_priority(1)
}

/// Whether a token request carries a one-time code.
pub fn has_code(request: &Request) -> bool {
    form(request).iter().any(|(name, _)| name == "code")
}

/// The password grant without a code of an account with two-factor authentication, answered
/// like the server does: with a request for the code.
pub fn code_requested() -> Mock {
    password_grant()
        .and(|request: &Request| !has_code(request))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "mfaEnabled": true })))
}

/// The password grant carrying `code`.
pub fn code_grant(code: &str) -> MockBuilder {
    password_grant().and(body_string_contains(format!("code={code}")))
}

/// Mounts the sign-in of an account with two-factor authentication that accepts `code`.
pub async fn two_factor_login(server: &MockServer, code: &str, access_token: &str) {
    code_requested().mount(server).await;
    code_grant(code)
        .respond_with(grant(access_token, "refresh-1"))
        .mount(server)
        .await;
}

/// Requests sent to `target`.
pub async fn requests_to(server: &MockServer, target: &str) -> Vec<Request> {
    server
        .received_requests()
        .await
        .unwrap()
        .into_iter()
        .filter(|request| request.url.path() == target)
        .collect()
}

pub fn subaccount(name: &str) -> Value {
    json!({ "uuid": format!("id-{name}"), "displayName": name, "status": "ACTIVE" })
}

/// Form fields of a recorded request.
pub fn form(request: &Request) -> Vec<(String, String)> {
    form_urlencoded::parse(&request.body).into_owned().collect()
}

pub fn bearer(request: &Request) -> Option<String> {
    let value = request.headers.get("authorization")?.to_str().ok()?;
    value.strip_prefix("Bearer ").map(str::to_owned)
}
