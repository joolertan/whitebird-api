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

    /// Returns `true` if the access token expires within `margin` from now. The expiry is read
    /// from the token itself when `expires_at` is unknown.
    pub(crate) fn expires_within(&self, margin: Duration) -> bool {
        self.expires_at
            .or_else(|| jwt_expiry(&self.access_token))
            .is_some_and(|at| at <= unix_now().saturating_add(margin.as_secs()))
    }

    /// The refresh token, unless it is a JWT that has expired.
    pub(crate) fn usable_refresh_token(&self) -> Option<&str> {
        self.refresh_token
            .as_deref()
            .map(str::trim)
            .filter(|token| !token.is_empty())
            .filter(|token| jwt_expiry(token).is_none_or(|at| at > unix_now()))
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

/// The `exp` claim of a JWT; `None` if `token` is not a JWT with one.
pub(crate) fn jwt_expiry(token: &str) -> Option<u64> {
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
/// The client reads the tokens before every authenticated request, saves them after each login
/// or refresh and clears them when the session ends: after `client.user().logout()` or when the
/// refresh token is rejected. Implement it to keep a session across restarts, for example in a
/// file or a keychain.
///
/// The methods cannot fail: [`load`](Self::load) returns `None` when the stored data is missing,
/// unreadable or not valid tokens, so the client signs in again, and failures to save or clear
/// should be logged rather than reported, since the request that obtained the tokens succeeded.
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
///         // Read the file and decode it with serde_json here; None if that fails.
///         self.0.lock().unwrap_or_else(PoisonError::into_inner).clone()
///     }
///
///     fn save(&self, tokens: &Tokens) {
///         // Write serde_json::to_vec(tokens) to a temporary file and rename it here.
///         *self.0.lock().unwrap_or_else(PoisonError::into_inner) = Some(tokens.clone());
///     }
///
///     fn clear(&self) {
///         // Delete the file here.
///         *self.0.lock().unwrap_or_else(PoisonError::into_inner) = None;
///     }
/// }
/// ```
pub trait TokenStore: Send + Sync {
    /// Returns the stored tokens; `None` if there are none or they cannot be read.
    fn load(&self) -> Option<Tokens>;

    /// Replaces the stored tokens.
    fn save(&self, tokens: &Tokens);

    /// Forgets the stored tokens.
    fn clear(&self);
}

/// Lets the caller keep a handle on a store it gives to the client.
impl<T: TokenStore + ?Sized> TokenStore for Arc<T> {
    fn load(&self) -> Option<Tokens> {
        (**self).load()
    }

    fn save(&self, tokens: &Tokens) {
        (**self).save(tokens);
    }

    fn clear(&self) {
        (**self).clear();
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

    fn clear(&self) {
        *self.0.lock().unwrap_or_else(PoisonError::into_inner) = None;
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
    fn reads_expiry_from_the_token_when_unknown() {
        let payload = URL_SAFE_NO_PAD.encode(br#"{"exp":1700000000}"#);
        let tokens = Tokens {
            access_token: format!("h.{payload}.s"),
            refresh_token: None,
            expires_at: None,
        };
        assert!(tokens.expires_within(Duration::ZERO));
    }

    #[test]
    fn expired_refresh_tokens_are_not_used() {
        let expired = format!("h.{}.s", URL_SAFE_NO_PAD.encode(br#"{"exp":1700000000}"#));
        let valid = format!("h.{}.s", URL_SAFE_NO_PAD.encode(br#"{"exp":99999999999}"#));
        let tokens = |refresh: &str| Tokens::new("access", Some(refresh.to_owned()));
        assert_eq!(tokens(&expired).usable_refresh_token(), None);
        assert_eq!(tokens(&valid).usable_refresh_token(), Some(valid.as_str()));
        assert_eq!(tokens("opaque").usable_refresh_token(), Some("opaque"));
        assert_eq!(tokens(" ").usable_refresh_token(), None);
    }

    #[test]
    fn debug_redacts_tokens() {
        let tokens = Tokens::new("secret-access", Some("secret-refresh".into()));
        assert!(!format!("{tokens:?}").contains("secret"));
    }
}
