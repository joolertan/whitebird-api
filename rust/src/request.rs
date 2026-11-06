use std::fmt;
use std::time::Duration;

use bytes::Bytes;
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use reqwest::header::{CONTENT_TYPE, HeaderMap, HeaderName, HeaderValue};
use reqwest::{Method, StatusCode};
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;
use serde_json::value::RawValue;

use crate::client::Client;
use crate::error::{ApiError, Error, ErrorKind, Result};
use crate::file::File;
use crate::generated::Service;
use crate::pagination::{PageParam, Paginator};
use crate::sse::EventStream;

/// Response wrapper that the client strips before decoding the payload.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum Envelope {
    /// `{"retCode": 0, "retMsg": "OK", "result": ...}` of the trading services: `result` is
    /// the payload (the whole body if it has none). A `retCode` (or `code`) other than 0 and
    /// 200 is an error even with HTTP 200, described by `retMsg` (or `message`).
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

    /// Overrides whether requests are idempotent. By default GET, HEAD and OPTIONS are, and
    /// are retried after any transient failure; other requests are retried only when the
    /// connection could not be established or on HTTP 429.
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
///
/// # Examples
///
/// ```
/// use whitebird::{File, Multipart};
///
/// let form = Multipart::new()
///     .json("request", serde_json::json!({ "criteria": ["certifiedExpert"] }))
///     .files("files", [File::new(b"%PDF-1.7".to_vec()).with_name("diploma.pdf")])
///     .file("photo", b"\xFF\xD8\xFF".to_vec())
///     .text("comment", "two diplomas");
/// ```
#[derive(Debug, Clone, Default)]
#[must_use]
pub struct Multipart {
    parts: Vec<Part>,
}

#[derive(Debug, Clone)]
struct Part {
    name: String,
    content: Content,
    content_type: Option<String>,
}

#[derive(Debug, Clone)]
enum Content {
    Text(String),
    File(File),
}

impl Multipart {
    /// Creates an empty form.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds text parts: `None` is skipped, a sequence adds one part per item and objects are
    /// added as JSON parts.
    pub fn text(mut self, name: &str, value: impl Serialize) -> Self {
        let mut pending = match serde_json::to_value(value) {
            Ok(Value::Array(items)) => items,
            Ok(value) => vec![value],
            Err(_) => Vec::new(),
        };
        pending.reverse();
        while let Some(value) = pending.pop() {
            match value {
                Value::Null => {}
                Value::Array(items) => pending.extend(items.into_iter().rev()),
                Value::String(text) => self.push(name, Content::Text(text), None),
                object @ Value::Object(_) => self.push_json(name, &object),
                scalar => self.push(name, Content::Text(scalar.to_string()), None),
            }
        }
        self
    }

    /// Adds `value` as one JSON part with content type `application/json`; `None` is skipped.
    pub fn json(mut self, name: &str, value: impl Serialize) -> Self {
        match serde_json::to_value(value) {
            Ok(Value::Null) | Err(_) => {}
            Ok(value) => self.push_json(name, &value),
        }
        self
    }

    /// Adds the JSON text of `value` as a plain text part, the way a browser sends
    /// `JSON.stringify(value)`; `None` is skipped.
    pub fn json_text(mut self, name: &str, value: impl Serialize) -> Self {
        match serde_json::to_value(value) {
            Ok(Value::Null) | Err(_) => {}
            Ok(value) => self.push(name, Content::Text(value.to_string()), None),
        }
        self
    }

    /// Adds a file part. See [`File`] for the name and media type it is sent with.
    pub fn file(mut self, name: &str, file: impl Into<File>) -> Self {
        self.push(name, Content::File(file.into()), None);
        self
    }

    /// Adds one file part per item, all under `name`; an `Option` adds one part or none.
    pub fn files<F: Into<File>>(mut self, name: &str, files: impl IntoIterator<Item = F>) -> Self {
        for file in files {
            self.push(name, Content::File(file.into()), None);
        }
        self
    }

