use std::collections::HashMap;
use std::fmt;
use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use reqwest::header::{ACCEPT_LANGUAGE, HeaderValue, USER_AGENT};
use reqwest::{Method, StatusCode, Url};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::auth::{Credentials, Endpoints, MemoryTokenStore, Session, TokenStore, Tokens};
use crate::captcha::CaptchaSolver;
use crate::error::{ApiError, Error, ErrorKind, Result};
use crate::generated::{
    Environment, OAUTH_BASIC, OAUTH_CAPTCHA_PATH, OAUTH_SERVICE, OAUTH_TOKEN_PATH,
    OPERATION_MFA_SERVICE, OPERATION_MFA_TOKEN_PATH, Service,
};
use crate::otp::CodeProvider;
use crate::request::{self, RequestBuilder, RequestOptions, Response};
use crate::retry::{self, RetryPolicy};

pub(crate) const USER_AGENT_VALUE: &str = concat!("whitebird-api-rust/", env!("CARGO_PKG_VERSION"));
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);
const MFA_TOKEN_HEADER: &str = "x-mfa-token";

/// Asynchronous Whitebird client.
///
/// Cloning is cheap: clones share the connection pool, the session and the MFA token.
/// Endpoints are grouped by resource, e.g. `client.brokerage().otc_orders()`.
///
/// # Examples
///
/// ```no_run
/// use whitebird::{Client, Environment};
///
/// # async fn run() -> whitebird::Result<()> {
/// let client = Client::builder()
///     .credentials("me@example.com", "password")
///     .environment(Environment::Demo)
///     .build()?;
/// let subaccount = client.brokerage().subaccounts().me().await?;
/// # Ok(())
/// # }
/// ```
#[derive(Clone)]
pub struct Client {
    inner: Arc<Inner>,
    pub(crate) options: RequestOptions,
}

struct Inner {
    http: reqwest::Client,
    environment: Environment,
    base_urls: HashMap<Service, String>,
    session: Session,
    retry: RetryPolicy,
    timeout: Duration,
}

impl Client {
    /// Starts configuring a client.
    pub fn builder() -> ClientBuilder {
        ClientBuilder::default()
    }

    /// Logs in with the configured credentials and returns the new tokens.
    ///
    /// Login otherwise happens lazily on the first authenticated call; calling this first
    /// surfaces credential and two-factor problems early. The client sends the password alone
    /// and, when the server asks for it, again with a code from the
    /// [code provider](ClientBuilder::otp). When the server asks for a captcha, every later
    /// request of the sign-in carries an answer from the [solver](ClientBuilder::captcha).
    ///
    /// # Errors
    ///
    /// Returns an error for which [`Error::is_mfa_required`] is `true` if the server asks for a
    /// code and no code provider is configured, [`Error::is_captcha_required`] if the server
    /// asks for a captcha and no solver is configured (or three answers were rejected), an
    /// [`ErrorKind::Authentication`](crate::ErrorKind::Authentication) error if there is no
    /// account with this username or the password or the code is rejected, and
    /// [`Error::Config`] if no credentials are configured.
    pub async fn login(&self) -> Result<Tokens> {
        self.inner.session.login().await
    }

    /// Exchanges a one-time code for the operation MFA token that sensitive operations such as
    /// withdrawals need, stores it on the client (see [`set_mfa_token`](Self::set_mfa_token))
    /// and returns it.
    ///
    /// Without `code`, one comes from the [code provider](ClientBuilder::otp). With a provider
    /// configured the client calls this by itself when such an operation is refused for lack
    /// of a valid token, then sends the operation again; concurrent refusals share one
    /// confirmation.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Config`] if there is no `code` and no code provider, and an API error if
    /// the code is rejected (`INVALID_MFA_CODE`).
    ///
    /// # Examples
    ///
    /// ```no_run
    /// # async fn run(client: whitebird::Client) -> whitebird::Result<()> {
    /// client.confirm_operation("WITHDRAWAL", Some("123456")).await?;
    /// // Withdrawals made with `client` now carry the token.
    /// # Ok(())
    /// # }
    /// ```
    pub async fn confirm_operation(&self, operation: &str, code: Option<&str>) -> Result<String> {
        let code = match code {
            Some(code) => code.trim().to_owned(),
            None => match &self.inner.session.otp {
                Some(otp) => otp.code().await?,
                None => {
                    return Err(Error::Config(
                        "no one-time code: pass one or configure ClientBuilder::otp".into(),
                    ));
                }
            },
        };
        let request = self
            .request(
                OPERATION_MFA_SERVICE,
                Method::POST,
                OPERATION_MFA_TOKEN_PATH,
            )
            .json(json!({ "operation": operation, "mfaCode": code }));
        let token = issued_mfa_token(self.execute(&request).await?).await?;
        self.set_mfa_token(token.clone());
        Ok(token)
    }

