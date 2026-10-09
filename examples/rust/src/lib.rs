//! Shared setup of the examples: credentials from the environment and a session kept in a file.

use std::env;
use std::fs;
use std::path::PathBuf;

use whitebird::{Client, TokenStore, Tokens, Totp};

/// Error type of the examples' `main` functions.
pub type Error = Box<dyn std::error::Error>;

/// Keeps the session tokens in a JSON file so that later runs skip the login. The file grants
/// access to the account: keep it private.
pub struct FileTokenStore {
    path: PathBuf,
}

impl FileTokenStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }
}

impl TokenStore for FileTokenStore {
    fn load(&self) -> Option<Tokens> {
        let json = fs::read(&self.path).ok()?;
        serde_json::from_slice(&json).ok()
    }

    fn save(&self, tokens: &Tokens) {
        if let Ok(json) = serde_json::to_vec_pretty(tokens) {
            if let Err(error) = fs::write(&self.path, json) {
                eprintln!(
                    "cannot save the session to {}: {error}",
                    self.path.display()
                );
            }
        }
    }
}

/// Reads a required environment variable.
///
/// # Errors
///
/// Returns an error naming the variable if it is not set.
pub fn var(name: &str) -> Result<String, Error> {
    env::var(name).map_err(|_| format!("set the {name} environment variable").into())
}

/// A client signed in as `WHITEBIRD_USERNAME` / `WHITEBIRD_PASSWORD`, with TOTP codes computed
/// from `WHITEBIRD_TOTP_SECRET` when it is set, sharing the session file of `login_totp`.
///
/// # Errors
///
/// Returns an error if a variable is missing or the TOTP secret is not valid base32.
pub fn client() -> Result<Client, Error> {
    let mut builder = Client::builder()
        .credentials(var("WHITEBIRD_USERNAME")?, var("WHITEBIRD_PASSWORD")?)
        .token_store(FileTokenStore::new("whitebird-session.json"));
    if let Ok(secret) = env::var("WHITEBIRD_TOTP_SECRET") {
        builder = builder.otp(Totp::new(&secret)?);
    }
    Ok(builder.build()?)
}
