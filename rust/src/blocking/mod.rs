//! Blocking client, for programs that do not use async Rust.
//!
//! [`Client`] wraps the asynchronous [`crate::Client`] and runs each call to completion on an
//! internal current-thread Tokio runtime. Resources, parameters and models are the same; paginated
//! endpoints return an [`Iterator`] and event streams an [`Iterator`] too.
//!
//! The blocking client must not be used from inside an async runtime: blocking calls panic
//! there. Use the asynchronous client instead.
//!
//! ```no_run
//! use whitebird::params::BrokerageOtcOrdersListParams;
//!
//! # fn main() -> whitebird::Result<()> {
//! let client = whitebird::Client::builder()
//!     .credentials("me@example.com", "password")
//!     .build_blocking()?;
//!
//! let subaccount = client.brokerage().subaccounts().me()?;
//! for order in client.brokerage().otc_orders().list(BrokerageOtcOrdersListParams::default()) {
//!     println!("{:?}", order?.status);
//! }
//! # Ok(())
//! # }
//! ```

mod stream;

use std::future::Future;
use std::sync::Arc;

use reqwest::header::HeaderMap;
use reqwest::{Method, StatusCode};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::runtime::Runtime;

pub use self::stream::{EventStream, Pages, Paginator};
#[doc(inline)]
pub use crate::generated::blocking as resources;
use crate::{
    Bytes, Envelope, Environment, Error, Multipart, PageParam, RequestOptions, Result, Service,
    Tokens,
};

/// Blocking Whitebird client, created by [`ClientBuilder::build_blocking`](crate::ClientBuilder::build_blocking).
///
/// Cloning is cheap: clones share the runtime, the connection pool and the session.
#[derive(Debug, Clone)]
pub struct Client {
    inner: crate::Client,
    runtime: Arc<Runtime>,
}

impl Client {
    pub(crate) fn new(inner: crate::Client) -> Result<Self> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|error| Error::Config(format!("cannot start the runtime: {error}")))?;
        Ok(Self {
            inner,
            runtime: Arc::new(runtime),
        })
    }

    /// Logs in with the configured credentials and returns the new tokens.
    ///
    /// # Errors
    ///
    /// See [`crate::Client::login`].
    pub fn login(&self) -> Result<Tokens> {
        self.block_on(self.inner.login())
    }

    /// Exchanges a one-time code for an operation MFA token, stores it and returns it.
    ///
    /// # Errors
    ///
    /// See [`crate::Client::confirm_operation`].
    pub fn confirm_operation(&self, operation: &str, code: Option<&str>) -> Result<String> {
        self.block_on(self.inner.confirm_operation(operation, code))
    }

    /// Sets the operation MFA token sent as `x-mfa-token` with every request.
    pub fn set_mfa_token(&self, token: impl Into<String>) {
        self.inner.set_mfa_token(token);
    }

    /// Stops sending the operation MFA token.
    pub fn clear_mfa_token(&self) {
        self.inner.clear_mfa_token();
    }

    /// Returns the operation MFA token, if one is set.
    pub fn mfa_token(&self) -> Option<String> {
        self.inner.mfa_token()
    }

    /// Environment the client talks to.
    pub fn environment(&self) -> Environment {
        self.inner.environment()
    }

    /// Base URL used for `service`.
    pub fn base_url(&self, service: Service) -> &str {
        self.inner.base_url(service)
    }

    /// Returns a client that applies `options` to every request it sends.
    #[must_use]
    pub fn with_options(&self, options: RequestOptions) -> Self {
        Self {
            inner: self.inner.with_options(options),
            runtime: Arc::clone(&self.runtime),
        }
    }

    /// Builds a request to any endpoint; see [`crate::Client::request`].
    pub fn request(
        &self,
        service: Service,
        method: Method,
        path: impl Into<String>,
    ) -> RequestBuilder {
        RequestBuilder {
            inner: self.inner.request(service, method, path),
            client: self.clone(),
        }
    }

    /// The asynchronous client this one wraps.
    pub fn as_async(&self) -> &crate::Client {
        &self.inner
    }

    pub(crate) fn block_on<F: Future>(&self, future: F) -> F::Output {
        self.runtime.block_on(future)
    }
}