    /// Sets the media type of the parts named `name` added so far.
    pub fn content_type(mut self, name: &str, media_type: &str) -> Self {
        for part in self.parts.iter_mut().filter(|part| part.name == name) {
            part.content_type = Some(media_type.to_owned());
        }
        self
    }

    pub(crate) fn has(&self, name: &str) -> bool {
        self.parts.iter().any(|part| part.name == name)
    }

    fn push(&mut self, name: &str, content: Content, content_type: Option<&str>) {
        self.parts.push(Part {
            name: name.to_owned(),
            content,
            content_type: content_type.map(str::to_owned),
        });
    }

    fn push_json(&mut self, name: &str, value: &Value) {
        self.push(
            name,
            Content::Text(value.to_string()),
            Some("application/json"),
        );
    }

    /// Returns the first media type that is not valid.
    fn invalid_media_type(&self) -> Option<&str> {
        self.parts
            .iter()
            .filter_map(Part::media_type)
            .find(|media_type| {
                reqwest::multipart::Part::text("")
                    .mime_str(media_type)
                    .is_err()
            })
    }

    fn to_form(&self) -> reqwest::multipart::Form {
        self.parts
            .iter()
            .fold(reqwest::multipart::Form::new(), |form, part| {
                form.part(part.name.clone(), part.to_part())
            })
    }
}

impl Part {
    fn to_part(&self) -> reqwest::multipart::Part {
        let part = || match &self.content {
            Content::Text(text) => reqwest::multipart::Part::text(text.clone()),
            Content::File(file) => reqwest::multipart::Part::stream_with_length(
                reqwest::Body::from(file.data.clone()),
                file.data.len() as u64,
            )
            .file_name(file.file_name(&self.name)),
        };
        // Media types are validated when the form is set on the request.
        match self.media_type() {
            Some(media_type) => part().mime_str(media_type).unwrap_or_else(|_| part()),
            None => part(),
        }
    }

    fn media_type(&self) -> Option<&str> {
        match (&self.content_type, &self.content) {
            (Some(media_type), _) => Some(media_type),
            (None, Content::File(file)) => Some(file.media_type()),
            (None, Content::Text(_)) => None,
        }
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
    pub(crate) mfa_operation: Option<String>,
    pub(crate) session: Option<SessionEffect>,
    pub(crate) options: RequestOptions,
    pub(crate) invalid: Option<String>,
}

/// What a request does to the client's session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SessionEffect {
    /// The response carries a new token pair that replaces the session (password change).
    Replace,
    /// The session ends after the request, whatever its outcome (logout).
    End,
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
            mfa_operation: None,
            session: None,
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

    /// Adds a query parameter unless one with this name was added.
    pub(crate) fn default_query(self, name: &str, value: impl Serialize) -> Self {
        if self.query.iter().any(|(key, _)| key == name) {
            self
        } else {
            self.query(name, value)
        }
    }

