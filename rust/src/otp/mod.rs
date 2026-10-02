//! One-time codes for two-factor login.

#[cfg(feature = "otp")]
mod totp;

use std::fmt;
use std::sync::Arc;

#[cfg(feature = "otp")]
pub use self::totp::Totp;

/// Source of the TOTP code sent with the password when two-factor authentication is on.
///
/// A provider is asked for a code on every password login, so a callback or [`Totp`] keeps
/// working when the client has to log in again hours later; a fixed code only suits a single
/// login.
///
/// # Examples
///
/// ```
/// use whitebird::{Client, CodeProvider};
///
/// # fn main() -> whitebird::Result<()> {
/// let client = Client::builder()
///     .credentials("me@example.com", "password")
///     .otp(CodeProvider::from_fn(|| std::env::var("WHITEBIRD_CODE").unwrap_or_default()))
///     .build()?;
/// # Ok(())
/// # }
/// ```
#[derive(Clone)]
pub struct CodeProvider(Source);

#[derive(Clone)]
enum Source {
    Fixed(String),
    Callback(Arc<dyn Fn() -> String + Send + Sync>),
    #[cfg(feature = "otp")]
    Totp(Totp),
}

impl CodeProvider {
    /// Always returns `code`.
    pub fn fixed(code: impl Into<String>) -> Self {
        Self(Source::Fixed(code.into()))
    }

    /// Calls `callback` for every login, for example to prompt the user.
    pub fn from_fn(callback: impl Fn() -> String + Send + Sync + 'static) -> Self {
        Self(Source::Callback(Arc::new(callback)))
    }

    pub(crate) fn code(&self) -> String {
        match &self.0 {
            Source::Fixed(code) => code.clone(),
            Source::Callback(callback) => callback(),
            #[cfg(feature = "otp")]
            Source::Totp(totp) => totp.code(),
        }
    }
}

impl From<&str> for CodeProvider {
    fn from(code: &str) -> Self {
        Self::fixed(code)
    }
}

impl From<String> for CodeProvider {
    fn from(code: String) -> Self {
        Self::fixed(code)
    }
}

#[cfg(feature = "otp")]
impl From<Totp> for CodeProvider {
    fn from(totp: Totp) -> Self {
        Self(Source::Totp(totp))
    }
}

impl fmt::Debug for CodeProvider {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let kind = match &self.0 {
            Source::Fixed(_) => "Fixed",
            Source::Callback(_) => "Callback",
            #[cfg(feature = "otp")]
            Source::Totp(_) => "Totp",
        };
        f.debug_tuple("CodeProvider")
            .field(&format_args!("{kind}"))
            .finish()
    }
}
