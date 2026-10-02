use std::fmt;
use std::time::Duration;

use bytes::Bytes;
use percent_encoding::{AsciiSet, CONTROLS, utf8_percent_encode};
use reqwest::header::{CONTENT_TYPE, HeaderMap, HeaderName, HeaderValue};
use reqwest::{Method, StatusCode};
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;
use serde_json::value::RawValue;

use crate::client::Client;
use crate::error::{ApiError, Error, ErrorKind, Result};
use crate::generated::Service;
use crate::pagination::{PageParam, Paginator};
use crate::sse::EventStream;

/// Response wrapper that the client strips before decoding the payload.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum Envelope {
    /// `{"retCode": 0, "retMsg": "OK", "result": ...}`: `result` is the payload and a non-zero
    /// `retCode` is an error even with HTTP 200.
    Retcode,
}

/// Per-request settings applied on top of the client defaults.
///
/// # Examples
///
/// ```
/// use std::time::Duration;
/// use whitebird::header::{HeaderName, HeaderValue};
/// use whitebird::{Client, RequestOptions};
///
/// # fn main() -> whitebird::Result<()> {
/// let client = Client::builder().build()?;
/// let patient = client.with_options(
///     RequestOptions::new()
///         .timeout(Duration::from_secs(120))
///         .max_retries(5)
///         .header(HeaderName::from_static("accept-language"), HeaderValue::from_static("en")),
/// );
/// # Ok(())
/// # }
/// ```
#[derive(Debug, Clone, Default)]
#[must_use]
pub struct RequestOptions {
    pub(crate) timeout: Option<Duration>,
    pub(crate) headers: HeaderMap,
    pub(crate) max_retries: Option<u32>,
    pub(crate) idempotent: Option<bool>,
}

impl RequestOptions {
    /// Creates options that change nothing.
    pub fn new() -> Self {
        Self::default()
    }

    /// Sets the total time allowed for each attempt.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }

    /// Adds a header sent with every request.
    pub fn header(mut self, name: HeaderName, value: HeaderValue) -> Self {
        self.headers.insert(name, value);
        self
    }

    /// Sets how many times a failed idempotent request is retried.
    pub fn max_retries(mut self, max_retries: u32) -> Self {
        self.max_retries = Some(max_retries);
        self
    }

    /// Overrides whether requests may be retried. By default GET, HEAD, PUT, DELETE and OPTIONS
    /// are retried and POST is not.
    pub fn idempotent(mut self, idempotent: bool) -> Self {
        self.idempotent = Some(idempotent);
        self
    }

    pub(crate) fn merge(mut self, other: Self) -> Self {
        self.timeout = other.timeout.or(self.timeout);
        self.max_retries = other.max_retries.or(self.max_retries);
        self.idempotent = other.idempotent.or(self.idempotent);
        self.headers.extend(other.headers);
        self
    }
}

/// Multipart form body.
#[derive(Debug, Clone, Default)]
#[must_use]
pub struct Multipart {
    parts: Vec<(String, Part)>,
}

#[derive(Debug, Clone)]
enum Part {
    Text(String),
    Json(String),
    File(Vec<u8>),
}

impl Multipart {
    /// Creates an empty form.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds a text part; `None` is skipped and sequences add one part per item.
    pub fn text(mut self, name: &str, value: impl Serialize) -> Self {
        let values = serde_json::to_value(value).map(scalars).unwrap_or_default();
        self.parts.extend(
            values
                .into_iter()
                .map(|value| (name.to_owned(), Part::Text(value))),
        );
        self
    }

    /// Adds a part holding `value` as JSON, with content type `application/json`.
    pub fn json(mut self, name: &str, value: impl Serialize) -> Self {
        if let Ok(json) = serde_json::to_string(&value) {
            self.parts.push((name.to_owned(), Part::Json(json)));
        }
        self
    }

    /// Adds a file part named after the field; `None` is skipped.
    pub fn file(mut self, name: &str, data: impl Into<Option<Vec<u8>>>) -> Self {
        if let Some(data) = data.into() {
            self.parts.push((name.to_owned(), Part::File(data)));
        }
        self
    }

    fn to_form(&self) -> reqwest::multipart::Form {
        self.parts.iter().fold(
            reqwest::multipart::Form::new(),
            |form, (name, part)| match part {
                Part::Text(value) => form.text(name.clone(), value.clone()),
                Part::Json(value) => {
                    let part = reqwest::multipart::Part::text(value.clone());
                    match part.mime_str("application/json") {
                        Ok(part) => form.part(name.clone(), part),
                        Err(_) => form.text(name.clone(), value.clone()),
                    }
                }
                Part::File(data) => {
                    let file =
                        reqwest::multipart::Part::bytes(data.clone()).file_name(name.clone());
                    form.part(name.clone(), file)
                }
            },
        )
    }
}

#[derive(Debug, Clone)]
pub(crate) enum Body {
    Empty,
    Json(Value),
    Form(Value),
    Multipart(Multipart),
}

