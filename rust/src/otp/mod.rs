//! One-time codes for two-factor login and operation confirmation.

#[cfg(feature = "otp")]
mod totp;

use std::fmt;
use std::future::Future;
use std::sync::Arc;

use futures_util::FutureExt;
use futures_util::future::BoxFuture;

use crate::error::{BoxError, Error, Result};

#[cfg(feature = "otp")]
pub use self::totp::Totp;

type Provide = dyn Fn() -> BoxFuture<'static, Result<String, BoxError>> + Send + Sync;

/// Source of the TOTP codes sent when the sign-in of an account with two-factor authentication
/// asks for one, and with [operation confirmations](crate::Client::confirm_operation).
///
/// A provider is asked for a code only when one is needed, so a callback or [`Totp`] keeps
/// working when the client has to log in again hours later; a fixed code only suits a single
/// login. A callback means the TOTP secret never has to be given to the client. Surrounding
/// whitespace is removed from the codes.
///
/// # Examples
///
/// ```no_run
/// use whitebird::{Client, CodeProvider};
///
/// let otp = CodeProvider::from_fn(|| {
///     println!("Code from the authenticator app:");
///     let mut code = String::new();
///     std::io::stdin().read_line(&mut code)?;
///     Ok(code.trim().to_owned())
/// });
/// let client = Client::builder()
///     .credentials("me@example.com", "password")
///     .otp(otp)
///     .build()?;
/// # Ok::<(), whitebird::Error>(())
/// ```
#[derive(Clone)]
pub struct CodeProvider(Source);

#[derive(Clone)]
enum Source {
    Fixed(String),
    Callback(Arc<Provide>),
    #[cfg(feature = "otp")]
    Totp(Totp),
}

impl CodeProvider {
    /// Always returns `code`.
    pub fn fixed(code: impl Into<String>) -> Self {
        Self(Source::Fixed(code.into()))
    }

    /// Creates a provider from an async function, such as one that waits for the code in a
    /// chat.
    pub fn new<F, Fut>(provide: F) -> Self
    where
        F: Fn() -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<String, BoxError>> + Send + 'static,
    {
        Self(Source::Callback(Arc::new(move || provide().boxed())))
    }

    /// Creates a provider from a blocking function, which runs on a thread where blocking is
    /// fine, such as one prompting in the terminal.
    pub fn from_fn<F>(provide: F) -> Self
    where
        F: Fn() -> Result<String, BoxError> + Send + Sync + 'static,
    {
        let provide = Arc::new(provide);
        Self::new(move || {
            let provide = Arc::clone(&provide);
            async move {
                tokio::task::spawn_blocking(move || provide())
                    .await
                    .map_err(BoxError::from)?
            }
        })
    }

    /// The next code, without surrounding whitespace.
    pub(crate) async fn code(&self) -> Result<String> {
        let code = match &self.0 {
            Source::Fixed(code) => code.clone(),
            Source::Callback(provide) => provide().await.map_err(Error::CodeProvider)?,
            #[cfg(feature = "otp")]
            Source::Totp(totp) => totp.code(),
        };
        Ok(code.trim().to_owned())
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
