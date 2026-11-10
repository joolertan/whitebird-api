mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::*;
use serde_json::json;
use whitebird::params::UserPasswordChangeParams;
use whitebird::{
    CodeProvider, Error, ErrorKind, MemoryTokenStore, Method, Service, TokenStore, Tokens,
};
use wiremock::matchers::{body_string_contains, header, method, path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

async fn me_responds(server: &MockServer, token: &str) {
    Mock::given(method("GET"))
        .and(path(ME_PATH))
        .and(header("authorization", format!("Bearer {token}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(subaccount("main")))
        .mount(server)
        .await;
}

fn two_factor_client(server: &MockServer, otp: impl Into<CodeProvider>) -> whitebird::Client {
    builder(server)
        .credentials("me@example.com", "secret")
        .otp(otp)
        .build()
        .unwrap()
}

#[tokio::test]
async fn logs_in_lazily_in_two_steps_like_the_web_app() {
    let server = MockServer::start().await;
    let access = jwt("me", 3600);
    password_grant()
        .and(|request: &Request| !has_code(request))
        .and(header(
            "authorization",
            "Basic YXV0aENsaWVudElkOlhZN2ttem9OemwxMDA=",
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "mfaEnabled": true })))
        .expect(1)
        .mount(&server)
        .await;
    code_grant("123456")
        .respond_with(grant(&access, "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;
    me_responds(&server, &access).await;

    let client = builder(&server)
        .credentials("Trader@Example.com", "secret")
        .otp(" 123456\n")
        .build()
        .unwrap();
    let me = client.brokerage().subaccounts().me().await.unwrap();
    client.brokerage().subaccounts().me().await.unwrap();

    assert_eq!(me.display_name.as_deref(), Some("main"));
    let logins = requests_to(&server, TOKEN_PATH).await;
    assert_eq!(
        form(&logins[0]),
        [
            ("grant_type".into(), "password".into()),
            ("username".into(), "trader@example.com".into()),
            ("password".into(), "secret".into()),
        ]
    );
    assert_eq!(
        form(&logins[1]),
        [
            ("grant_type".into(), "password".into()),
            ("username".into(), "trader@example.com".into()),
            ("password".into(), "secret".into()),
            ("code".into(), "123456".into()),
        ]
    );
    assert!(logins.iter().all(|r| !r.headers.contains_key("captcha")));
}

#[tokio::test]
async fn takes_the_tokens_of_the_first_step_without_asking_for_a_code() {
    let server = MockServer::start().await;
    password_grant()
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(1)
        .mount(&server)
        .await;
    let calls = Arc::new(Mutex::new(0));
    let counter = Arc::clone(&calls);

    let tokens = two_factor_client(
        &server,
        CodeProvider::from_fn(move || {
            *counter.lock().unwrap() += 1;
            Ok("123456".into())
        }),
    )
    .login()
    .await
    .unwrap();

    assert_eq!(tokens.refresh_token.as_deref(), Some("refresh-1"));
    assert!(tokens.expires_at.is_some());
    assert_eq!(*calls.lock().unwrap(), 0);
}

#[tokio::test]
async fn asks_the_code_provider_on_every_login() {
    let server = MockServer::start().await;
    two_factor_login(&server, "654321", &jwt("me", 3600)).await;
    let calls = Arc::new(Mutex::new(0));
    let counter = Arc::clone(&calls);
    let client = two_factor_client(
        &server,
        CodeProvider::from_fn(move || {
            *counter.lock().unwrap() += 1;
            Ok("654321".into())
        }),
    );

    client.login().await.unwrap();
    client.login().await.unwrap();

    assert_eq!(*calls.lock().unwrap(), 2);
}

#[tokio::test]
async fn awaits_an_async_code_provider() {
    let server = MockServer::start().await;
    two_factor_login(&server, "246810", &jwt("me", 3600)).await;
    let client = two_factor_client(
        &server,
        CodeProvider::new(|| async {
            tokio::task::yield_now().await;
            Ok("246810".to_owned())
        }),
    );

    client.login().await.unwrap();
}

#[tokio::test]
async fn reports_an_unknown_username() {
    let server = MockServer::start().await;
    password_grant()
        .respond_with(ResponseTemplate::new(400).set_body_json(json!({
            "code": 400,
            "status": "NOT_FOUND",
            "message": "User not found",
        })))
        .expect(1)
        .mount(&server)
        .await;

    let error = two_factor_client(&server, "123456")
        .login()
        .await
        .unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::Authentication), "{error:?}");
    let api = error.api().unwrap();
    assert_eq!(api.message(), "no Whitebird account with this username");
    assert_eq!(api.error_code(), Some("NOT_FOUND"));
}

#[tokio::test]
async fn reports_a_failing_code_provider() {
    let server = MockServer::start().await;
    code_requested().expect(1).mount(&server).await;
    code_grant("")
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .expect(0)
        .mount(&server)
        .await;

    let error = two_factor_client(
        &server,
        CodeProvider::from_fn(|| Err("no code entered".into())),
    )
    .login()
    .await
    .unwrap_err();

    assert!(matches!(error, Error::CodeProvider(_)), "{error:?}");
}

#[cfg(feature = "otp")]
#[tokio::test]
async fn computes_totp_codes_from_the_secret() {
    let server = MockServer::start().await;
    code_requested().mount(&server).await;
    password_grant()
        .and(has_code)
        .respond_with(grant(&jwt("me", 3600), "refresh-1"))
        .mount(&server)
        .await;
    let totp = whitebird::Totp::new("JBSWY3DPEHPK3PXP").unwrap();
    let client = two_factor_client(&server, totp.clone());

    let before = totp.code();
    client.login().await.unwrap();
    let after = totp.code();

    let logins = requests_to(&server, TOKEN_PATH).await;
    let fields = form(&logins[1]);
    let (_, code) = fields.iter().find(|(name, _)| name == "code").unwrap();
    assert!(*code == before || *code == after);
}

#[tokio::test]
async fn reports_mfa_required_when_the_server_asks_for_a_code_without_a_provider() {
    let server = MockServer::start().await;
    code_requested().expect(1).mount(&server).await;

    let client = builder(&server)
        .credentials("me@example.com", "secret")
        .build()
        .unwrap();
    let error = client.login().await.unwrap_err();

    assert!(error.is_mfa_required(), "{error:?}");
    assert!(error.is_authentication());
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
        code_requested().mount(&server).await;
        code_grant("000000")
            .respond_with(answer)
            .mount(&server)
            .await;

        let error = two_factor_client(&server, "000000")
            .login()
            .await
            .unwrap_err();

        let api = error.api().unwrap();
        assert_eq!(api.kind(), ErrorKind::Authentication);
        assert_eq!(api.message(), message);
        assert!(!error.is_mfa_required());
    }
}

#[tokio::test]
async fn wrong_password_is_an_authentication_error() {
    for body in [
        json!({ "error": "invalid_grant", "error_description": "Bad credentials" }),
        json!({ "status": "BAD_CREDENTIALS", "message": "Bad credentials" }),
    ] {
        let server = MockServer::start().await;
        password_grant()
            .respond_with(ResponseTemplate::new(400).set_body_json(&body))
            .expect(1)
            .mount(&server)
            .await;

        let error = two_factor_client(&server, "123456")
            .login()
            .await
            .unwrap_err();

        assert_eq!(error.kind(), Some(ErrorKind::Authentication), "{body}");
    }
}

#[tokio::test]
async fn blocked_accounts_and_too_many_attempts_keep_their_kind() {
    for (body, kind) in [
        (
            json!({ "message": "Username is blocked" }),
            ErrorKind::BadRequest,
        ),
        (
            json!({ "message": "Too many requests" }),
            ErrorKind::RateLimited,
        ),
    ] {
        let server = MockServer::start().await;
        password_grant()
            .respond_with(ResponseTemplate::new(400).set_body_json(&body))
            .mount(&server)
            .await;

        let error = two_factor_client(&server, "123456")
            .login()
            .await
            .unwrap_err();

        assert_eq!(error.kind(), Some(kind), "{body}");
    }
}

#[tokio::test]
async fn refreshes_a_minute_before_the_token_expires() {
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

    let client = signed_in(&server, &jwt("expiring", 45));
    client.brokerage().subaccounts().me().await.unwrap();
}

#[tokio::test]
async fn reads_the_expiry_from_the_token_when_the_store_has_none() {
    let server = MockServer::start().await;
    let fresh = jwt("fresh", 3600);
    refresh_grant()
        .respond_with(grant(&fresh, "refresh-2"))
        .expect(1)
        .mount(&server)
        .await;
    me_responds(&server, &fresh).await;

    let client = builder(&server)
        .token_store(MemoryTokenStore::from(Tokens {
            access_token: jwt("expired", -10),
            refresh_token: Some("refresh-1".into()),
            expires_at: None,
        }))
        .build()
        .unwrap();
    client.brokerage().subaccounts().me().await.unwrap();
}

#[tokio::test]
async fn keeps_the_refresh_token_when_the_refresh_returns_none() {
    let server = MockServer::start().await;
    let fresh = jwt("fresh", 3600);
    refresh_grant()
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({ "access_token": fresh, "expires_in": 3600 })),
        )
        .mount(&server)
        .await;
    me_responds(&server, &fresh).await;
    let store = Arc::new(MemoryTokenStore::from(Tokens::new(
        jwt("expired", -1),
        Some("refresh-1".into()),
    )));

    builder(&server)
        .token_store(Arc::clone(&store))
        .build()
        .unwrap()
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap();

    let saved = store.load().unwrap();
    assert_eq!(saved.access_token, fresh);
    assert_eq!(saved.refresh_token.as_deref(), Some("refresh-1"));
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
async fn a_401_without_a_way_to_renew_is_returned_as_is() {
    let server = MockServer::start().await;
    Mock::given(path(ME_PATH))
        .respond_with(ResponseTemplate::new(401).set_body_json(json!({ "message": "Expired" })))
        .expect(1)
        .mount(&server)
        .await;

    let error = builder(&server)
        .token_store(MemoryTokenStore::from(Tokens::new(jwt("me", 3600), None)))
        .build()
        .unwrap()
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::Authentication));
    assert_eq!(error.api().unwrap().message(), "Expired");
}

