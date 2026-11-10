//! OAuth session: password login, refresh and single-flight renewal.

mod tokens;

use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError, RwLock};
use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use bytes::Bytes;
use reqwest::header::{ACCEPT_LANGUAGE, AUTHORIZATION, CONTENT_TYPE, HeaderValue, USER_AGENT};
use reqwest::{Method, StatusCode};
use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;

pub use self::tokens::{MemoryTokenStore, TokenStore, Tokens};
use crate::captcha::{Captcha, CaptchaSolver};
use crate::client::USER_AGENT_VALUE;
use crate::error::{ApiError, Error, ErrorKind, Result};
use crate::otp::CodeProvider;

/// Tokens closer than this to their expiry are renewed before use, like the web app does.
const REFRESH_MARGIN: Duration = Duration::from_secs(60);
/// Captchas tried per login when the answers are rejected.
const CAPTCHA_ATTEMPTS: u32 = 3;
/// Message of the error when the server asks for a captcha and no solver is configured.
const CAPTCHA_HINT: &str =
    "Whitebird asks for a captcha to sign in: configure ClientBuilder::captcha";

#[derive(Clone)]
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
    pub(crate) captcha: String,
    pub(crate) basic: String,
}

pub(crate) struct Session {
    pub(crate) http: reqwest::Client,
    pub(crate) endpoints: Endpoints,
    /// Replaced when the password is changed through the client.
    pub(crate) credentials: RwLock<Option<Credentials>>,
    pub(crate) otp: Option<CodeProvider>,
    pub(crate) captcha: Option<CaptchaSolver>,
    pub(crate) store: Arc<dyn TokenStore>,
    pub(crate) timeout: Duration,
    /// `Accept-Language` sent with every request.
    pub(crate) language: Option<HeaderValue>,
    /// Held while tokens are renewed so that concurrent requests share one refresh or login.
    pub(crate) renewal: tokio::sync::Mutex<()>,
    /// Operation MFA token sent as `x-mfa-token`.
    pub(crate) mfa_token: RwLock<Option<String>>,
    /// Held while an operation MFA token is obtained so that concurrent operations share one.
    pub(crate) confirmation: tokio::sync::Mutex<()>,
    /// Incremented when the session ends or is replaced: tokens from a login or refresh that
    /// started before are then not saved.
    pub(crate) generation: AtomicU64,
    /// Orders saving tokens with ending or replacing the session.
    pub(crate) saving: Mutex<()>,
}

enum Grant {
    Issued(Tokens),
    /// HTTP 200 without an access token: the server wants a (valid) one-time code.
    CodeRequired(Bytes),
}

/// Captcha state of one sign-in.
#[derive(Default)]
struct LoginCaptcha {
    /// The server asked for a captcha: every later grant of the sign-in carries one.
    required: bool,
    /// Answers the server rejected.
    rejected: u32,
}

impl Session {
    /// Returns a valid access token, logging in or refreshing first if needed.
    pub(crate) async fn access_token(&self) -> Result<String> {
        if let Some(token) = self.fresh_token(None) {
            return Ok(token);
        }
        let _renewal = self.renewal.lock().await;
        // Another request, or another process sharing the store, may have renewed meanwhile.
        if let Some(token) = self.fresh_token(None) {
            return Ok(token);
        }
        self.renew(None).await?.ok_or_else(not_signed_in)
    }

    /// Returns a new access token after the API rejected `rejected`, unless another request
    /// already replaced it; `None` if there is no way to get one.
    pub(crate) async fn renew_rejected(&self, rejected: &str) -> Result<Option<String>> {
        let _renewal = self.renewal.lock().await;
        if let Some(token) = self.fresh_token(Some(rejected)) {
            return Ok(Some(token));
        }
        self.renew(Some(rejected)).await
    }

    pub(crate) async fn login(&self) -> Result<Tokens> {
        let _renewal = self.renewal.lock().await;
        let generation = self.generation();
        self.password_grant(generation).await
    }

    /// Forgets the session after a logout: tokens, store and operation MFA token. A login or
    /// refresh still in flight does not save its tokens.
    pub(crate) fn end(&self) {
        let _saving = self.saving.lock().unwrap_or_else(PoisonError::into_inner);
        self.generation.fetch_add(1, Ordering::SeqCst);
        self.forget();
    }