impl Body {
    pub(crate) fn apply(&self, http: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        match self {
            Self::Empty => http,
            Self::Json(value) => http
                .header(CONTENT_TYPE, "application/json")
                .body(value.to_string()),
            Self::Form(value) => {
                let mut form = form_urlencoded::Serializer::new(String::new());
                if let Value::Object(fields) = value {
                    for (name, value) in fields {
                        for value in scalars(value.clone()) {
                            form.append_pair(name, &value);
                        }
                    }
                }
                http.header(CONTENT_TYPE, "application/x-www-form-urlencoded")
                    .body(form.finish())
            }
            Self::Multipart(form) => http.multipart(form.to_form()),
        }
    }
}

/// A request to any endpoint, sent with the client's authentication, retries and error
/// handling. Created by [`Client::request`].
///
/// Parameter values accept anything serializable: `None` is skipped, sequences repeat the
/// parameter and enums, timestamps and decimals are sent in their wire format.
#[derive(Debug, Clone)]
#[must_use = "a request does nothing until it is sent"]
pub struct RequestBuilder {
    pub(crate) client: Client,
    pub(crate) service: Service,
    pub(crate) method: Method,
    pub(crate) path: String,
    pub(crate) query: Vec<(String, String)>,
    pub(crate) headers: Vec<(String, String)>,
    pub(crate) body: Body,
    pub(crate) public: bool,
    pub(crate) streaming: bool,
    pub(crate) envelope: Option<Envelope>,
    pub(crate) options: RequestOptions,
    pub(crate) invalid: Option<String>,
}

impl RequestBuilder {
    pub(crate) fn new(client: Client, service: Service, method: Method, path: String) -> Self {
        let options = client.options.clone();
        Self {
            client,
            service,
            method,
            path,
            query: Vec::new(),
            headers: Vec::new(),
            body: Body::Empty,
            public: false,
            streaming: false,
            envelope: None,
            options,
            invalid: None,
        }
    }

    /// Adds a query parameter.
    pub fn query(mut self, name: &str, value: impl Serialize) -> Self {
        match serde_json::to_value(value) {
            Ok(value) => self.query.extend(
                scalars(value)
                    .into_iter()
                    .map(|value| (name.to_owned(), value)),
            ),
            Err(error) => {
                self.invalid = Some(format!("cannot encode query parameter {name}: {error}"));
            }
        }
        self
    }

    /// Adds a header.
    pub fn header(mut self, name: &str, value: impl Serialize) -> Self {
        match serde_json::to_value(value) {
            Ok(value) => self.headers.extend(
                scalars(value)
                    .into_iter()
                    .map(|value| (name.to_owned(), value)),
            ),
            Err(error) => self.invalid = Some(format!("cannot encode header {name}: {error}")),
        }
        self
    }

    /// Sets a JSON body.
    pub fn json(mut self, body: impl Serialize) -> Self {
        match serde_json::to_value(body) {
            Ok(value) => self.body = Body::Json(value),
            Err(error) => self.invalid = Some(format!("cannot encode request body: {error}")),
        }
        self
    }

    /// Sets a form-encoded body from a struct or map.
    pub fn form(mut self, body: impl Serialize) -> Self {
        match serde_json::to_value(body) {
            Ok(value @ Value::Object(_)) => self.body = Body::Form(value),
            Ok(_) => self.invalid = Some("a form body must be a struct or a map".into()),
            Err(error) => self.invalid = Some(format!("cannot encode request body: {error}")),
        }
        self
    }

    /// Sets a multipart body.
    pub fn multipart(mut self, form: Multipart) -> Self {
        self.body = Body::Multipart(form);
        self
    }

    /// Sends the request without a bearer token, for public endpoints.
    pub fn public(mut self) -> Self {
        self.public = true;
        self
    }

    /// Unwraps the given envelope from the response.
    pub fn envelope(mut self, envelope: Envelope) -> Self {
        self.envelope = Some(envelope);
        self
    }

    /// Applies per-request options on top of those of the client.
    pub fn options(mut self, options: RequestOptions) -> Self {
        self.options = self.options.merge(options);
        self
    }

    /// Sends the request.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Api`] for an error answer (after the retries allowed for the method),
    /// [`Error::Transport`] when the API cannot be reached and [`Error::Config`] for an invalid
    /// request.
    pub async fn send(self) -> Result<Response> {
        let response = self.client.execute(&self).await?;
        Ok(Response {
            inner: response,
            envelope: self.envelope,
        })
    }

    /// Turns the request into a lazy stream over the items of a Spring Data page. Iteration
    /// starts from the page number already set on the request, or from page 0.
    pub fn paginate<T>(self, param: PageParam) -> Paginator<T>
    where
        T: DeserializeOwned + Send + 'static,
    {
        let start = self.page_number(param).unwrap_or_default();
        Paginator::new(self, param, start)
    }