#[tokio::test]
async fn logs_in_again_when_the_refresh_token_is_rejected() {
    for status in [400, 401] {
        let server = MockServer::start().await;
        let fresh = jwt("fresh", 3600);
        refresh_grant()
            .respond_with(
                ResponseTemplate::new(status).set_body_json(json!({ "error": "invalid_grant" })),
            )
            .expect(1)
            .mount(&server)
            .await;
        two_factor_login(&server, "123456", &fresh).await;
        me_responds(&server, &fresh).await;
        let store = Arc::new(MemoryTokenStore::from(Tokens::new(
            jwt("expired", -60),
            Some("revoked".into()),
        )));

        let client = builder(&server)
            .credentials("me@example.com", "secret")
            .otp("123456")
            .token_store(Arc::clone(&store))
            .build()
            .unwrap();
        client.set_mfa_token("mfa-1");
        client.brokerage().subaccounts().me().await.unwrap();

        assert_eq!(store.load().unwrap().access_token, fresh);
        assert_eq!(client.mfa_token(), None);
    }
}

#[tokio::test]
async fn a_rejected_refresh_without_credentials_ends_the_session() {
    let server = MockServer::start().await;
    refresh_grant()
        .respond_with(
            ResponseTemplate::new(400).set_body_json(json!({ "error": "invalid_request" })),
        )
        .expect(1)
        .mount(&server)
        .await;
    let store = Arc::new(MemoryTokenStore::from(Tokens::new(
        jwt("expired", -60),
        Some("revoked".into()),
    )));

    let error = builder(&server)
        .token_store(Arc::clone(&store))
        .build()
        .unwrap()
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::Authentication), "{error:?}");
    assert_eq!(store.load(), None);
}

