//! OAuth session: password login, refresh and single-flight renewal.

mod tokens;

use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use bytes::Bytes;
use reqwest::StatusCode;
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE, USER_AGENT};
use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize};

pub use self::tokens::{MemoryTokenStore, TokenStore, Tokens};
use crate::captcha::{Captcha, CaptchaSolver};
use crate::client::USER_AGENT_VALUE;
use crate::error::{ApiError, Error, ErrorKind, Result};
use crate::otp::CodeProvider;

/// Tokens closer than this to their expiry are refreshed before use.
const REFRESH_MARGIN: Duration = Duration::from_secs(30);
/// Captchas tried per login when the answers are rejected.
const CAPTCHA_ATTEMPTS: u32 = 3;

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
    pub(crate) mfa_status: String,
    pub(crate) captcha: String,
    pub(crate) basic: String,
}

pub(crate) struct Session {
    pub(crate) http: reqwest::Client,
    pub(crate) endpoints: Endpoints,
    pub(crate) credentials: Option<Credentials>,
    pub(crate) otp: Option<CodeProvider>,
    pub(crate) captcha: Option<CaptchaSolver>,
    pub(crate) store: Arc<dyn TokenStore>,
    pub(crate) timeout: Duration,
    /// Held while tokens are renewed so that concurrent requests share one refresh.
    pub(crate) renewal: tokio::sync::Mutex<()>,
}

struct Login<'a> {
    username: &'a str,
    password: &'a str,
    code: Option<String>,
    /// Value of the `captcha` header.
    captcha: Option<String>,
    mfa_enabled: Option<bool>,
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
        match self.token_request(&form, None).await? {
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
        let mfa_enabled = if self.otp.is_none() || self.captcha.is_some() {
            self.mfa_enabled(&username).await
        } else {
            None
        };
        let two_factor = mfa_enabled.unwrap_or(self.otp.is_some());
        let otp = match &self.otp {
            Some(otp) if two_factor => Some(otp),
            None if two_factor => return Err(mfa_required(StatusCode::OK, &[])),
            _ => None,
        };
        let solver = self.captcha.as_ref().filter(|_| !two_factor);

        let mut attempt = 1;
        loop {
            let captcha = match solver {
                Some(solver) => Some(self.solve_captcha(solver).await?),
                None => None,
            };
            let login = Login {
                username: &username,
                password: &credentials.password,
                code: otp.map(CodeProvider::code),
                captcha,
                mfa_enabled,
            };
            match self.password_attempt(login).await {
                Err(Error::Api(error))
                    if solver.is_some()
                        && attempt < CAPTCHA_ATTEMPTS
                        && error.error_code() == Some("INVALID_CAPTCHA") =>
                {
                    attempt += 1;
                }
                result => return result,
            }
        }
    }

    async fn password_attempt(&self, login: Login<'_>) -> Result<Tokens> {
        let mut form = vec![
            ("grant_type", "password"),
            ("username", login.username),
            ("password", login.password),
        ];
        if let Some(code) = &login.code {
            form.push(("code", code));
        }
        let grant = match self.token_request(&form, login.captcha.as_deref()).await {
            Err(Error::Api(error))
                if login.code.is_none()
                    && login.captcha.is_none()
                    && login.mfa_enabled.is_none()
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
            Grant::CodeRequired(body) if login.code.is_some() => Err(ApiError::new(
                ErrorKind::Authentication,
                StatusCode::OK,
                "the one-time code was not accepted",
                &body,
            )
            .into()),
            Grant::CodeRequired(body) => Err(mfa_required(StatusCode::OK, &body)),
        }
    }

    async fn token_request(&self, form: &[(&str, &str)], captcha: Option<&str>) -> Result<Grant> {
        #[derive(Deserialize)]
        struct Body {
            access_token: Option<String>,
            refresh_token: Option<String>,
            expires_in: Option<u64>,
        }

        let body = form_urlencoded::Serializer::new(String::new())
            .extend_pairs(form)
            .finish();
        let mut request = self
            .http
            .post(&self.endpoints.token)
            .header(AUTHORIZATION, format!("Basic {}", self.endpoints.basic))
            .header(CONTENT_TYPE, "application/x-www-form-urlencoded")
            .header(USER_AGENT, USER_AGENT_VALUE)
            .timeout(self.timeout)
            .body(body);
        if let Some(captcha) = captcha {
            request = request.header("captcha", captcha);
        }
        let response = request.send().await?;
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
            .post(&self.endpoints.mfa_status)
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

    /// Fetches a captcha, has `solver` solve it and returns the `captcha` header for the answer.
    async fn solve_captcha(&self, solver: &CaptchaSolver) -> Result<String> {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct Challenge {
            #[serde(deserialize_with = "base64_image")]
            background_image: Bytes,
            #[serde(deserialize_with = "base64_image")]
            puzzle_piece: Bytes,
            position_y: u32,
        }

        #[derive(Serialize)]
        #[serde(rename_all = "camelCase")]
        struct Answer<'a> {
            slider_position: u32,
            request_id: &'a str,
        }

        let request_id = uuid_v4();
        let response = self
            .http
            .get(format!("{}?requestId={request_id}", self.endpoints.captcha))
            .header(USER_AGENT, USER_AGENT_VALUE)
            .timeout(self.timeout)
            .send()
            .await?;
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = response.bytes().await?;
        if !status.is_success() {
            return Err(ApiError::from_response(status, &headers, &bytes).into());
        }
        let challenge: Challenge =
            serde_json::from_slice(&bytes).map_err(|error| Error::decode(error, &bytes))?;
        let slider_position = solver
            .solve(Captcha {
                request_id: request_id.clone(),
                background_image: challenge.background_image,
                puzzle_piece: challenge.puzzle_piece,
                position_y: challenge.position_y,
            })
            .await?;
        let answer = serde_json::to_vec(&Answer {
            slider_position,
            request_id: &request_id,
        })
        .map_err(|error| Error::Config(format!("cannot encode the captcha answer: {error}")))?;
        Ok(STANDARD.encode(answer))
    }
}

/// Decodes a base64 image, with or without a `data:` URL prefix.
fn base64_image<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Bytes, D::Error> {
    let text = String::deserialize(deserializer)?;
    let data = text
        .split_once(";base64,")
        .map_or(text.as_str(), |(_, data)| data);
    STANDARD
        .decode(data)
        .map(Bytes::from)
        .map_err(D::Error::custom)
}

fn uuid_v4() -> String {
    let mut bytes = fastrand::u128(..).to_be_bytes();
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex = format!("{:032x}", u128::from_be_bytes(bytes));
    format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    )
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uuids_are_version_4() {
        let id = uuid_v4();
        assert_eq!(id.len(), 36);
        assert_eq!(&id[14..15], "4");
        assert!(matches!(&id[19..20], "8" | "9" | "a" | "b"));
        assert_ne!(id, uuid_v4());
    }
}
