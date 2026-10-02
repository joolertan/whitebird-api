//! OAuth session: password login, refresh and single-flight renewal.

mod tokens;

use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use reqwest::StatusCode;
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE, USER_AGENT};
use serde::Deserialize;

pub use self::tokens::{MemoryTokenStore, TokenStore, Tokens};
use crate::client::USER_AGENT_VALUE;
use crate::error::{ApiError, Error, ErrorKind, Result};
use crate::otp::CodeProvider;

/// Tokens closer than this to their expiry are refreshed before use.
const REFRESH_MARGIN: Duration = Duration::from_secs(30);

pub(crate) struct Credentials {
    pub(crate) username: String,
    pub(crate) password: String,
}

impl fmt::Debug for Credentials {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Credentials")
            .field("username", &self.username)
            .field("password", &"<redacted>")
            .finish_non_exhaustive()
    }
}

pub(crate) struct Endpoints {
    pub(crate) token: String,
    pub(crate) mfa_check: String,
    pub(crate) basic: String,
}

pub(crate) struct Session {
    pub(crate) http: reqwest::Client,
    pub(crate) endpoints: Endpoints,
    pub(crate) credentials: Option<Credentials>,
    pub(crate) otp: Option<CodeProvider>,
    pub(crate) store: Arc<dyn TokenStore>,
    pub(crate) timeout: Duration,
    /// Held while tokens are renewed so that concurrent requests share one refresh.
    pub(crate) renewal: tokio::sync::Mutex<()>,
}

enum Grant {
    Issued(Tokens),
    /// HTTP 200 without an access token: the server wants a (valid) one-time code.
    CodeRequired(Bytes),
}

impl Session {
    /// Returns a valid access token, logging in or refreshing first if needed.
    pub(crate) async fn access_token(&self) -> Result<String> {
        if let Some(token) = self.fresh_token() {
            return Ok(token);
        }
        let _renewal = self.renewal.lock().await;
        if let Some(token) = self.fresh_token() {
            return Ok(token);
        }
        Ok(self.renew().await?.access_token)
    }

    /// Returns a new access token after the API rejected `rejected`, unless another request
    /// already replaced it.
    pub(crate) async fn renew_rejected(&self, rejected: &str) -> Result<String> {
        let _renewal = self.renewal.lock().await;
        if let Some(token) = self.fresh_token().filter(|token| token != rejected) {
            return Ok(token);
        }
        Ok(self.renew().await?.access_token)
    }

    pub(crate) async fn login(&self) -> Result<Tokens> {
        let _renewal = self.renewal.lock().await;
        self.password_grant().await
    }

    fn fresh_token(&self) -> Option<String> {
        self.store
            .load()
            .filter(|tokens| !tokens.expires_within(REFRESH_MARGIN))
            .map(|tokens| tokens.access_token)
    }

    async fn renew(&self) -> Result<Tokens> {
        if let Some(refresh_token) = self.store.load().and_then(|tokens| tokens.refresh_token) {
            match self.refresh_grant(&refresh_token).await {
                Ok(tokens) => return Ok(tokens),
                Err(Error::Api(_)) if self.credentials.is_some() => {}
                Err(error) => return Err(error),
            }
        }
        self.password_grant().await
    }

    async fn refresh_grant(&self, refresh_token: &str) -> Result<Tokens> {
        let form = [
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token),
        ];
        match self.token_request(&form).await? {
            Grant::Issued(mut tokens) => {
                tokens
                    .refresh_token
                    .get_or_insert_with(|| refresh_token.to_owned());
                self.store.save(&tokens);
                Ok(tokens)
            }
            Grant::CodeRequired(body) => Err(ApiError::new(
                ErrorKind::Authentication,
                StatusCode::OK,
                "the refresh token was not accepted",
                &body,
            )
            .into()),
        }
    }

    async fn password_grant(&self) -> Result<Tokens> {
        let Some(credentials) = &self.credentials else {
            return Err(Error::Config(
                "not signed in: configure credentials or a token store with a session".into(),
            ));
        };
        let username = credentials.username.to_lowercase();
        let code = self.otp.as_ref().map(CodeProvider::code);
        let mfa_enabled = match code {
            Some(_) => None,
            None => self.mfa_enabled(&username).await,
        };
        if mfa_enabled == Some(true) {
            return Err(mfa_required(StatusCode::OK, &[]));
        }

        let mut form = vec![
            ("grant_type", "password"),
            ("username", username.as_str()),
            ("password", credentials.password.as_str()),
        ];
        if let Some(code) = &code {
            form.push(("code", code));
        }
        let grant = match self.token_request(&form).await {
            Err(Error::Api(error))
                if code.is_none()
                    && mfa_enabled.is_none()
                    && error.error_code() == Some("invalid_grant") =>
            {
                return Err(mfa_required(error.status(), error.body().as_bytes()));
            }
            result => result?,
        };
        match grant {
            Grant::Issued(tokens) => {
                self.store.save(&tokens);
                Ok(tokens)
            }
            Grant::CodeRequired(body) if code.is_some() => Err(ApiError::new(
                ErrorKind::Authentication,
                StatusCode::OK,
                "the one-time code was not accepted",
                &body,
            )
            .into()),
            Grant::CodeRequired(body) => Err(mfa_required(StatusCode::OK, &body)),
        }
    }

    async fn token_request(&self, form: &[(&str, &str)]) -> Result<Grant> {
        #[derive(Deserialize)]
        struct Body {
            access_token: Option<String>,
            refresh_token: Option<String>,
            expires_in: Option<u64>,
        }

        let body = form_urlencoded::Serializer::new(String::new())
            .extend_pairs(form)
            .finish();
        let response = self
            .http
            .post(&self.endpoints.token)
            .header(AUTHORIZATION, format!("Basic {}", self.endpoints.basic))
            .header(CONTENT_TYPE, "application/x-www-form-urlencoded")
            .header(USER_AGENT, USER_AGENT_VALUE)
            .timeout(self.timeout)
            .body(body)
            .send()
            .await?;
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = response.bytes().await?;
        if !status.is_success() {
            return Err(ApiError::from_response(status, &headers, &bytes).into());
        }
        let body: Body =
            serde_json::from_slice(&bytes).map_err(|error| Error::decode(error, &bytes))?;
        Ok(match body.access_token {
            Some(access_token) => Grant::Issued(Tokens::issued(
                access_token,
                body.refresh_token,
                body.expires_in,
            )),
            None => Grant::CodeRequired(bytes),
        })
    }

    /// Asks whether the account has two-factor authentication on; `None` if that is unknown.
    async fn mfa_enabled(&self, username: &str) -> Option<bool> {
        #[derive(Deserialize)]
        struct Status {
            #[serde(rename = "mfaEnabled")]
            mfa_enabled: bool,
        }

        let response = self
            .http
            .post(&self.endpoints.mfa_check)
            .header(CONTENT_TYPE, "application/json")
            .header(USER_AGENT, USER_AGENT_VALUE)
            .timeout(self.timeout)
            .body(serde_json::json!({ "username": username }).to_string())
            .send()
            .await
            .ok()?;
        if !response.status().is_success() {
            return None;
        }
        let body = response.bytes().await.ok()?;
        serde_json::from_slice::<Status>(&body)
            .ok()
            .map(|status| status.mfa_enabled)
    }
}

fn mfa_required(status: StatusCode, body: &[u8]) -> Error {
    ApiError::new(
        ErrorKind::MfaRequired,
        status,
        "two-factor authentication is enabled: configure a one-time code with ClientBuilder::otp",
        body,
    )
    .into()
}