#[tokio::test]
async fn other_refresh_failures_keep_the_session() {
    let server = MockServer::start().await;
    refresh_grant()
        .respond_with(ResponseTemplate::new(503))
        .expect(1)
        .mount(&server)
        .await;
    password_grant()
        .respond_with(grant("unused", "unused"))
        .expect(0)
        .mount(&server)
        .await;
    let tokens = Tokens::new(jwt("expired", -60), Some("refresh-1".into()));
    let store = Arc::new(MemoryTokenStore::from(tokens.clone()));

    let error = builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .token_store(Arc::clone(&store))
        .build()
        .unwrap()
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap_err();

    assert_eq!(error.kind(), Some(ErrorKind::Server));
    assert_eq!(store.load(), Some(tokens));
}

#[tokio::test]
async fn skips_the_refresh_when_the_refresh_token_has_expired() {
    let server = MockServer::start().await;
    let fresh = jwt("fresh", 3600);
    refresh_grant()
        .respond_with(grant("unused", "unused"))
        .expect(0)
        .mount(&server)
        .await;
    two_factor_login(&server, "123456", &fresh).await;
    me_responds(&server, &fresh).await;

    builder(&server)
        .credentials("me@example.com", "secret")
        .otp("123456")
        .token_store(MemoryTokenStore::from(Tokens::new(
            jwt("expired", -60),
            Some(jwt("refresh", -1)),
        )))
        .build()
        .unwrap()
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap();
}