    /// Sends a request for an operation that needs an MFA token, like the web app: with the
    /// token held, if any; when the operation is refused for lack of a valid token and a code
    /// provider is configured, obtains a token and sends it once more.
    pub(crate) async fn execute_with_mfa(
        &self,
        request: &RequestBuilder,
        operation: &str,
    ) -> Result<reqwest::Response> {
        let sent = self.mfa_token();
        match self.execute(request).await {
            Err(error) if error.is_mfa_required() && self.inner.session.otp.is_some() => {
                self.renew_mfa_token(operation, sent.as_deref()).await?;
                self.execute(request).await
            }
            result => result,
        }
    }

    /// Obtains a new operation MFA token after `rejected` (or no token) was refused, unless a
    /// concurrent operation already did.
    async fn renew_mfa_token(&self, operation: &str, rejected: Option<&str>) -> Result<()> {
        let _confirmation = self.inner.session.confirmation.lock().await;
        if self
            .mfa_token()
            .is_some_and(|token| Some(token.as_str()) != rejected)
        {
            return Ok(());
        }
        self.confirm_operation(operation, None).await.map(drop)
    }

    /// Sets the operation MFA token sent as `x-mfa-token` with every request, as required by
    /// sensitive operations such as withdrawals.
    pub fn set_mfa_token(&self, token: impl Into<String>) {
        let token: String = token.into();
        self.inner
            .session
            .set_mfa_token(Some(token.trim().to_owned()));
    }

    /// Stops sending the operation MFA token.
    pub fn clear_mfa_token(&self) {
        self.inner.session.set_mfa_token(None);
    }

    /// Returns the operation MFA token, if one is set.
    pub fn mfa_token(&self) -> Option<String> {
        self.inner.session.mfa_token()
    }

    /// Forgets the session after a logout: tokens, token store and operation MFA token.
    pub(crate) fn end_session(&self) {
        self.inner.session.end();
    }

    /// The session generation, captured when a request that replaces the session is sent.
    pub(crate) fn session_generation(&self) -> u64 {
        self.inner.session.generation()
    }

    /// Stores the token pair that `response`, the answer to a password change sent at
    /// `generation`, carries as the new session, with `new_password` for later logins, and
    /// returns the response with its body intact.
    pub(crate) async fn replace_session(
        &self,
        response: reqwest::Response,
        generation: u64,
        new_password: Option<&str>,
    ) -> Result<reqwest::Response> {
        #[derive(Deserialize)]
        struct Pair {
            access_token: Option<String>,
            refresh_token: Option<String>,
            expires_in: Option<u64>,
        }

        let (response, body) = Response::buffer(response).await?;
        if let Ok(Pair {
            access_token: Some(access_token),
            refresh_token,
            expires_in,
        }) = serde_json::from_slice(&body)
        {
            if !access_token.is_empty() {
                let refresh_token = refresh_token.filter(|token| !token.is_empty());
                let tokens = Tokens::issued(access_token, refresh_token, expires_in);
                self.inner.session.replace(generation, tokens, new_password);
            }
        }
        Ok(response)
    }

    /// Environment the client talks to.
    pub fn environment(&self) -> Environment {
        self.inner.environment
    }

    /// Base URL used for `service`.
    pub fn base_url(&self, service: Service) -> &str {
        self.inner
            .base_urls
            .get(&service)
            .map_or_else(|| service.url(self.inner.environment), String::as_str)
    }

