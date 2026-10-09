//! Prints the balances of the Whitebird wallet.
//!
//! `cargo run --bin balances` (credentials in `WHITEBIRD_*` variables)

use whitebird::models::AccountingAccountType;
use whitebird::params::AccountingAccountsGetParams;
use whitebird_examples::Error;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let client = whitebird_examples::client()?;

    let wallet = client
        .accounting()
        .accounts()
        .get(
            AccountingAccountsGetParams::builder()
                .r#type(AccountingAccountType::UserBalance)
                .build(),
        )
        .await?;
    for balance in wallet.balances.unwrap_or_default() {
        println!(
            "{:<10} {}",
            balance.currency.as_deref().unwrap_or("?"),
            balance.amount.unwrap_or_default()
        );
    }
    Ok(())
}
