use std::time::Duration;

use reqwest::header::{HeaderMap, RETRY_AFTER};
use reqwest::{Method, StatusCode};

/// Exponential backoff with jitter for idempotent requests.
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

pub(crate) fn is_idempotent(method: &Method) -> bool {
    matches!(
        *method,
        Method::GET | Method::HEAD | Method::PUT | Method::DELETE | Method::OPTIONS
    )
}

pub(crate) fn is_retryable(status: StatusCode) -> bool {
    matches!(status.as_u16(), 429 | 502 | 503 | 504)
}

pub(crate) fn is_retryable_error(error: &reqwest::Error) -> bool {
    error.is_connect() || error.is_timeout() || error.is_request()
}

/// Parses `Retry-After` given in seconds; HTTP dates fall back to the computed backoff.
pub(crate) fn retry_after(headers: &HeaderMap) -> Option<Duration> {
    let value = headers.get(RETRY_AFTER)?.to_str().ok()?.trim();
    value
        .parse::<f64>()
        .ok()
        .filter(|seconds| seconds.is_finite() && *seconds >= 0.0)
        .map(Duration::from_secs_f64)
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
    fn parses_retry_after_seconds() {
        let mut headers = HeaderMap::new();
        headers.insert(RETRY_AFTER, "2".parse().unwrap());
        assert_eq!(retry_after(&headers), Some(Duration::from_secs(2)));
        headers.insert(
            RETRY_AFTER,
            "Wed, 21 Oct 2015 07:28:00 GMT".parse().unwrap(),
        );
        assert_eq!(retry_after(&headers), None);
    }
}