    /// Makes `tokens`, issued by a password change sent at `generation`, the session, keeping
    /// the previous refresh token if the answer has none, and `new_password` the password of
    /// later logins. Ignored if the session ended or was replaced since the request was sent.
    pub(crate) fn replace(&self, generation: u64, mut tokens: Tokens, new_password: Option<&str>) {
        let _saving = self.saving.lock().unwrap_or_else(PoisonError::into_inner);
        if self.generation() != generation {
            return;
        }
        self.generation.fetch_add(1, Ordering::SeqCst);
        if tokens.refresh_token.is_none() {
            tokens.refresh_token = self
                .store
                .load()
                .and_then(|previous| previous.refresh_token);
        }
        self.store.save(&tokens);
        if let Some(password) = new_password {
            let mut credentials = self
                .credentials
                .write()
                .unwrap_or_else(PoisonError::into_inner);
            if let Some(credentials) = credentials.as_mut() {
                password.clone_into(&mut credentials.password);
            }
        }
    }

    /// The current session generation, to pass to [`replace`](Self::replace).
    pub(crate) fn generation(&self) -> u64 {
        self.generation.load(Ordering::SeqCst)
    }

    fn credentials(&self) -> Option<Credentials> {
        self.credentials
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn has_credentials(&self) -> bool {
        self.credentials
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .is_some()
    }

    pub(crate) fn mfa_token(&self) -> Option<String> {
        self.mfa_token
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    pub(crate) fn set_mfa_token(&self, token: Option<String>) {
        *self
            .mfa_token
            .write()
            .unwrap_or_else(PoisonError::into_inner) = token;
    }

    /// The stored access token if it is not about to expire and is not `rejected`.
    fn fresh_token(&self, rejected: Option<&str>) -> Option<String> {
        self.store
            .load()
            .filter(|tokens| !tokens.expires_within(REFRESH_MARGIN))
            .map(|tokens| tokens.access_token)
            .filter(|token| Some(token.as_str()) != rejected)
    }

    /// Saves tokens obtained by a login or refresh, unless the session ended meanwhile.
    fn save(&self, generation: u64, tokens: &Tokens) {
        let _saving = self.saving.lock().unwrap_or_else(PoisonError::into_inner);
        if self.generation() == generation {
            self.store.save(tokens);
        }
    }

    fn forget(&self) {
        self.store.clear();
        self.set_mfa_token(None);
    }

    /// Refreshes the stored session or signs in with the password; `None` if neither is
    /// possible.
    async fn renew(&self, rejected: Option<&str>) -> Result<Option<String>> {
        let generation = self.generation();
        let mut refused = None;
        if let Some(tokens) = self.store.load() {
            if let Some(refresh_token) = tokens.usable_refresh_token() {
                match self.refresh_grant(refresh_token, generation).await {
                    Ok(tokens) => return Ok(Some(tokens.access_token)),
                    // Like the web app: a refresh answered 400 or 401 ends the session.
                    Err(Error::Api(error)) if is_refusal(&error) => {
                        self.forget();
                        refused = Some(error);
                    }
                    Err(error) => return Err(error),
                }
            } else if !self.has_credentials()
                && !tokens.expires_within(Duration::ZERO)
                && rejected != Some(tokens.access_token.as_str())
            {
                // Nothing to renew it with: use the token until it expires.
                return Ok(Some(tokens.access_token));
            }
        }
        if self.has_credentials() {
            return Ok(Some(self.password_grant(generation).await?.access_token));
        }
        match refused {
            Some(error) => Err(error.with_kind(ErrorKind::Authentication).into()),
            None => Ok(None),
        }
    }

    async fn refresh_grant(&self, refresh_token: &str, generation: u64) -> Result<Tokens> {
        let form = [
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token),
        ];
        match self.token_request(&form, None).await? {
            Grant::Issued(mut tokens) => {
                tokens
                    .refresh_token
                    .get_or_insert_with(|| refresh_token.to_owned());
                self.save(generation, &tokens);
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

    /// Signs in like the web app: sends the password alone, solves a captcha once the server
    /// asks for one (and for every later grant of the sign-in), and sends the one-time code if
    /// the server asks for it.
    async fn password_grant(&self, generation: u64) -> Result<Tokens> {
        let Some(credentials) = self.credentials() else {
            return Err(not_signed_in());
        };
        let username = credentials.username.to_lowercase();
        let form = |code: Option<&'_ str>| {
            let mut form = vec![
                ("grant_type", "password".to_owned()),
                ("username", username.clone()),
                ("password", credentials.password.clone()),
            ];
            form.extend(code.map(|code| ("code", code.to_owned())));
            form
        };
        let mut captcha = LoginCaptcha::default();
        let tokens = match self.grant(&pairs(&form(None)), &mut captcha).await? {
            Grant::Issued(tokens) => tokens,
            Grant::CodeRequired(body) => {
                let Some(otp) = &self.otp else {
                    return Err(mfa_required(&body));
                };
                let code = otp.code().await?;
                match self.grant(&pairs(&form(Some(&code))), &mut captcha).await? {
                    Grant::Issued(tokens) => tokens,
                    Grant::CodeRequired(body) => {
                        return Err(ApiError::new(
                            ErrorKind::Authentication,
                            StatusCode::OK,
                            "the one-time code was not accepted",
                            &body,
                        )
                        .into());
                    }
                }
            }
        };
        self.save(generation, &tokens);
        Ok(tokens)
    }

    /// Sends a password grant, with the answer to a new captcha once the server asked for one.
    /// A rejected answer is retried with a new captcha.
    async fn grant(&self, form: &[(&str, &str)], captcha: &mut LoginCaptcha) -> Result<Grant> {
        loop {
            let answer = match &self.captcha {
                Some(solver) if captcha.required => Some(self.solve_captcha(solver).await?),
                _ => None,
            };
            let error = match self.token_request(form, answer.as_deref()).await {
                Err(Error::Api(error)) if error.kind() == ErrorKind::CaptchaRequired => error,
                Err(Error::Api(error)) if error.error_code() == Some("NOT_FOUND") => {
                    return Err(error
                        .with_kind(ErrorKind::Authentication)
                        .with_message("no Whitebird account with this username")
                        .into());
                }
                result => return result,
            };
            if !captcha.required {
                if self.captcha.is_none() {
                    return Err(error.with_message(CAPTCHA_HINT).into());
                }
                captcha.required = true;
                continue;
            }
            captcha.rejected += 1;
            if captcha.rejected >= CAPTCHA_ATTEMPTS || error.error_code() != Some("INVALID_CAPTCHA")
            {
                return Err(error.into());
            }
        }
    }

    fn post(&self, url: &str) -> reqwest::RequestBuilder {
        self.request(Method::POST, url)
    }

    fn request(&self, method: Method, url: &str) -> reqwest::RequestBuilder {
        let mut request = self
            .http
            .request(method, url)
            .header(USER_AGENT, USER_AGENT_VALUE)
            .timeout(self.timeout);
        if let Some(language) = &self.language {
            request = request.header(ACCEPT_LANGUAGE, language.clone());
        }
        request
    }

    async fn token_request(&self, form: &[(&str, &str)], captcha: Option<&str>) -> Result<Grant> {
        #[derive(Deserialize)]
        struct Body {
            access_token: Option<String>,
            refresh_token: Option<String>,
            expires_in: Option<Value>,
        }

        let body = form_urlencoded::Serializer::new(String::new())
            .extend_pairs(form)
            .finish();
        let mut request = self
            .post(&self.endpoints.token)
            .header(AUTHORIZATION, format!("Basic {}", self.endpoints.basic))
            .header(CONTENT_TYPE, "application/x-www-form-urlencoded")
            .body(body);
        if let Some(captcha) = captcha {
            request = request.header("captcha", captcha);
        }
        let response = request.send().await?;
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = response.bytes().await?;
        if !status.is_success() {
            return Err(ApiError::from_login_response(status, &headers, &bytes).into());
        }
        let body: Body =
            serde_json::from_slice(&bytes).map_err(|error| Error::decode(error, &bytes))?;
        Ok(match body.access_token.filter(|token| !token.is_empty()) {
            Some(access_token) => Grant::Issued(Tokens::issued(
                access_token,
                body.refresh_token.filter(|token| !token.is_empty()),
                body.expires_in.as_ref().and_then(seconds),
            )),
            None => Grant::CodeRequired(bytes),
        })
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

        /// The web app sends the value of its range input, a string.
        #[derive(Serialize)]
        #[serde(rename_all = "camelCase")]
        struct Answer<'a> {
            slider_position: String,
            request_id: &'a str,
        }

        let request_id = uuid_v4();
        let response = self
            .request(
                Method::GET,
                &format!("{}?requestId={request_id}", self.endpoints.captcha),
            )
            .send()
            .await?;
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = response.bytes().await?;
        if !status.is_success() {
            return Err(ApiError::from_login_response(status, &headers, &bytes).into());
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
            slider_position: slider_position.to_string(),
            request_id: &request_id,
        })
        .map_err(|error| Error::Config(format!("cannot encode the captcha answer: {error}")))?;
        Ok(STANDARD.encode(answer))
    }
}

fn pairs<'a>(form: &'a [(&'a str, String)]) -> Vec<(&'a str, &'a str)> {
    form.iter()
        .map(|(name, value)| (*name, value.as_str()))
        .collect()
}

/// Whether a failed refresh means the session is over: 400 or 401, or no token issued.
fn is_refusal(error: &ApiError) -> bool {
    matches!(error.status().as_u16(), 400 | 401) || error.status().is_success()
}

/// `expires_in` as whole seconds, whether sent as a number or a string.
fn seconds(value: &Value) -> Option<u64> {
    match value {
        Value::Number(number) => number.as_u64().or_else(|| {
            number
                .as_f64()
                .filter(|seconds| seconds.is_finite() && *seconds >= 0.0)
                .map(|seconds| Duration::from_secs_f64(seconds.min(1e12)).as_secs())
        }),
        Value::String(text) => text.trim().parse().ok(),
        _ => None,
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

fn not_signed_in() -> Error {
    Error::Config(
        "not signed in: configure credentials, or a token store holding a valid session".into(),
    )
}

fn mfa_required(body: &[u8]) -> Error {
    ApiError::new(
        ErrorKind::MfaRequired,
        StatusCode::OK,
        "two-factor authentication is enabled: configure a one-time code with ClientBuilder::otp",
        body,
    )
    .into()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session(store: Arc<MemoryTokenStore>) -> Session {
        Session {
            http: reqwest::Client::new(),
            endpoints: Endpoints {
                token: String::new(),
                captcha: String::new(),
                basic: String::new(),
            },
            credentials: RwLock::new(None),
            otp: None,
            captcha: None,
            store,
            timeout: Duration::from_secs(1),
            language: None,
            renewal: tokio::sync::Mutex::new(()),
            mfa_token: RwLock::new(Some("mfa".into())),
            confirmation: tokio::sync::Mutex::new(()),
            generation: AtomicU64::new(0),
            saving: Mutex::new(()),
        }
    }

    #[test]
    fn a_login_finishing_after_the_session_ended_is_not_saved() {
        let store = Arc::new(MemoryTokenStore::from(Tokens::new("old", None)));
        let session = session(Arc::clone(&store));

        let started = session.generation();
        session.end();
        session.save(started, &Tokens::new("late", None));

        assert_eq!(store.load(), None);
        assert_eq!(session.mfa_token(), None);
        session.save(session.generation(), &Tokens::new("next", None));
        assert_eq!(store.load().unwrap().access_token, "next");
    }

    #[test]
    fn a_replaced_session_is_not_overwritten_by_an_older_refresh() {
        let store = Arc::new(MemoryTokenStore::new());
        let session = session(Arc::clone(&store));

        let started = session.generation();
        session.replace(started, Tokens::new("changed", None), None);
        session.save(started, &Tokens::new("refreshed", None));

        assert_eq!(store.load().unwrap().access_token, "changed");
    }

    #[test]
    fn a_password_change_answered_after_the_session_ended_is_ignored() {
        let store = Arc::new(MemoryTokenStore::from(Tokens::new("old", Some("r".into()))));
        let session = session(Arc::clone(&store));
        *session.credentials.write().unwrap() = Some(Credentials {
            username: "me".into(),
            password: "old secret".into(),
        });

        let sent = session.generation();
        session.end();
        session.replace(sent, Tokens::new("changed", None), Some("new secret"));
        assert_eq!(store.load(), None);
        assert_eq!(session.credentials().unwrap().password, "old secret");

        let sent = session.generation();
        session.replace(sent, Tokens::new("changed", None), Some("new secret"));
        assert_eq!(store.load(), Some(Tokens::new("changed", None)));
        assert_eq!(session.credentials().unwrap().password, "new secret");
    }

    #[test]
    fn reads_expires_in_leniently() {
        assert_eq!(seconds(&serde_json::json!(3600)), Some(3600));
        assert_eq!(seconds(&serde_json::json!(3599.9)), Some(3599));
        assert_eq!(seconds(&serde_json::json!("60")), Some(60));
        assert_eq!(seconds(&serde_json::json!(-1)), None);
    }

    #[test]
    fn uuids_are_version_4() {
        let id = uuid_v4();
        assert_eq!(id.len(), 36);
        assert_eq!(&id[14..15], "4");
        assert!(matches!(&id[19..20], "8" | "9" | "a" | "b"));
        assert_ne!(id, uuid_v4());
    }
}