#[tokio::test]
async fn uses_an_expiring_token_that_cannot_be_renewed() {
    let server = MockServer::start().await;
    let expiring = jwt("expiring", 30);
    me_responds(&server, &expiring).await;

    builder(&server)
        .token_store(MemoryTokenStore::from(Tokens::new(&expiring, None)))
        .build()
        .unwrap()
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap();

    assert!(requests_to(&server, TOKEN_PATH).await.is_empty());
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
    saved: Mutex<Vec<Option<Tokens>>>,
}

impl TokenStore for RecordingStore {
    fn load(&self) -> Option<Tokens> {
        self.saved.lock().unwrap().last().cloned().flatten()
    }

    fn save(&self, tokens: &Tokens) {
        self.saved.lock().unwrap().push(Some(tokens.clone()));
    }

    fn clear(&self) {
        self.saved.lock().unwrap().push(None);
    }
}

#[tokio::test]
async fn token_store_keeps_the_session_across_clients() {
    let server = MockServer::start().await;
    let access = jwt("me", 3600);
    two_factor_login(&server, "123456", &access).await;
    me_responds(&server, &access).await;
    let store = Arc::new(RecordingStore::default());
    let client = || {
        builder(&server)
            .credentials("me@example.com", "secret")
            .otp("123456")
            .token_store(Arc::clone(&store))
            .build()
            .unwrap()
    };

    client().brokerage().subaccounts().me().await.unwrap();
    client().brokerage().subaccounts().me().await.unwrap();

    let saved = store.saved.lock().unwrap();
    assert_eq!(saved.len(), 1);
    assert_eq!(saved[0].as_ref().unwrap().access_token, access);
}

#[tokio::test]
async fn adopts_a_session_another_process_saved_meanwhile() {
    let server = MockServer::start().await;
    let theirs = jwt("theirs", 3600);
    refresh_grant()
        .respond_with(grant("unused", "unused"))
        .expect(0)
        .mount(&server)
        .await;
    me_responds(&server, &theirs).await;
    let store = Arc::new(MemoryTokenStore::from(Tokens::new(
        jwt("expired", -1),
        Some("refresh-1".into()),
    )));
    let client = builder(&server)
        .token_store(Arc::clone(&store))
        .build()
        .unwrap();

    store.save(&Tokens::new(&theirs, Some("refresh-2".into())));
    client.brokerage().subaccounts().me().await.unwrap();
}

#[tokio::test]
async fn logout_ends_the_session_even_when_it_fails() {
    for answer in [ResponseTemplate::new(200), ResponseTemplate::new(500)] {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/user/logout"))
            .respond_with(answer)
            .expect(1)
            .mount(&server)
            .await;
        let store = Arc::new(RecordingStore::default());
        store.save(&Tokens::new(jwt("me", 3600), Some("refresh-1".into())));
        let client = builder(&server)
            .token_store(Arc::clone(&store))
            .build()
            .unwrap();
        client.set_mfa_token("mfa-1");

        let _ = client.user().logout().await;

        assert_eq!(store.load(), None);
        assert_eq!(client.mfa_token(), None);
        let error = client.brokerage().subaccounts().me().await.unwrap_err();
        assert!(matches!(error, Error::Config(_)), "{error:?}");
    }
}