    /// Returns a client that applies `options` to every request it sends. The returned client
    /// shares the session with this one.
    #[must_use]
    pub fn with_options(&self, options: RequestOptions) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
            options: self.options.clone().merge(options),
        }
    }

    /// Builds a request to any endpoint, for endpoints not yet covered by the SDK. It is sent
    /// with the same authentication, retries and error handling as the generated methods.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use whitebird::{Client, Method, Service};
    ///
    /// # async fn run(client: Client) -> whitebird::Result<()> {
    /// let page: serde_json::Value = client
    ///     .request(Service::Brokerage, Method::GET, "/api/v1/brokerage/some-new-endpoint")
    ///     .query("page", 0)
    ///     .send()
    ///     .await?
    ///     .json()
    ///     .await?;
    /// # Ok(())
    /// # }
    /// ```
    pub fn request(
        &self,
        service: Service,
        method: Method,
        path: impl Into<String>,
    ) -> RequestBuilder {
        RequestBuilder::new(self.clone(), service, method, path.into())
    }

    /// Builds a request to `template`, whose `{}` placeholders are replaced by the encoded
    /// `params`; a parameter that would change the path makes the request invalid.
    pub(crate) fn request_path(
        &self,
        service: Service,
        method: Method,
        template: &str,
        params: &[&dyn fmt::Display],
    ) -> RequestBuilder {
        match request::fill_path(template, params) {
            Ok(path) => self.request(service, method, path),
            Err(problem) => {
                let mut request = self.request(service, method, template);
                request.invalid = Some(problem);
                request
            }
        }
    }

    pub(crate) async fn execute(&self, request: &RequestBuilder) -> Result<reqwest::Response> {
        if let Some(problem) = &request.invalid {
            return Err(Error::Config(problem.clone()));
        }
        let url = self.url(request)?;
        // Requests that are not idempotent are retried only if they did not reach the server.
        let idempotent = request
            .options
            .idempotent
            .unwrap_or_else(|| retry::is_idempotent(&request.method));
        let max_retries = match request.options.max_retries {
            _ if request.streaming => 0,
            Some(max_retries) => max_retries,
            None => self.inner.retry.max_retries,
        };
        let session = &self.inner.session;
        let mut token = if request.public {
            None
        } else {
            Some(session.access_token().await?)
        };
        let mut renewed = false;
        let mut attempt = 0;
        loop {
            let delay = match self
                .http_request(request, url.clone(), token.as_deref())
                .send()
                .await
            {
                Ok(response) if response.status().is_success() => return Ok(response),
                Ok(response) => {
                    let status = response.status();
                    if status == StatusCode::UNAUTHORIZED && !renewed {
                        if let Some(rejected) = &token {
                            match session.renew_rejected(rejected).await? {
                                Some(renewed_token) => {
                                    token = Some(renewed_token);
                                    renewed = true;
                                    continue;
                                }
                                None => return Err(api_error(response).await),
                            }
                        }
                    }
                    let retry_after = retry::retry_after(response.headers());
                    match self.retry_delay(
                        attempt,
                        max_retries,
                        retry::is_retryable(status, idempotent),
                        retry_after,
                    ) {
                        Some(delay) => delay,
                        None => return Err(api_error(response).await),
                    }
                }
                Err(error) => match self.retry_delay(
                    attempt,
                    max_retries,
                    retry::is_retryable_error(&error, idempotent),
                    None,
                ) {
                    Some(delay) => delay,
                    None => return Err(error.into()),
                },
            };
            attempt = attempt.saturating_add(1);
            tokio::time::sleep(delay).await;
        }
    }

    fn retry_delay(
        &self,
        attempt: u32,
        max_retries: u32,
        retryable: bool,
        retry_after: Option<Duration>,
    ) -> Option<Duration> {
        if retryable && attempt < max_retries {
            self.inner.retry.delay(attempt, retry_after)
        } else {
            None
        }
    }

    fn url(&self, request: &RequestBuilder) -> Result<Url> {
        let base = self.base_url(request.service);
        let mut url = Url::parse(&format!("{base}{}", request.path)).map_err(|error| {
            Error::Config(format!("invalid URL {base}{}: {error}", request.path))
        })?;
        if !request.query.is_empty() {
            url.query_pairs_mut().extend_pairs(&request.query);
        }
        Ok(url)
    }

    fn http_request(
        &self,
        request: &RequestBuilder,
        url: Url,
        token: Option<&str>,
    ) -> reqwest::RequestBuilder {
        let mut http = self
            .inner
            .http
            .request(request.method.clone(), url)
            .header(USER_AGENT, USER_AGENT_VALUE);
        if let Some(language) = &self.inner.session.language {
            http = http.header(ACCEPT_LANGUAGE, language.clone());
        }
        if let Some(token) = token {
            http = http.bearer_auth(token);
        }
        if let Some(mfa_token) = self.mfa_token() {
            http = http.header(MFA_TOKEN_HEADER, mfa_token);
        }
        for (name, value) in &request.headers {
            http = http.header(name, value);
        }
        http = http.headers(request.options.headers.clone());
        if !request.streaming {
            http = http.timeout(request.options.timeout.unwrap_or(self.inner.timeout));
        }
        request.body.apply(http)
    }
}