    /// Adds a header unless one with this name was added.
    #[allow(
        dead_code,
        reason = "used by generated code when a header parameter has a default"
    )]
    pub(crate) fn default_header(self, name: &str, value: impl Serialize) -> Self {
        if self
            .headers
            .iter()
            .any(|(key, _)| key.eq_ignore_ascii_case(name))
        {
            self
        } else {
            self.header(name, value)
        }
    }

    /// Sets a field of the object body (JSON, form or multipart) unless it is set.
    pub(crate) fn default_field(mut self, name: &str, value: impl Serialize) -> Self {
        match &mut self.body {
            Body::Json(Value::Object(fields)) | Body::Form(Value::Object(fields)) => {
                if fields.get(name).is_none_or(Value::is_null) {
                    match serde_json::to_value(value) {
                        Ok(value) => {
                            fields.insert(name.to_owned(), value);
                        }
                        Err(error) => {
                            self.invalid =
                                Some(format!("cannot encode body field {name}: {error}"));
                        }
                    }
                }
            }
            Body::Multipart(form) if !form.has(name) => {
                *form = std::mem::take(form).text(name, value);
            }
            _ => {}
        }
        self
    }

    pub(crate) fn ends_session(mut self) -> Self {
        self.session = Some(SessionEffect::End);
        self
    }

    pub(crate) fn replaces_session(mut self) -> Self {
        self.session = Some(SessionEffect::Replace);
        self
    }

    /// Sets a multipart body.
    pub fn multipart(mut self, form: Multipart) -> Self {
        if let Some(media_type) = form.invalid_media_type() {
            self.invalid = Some(format!("invalid multipart media type {media_type:?}"));
        }
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

    /// Marks the request as a sensitive operation, such as `WITHDRAWAL`, that needs an operation
    /// MFA token; see [`Client::confirm_operation`].
    pub fn mfa_operation(mut self, operation: impl Into<String>) -> Self {
        self.mfa_operation = Some(operation.into());
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
        let generation = self.client.session_generation();
        let result = match &self.mfa_operation {
            Some(operation) => self.client.execute_with_mfa(&self, operation).await,
            None => self.client.execute(&self).await,
        };
        let response = match self.session {
            Some(SessionEffect::End) => {
                self.client.end_session();
                result?
            }
            Some(SessionEffect::Replace) => {
                // A password change: later logins use the new password.
                let new_password = match &self.body {
                    Body::Json(Value::Object(fields)) | Body::Form(Value::Object(fields)) => {
                        fields.get("newPassword").and_then(Value::as_str)
                    }
                    _ => None,
                };
                self.client
                    .replace_session(result?, generation, new_password)
                    .await?
            }
            None => result?,
        };
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

    /// Like [`json`](Self::json), for payloads without a fixed shape (`T` is `String` or
    /// `serde_json::Value`): a body that is not JSON of that type is returned as text.
    #[allow(
        dead_code,
        reason = "used by generated code when an operation returns untyped JSON"
    )]
    pub(crate) async fn json_or_text<T: DeserializeOwned>(self) -> Result<T> {
        if self.envelope.is_some() {
            return self.json().await;
        }
        let bytes = self.inner.bytes().await?;
        let text = || Value::String(String::from_utf8_lossy(&bytes).into_owned());
        if bytes.trim_ascii().is_empty() {
            return serde_json::from_value(Value::Null)
                .or_else(|_| serde_json::from_value(text()))
                .map_err(|error| Error::decode(error, &bytes));
        }
        serde_json::from_slice(&bytes).or_else(|error| {
            serde_json::from_value(text()).map_err(|_| Error::decode(error, &bytes))
        })
    }

    /// Reads the whole body of `response`, returning a response that still has it.
    pub(crate) async fn buffer(response: reqwest::Response) -> Result<(reqwest::Response, Bytes)> {
        let status = response.status();
        let version = response.version();
        let headers = response.headers().clone();
        let body = response.bytes().await?;
        let mut rebuilt = http::Response::new(body.clone());
        *rebuilt.status_mut() = status;
        *rebuilt.version_mut() = version;
        *rebuilt.headers_mut() = headers;
        Ok((reqwest::Response::from(rebuilt), body))
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
        #[serde(rename = "retCode", default)]
        ret_code: Option<Value>,
        #[serde(default)]
        code: Option<Value>,
        #[serde(rename = "retMsg", default)]
        ret_msg: Option<Value>,
        #[serde(default)]
        message: Option<Value>,
        #[serde(borrow, default, deserialize_with = "present")]
        result: Option<&'a RawValue>,
    }

    /// Keeps `"result": null` apart from a missing `result`.
    fn present<'de, D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Option<&'de RawValue>, D::Error> {
        serde::Deserialize::deserialize(deserializer).map(Some)
    }

    let decode = |payload: &[u8]| {
        serde_json::from_slice(payload).map_err(|error| Error::decode(error, payload))
    };
    // A body that is not an object has no envelope.
    let envelope = match serde_json::from_slice::<Retcode<'_>>(body) {
        Ok(envelope) if body.trim_ascii_start().starts_with(b"{") => envelope,
        _ => return decode(body),
    };
    let number = |value: Option<&Value>| match value {
        Some(Value::Number(number)) => number.as_i64(),
        Some(Value::String(text)) => text.trim().parse().ok(),
        _ => None,
    };
    let code = number(envelope.ret_code.as_ref()).or_else(|| number(envelope.code.as_ref()));
    if let Some(code) = code.filter(|code| !matches!(code, 0 | 200)) {
        let message = [&envelope.ret_msg, &envelope.message]
            .into_iter()
            .find_map(|text| text.as_ref()?.as_str().filter(|text| !text.is_empty()))
            .unwrap_or_default();
        return Err(ApiError::new(ErrorKind::Other, status, message, body)
            .with_code(code)
            .into());
    }
    match envelope.result {
        Some(result) => decode(result.get().as_bytes()),
        None => decode(body),
    }
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