#[tokio::test]
async fn password_change_replaces_the_session() {
    let server = MockServer::start().await;
    let old = jwt("old", 3600);
    let new = jwt("new", 3600);
    Mock::given(method("POST"))
        .and(path("/api/v1/user/password/change-and-reauthorize"))
        .and(header("authorization", format!("Bearer {old}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": new,
            "refresh_token": "refresh-2",
        })))
        .expect(1)
        .mount(&server)
        .await;
    me_responds(&server, &new).await;
    let store = Arc::new(MemoryTokenStore::from(Tokens::new(
        &old,
        Some("refresh-1".into()),
    )));
    let client = builder(&server)
        .token_store(Arc::clone(&store))
        .build()
        .unwrap();

    let pair = client
        .user()
        .password()
        .change(
            UserPasswordChangeParams::builder()
                .old_password("old secret")
                .new_password("New secret 1")
                .build(),
        )
        .await
        .unwrap();
    client.brokerage().subaccounts().me().await.unwrap();

    assert_eq!(pair.access_token.as_deref(), Some(new.as_str()));
    let saved = store.load().unwrap();
    assert_eq!(saved.access_token, new);
    assert_eq!(saved.refresh_token.as_deref(), Some("refresh-2"));
}

#[tokio::test]
async fn later_logins_use_the_changed_password() {
    let server = MockServer::start().await;
    let old = jwt("old", 3600);
    let new = jwt("new", 3600);
    Mock::given(method("POST"))
        .and(path("/api/v1/user/password/change-and-reauthorize"))
        // No refresh token in the answer: the previous one stays.
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "access_token": new })))
        .expect(1)
        .mount(&server)
        .await;
    password_grant()
        .respond_with(grant(&jwt("next", 3600), "refresh-2"))
        .expect(1)
        .mount(&server)
        .await;
    let store = Arc::new(MemoryTokenStore::from(Tokens::new(
        &old,
        Some("refresh-1".into()),
    )));
    let client = builder(&server)
        .credentials("me@example.com", "Old secret 1")
        .token_store(Arc::clone(&store))
        .build()
        .unwrap();

    client
        .user()
        .password()
        .change(
            UserPasswordChangeParams::builder()
                .old_password("Old secret 1")
                .new_password("New secret 1")
                .build(),
        )
        .await
        .unwrap();
    let saved = store.load().unwrap();
    assert_eq!(saved.access_token, new);
    assert_eq!(saved.refresh_token.as_deref(), Some("refresh-1"));
    client.login().await.unwrap();

    let grants = requests_to(&server, TOKEN_PATH).await;
    let password = form(&grants[0])
        .into_iter()
        .find(|(name, _)| name == "password")
        .map(|(_, value)| value);
    assert_eq!(password.as_deref(), Some("New secret 1"));
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
async fn sends_the_language_with_every_request() {
    let server = MockServer::start().await;
    let access = jwt("me", 3600);
    password_grant()
        .respond_with(grant(&access, "refresh-1"))
        .mount(&server)
        .await;
    me_responds(&server, &access).await;

    builder(&server)
        .credentials("me@example.com", "secret")
        .language("en")
        .build()
        .unwrap()
        .brokerage()
        .subaccounts()
        .me()
        .await
        .unwrap();

    let requests = server.received_requests().await.unwrap();
    assert_eq!(requests.len(), 2);
    for request in requests {
        assert_eq!(request.headers["accept-language"], "en", "{}", request.url);
    }
    let invalid = builder(&server).language("e\nn").build().unwrap_err();
    assert!(matches!(invalid, Error::Config(_)), "{invalid:?}");
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

    client.set_mfa_token(" mfa-1\n");
    client.brokerage().subaccounts().me().await.unwrap();
    client.clear_mfa_token();
    client.brokerage().subaccounts().me().await.unwrap();

    let requests = server.received_requests().await.unwrap();
    assert_eq!(header_of(&requests[0]).as_deref(), Some("mfa-1"));
    assert_eq!(header_of(&requests[1]), None);
}
