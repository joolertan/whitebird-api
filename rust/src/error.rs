use std::fmt;
use std::time::Duration;

use reqwest::StatusCode;
use reqwest::header::HeaderMap;
use serde_json::Value;

use crate::retry;

/// Result of a fallible SDK call.
pub type Result<T, E = Error> = std::result::Result<T, E>;

/// Error returned by the client.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum Error {
    /// The API answered with an error.
    #[error(transparent)]
    Api(#[from] ApiError),
    /// The request could not be sent or the response could not be read: network failure,
    /// timeout, TLS problem.
    #[error(transparent)]
    Transport(#[from] reqwest::Error),
    /// The response body does not have the expected shape.
    #[error("cannot decode response: {source}")]
    Decode {
        /// The underlying JSON error.
        source: serde_json::Error,
        /// The response body.
        body: String,
    },
    /// The client or a request is misconfigured, for example an invalid base URL, a missing
    /// password or a body that cannot be serialized.
    #[error("{0}")]
    Config(String),
    /// The [captcha solver](crate::CaptchaSolver) failed.
    #[error("the captcha solver failed: {0}")]
    Captcha(#[source] BoxError),
    /// The [code provider](crate::CodeProvider) failed.
    #[error("the code provider failed: {0}")]
    CodeProvider(#[source] BoxError),
}

/// Error type returned by user callbacks such as the [captcha solver](crate::CaptchaSolver)
/// and the [code provider](crate::CodeProvider).
pub type BoxError = Box<dyn std::error::Error + Send + Sync>;

impl Error {
    /// Returns the API error, if the API answered with one.
    pub fn api(&self) -> Option<&ApiError> {
        match self {
            Self::Api(error) => Some(error),
            _ => None,
        }
    }

    /// Returns the kind of the API error, if the API answered with one.
    pub fn kind(&self) -> Option<ErrorKind> {
        self.api().map(ApiError::kind)
    }

    /// Returns `true` if the credentials, a token or a one-time code were rejected.
    pub fn is_authentication(&self) -> bool {
        self.kind().is_some_and(ErrorKind::is_authentication)
    }

    /// Returns `true` if a TOTP code (on login) or an operation MFA token is required.
    pub fn is_mfa_required(&self) -> bool {
        self.kind() == Some(ErrorKind::MfaRequired)
    }

    /// Returns `true` if login needs a solved captcha; see
    /// [`ClientBuilder::captcha`](crate::ClientBuilder::captcha).
    pub fn is_captcha_required(&self) -> bool {
        self.kind() == Some(ErrorKind::CaptchaRequired)
    }

    /// Returns `true` if the account must pass SMS or liveness verification first.
    pub fn is_verification_required(&self) -> bool {
        self.kind() == Some(ErrorKind::VerificationRequired)
    }

    /// Returns `true` if the requested object does not exist.
    pub fn is_not_found(&self) -> bool {
        self.kind() == Some(ErrorKind::NotFound)
    }

    /// Returns `true` if the client is being rate limited.
    pub fn is_rate_limited(&self) -> bool {
        self.kind() == Some(ErrorKind::RateLimited)
    }

    /// Returns `true` if the request timed out.
    pub fn is_timeout(&self) -> bool {
        matches!(self, Self::Transport(error) if error.is_timeout())
    }

    pub(crate) fn decode(source: serde_json::Error, body: &[u8]) -> Self {
        Self::Decode {
            source,
            body: String::from_utf8_lossy(body).into_owned(),
        }
    }
}

/// Category of an [`ApiError`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum ErrorKind {
    /// HTTP 400: the request was rejected as invalid.
    BadRequest,
    /// HTTP 400 with `SMS_VERIFICATION_REQUIRED` or `LIVENESS_VERIFICATION_REQUIRED`.
    VerificationRequired,
    /// HTTP 401 or a rejected OAuth grant.
    Authentication,
    /// Login needs a TOTP code, or the operation needs an MFA token.
    MfaRequired,
    /// Login needs a solved captcha: no solver is configured, or it kept failing.
    CaptchaRequired,
    /// HTTP 403.
    PermissionDenied,
    /// HTTP 404.
    NotFound,
    /// HTTP 409.
    Conflict,
    /// HTTP 429.
    RateLimited,
    /// HTTP 5xx.
    Server,
    /// Any other error, including a non-zero `retCode` in an envelope.
    Other,
}

impl ErrorKind {
    /// Returns `true` for [`BadRequest`](Self::BadRequest) and its refinement
    /// [`VerificationRequired`](Self::VerificationRequired).
    pub fn is_bad_request(self) -> bool {
        matches!(self, Self::BadRequest | Self::VerificationRequired)
    }

    /// Returns `true` for [`Authentication`](Self::Authentication) and its refinements
    /// [`MfaRequired`](Self::MfaRequired) and [`CaptchaRequired`](Self::CaptchaRequired).
    pub fn is_authentication(self) -> bool {
        matches!(
            self,
            Self::Authentication | Self::MfaRequired | Self::CaptchaRequired
        )
    }

    fn classify(status: StatusCode, error_code: Option<&str>, message: &str) -> Self {
        const VERIFICATION: [&str; 2] = [
            "SMS_VERIFICATION_REQUIRED",
            "LIVENESS_VERIFICATION_REQUIRED",
        ];
        // The refusal comes in `message` or in `status`, like the web app checks.
        if [Some(message), error_code]
            .into_iter()
            .flatten()
            .any(|text| text.to_ascii_lowercase().contains(MFA_REFUSAL))
        {
            return Self::MfaRequired;
        }
        if status == StatusCode::BAD_REQUEST
            && error_code.is_some_and(|code| VERIFICATION.contains(&code))
        {
            return Self::VerificationRequired;
        }
        if error_code == Some("invalid_grant") {
            return Self::Authentication;
        }
        match status.as_u16() {
            400 => Self::BadRequest,
            401 => Self::Authentication,
            403 => Self::PermissionDenied,
            404 => Self::NotFound,
            409 => Self::Conflict,
            429 => Self::RateLimited,
            500..=599 => Self::Server,
            _ => Self::Other,
        }
    }

    /// Refines the kind of an error answered to a sign-in request (password or refresh grant):
    /// a rejection mentioning a captcha asks for one, and other 400 and 401 answers mean the
    /// credentials, the refresh token or the code were wrong.
    fn classify_login(self, status: StatusCode, error_code: Option<&str>, message: &str) -> Self {
        if error_code.is_some_and(|code| code.to_ascii_uppercase().contains("CAPTCHA")) {
            return Self::CaptchaRequired;
        }
        if message == TOO_MANY_REQUESTS {
            return Self::RateLimited;
        }
        if matches!(status.as_u16(), 400 | 401)
            && message != USER_BLOCKED
            && !matches!(self, Self::MfaRequired | Self::VerificationRequired)
        {
            return Self::Authentication;
        }
        self
    }
}

/// Part of the message (or `status`) refusing an operation that needs an MFA token.
const MFA_REFUSAL: &str = "requires mfa checking";
/// Message of a sign-in refused because of too many attempts.
const TOO_MANY_REQUESTS: &str = "Too many requests";
/// Message of a sign-in to a blocked account.
const USER_BLOCKED: &str = "Username is blocked";
/// Longest part of a non-JSON error body used as the message.
const MAX_TEXT_MESSAGE: usize = 200;

/// Error answered by the API.
///
/// Whitebird services answer `{"code", "status", "subStatus", "message", "fields"}`, the OAuth server
/// answers `{"error", "error_description"}` and trading services answer `{"retCode", "retMsg"}`;
/// all of them are normalized here. A body that is not JSON, such as the HTML page of a proxy,
/// becomes the message.
#[derive(Clone)]
pub struct ApiError(Box<Details>);

#[derive(Debug, Clone)]
struct Details {
    kind: ErrorKind,
    status: StatusCode,
    code: Option<i64>,
    error_code: Option<String>,
    sub_status: Option<String>,
    message: String,
    fields: Vec<Value>,
    body: String,
    retry_after: Option<Duration>,
}

impl ApiError {
    pub(crate) fn from_response(status: StatusCode, headers: &HeaderMap, body: &[u8]) -> Self {
        let json: Value = serde_json::from_slice(body).unwrap_or_default();
        let text = |key: &str| {
            json.get(key)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        };
        let error_code = text("status").or_else(|| text("error")).map(str::to_owned);
        let message = text("message")
            .or_else(|| text("retMsg"))
            .or_else(|| text("error_description"))
            .or_else(|| text("error"))
            .map(str::to_owned)
            .or_else(|| plain_text(&json, body))
            .or_else(|| status.canonical_reason().map(str::to_owned))
            .unwrap_or_default();
        let code = json
            .get("code")
            .filter(|code| !code.is_null())
            .or_else(|| json.get("retCode"))
            .and_then(|code| match code {
                Value::String(code) => code.parse().ok(),
                code => code.as_i64(),
            });
        let fields = match json.get("fields") {
            Some(Value::Array(fields)) => fields.clone(),
            Some(Value::Null) | None => Vec::new(),
            Some(other) => vec![other.clone()],
        };
        Self(Box::new(Details {
            kind: ErrorKind::classify(status, error_code.as_deref(), &message),
            status,
            code,
            error_code,
            sub_status: text("subStatus").map(str::to_owned),
            message,
            fields,
            body: String::from_utf8_lossy(body).into_owned(),
            retry_after: retry::retry_after(headers),
        }))
    }

    /// Like [`from_response`](Self::from_response), for answers of the token endpoint.
    pub(crate) fn from_login_response(
        status: StatusCode,
        headers: &HeaderMap,
        body: &[u8],
    ) -> Self {
        let mut error = Self::from_response(status, headers, body);
        let details = &mut *error.0;
        details.kind =
            details
                .kind
                .classify_login(status, details.error_code.as_deref(), &details.message);
        error
    }

    pub(crate) fn new(
        kind: ErrorKind,
        status: StatusCode,
        message: impl Into<String>,
        body: &[u8],
    ) -> Self {
        Self(Box::new(Details {
            kind,
            status,
            code: None,
            error_code: None,
            sub_status: None,
            message: message.into(),
            fields: Vec::new(),
            body: String::from_utf8_lossy(body).into_owned(),
            retry_after: None,
        }))
    }

    pub(crate) fn with_code(mut self, code: i64) -> Self {
        self.0.code = Some(code);
        self
    }

    pub(crate) fn with_kind(mut self, kind: ErrorKind) -> Self {
        self.0.kind = kind;
        self
    }

    pub(crate) fn with_message(mut self, message: impl Into<String>) -> Self {
        self.0.message = message.into();
        self
    }

    /// Category of the error.
    pub fn kind(&self) -> ErrorKind {
        self.0.kind
    }

    /// HTTP status of the response (200 for envelope errors).
    pub fn status(&self) -> StatusCode {
        self.0.status
    }

    /// Numeric error code: `code` of a service error or `retCode` of a trading service.
    pub fn code(&self) -> Option<i64> {
        self.0.code
    }

    /// Machine-readable error code: `status` of a service error (`SMS_VERIFICATION_REQUIRED`)
    /// or `error` of an OAuth error (`invalid_grant`).
    pub fn error_code(&self) -> Option<&str> {
        self.0.error_code.as_deref()
    }

    /// Refinement of [`error_code`](Self::error_code): `subStatus` of a service error.
    pub fn sub_status(&self) -> Option<&str> {
        self.0.sub_status.as_deref()
    }

    /// Human-readable message.
    pub fn message(&self) -> &str {
        &self.0.message
    }

    /// Per-field validation errors, as sent by the API.
    pub fn fields(&self) -> &[Value] {
        &self.0.fields
    }

    /// Raw response body.
    pub fn body(&self) -> &str {
        &self.0.body
    }

    /// Delay requested by the `Retry-After` header.
    pub fn retry_after(&self) -> Option<Duration> {
        self.0.retry_after
    }
}

impl fmt::Debug for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0.status)?;
        if let Some(code) = &self.0.error_code {
            write!(f, " {code}")?;
        }
        write!(f, ": {}", self.0.message)
    }
}

