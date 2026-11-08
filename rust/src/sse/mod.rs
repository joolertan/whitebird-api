//! Server-sent event streams, such as account notifications.

mod parser;

use std::marker::PhantomData;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;

use futures_core::Stream;
use futures_util::StreamExt;
use futures_util::stream::{self, BoxStream};
use reqwest::StatusCode;
use reqwest::header::CONTENT_TYPE;
use serde::de::DeserializeOwned;

use self::parser::{Parser, RawEvent};
use crate::error::{Error, ErrorKind, Result};
use crate::request::{RequestBuilder, Response};

/// Reconnection delay until the server sets one with a `retry:` field.
const DEFAULT_RETRY: Duration = Duration::from_secs(3);
const MAX_BACKOFF: Duration = Duration::from_secs(30);

/// A server-sent event whose data decodes as `T`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event<T = serde_json::Value> {
    /// Event type, e.g. `order-updates`; `message` when the server does not name it.
    pub event: String,
    /// ID of the event (or of the last event that had one).
    pub id: Option<String>,
    /// Raw event data.
    pub data: String,
    payload: PhantomData<fn() -> T>,
}

impl<T: DeserializeOwned> Event<T> {
    /// Decodes the data as JSON.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Decode`] if the data is not JSON of the expected shape.
    pub fn json(&self) -> Result<T> {
        serde_json::from_str(&self.data).map_err(|error| Error::decode(error, self.data.as_bytes()))
    }
}

impl<T> From<RawEvent> for Event<T> {
    fn from(raw: RawEvent) -> Self {
        Self {
            event: raw.event,
            id: raw.id,
            data: raw.data,
            payload: PhantomData,
        }
    }
}

/// Stream of server-sent events.
///
/// The connection is opened on the first poll. When it drops, the stream reconnects with
/// `Last-Event-ID` after the delay requested by the server (3 seconds by default), backing off
/// up to 30 seconds while the server is unreachable, failing or answering something other than
/// an event stream. It ends with an error when the server rejects the request, e.g. with 401
/// after a token refresh or with 403, and without one when it answers 204 No Content. Events
/// without data, such as keep-alives, are skipped; dropping the stream closes the connection.
///
/// # Examples
///
/// An event whose data cannot be decoded is best skipped rather than ending the loop:
///
/// ```no_run
/// use futures::TryStreamExt;
///
/// # async fn run(client: whitebird::Client) -> whitebird::Result<()> {
/// let mut events = client.notifications().stream();
/// while let Some(event) = events.try_next().await? {
///     match event.json() {
///         Ok(message) => println!("{:?}: {:?}", message.r#type, message.payload),
///         Err(error) => eprintln!("skipped event {:?}: {error}", event.id),
///     }
/// }
/// # Ok(())
/// # }
/// ```
#[must_use = "streams do nothing unless polled"]
pub struct EventStream<T>(BoxStream<'static, Result<Event<T>>>);

impl<T: 'static> EventStream<T> {
    pub(crate) fn new(request: RequestBuilder) -> Self {
        let connection = Connection {
            request,
            parser: Parser::default(),
            response: None,
            retry: DEFAULT_RETRY,
            failures: 0,
            finished: false,
        };
        let events = stream::unfold(connection, |mut connection| async move {
            let event = connection.next_event().await?;
            Some((event.map(Event::from), connection))
        });
        Self(events.fuse().boxed())
    }
}

impl<T> Stream for EventStream<T> {
    type Item = Result<Event<T>>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.0.poll_next_unpin(cx)
    }
}

struct Connection {
    request: RequestBuilder,
    parser: Parser,
    response: Option<Response>,
    retry: Duration,
    failures: u32,
    finished: bool,
}

impl Connection {
    async fn next_event(&mut self) -> Option<Result<RawEvent>> {
        while !self.finished {
            if let Some(event) = self.parser.next_event() {
                if event.data.trim().is_empty() {
                    continue;
                }
                return Some(Ok(event));
            }
            if let Some(response) = &mut self.response {
                if let Ok(Some(chunk)) = response.chunk().await {
                    self.parser.push(&chunk);
                    if let Some(retry) = self.parser.take_retry() {
                        self.retry = retry;
                    }
                } else {
                    self.response = None;
                    self.parser.reset();
                    tokio::time::sleep(self.retry).await;
                }
                continue;
            }
            match self.connect().await {
                Ok(response) if response.status() == StatusCode::NO_CONTENT => {
                    // The server asks not to reconnect.
                    self.finished = true;
                }
                Ok(response) if is_event_stream(&response) => {
                    self.response = Some(response);
                    self.failures = 0;
                }
                // Such as the HTML page of a proxy: try again later, like a server error.
                Ok(_) => {
                    self.failures = self.failures.saturating_add(1);
                    tokio::time::sleep(self.backoff()).await;
                }
                Err(error) if is_transient(&error) => {
                    self.failures = self.failures.saturating_add(1);
                    tokio::time::sleep(self.backoff()).await;
                }
                Err(error) => {
                    self.finished = true;
                    return Some(Err(error));
                }
            }
        }
        None
    }

    async fn connect(&self) -> Result<Response> {
        let mut request = self
            .request
            .clone()
            .header("Accept", "text/event-stream")
            .header("Cache-Control", "no-cache")
            .header("Last-Event-ID", self.parser.last_event_id());
        request.streaming = true;
        request.send().await
    }

    fn backoff(&self) -> Duration {
        let factor = 2u32.saturating_pow(self.failures.saturating_sub(1));
        self.retry.saturating_mul(factor).min(MAX_BACKOFF)
    }
}

fn is_event_stream(response: &Response) -> bool {
    response
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.to_ascii_lowercase().contains("text/event-stream"))
}

fn is_transient(error: &Error) -> bool {
    match error {
        Error::Transport(_) => true,
        Error::Api(error) => matches!(error.kind(), ErrorKind::Server | ErrorKind::RateLimited),
        _ => false,
    }
}
