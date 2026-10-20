#![cfg(feature = "blocking")]

mod common;

use common::*;
use serde_json::{Value, json};
use whitebird::params::BrokerageSubaccountsListParams;
use whitebird::{CodeProvider, ErrorKind, Method, Service};
use wiremock::matchers::{body_string_contains, header, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

/// The blocking client runs its own runtime, so the mock server lives on another one.
struct Server {
    mock: MockServer,
    runtime: tokio::runtime::Runtime,
}

impl Server {
    fn start() -> Self {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();
        let mock = runtime.block_on(MockServer::start());
        Self { mock, runtime }
    }

    fn mount(&self, mock: Mock) {
        self.runtime.block_on(mock.mount(&self.mock));
    }

    fn client(&self) -> whitebird::blocking::Client {
        builder(&self.mock)
            .credentials("me@example.com", "secret")
            .otp("123456")
            .build_blocking()
            .unwrap()
    }
}

#[test]
fn logs_in_and_calls_endpoints() {
    let server = Server::start();
    let access = jwt("me", 3600);
    server.mount(
        password_grant()
            .respond_with(grant(&access, "refresh-1"))
            .expect(1),
    );
    server.mount(
        Mock::given(path(ME_PATH))
            .respond_with(ResponseTemplate::new(200).set_body_json(subaccount("main"))),
    );
    let client = server.client();

    let me = client.brokerage().subaccounts().me().unwrap();
    let raw: Value = client
        .request(Service::Brokerage, Method::GET, ME_PATH)
        .send()
        .unwrap()
        .json()
        .unwrap();

    assert_eq!(me.display_name.as_deref(), Some("main"));
    assert_eq!(raw["displayName"], "main");
}

#[test]
fn iterates_pages_and_events() {
    let server = Server::start();
    server.mount(password_grant().respond_with(grant(&jwt("me", 3600), "refresh-1")));
    for (number, last) in [(0, false), (1, true)] {
        server.mount(
            Mock::given(path("/api/v1/brokerage/subaccount/page"))
                .and(query_param("page", number.to_string()))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                    "content": [subaccount(&format!("sub-{number}"))],
                    "last": last,
                }))),
        );
    }
    server.mount(
        Mock::given(path("/api/v1/sse/connect")).respond_with(
            ResponseTemplate::new(200)
                .set_body_raw("retry: 10\nid: 1\ndata: {}\n\n", "text/event-stream"),
        ),
    );
    server.mount(
        Mock::given(path("/api/v1/sse/connect"))
            .and(header("last-event-id", "1"))
            .respond_with(ResponseTemplate::new(403))
            .with_priority(1),
    );
    let client = server.client();

    let names: Vec<_> = client
        .brokerage()
        .subaccounts()
        .list(BrokerageSubaccountsListParams::default())
        .map(|item| item.unwrap().display_name.unwrap())
        .collect();
    let events: Vec<_> = client
        .request(Service::Notification, Method::GET, "/api/v1/sse/connect")
        .events::<Value>()
        .collect();

    assert_eq!(names, ["sub-0", "sub-1"]);
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].as_ref().unwrap().id.as_deref(), Some("1"));
    assert_eq!(
        events[1].as_ref().unwrap_err().kind(),
        Some(ErrorKind::PermissionDenied)
    );
}

#[test]
fn confirms_operations() {
    let server = Server::start();
    server.mount(password_grant().respond_with(grant(&jwt("me", 3600), "refresh-1")));
    server
        .mount(Mock::given(path(MFA_TOKEN_PATH)).respond_with(
            ResponseTemplate::new(200).set_body_json(json!({ "mfaToken": "mfa-1" })),
        ));
    let client = server.client();

    let token = client.confirm_operation("WITHDRAWAL", None).unwrap();

    assert_eq!(token, "mfa-1");
    assert_eq!(client.mfa_token().as_deref(), Some("mfa-1"));
}

#[test]
fn calls_a_blocking_code_provider() {
    let server = Server::start();
    server.mount(
        password_grant()
            .and(body_string_contains("code=112233"))
            .respond_with(grant(&jwt("me", 3600), "refresh-1"))
            .expect(1),
    );
    let client = builder(&server.mock)
        .credentials("me@example.com", "secret")
        .otp(CodeProvider::from_fn(|| Ok("112233".into())))
        .build_blocking()
        .unwrap();

    client.login().unwrap();
}
