use std::time::{Duration, SystemTime, UNIX_EPOCH};

use chrono::DateTime;
use reqwest::header::{HeaderMap, RETRY_AFTER};
use reqwest::{Method, StatusCode};

/// Exponential backoff with jitter for retried requests.
#[derive(Debug, Clone, Copy)]
pub(crate) struct RetryPolicy {
    pub(crate) max_retries: u32,
    base: Duration,
    max_delay: Duration,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_retries: 2,
            base: Duration::from_millis(500),
            max_delay: Duration::from_secs(30),
        }
    }
}

impl RetryPolicy {
    pub(crate) fn with_max_retries(max_retries: u32) -> Self {
        Self {
            max_retries,
            ..Self::default()
        }
    }

    /// Delay before retry number `attempt` (0-based), or `None` when the server asks to wait
    /// longer than the policy allows.
    pub(crate) fn delay(&self, attempt: u32, retry_after: Option<Duration>) -> Option<Duration> {
        if let Some(requested) = retry_after {
            return (requested <= self.max_delay).then_some(requested);
        }
        let exponential = self
            .base
            .saturating_mul(2u32.saturating_pow(attempt))
            .min(self.max_delay);
        Some(exponential.mul_f64(0.5 + fastrand::f64() / 2.0))
    }
}

/// Methods retried after any transient failure. Others are retried only when the request
/// certainly did not reach the server: the connection failed, or the answer was 429.
pub(crate) fn is_idempotent(method: &Method) -> bool {
    matches!(*method, Method::GET | Method::HEAD | Method::OPTIONS)
}

/// Whether an error answer may be retried, for an idempotent request or not.
pub(crate) fn is_retryable(status: StatusCode, idempotent: bool) -> bool {
    match status.as_u16() {
        429 => true,
        502..=504 => idempotent,
        _ => false,
    }
}

/// Whether a failed attempt may be retried, for an idempotent request or not.
pub(crate) fn is_retryable_error(error: &reqwest::Error, idempotent: bool) -> bool {
    if idempotent {
        error.is_connect() || error.is_timeout() || error.is_request()
    } else {
        error.is_connect()
    }
}

/// Longest `Retry-After` taken literally; longer ones are capped.
const MAX_RETRY_AFTER: Duration = Duration::from_secs(24 * 60 * 60);

/// Parses `Retry-After`, given in seconds or as an HTTP date.
pub(crate) fn retry_after(headers: &HeaderMap) -> Option<Duration> {
    let value = headers.get(RETRY_AFTER)?.to_str().ok()?.trim();
    if let Ok(seconds) = value.parse::<f64>() {
        if !seconds.is_finite() {
            return None;
        }
        return Some(
            Duration::try_from_secs_f64(seconds.max(0.0))
                .map_or(MAX_RETRY_AFTER, |delay| delay.min(MAX_RETRY_AFTER)),
        );
    }
    let date = DateTime::parse_from_rfc2822(value).ok()?;
    let at = UNIX_EPOCH + Duration::from_secs(u64::try_from(date.timestamp()).unwrap_or(0));
    let delay = at
        .duration_since(SystemTime::now())
        .unwrap_or(Duration::ZERO);
    Some(delay.min(MAX_RETRY_AFTER))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_and_honours_retry_after() {
        let policy = RetryPolicy::default();
        let first = policy.delay(0, None).unwrap();
        assert!(first >= Duration::from_millis(250) && first <= Duration::from_millis(500));
        let third = policy.delay(2, None).unwrap();
        assert!(third >= Duration::from_secs(1) && third <= Duration::from_secs(2));
        assert_eq!(
            policy.delay(0, Some(Duration::from_secs(3))),
            Some(Duration::from_secs(3))
        );
        assert_eq!(policy.delay(0, Some(Duration::from_secs(3600))), None);
    }

    #[test]
    fn parses_retry_after() {
        let parse = |value: &str| {
            let mut headers = HeaderMap::new();
            headers.insert(RETRY_AFTER, value.parse().unwrap());
            retry_after(&headers)
        };
        assert_eq!(parse("2"), Some(Duration::from_secs(2)));
        assert_eq!(parse("0.5"), Some(Duration::from_millis(500)));
        assert_eq!(parse("-3"), Some(Duration::ZERO));
        assert_eq!(parse("1e20"), Some(MAX_RETRY_AFTER));
        assert_eq!(parse("inf"), None);
        assert_eq!(parse("soon"), None);
        assert_eq!(parse("Wed, 21 Oct 2015 07:28:00 GMT"), Some(Duration::ZERO));
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
        let later = DateTime::from_timestamp(i64::try_from(now.as_secs()).unwrap() + 90, 0)
            .unwrap()
            .format("%a, %d %b %Y %H:%M:%S GMT")
            .to_string();
        let delay = parse(&later).unwrap();
        assert!(delay > Duration::from_secs(80) && delay <= Duration::from_secs(90));
    }

    #[test]
    fn retries_depend_on_the_method() {
        assert!(is_idempotent(&Method::GET));
        assert!(!is_idempotent(&Method::PUT));
        assert!(!is_idempotent(&Method::DELETE));
        assert!(is_retryable(StatusCode::TOO_MANY_REQUESTS, false));
        assert!(is_retryable(StatusCode::BAD_GATEWAY, true));
        assert!(!is_retryable(StatusCode::BAD_GATEWAY, false));
        assert!(!is_retryable(StatusCode::INTERNAL_SERVER_ERROR, true));
    }

    #[test]
    fn backoff_never_overflows() {
        let policy = RetryPolicy::with_max_retries(u32::MAX);
        for attempt in [31, 32, 64, u32::MAX] {
            assert!(policy.delay(attempt, None).unwrap() <= Duration::from_secs(30));
        }
    }
}
