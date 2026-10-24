//! Signs in and keeps the session in `whitebird-session.json`, so the next run reuses it instead
//! of logging in again.
//!
//! `WHITEBIRD_USERNAME=... WHITEBIRD_PASSWORD=... cargo run --bin login`

use whitebird_examples::Error;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let client = whitebird_examples::client()?;

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