/// Replaces the `{}` placeholders of `template` with the percent-encoded `params`. Values that
/// would change the path, such as `..`, are rejected.
pub(crate) fn fill_path(template: &str, params: &[&dyn fmt::Display]) -> Result<String, String> {
    const SEGMENT: &AsciiSet = &NON_ALPHANUMERIC
        .remove(b'-')
        .remove(b'.')
        .remove(b'_')
        .remove(b'~');
    let mut path = String::with_capacity(template.len());
    let mut pieces = template.split("{}");
    path.push_str(pieces.next().unwrap_or_default());
    let mut params = params.iter();
    for piece in pieces {
        let value = params
            .next()
            .ok_or_else(|| format!("missing path parameter in {template}"))?
            .to_string();
        if matches!(value.as_str(), "" | "." | "..") {
            return Err(format!("invalid path parameter {value:?} for {template}"));
        }
        path.extend(utf8_percent_encode(&value, SEGMENT));
        path.push_str(piece);
    }
    Ok(path)
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
    fn path_parameters_are_encoded() {
        let fill = |value: &dyn fmt::Display| fill_path("/a/{}/b", &[value]);
        assert_eq!(fill(&"x y/c?d").unwrap(), "/a/x%20y%2Fc%3Fd/b");
        assert_eq!(fill(&r"..\..\x").unwrap(), "/a/..%5C..%5Cx/b");
        assert_eq!(fill(&"%2e%2e").unwrap(), "/a/%252e%252e/b");
        assert_eq!(fill(&"v1.2_x-y~z").unwrap(), "/a/v1.2_x-y~z/b");
        assert_eq!(fill(&42).unwrap(), "/a/42/b");
        for value in ["", ".", ".."] {
            assert!(fill(&value).is_err(), "{value:?}");
        }
        assert_eq!(fill_path("/{}/{}", &[&"a", &7]).unwrap(), "/a/7");
    }

    #[test]
    fn retcode_envelopes() {
        let ok = |body: &str| unwrap_retcode::<Value>(StatusCode::OK, body.as_bytes());
        assert_eq!(ok(r#"{"retCode": 0, "result": [1]}"#).unwrap(), json!([1]));
        assert_eq!(
            ok(r#"{"retCode": 200, "result": null}"#).unwrap(),
            Value::Null
        );
        assert_eq!(ok(r#"{"id": 5}"#).unwrap(), json!({ "id": 5 }));
        assert_eq!(ok("[1, 2]").unwrap(), json!([1, 2]));
        let error = ok(r#"{"code": "19", "message": "Order not found"}"#).unwrap_err();
        let api = error.api().unwrap();
        assert_eq!(api.code(), Some(19));
        assert_eq!(api.message(), "Order not found");
        let error = ok(r#"{"retCode": 10001, "retMsg": "params error"}"#).unwrap_err();
        assert_eq!(error.api().unwrap().message(), "params error");
    }
}