impl std::error::Error for ApiError {}

/// The trimmed text of a body that is not JSON, shortened to [`MAX_TEXT_MESSAGE`] characters.
fn plain_text(json: &Value, body: &[u8]) -> Option<String> {
    if !json.is_null() {
        return None;
    }
    let text = String::from_utf8_lossy(body);
    let text = text.trim();
    if text.is_empty() {
        return None;
    }
    Some(match text.char_indices().nth(MAX_TEXT_MESSAGE) {
        Some((end, _)) => format!("{}…", &text[..end]),
        None => text.to_owned(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(status: u16, body: &str) -> ApiError {
        ApiError::from_response(
            StatusCode::from_u16(status).unwrap(),
            &HeaderMap::new(),
            body.as_bytes(),
        )
    }

    #[test]
    fn service_error() {
        let error = parse(
            400,
            r#"{"code": 1001, "status": "VALIDATION_ERROR", "subStatus": "AMOUNT_TOO_LOW", "message": "Invalid amount", "fields": [{"field": "amount"}]}"#,
        );
        assert_eq!(error.kind(), ErrorKind::BadRequest);
        assert_eq!(error.code(), Some(1001));
        assert_eq!(error.error_code(), Some("VALIDATION_ERROR"));
        assert_eq!(error.sub_status(), Some("AMOUNT_TOO_LOW"));
        assert_eq!(error.message(), "Invalid amount");
        assert_eq!(error.fields().len(), 1);
        assert_eq!(
            error.to_string(),
            "400 Bad Request VALIDATION_ERROR: Invalid amount"
        );
    }

    #[test]
    fn oauth_error() {
        let error = parse(
            400,
            r#"{"error": "invalid_grant", "error_description": "Bad credentials"}"#,
        );
        assert_eq!(error.kind(), ErrorKind::Authentication);
        assert_eq!(error.message(), "Bad credentials");
    }

    #[test]
    fn non_json_body() {
        let error = parse(403, "  Access denied by DDoS-Guard\n");
        assert_eq!(error.kind(), ErrorKind::PermissionDenied);
        assert_eq!(error.message(), "Access denied by DDoS-Guard");
        assert_eq!(error.body(), "  Access denied by DDoS-Guard\n");
        let long = parse(502, &"x".repeat(500));
        assert_eq!(long.message().chars().count(), MAX_TEXT_MESSAGE + 1);
        assert_eq!(parse(502, "").message(), "Bad Gateway");
    }

    #[test]
    fn trading_error() {
        let error = parse(400, r#"{"retCode": 10001, "retMsg": "params error"}"#);
        assert_eq!(error.kind(), ErrorKind::BadRequest);
        assert_eq!(error.code(), Some(10001));
        assert_eq!(error.message(), "params error");
    }

    #[test]
    fn mfa_refusal_in_message_or_status() {
        for body in [
            r#"{"message": "Operation denied, requires mfa checking"}"#,
            r#"{"status": "Operation denied, requires mfa checking"}"#,
        ] {
            assert_eq!(parse(403, body).kind(), ErrorKind::MfaRequired, "{body}");
        }
        assert_eq!(
            parse(400, r#"{"status": "MFA_CHECKING_REQUIRED"}"#).kind(),
            ErrorKind::BadRequest
        );
    }

    #[test]
    fn captcha_codes_matter_only_on_sign_in() {
        let body = br#"{"status": "INVALID_RECAPTCHA", "message": "Bad captcha"}"#;
        let headers = HeaderMap::new();
        let other = ApiError::from_response(StatusCode::FORBIDDEN, &headers, body);
        let login = ApiError::from_login_response(StatusCode::BAD_REQUEST, &headers, body);
        assert_eq!(other.kind(), ErrorKind::PermissionDenied);
        assert_eq!(login.kind(), ErrorKind::CaptchaRequired);
    }

    #[test]
    fn sign_in_rejections() {
        let login = |status: u16, body: &str| {
            ApiError::from_login_response(
                StatusCode::from_u16(status).unwrap(),
                &HeaderMap::new(),
                body.as_bytes(),
            )
            .kind()
        };
        assert_eq!(
            login(400, r#"{"error": "invalid_request"}"#),
            ErrorKind::Authentication
        );
        assert_eq!(
            login(400, r#"{"message": "Username is blocked"}"#),
            ErrorKind::BadRequest
        );
        assert_eq!(
            login(400, r#"{"message": "Too many requests"}"#),
            ErrorKind::RateLimited
        );
        assert_eq!(login(503, ""), ErrorKind::Server);
    }
}