/// Blocking counterpart of [`crate::RequestBuilder`].
#[derive(Debug, Clone)]
#[must_use = "a request does nothing until it is sent"]
pub struct RequestBuilder {
    inner: crate::RequestBuilder,
    client: Client,
}

impl RequestBuilder {
    /// Adds a query parameter.
    pub fn query(self, name: &str, value: impl Serialize) -> Self {
        self.map(|request| request.query(name, value))
    }

    /// Adds a header.
    pub fn header(self, name: &str, value: impl Serialize) -> Self {
        self.map(|request| request.header(name, value))
    }

    /// Sets a JSON body.
    pub fn json(self, body: impl Serialize) -> Self {
        self.map(|request| request.json(body))
    }

    /// Sets a form-encoded body from a struct or map.
    pub fn form(self, body: impl Serialize) -> Self {
        self.map(|request| request.form(body))
    }

    /// Sets a multipart body.
    pub fn multipart(self, form: Multipart) -> Self {
        self.map(|request| request.multipart(form))
    }

    /// Sends the request without a bearer token, for public endpoints.
    pub fn public(self) -> Self {
        self.map(crate::RequestBuilder::public)
    }

    /// Unwraps the given envelope from the response.
    pub fn envelope(self, envelope: Envelope) -> Self {
        self.map(|request| request.envelope(envelope))
    }

    /// Marks the request as a sensitive operation that needs an operation MFA token.
    pub fn mfa_operation(self, operation: impl Into<String>) -> Self {
        self.map(|request| request.mfa_operation(operation))
    }

    /// Applies per-request options on top of those of the client.
    pub fn options(self, options: RequestOptions) -> Self {
        self.map(|request| request.options(options))
    }

    /// Sends the request.
    ///
    /// # Errors
    ///
    /// See [`crate::RequestBuilder::send`].
    pub fn send(self) -> Result<Response> {
        let inner = self.client.block_on(self.inner.send())?;
        Ok(Response {
            inner,
            runtime: self.client.runtime,
        })
    }

    /// Turns the request into a lazy iterator over the items of a Spring Data page; see
    /// [`crate::RequestBuilder::paginate`].
    pub fn paginate<T>(self, param: PageParam) -> Paginator<T>
    where
        T: DeserializeOwned + Send + 'static,
    {
        Paginator::new(&self.client, self.inner.paginate(param))
    }

    /// Turns the request into an iterator of server-sent events that reconnects automatically.
    pub fn events<T: 'static>(self) -> EventStream<T> {
        EventStream::new(&self.client, self.inner.events())
    }

    fn map(self, f: impl FnOnce(crate::RequestBuilder) -> crate::RequestBuilder) -> Self {
        Self {
            inner: f(self.inner),
            client: self.client,
        }
    }
}

/// Blocking counterpart of [`crate::Response`].
#[derive(Debug)]
pub struct Response {
    inner: crate::Response,
    runtime: Arc<Runtime>,
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

    /// Decodes the JSON body, unwrapping the envelope if the request has one.
    ///
    /// # Errors
    ///
    /// See [`crate::Response::json`].
    pub fn json<T: DeserializeOwned>(self) -> Result<T> {
        self.runtime.block_on(self.inner.json())
    }

    /// Returns the raw body.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Transport`] if the body cannot be read.
    pub fn bytes(self) -> Result<Bytes> {
        self.runtime.block_on(self.inner.bytes())
    }

    /// Returns the body as text.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Transport`] if the body cannot be read.
    pub fn text(self) -> Result<String> {
        self.runtime.block_on(self.inner.text())
    }
}