    /// Turns the request into a stream of server-sent events that reconnects automatically.
    pub fn events<T: 'static>(self) -> EventStream<T> {
        EventStream::new(self)
    }

    fn page_number(&self, param: PageParam) -> Option<u32> {
        match (param, &self.body) {
            (PageParam::Query(name), _) => {
                let (_, value) = self.query.iter().rev().find(|(key, _)| key == name)?;
                value.parse().ok()
            }
            (
                PageParam::Body(name),
                Body::Json(Value::Object(fields)) | Body::Form(Value::Object(fields)),
            ) => fields.get(name)?.as_u64()?.try_into().ok(),
            (PageParam::Body(_), _) => None,
        }
    }

    pub(crate) fn with_page(mut self, param: PageParam, number: u32) -> Self {
        match (param, &mut self.body) {
            (PageParam::Query(name), _) => {
                self.query.retain(|(key, _)| key != name);
                self.query.push((name.to_owned(), number.to_string()));
            }
            (
                PageParam::Body(name),
                Body::Json(Value::Object(fields)) | Body::Form(Value::Object(fields)),
            ) => {
                fields.insert(name.to_owned(), number.into());
            }
            (PageParam::Body(name), body) => {
                *body = Body::Json(Value::Object(
                    [(name.to_owned(), number.into())].into_iter().collect(),
                ));
            }
        }
        self
    }
}

/// Successful response to a [`RequestBuilder`].
#[derive(Debug)]
pub struct Response {
    inner: reqwest::Response,
    envelope: Option<Envelope>,
}

impl Response {
    /// HTTP status.
    pub fn status(&self) -> StatusCode {
        self.inner.status()
    }

    /// Response headers.
    pub fn headers(&self) -> &HeaderMap {
        self.inner.headers()
    }

    /// Decodes the JSON body, unwrapping the envelope if the request has one. An empty body
    /// decodes as `null`.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Decode`] if the body does not match `T`, [`Error::Api`] if the envelope
    /// reports an error and [`Error::Transport`] if the body cannot be read.
    pub async fn json<T: DeserializeOwned>(self) -> Result<T> {
        let status = self.inner.status();
        let bytes = self.inner.bytes().await?;
        let body: &[u8] = if bytes.trim_ascii().is_empty() {
            b"null"
        } else {
            &bytes
        };
        match self.envelope {
            None => serde_json::from_slice(body).map_err(|error| Error::decode(error, body)),
            Some(Envelope::Retcode) => unwrap_retcode(status, body),
        }
    }

    /// Returns the raw body.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Transport`] if the body cannot be read.
    pub async fn bytes(self) -> Result<Bytes> {
        Ok(self.inner.bytes().await?)
    }

    /// Returns the body as text.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Transport`] if the body cannot be read.
    pub async fn text(self) -> Result<String> {
        Ok(self.inner.text().await?)
    }

    pub(crate) async fn chunk(&mut self) -> Result<Option<Bytes>> {
        Ok(self.inner.chunk().await?)
    }
}

fn unwrap_retcode<T: DeserializeOwned>(status: StatusCode, body: &[u8]) -> Result<T> {
    #[derive(serde::Deserialize)]
    struct Retcode<'a> {
        #[serde(rename = "retCode")]
        code: i64,
        #[serde(rename = "retMsg", default)]
        message: String,
        #[serde(borrow, default)]
        result: Option<&'a RawValue>,
    }

    let envelope: Retcode<'_> =
        serde_json::from_slice(body).map_err(|error| Error::decode(error, body))?;
    if envelope.code != 0 {
        return Err(
            ApiError::new(ErrorKind::Other, status, envelope.message, body)
                .with_code(envelope.code)
                .into(),
        );
    }
    let result = envelope.result.map_or("null", RawValue::get);
    serde_json::from_str(result).map_err(|error| Error::decode(error, result.as_bytes()))
}

/// Flattens a serialized parameter into the strings sent on the wire.
fn scalars(value: Value) -> Vec<String> {
    match value {
        Value::Null => Vec::new(),
        Value::String(value) => vec![value],
        Value::Array(items) => items.into_iter().flat_map(scalars).collect(),
        other => vec![other.to_string()],
    }
}

/// Percent-encodes a path parameter.
pub(crate) fn path_segment(value: impl fmt::Display) -> String {
    const SEGMENT: &AsciiSet = &CONTROLS
        .add(b' ')
        .add(b'"')
        .add(b'#')
        .add(b'%')
        .add(b'/')
        .add(b'<')
        .add(b'>')
        .add(b'?')
        .add(b'`')
        .add(b'{')
        .add(b'}');
    utf8_percent_encode(&value.to_string(), SEGMENT).to_string()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn scalars_skip_null_and_repeat_sequences() {
        assert!(scalars(Value::Null).is_empty());
        assert_eq!(scalars(json!(["A", "B"])), ["A", "B"]);
        assert_eq!(scalars(json!(5)), ["5"]);
        assert_eq!(scalars(json!(true)), ["true"]);
    }

    #[test]
    fn path_segments_are_encoded() {
        assert_eq!(path_segment("a b/c?d"), "a%20b%2Fc%3Fd");
        assert_eq!(path_segment(42), "42");
    }
}
