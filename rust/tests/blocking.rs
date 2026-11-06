#![cfg(feature = "blocking")]

mod common;

use std::time::Duration;

use common::*;
use serde_json::{Value, json};
use whitebird::params::BrokerageSubaccountsListParams;
use whitebird::{CodeProvider, ErrorKind, Method, Service};
use wiremock::matchers::{header, path, query_param};
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
        runtime.block_on(mfa_check(true).mount(&mock));
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
    server.mount(code_requested());
    server.mount(
        code_grant("112233")
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

/// Serves `{}` to every request and closes connections idle for 300 ms, like a server with a
/// short keep-alive timeout.
fn closing_idle_connections() -> String {
    use std::io::{Read, Write};

    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let mut stream = stream.unwrap();
            std::thread::spawn(move || {
                stream
                    .set_read_timeout(Some(Duration::from_millis(300)))
                    .unwrap();
                let mut buffer = Vec::new();
                let mut chunk = [0; 4096];
                loop {
                    let Ok(read @ 1..) = stream.read(&mut chunk) else {
                        let _ = stream.shutdown(std::net::Shutdown::Both);
                        return;
                    };
                    buffer.extend_from_slice(&chunk[..read]);
                    let text = String::from_utf8_lossy(&buffer).into_owned();
                    let Some(end) = text.find("\r\n\r\n") else {
                        continue;
                    };
                    let length = text[..end]
                        .lines()
                        .find_map(|line| {
                            line.to_ascii_lowercase()
                                .strip_prefix("content-length:")
                                .map(|value| value.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    if buffer.len() >= end + 4 + length {
                        buffer.drain(..end + 4 + length);
                        stream
                            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                            .unwrap();
                    }
                }
            });
        }
    });
    format!("http://{address}")
}

#[test]
fn notices_connections_the_server_closed_between_calls() {
    let url = closing_idle_connections();
    let client = whitebird::Client::builder()
        .base_url(Service::Api, url)
        .max_retries(0)
        .build_blocking()
        .unwrap();

    for attempt in 0..3 {
        if attempt > 0 {
            std::thread::sleep(Duration::from_millis(600));
        }
        client
            .request(Service::Api, Method::POST, "/orders")
            .public()
            .json(json!({ "attempt": attempt }))
            .send()
            .unwrap();
    }
}

#[tokio::test]
async fn can_be_dropped_inside_an_async_context() {
    let client = whitebird::Client::builder().build_blocking().unwrap();
    let async_client = client.as_async().clone();
    drop(client);
    drop(async_client);
}
