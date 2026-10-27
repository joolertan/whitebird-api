use std::collections::HashMap;
use std::fmt;
use std::sync::{Arc, PoisonError, RwLock};
use std::time::Duration;

use reqwest::header::USER_AGENT;
use reqwest::{Method, StatusCode, Url};

use serde_json::{Value, json};

use crate::auth::{Credentials, Endpoints, MemoryTokenStore, Session, TokenStore, Tokens};
use crate::captcha::CaptchaSolver;
use crate::error::{ApiError, Error, ErrorKind, Result};
use crate::generated::{
    Environment, OAUTH_BASIC, OAUTH_CAPTCHA_PATH, OAUTH_MFA_STATUS_PATH, OAUTH_SERVICE,
    OAUTH_TOKEN_PATH, OPERATION_MFA_SERVICE, OPERATION_MFA_TOKEN_PATH, Service,
};
use crate::otp::CodeProvider;
use crate::request::{RequestBuilder, RequestOptions};
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
    mfa_token: RwLock<Option<String>>,
}

impl Client {
    /// Starts configuring a client.
    pub fn builder() -> ClientBuilder {
        ClientBuilder::default()
    }

    /// Logs in with the configured credentials and returns the new tokens.
    ///
    /// Login otherwise happens lazily on the first authenticated call; calling this first
    /// surfaces credential and two-factor problems early. The client first asks whether the
    /// account has two-factor authentication on, then sends a code from the
    /// [code provider](ClientBuilder::otp) or, for accounts without it, a captcha answer.
    ///
    /// # Errors
    ///
    /// Returns an error for which [`Error::is_mfa_required`] is `true` if the account has
    /// two-factor authentication on and no code provider is configured,
    /// [`Error::is_captcha_required`] if the account must pass a captcha and no
    /// [solver](ClientBuilder::captcha) is configured, an
    /// [`ErrorKind::Authentication`](crate::ErrorKind::Authentication) error if the credentials
    /// or the code are rejected and [`Error::Config`] if no credentials are configured.
    pub async fn login(&self) -> Result<Tokens> {
        self.inner.session.login().await
    }

    /// Exchanges a one-time code for the operation MFA token that sensitive operations such as
    /// withdrawals need, stores it on the client (see [`set_mfa_token`](Self::set_mfa_token))
    /// and returns it.
    ///
    /// Without `code`, one comes from the [code provider](ClientBuilder::otp). With a provider
    /// configured the client calls this by itself before such operations, and again once when
    /// one is refused for lack of a valid token.
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
            Some(code) => code.to_owned(),
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

    /// Sends a request for an operation that needs an MFA token, obtaining one first if a code
    /// provider is configured and no token is set, and again once if the token is refused.
    pub(crate) async fn execute_with_mfa(
        &self,
        request: &RequestBuilder,
        operation: &str,
    ) -> Result<reqwest::Response> {
        if self.inner.session.otp.is_none() {
            return self.execute(request).await;
        }
        if self.mfa_token().is_none() {
            self.confirm_operation(operation, None).await?;
        }
        match self.execute(request).await {
            Err(error) if error.is_mfa_required() => {
                self.confirm_operation(operation, None).await?;
                self.execute(request).await
            }
            result => result,
        }
    }

    /// Sets the operation MFA token sent as `x-mfa-token` with every request, as required by
    /// sensitive operations such as withdrawals.
    pub fn set_mfa_token(&self, token: impl Into<String>) {
        *self
            .inner
            .mfa_token
            .write()
            .unwrap_or_else(PoisonError::into_inner) = Some(token.into());
    }

    /// Stops sending the operation MFA token.
    pub fn clear_mfa_token(&self) {
        *self
            .inner
            .mfa_token
            .write()
            .unwrap_or_else(PoisonError::into_inner) = None;
    }

