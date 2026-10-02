use std::fmt;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde::{Deserialize, Serialize};

/// OAuth tokens of a signed-in session.
///
/// Tokens are serializable so that a [`TokenStore`] can persist them across restarts.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tokens {
    /// Bearer token sent with every authenticated request.
    pub access_token: String,
    /// Token used to obtain a new access token without the password.
    pub refresh_token: Option<String>,
    /// Unix time, in seconds, at which the access token expires, if known.
    pub expires_at: Option<u64>,
}

impl Tokens {
    /// Creates tokens, reading the expiry from the `exp` claim of the access token.
    pub fn new(access_token: impl Into<String>, refresh_token: Option<String>) -> Self {
        let access_token = access_token.into();
        Self {
            expires_at: jwt_expiry(&access_token),
            access_token,
            refresh_token,
        }
    }

    pub(crate) fn issued(
        access_token: String,
        refresh_token: Option<String>,
        expires_in: Option<u64>,
    ) -> Self {
        let mut tokens = Self::new(access_token, refresh_token);
        if tokens.expires_at.is_none() {
            tokens.expires_at = expires_in.map(|seconds| unix_now() + seconds);
        }
        tokens
    }

    /// Returns `true` if the access token expires within `margin` from now.
    pub(crate) fn expires_within(&self, margin: Duration) -> bool {
        self.expires_at
            .is_some_and(|at| at <= unix_now() + margin.as_secs())
    }
}

impl fmt::Debug for Tokens {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Tokens")
            .field("access_token", &"<redacted>")
            .field(
                "refresh_token",
                &self.refresh_token.as_ref().map(|_| "<redacted>"),
            )
            .field("expires_at", &self.expires_at)
            .finish()
    }
}

fn jwt_expiry(token: &str) -> Option<u64> {
    #[derive(Deserialize)]
    struct Claims {
        exp: u64,
    }
    let payload = token.split('.').nth(1)?;
    let json = URL_SAFE_NO_PAD.decode(payload.trim_end_matches('=')).ok()?;
    serde_json::from_slice::<Claims>(&json)
        .ok()
        .map(|claims| claims.exp)
}

fn unix_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs())
}

/// Storage for the session tokens.
///
/// The client reads the tokens before every authenticated request and saves them after each
/// login or refresh. Implement it to keep a session across restarts, for example in a file or a
/// keychain.
///
/// # Examples
///
/// ```
/// use std::sync::{Mutex, PoisonError};
/// use whitebird::{TokenStore, Tokens};
///
/// #[derive(Default)]
/// struct FileStore(Mutex<Option<Tokens>>);
///
/// impl TokenStore for FileStore {
///     fn load(&self) -> Option<Tokens> {
///         self.0.lock().unwrap().clone()
///     }
///
///     fn save(&self, tokens: &Tokens) {
///         // Write serde_json::to_vec(tokens) to disk here.
///         *self.0.lock().unwrap() = Some(tokens.clone());
///     }
/// }
/// ```
pub trait TokenStore: Send + Sync {
    /// Returns the stored tokens, if any.
    fn load(&self) -> Option<Tokens>;

    /// Replaces the stored tokens.
    fn save(&self, tokens: &Tokens);
}

/// Lets the caller keep a handle on a store it gives to the client.
impl<T: TokenStore + ?Sized> TokenStore for Arc<T> {
    fn load(&self) -> Option<Tokens> {
        (**self).load()
    }

    fn save(&self, tokens: &Tokens) {
        (**self).save(tokens);
    }
}

/// In-memory [`TokenStore`], the default.
#[derive(Debug, Default)]
pub struct MemoryTokenStore(Mutex<Option<Tokens>>);

impl MemoryTokenStore {
    /// Creates an empty store.
    pub fn new() -> Self {
        Self::default()
    }
}

impl From<Tokens> for MemoryTokenStore {
    fn from(tokens: Tokens) -> Self {
        Self(Mutex::new(Some(tokens)))
    }
}

impl TokenStore for MemoryTokenStore {
    fn load(&self) -> Option<Tokens> {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn save(&self, tokens: &Tokens) {
        *self.0.lock().unwrap_or_else(PoisonError::into_inner) = Some(tokens.clone());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_expiry_from_jwt() {
        let payload = URL_SAFE_NO_PAD.encode(br#"{"sub":"me","exp":1700000000}"#);
        let tokens = Tokens::new(format!("eyJhbGciOiJIUzI1NiJ9.{payload}.sig"), None);
        assert_eq!(tokens.expires_at, Some(1_700_000_000));
        assert!(tokens.expires_within(Duration::ZERO));
    }

    #[test]
    fn falls_back_to_expires_in() {
        let tokens = Tokens::issued("opaque".into(), None, Some(3600));
        assert!(!tokens.expires_within(Duration::from_secs(30)));
        assert!(tokens.expires_within(Duration::from_secs(3600)));
        assert_eq!(Tokens::new("opaque", None).expires_at, None);
    }

    #[test]
    fn debug_redacts_tokens() {
        let tokens = Tokens::new("secret-access", Some("secret-refresh".into()));
        assert!(!format!("{tokens:?}").contains("secret"));
    }
}
