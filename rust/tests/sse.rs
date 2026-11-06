mod common;

use std::time::Duration;

use common::*;
use futures::StreamExt;
use serde::Deserialize;
use serde_json::json;
use whitebird::models::{NotificationStreamMessage, NotificationStreamMessageType};
use whitebird::sse::Event;
use whitebird::{ErrorKind, Method, Result, Service};
use wiremock::matchers::{header, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const CONNECT_PATH: &str = "/api/v1/sse/connect";

#[derive(Debug, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
struct OrderUpdate {
    order_id: String,
}

fn stream(body: &str) -> ResponseTemplate {
    ResponseTemplate::new(200).set_body_raw(body.to_owned(), "text/event-stream")
}

async fn events(server: &MockServer) -> Vec<Result<Event<OrderUpdate>>> {
    signed_in(server, &jwt("me", 3600))
        .request(Service::Notification, Method::GET, CONNECT_PATH)
        .events()
        .collect()
        .await
}

#[tokio::test]
async fn reconnects_with_the_last_event_id_until_rejected() {
    let server = MockServer::start().await;
    Mock::given(path(CONNECT_PATH))
        .and(header("accept", "text/event-stream"))
        .respond_with(stream(concat!(
            ": connected\n\n",
            "retry: 10\n",
            "event: order-updates\nid: 1\ndata: {\"orderId\":\n",
            "data: \"a\"}\n\n",
            "event: ping\ndata: \n\n",
            "id: 2\ndata: {\"orderId\":\"lost\"}",
        )))
        .up_to_n_times(1)
        .with_priority(5)
        .mount(&server)
        .await;
    Mock::given(header("last-event-id", "1"))
        .respond_with(ResponseTemplate::new(503))
        .up_to_n_times(1)
        .with_priority(1)
        .mount(&server)
        .await;
    Mock::given(header("last-event-id", "1"))
        .respond_with(stream("id: 3\ndata: {\"orderId\":\"b\"}\n\n"))
        .with_priority(2)
        .mount(&server)
        .await;
    Mock::given(header("last-event-id", "3"))
        .respond_with(ResponseTemplate::new(403).set_body_json(json!({ "message": "Denied" })))
        .mount(&server)
        .await;

    let events = events(&server).await;

    // The ping without data is skipped.
    assert_eq!(events.len(), 3, "{events:?}");
    let first = events[0].as_ref().unwrap();
    assert_eq!(first.event, "order-updates");
    assert_eq!(first.id.as_deref(), Some("1"));
    assert_eq!(first.json().unwrap().order_id, "a");
    let second = events[1].as_ref().unwrap();
    assert_eq!(second.event, "message");
    assert_eq!(second.json().unwrap().order_id, "b");
    let error = events[2].as_ref().unwrap_err();
    assert_eq!(error.kind(), Some(ErrorKind::PermissionDenied));
    assert_eq!(server.received_requests().await.unwrap().len(), 4);
}

#[tokio::test]
async fn ends_after_a_401_that_a_refresh_does_not_fix() {
    let server = MockServer::start().await;
    Mock::given(path(CONNECT_PATH))
        .respond_with(ResponseTemplate::new(401))
        .expect(2)
        .mount(&server)
        .await;
    refresh_grant()
        .respond_with(grant(&jwt("fresh", 3600), "refresh-2"))
        .expect(1)
        .mount(&server)
        .await;

    let events = events(&server).await;

    assert_eq!(events.len(), 1);
    assert_eq!(
        events[0].as_ref().unwrap_err().kind(),
        Some(ErrorKind::Authentication)
    );
}

#[tokio::test]
async fn generated_stream_decodes_notifications() {
    let server = MockServer::start().await;
    Mock::given(path(CONNECT_PATH))
        .and(header(
            "authorization",
            format!("Bearer {}", jwt("me", 3600)),
        ))
        .respond_with(stream(concat!(
            "retry: 10\n",
            "id: 1\ndata: {\"type\":\"ORDER_UPDATE\",\"createdAt\":\"2024-05-01T09:00:00\",",
            "\"payload\":{\"orderId\":\"a\"}}\n\n",
            "id: 2\ndata: {\"type\":\"LOYALTY_UPDATE\",\"extra\":1}\n\n",
        )))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    Mock::given(header("last-event-id", "2"))
        .respond_with(ResponseTemplate::new(403))
        .with_priority(1)
        .mount(&server)
        .await;

    let events: Vec<_> = signed_in(&server, &jwt("me", 3600))
        .notifications()
        .stream()
        .collect()
        .await;

    let messages: Vec<NotificationStreamMessage> = events[..2]
        .iter()
        .map(|event| event.as_ref().unwrap().json().unwrap())
        .collect();
    assert_eq!(
        messages[0].r#type,
        Some(NotificationStreamMessageType::OrderUpdate)
    );
    assert_eq!(messages[0].payload, Some(json!({ "orderId": "a" })));
    assert_eq!(
        messages[1].r#type,
        Some(NotificationStreamMessageType::Unknown(
            "LOYALTY_UPDATE".into()
        ))
    );
    assert!(events[2].is_err());
    assert_eq!(events.len(), 3);
}

#[tokio::test]
async fn retries_answers_that_are_not_event_streams() {
    let server = MockServer::start().await;
    Mock::given(path(CONNECT_PATH))
        .respond_with(
            ResponseTemplate::new(200).set_body_raw("<html>challenge</html>", "text/html"),
        )
        .up_to_n_times(1)
        .with_priority(1)
        .mount(&server)
        .await;
    Mock::given(path(CONNECT_PATH))
        .respond_with(stream("retry: 10\nid: 1\ndata: {\"orderId\":\"a\"}\n\n"))
        .up_to_n_times(1)
        .with_priority(2)
        .mount(&server)
        .await;
    Mock::given(path(CONNECT_PATH))
        .respond_with(ResponseTemplate::new(204))
        .with_priority(3)
        .mount(&server)
        .await;

    let events = tokio::time::timeout(Duration::from_secs(10), events(&server))
        .await
        .unwrap();

    assert_eq!(events.len(), 1, "{events:?}");
    assert_eq!(events[0].as_ref().unwrap().json().unwrap().order_id, "a");
    assert_eq!(server.received_requests().await.unwrap().len(), 3);
}

#[tokio::test]
async fn sends_the_language_and_the_last_event_id() {
    let server = MockServer::start().await;
    Mock::given(path(CONNECT_PATH))
        .and(header("accept-language", "ru"))
        .respond_with(stream(
            "id: 7\ndata: {\"orderId\":\"a\"}\n\nid: 8\ndata: {\"orderId\":",
        ))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    Mock::given(path(CONNECT_PATH))
        .and(header("last-event-id", "7"))
        .respond_with(ResponseTemplate::new(204))
        .with_priority(1)
        .mount(&server)
        .await;

    let events: Vec<Result<Event<OrderUpdate>>> = builder(&server)
        .language("ru")
        .token_store(whitebird::MemoryTokenStore::from(whitebird::Tokens::new(
            jwt("me", 3600),
            None,
        )))
        .build()
        .unwrap()
        .request(Service::Notification, Method::GET, CONNECT_PATH)
        .events()
        .collect()
        .await;

    assert_eq!(events.len(), 1, "{events:?}");
}