    /// Returns the operation MFA token, if one is set.
    pub fn mfa_token(&self) -> Option<String> {
        self.inner
            .mfa_token
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
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
    /// let methods: serde_json::Value = client
    ///     .request(Service::Brokerage, Method::GET, "/api/v1/brokerage/payment/methods")
    ///     .query("currency", "USD")
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

    pub(crate) async fn execute(&self, request: &RequestBuilder) -> Result<reqwest::Response> {
        if let Some(problem) = &request.invalid {
            return Err(Error::Config(problem.clone()));
        }
        let url = self.url(request)?;
        let idempotent = request
            .options
            .idempotent
            .unwrap_or_else(|| retry::is_idempotent(&request.method));
        let max_retries = match request.options.max_retries {
            _ if request.streaming || !idempotent => 0,
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
                            token = Some(session.renew_rejected(rejected).await?);
                            renewed = true;
                            continue;
                        }
                    }
                    let retry_after = retry::retry_after(response.headers());
                    match self.retry_delay(
                        attempt,
                        max_retries,
                        retry::is_retryable(status),
                        retry_after,
                    ) {
                        Some(delay) => delay,
                        None => return Err(api_error(response).await),
                    }
                }
                Err(error) => match self.retry_delay(
                    attempt,
                    max_retries,
                    retry::is_retryable_error(&error),
                    None,
                ) {
                    Some(delay) => delay,
                    None => return Err(error.into()),
                },
            };
            attempt += 1;
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
        Err(_) => std::str::from_utf8(&body)
            .ok()
            .map(|text| text.trim().to_owned()),
    };
    from_body
        .into_iter()
        .chain(header)
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
    http: Option<reqwest::Client>,
}

impl ClientBuilder {
    /// Sets the account email and password. The email is lowercased on login.
    pub fn credentials(mut self, username: impl Into<String>, password: impl Into<String>) -> Self {
        self.credentials = Some(Credentials {
            username: username.into(),
            password: password.into(),
        });
        self
    }

    /// Sets where TOTP codes come from when two-factor authentication is on: a fixed code
    /// (`&str`), a [`CodeProvider::from_fn`] or [`CodeProvider::new`] callback or, with the
    /// `otp` feature, a `Totp`. The provider is not asked when signing in to an account without
    /// two-factor authentication.
    pub fn otp(mut self, provider: impl Into<CodeProvider>) -> Self {
        self.otp = Some(provider.into());
        self
    }

    /// Sets the solver of the slider captcha that the API asks accounts without two-factor
    /// authentication to pass on login. Without one, such logins fail with an error for which
    /// [`Error::is_captcha_required`] is `true`. See [`CaptchaSolver`] for an example.
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

    /// Sets how many times failed idempotent requests are retried; 2 by default.
    pub fn max_retries(mut self, max_retries: u32) -> Self {
        self.max_retries = Some(max_retries);
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
    /// Returns [`Error::Config`] if a base URL is invalid and [`Error::Transport`] if the HTTP
    /// client cannot be initialized.
    pub fn build(self) -> Result<Client> {
        for (service, url) in &self.base_urls {
            Url::parse(url).map_err(|error| {
                Error::Config(format!("invalid base URL {url:?} for {service:?}: {error}"))
            })?;
        }
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
                mfa_status: format!("{auth_url}{OAUTH_MFA_STATUS_PATH}"),
                captcha: format!("{auth_url}{OAUTH_CAPTCHA_PATH}"),
                basic: OAUTH_BASIC.to_owned(),
            },
            credentials: self.credentials,
            otp: self.otp,
            captcha: self.captcha,
            store: self
                .token_store
                .unwrap_or_else(|| Arc::new(MemoryTokenStore::new())),
            timeout,
            renewal: tokio::sync::Mutex::new(()),
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
                mfa_token: RwLock::new(None),
            }),
            options: RequestOptions::default(),
        })
    }

    /// Builds a [`blocking::Client`](crate::blocking::Client) running on its own
    /// current-thread runtime.
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
            .finish_non_exhaustive()
    }
}
