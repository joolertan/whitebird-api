//! Downloads the PDF statement of exchange operations to a file.
//!
//! `cargo run --bin export_report -- [PATH]` (credentials in `WHITEBIRD_*` variables)

use whitebird::params::ReportsDownloadExchangeParams;
use whitebird_examples::Error;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "exchange-report.pdf".into());
    let client = whitebird_examples::client()?;

    let report = client
        .reports()
        // Like the web app: the latest 20 operations of any date.
        .download_exchange(ReportsDownloadExchangeParams::default())
        .await?;
    std::fs::write(&path, &report)?;
    println!("Saved {} bytes to {path}", report.len());
    Ok(())
}
