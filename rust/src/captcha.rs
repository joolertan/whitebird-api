use std::fmt;
use std::future::Future;
use std::sync::Arc;

use bytes::Bytes;
use futures_util::FutureExt;
use futures_util::future::BoxFuture;

use crate::error::{BoxError, Error, Result};

/// A slider captcha: slide [`puzzle_piece`](Self::puzzle_piece) horizontally across
/// [`background_image`](Self::background_image), at height [`position_y`](Self::position_y),
/// until it fills the cut-out.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Captcha {
    /// Identifier of the captcha, sent back with the answer.
    pub request_id: String,
    /// PNG background (290×192) with the cut-out.
    pub background_image: Bytes,
    /// PNG of the piece to slide (64×64).
    pub puzzle_piece: Bytes,
    /// Vertical offset of the piece in background pixels.
    pub position_y: u32,
}

type Solve = dyn Fn(Captcha) -> BoxFuture<'static, Result<u32, BoxError>> + Send + Sync;

/// Solves the slider captcha that accounts without two-factor authentication pass to sign in.
///
/// The solver returns the horizontal offset of the piece in background pixels (0 to 250). The SDK
/// never solves captchas itself: show the images to a person or call a solving service. A wrong
/// answer is retried with a new captcha, three attempts in all.
///
/// # Examples
///
/// ```no_run
/// use whitebird::{CaptchaSolver, Client};
///
/// let solver = CaptchaSolver::from_fn(|captcha| {
///     std::fs::write("captcha.png", &captcha.background_image)?;
///     std::fs::write("piece.png", &captcha.puzzle_piece)?;
///     println!("Offset of piece.png in captcha.png (row {}):", captcha.position_y);
///     let mut answer = String::new();
///     std::io::stdin().read_line(&mut answer)?;
///     Ok(answer.trim().parse()?)
/// });
/// let client = Client::builder()
///     .credentials("me@example.com", "password")
///     .captcha(solver)
///     .build()?;
/// # Ok::<(), whitebird::Error>(())
/// ```
#[derive(Clone)]
pub struct CaptchaSolver(Arc<Solve>);

impl CaptchaSolver {
    /// Creates a solver from an async function.
    pub fn new<F, Fut>(solve: F) -> Self
    where
        F: Fn(Captcha) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<u32, BoxError>> + Send + 'static,
    {
        Self(Arc::new(move |captcha| solve(captcha).boxed()))
    }

    /// Creates a solver from a blocking function, which runs on a thread where blocking is
    /// fine, such as one waiting for a person to answer.
    pub fn from_fn<F>(solve: F) -> Self
    where
        F: Fn(Captcha) -> Result<u32, BoxError> + Send + Sync + 'static,
    {
        let solve = Arc::new(solve);
        Self::new(move |captcha| {
            let solve = Arc::clone(&solve);
            async move {
                tokio::task::spawn_blocking(move || solve(captcha))
                    .await
                    .map_err(BoxError::from)?
            }
        })
    }

    pub(crate) async fn solve(&self, captcha: Captcha) -> Result<u32> {
        (self.0)(captcha).await.map_err(Error::Captcha)
    }
}

impl fmt::Debug for CaptchaSolver {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CaptchaSolver").finish_non_exhaustive()
    }
}