/// Reads the MFA token from the `mfaToken` field, a body that is just the token (as a JSON
/// string or plain text) or the `x-mfa-token` header.
async fn issued_mfa_token(response: reqwest::Response) -> Result<String> {
    let status = response.status();
    let header = response
        .headers()
        .get(MFA_TOKEN_HEADER)
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let body = response.bytes().await?;
    let from_body = match serde_json::from_slice::<Value>(&body) {
        Ok(Value::Object(mut fields)) => match fields.remove("mfaToken") {
            Some(Value::String(token)) => Some(token),
            _ => None,
        },
        Ok(Value::String(token)) => Some(token),
        Ok(_) => None,
        Err(_) => std::str::from_utf8(&body).ok().map(str::to_owned),
    };
    from_body
        .into_iter()
        .chain(header)
        .map(|token| token.trim().to_owned())
        .find(|token| !token.is_empty())
        .ok_or_else(|| {
            ApiError::new(
                ErrorKind::Other,
                status,
                "the response carries no MFA token",
                &body,
            )
            .into()
        })
}

async fn api_error(response: reqwest::Response) -> Error {
    let status = response.status();
    let headers = response.headers().clone();
    match response.bytes().await {
        Ok(body) => ApiError::from_response(status, &headers, &body).into(),
        Err(error) => error.into(),
    }
}

impl fmt::Debug for Client {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Client")
            .field("environment", &self.inner.environment)
            .field("base_urls", &self.inner.base_urls)
            .finish_non_exhaustive()
    }
}

/// Configures a [`Client`].
///
/// Only [`credentials`](Self::credentials) are needed for authenticated endpoints; public
/// endpoints work without any configuration.
#[derive(Default)]
#[must_use]
pub struct ClientBuilder {
    credentials: Option<Credentials>,
    otp: Option<CodeProvider>,
    environment: Environment,
    base_urls: HashMap<Service, String>,
    captcha: Option<CaptchaSolver>,
    token_store: Option<Arc<dyn TokenStore>>,
    timeout: Option<Duration>,
    max_retries: Option<u32>,
    language: Option<String>,
    http: Option<reqwest::Client>,
}

impl ClientBuilder {
    /// Sets the account email and password. The client sends the email lowercased, like the web
    /// app.
    pub fn credentials(mut self, username: impl Into<String>, password: impl Into<String>) -> Self {
        self.credentials = Some(Credentials {
            username: username.into(),
            password: password.into(),
        });
        self
    }

    /// Sets where TOTP codes come from when two-factor authentication is on: a fixed code
    /// (`&str`), a [`CodeProvider::from_fn`] or [`CodeProvider::new`] callback or, with the
    /// `otp` feature, a `Totp`. The provider is asked only when the server wants a code: at
    /// sign-in and when a withdrawal or another sensitive operation is refused for lack of an
    /// operation MFA token.
    pub fn otp(mut self, provider: impl Into<CodeProvider>) -> Self {
        self.otp = Some(provider.into());
        self
    }

    /// Sets the solver of the slider captcha that the API may ask for on login. Without one, such
    /// logins fail with an error for which [`Error::is_captcha_required`] is `true`. See
    /// [`CaptchaSolver`] for an example.
    pub fn captcha(mut self, solver: CaptchaSolver) -> Self {
        self.captcha = Some(solver);
        self
    }

    /// Selects the environment; production by default.
    pub fn environment(mut self, environment: Environment) -> Self {
        self.environment = environment;
        self
    }

    /// Overrides the base URL of one service, e.g. after the service moved to a new host.
    pub fn base_url(mut self, service: Service, url: impl Into<String>) -> Self {
        let url = url.into();
        self.base_urls
            .insert(service, url.trim_end_matches('/').to_owned());
        self
    }

