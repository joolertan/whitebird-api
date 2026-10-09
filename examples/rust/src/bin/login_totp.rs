//! Signs in with a TOTP code computed from the two-factor secret and keeps the session in
//! `whitebird-session.json`, so the next run reuses it instead of logging in again.
//!
//! `WHITEBIRD_USERNAME=... WHITEBIRD_PASSWORD=... WHITEBIRD_TOTP_SECRET=... cargo run --bin login_totp`

use whitebird::{Client, Totp};
use whitebird_examples::{Error, FileTokenStore, var};

#[tokio::main]
async fn main() -> Result<(), Error> {
    let client = Client::builder()
        .credentials(var("WHITEBIRD_USERNAME")?, var("WHITEBIRD_PASSWORD")?)
        .otp(Totp::new(&var("WHITEBIRD_TOTP_SECRET")?)?)
        .token_store(FileTokenStore::new("whitebird-session.json"))
        .build()?;

    // The first call signs in, unless the saved session is still valid.
    let user = client.user().me().await?;
    println!("Signed in as {}", user.email.as_deref().unwrap_or("?"));
    println!(
        "Two-factor authentication: {}",
        user.mfa_enabled.unwrap_or(false)
    );

    let account = client.client().get().await?;
    println!("Client status: {:?}", account.status);
    println!("Client type: {:?}", account.client_type);
    Ok(())
}