    /// Keeps the session tokens in `store` instead of memory, so a session can survive restarts.
    pub fn token_store(mut self, store: impl TokenStore + 'static) -> Self {
        self.token_store = Some(Arc::new(store));
        self
    }

    /// Sets the time allowed for each request attempt; 30 seconds by default.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }

    /// Sets how many times a failed request is retried; 2 by default. GET, HEAD and OPTIONS
    /// requests are retried on network errors, timeouts and answers 429, 502, 503 and 504;
    /// other requests only when the connection could not be established or on 429.
    pub fn max_retries(mut self, max_retries: u32) -> Self {
        self.max_retries = Some(max_retries);
        self
    }

    /// Sets the `Accept-Language` sent with every request, such as `en` or `ru`; the services
    /// use it for the language of their messages.
    pub fn language(mut self, language: impl Into<String>) -> Self {
        self.language = Some(language.into());
        self
    }

    /// Uses a preconfigured HTTP client, e.g. one with a proxy.
    pub fn http_client(mut self, http: reqwest::Client) -> Self {
        self.http = Some(http);
        self
    }

    /// Builds the client.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Config`] if a base URL or the language is invalid and
    /// [`Error::Transport`] if the HTTP client cannot be initialized.
    pub fn build(self) -> Result<Client> {
        for (service, url) in &self.base_urls {
            Url::parse(url).map_err(|error| {
                Error::Config(format!("invalid base URL {url:?} for {service:?}: {error}"))
            })?;
        }
        let language = self
            .language
            .as_deref()
            .map(str::trim)
            .filter(|language| !language.is_empty())
            .map(|language| {
                HeaderValue::from_str(language)
                    .map_err(|_| Error::Config(format!("invalid language {language:?}")))
            })
            .transpose()?;
        let timeout = self.timeout.unwrap_or(DEFAULT_TIMEOUT);
        let http = match self.http {
            Some(http) => http,
            None => reqwest::Client::builder()
                .connect_timeout(timeout)
                .build()?,
        };
        let auth_url = self
            .base_urls
            .get(&OAUTH_SERVICE)
            .map_or_else(|| OAUTH_SERVICE.url(self.environment), String::as_str);
        let session = Session {
            http: http.clone(),
            endpoints: Endpoints {
                token: format!("{auth_url}{OAUTH_TOKEN_PATH}"),
                captcha: format!("{auth_url}{OAUTH_CAPTCHA_PATH}"),
                basic: OAUTH_BASIC.to_owned(),
            },
            credentials: RwLock::new(self.credentials),
            otp: self.otp,
            captcha: self.captcha,
            store: self
                .token_store
                .unwrap_or_else(|| Arc::new(MemoryTokenStore::new())),
            timeout,
            language,
            renewal: tokio::sync::Mutex::new(()),
            mfa_token: RwLock::new(None),
            confirmation: tokio::sync::Mutex::new(()),
            generation: AtomicU64::new(0),
            saving: Mutex::new(()),
        };
        Ok(Client {
            inner: Arc::new(Inner {
                http,
                environment: self.environment,
                base_urls: self.base_urls,
                session,
                retry: self
                    .max_retries
                    .map_or_else(RetryPolicy::default, RetryPolicy::with_max_retries),
                timeout,
            }),
            options: RequestOptions::default(),
        })
    }

    /// Builds a [`blocking::Client`](crate::blocking::Client) running on its own runtime, whose
    /// thread keeps the connections alive between calls.
    ///
    /// # Errors
    ///
    /// Same as [`build`](Self::build), plus [`Error::Config`] if the runtime cannot be created.
    #[cfg(feature = "blocking")]
    #[cfg_attr(docsrs, doc(cfg(feature = "blocking")))]
    pub fn build_blocking(self) -> Result<crate::blocking::Client> {
        crate::blocking::Client::new(self.build()?)
    }
}

impl fmt::Debug for ClientBuilder {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ClientBuilder")
            .field("credentials", &self.credentials)
            .field("otp", &self.otp)
            .field("captcha", &self.captcha)
            .field("environment", &self.environment)
            .field("base_urls", &self.base_urls)
            .field("timeout", &self.timeout)
            .field("max_retries", &self.max_retries)
            .field("language", &self.language)
            .finish_non_exhaustive()
    }
}
