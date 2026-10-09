//! Walks the exchange order history page by page, newest first, stopping after N orders.
//!
//! `cargo run --bin order_history -- [N]` (credentials in `WHITEBIRD_*` variables)

use futures::{StreamExt, TryStreamExt};
use whitebird::models::ExchangeLeg;
use whitebird::params::ExchangeOrdersHistoryParams;
use whitebird_examples::Error;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let limit = std::env::args().nth(1).map_or(Ok(30), |n| n.parse())?;
    let client = whitebird_examples::client()?;

    let params = ExchangeOrdersHistoryParams::builder()
        .size(20)
        .sort(vec!["creationDate,desc".into()])
        .build();
    // Pages are fetched as the stream is consumed, so `take` stops the requests too.
    let mut orders = client.exchange().orders().history(params).take(limit);
    while let Some(order) = orders.try_next().await? {
        let created = order.creation_date.map(|date| date.to_string());
        let status = order.status.map(|status| status.to_string());
        println!(
            "{}  {:<10}  {} -> {}",
            created.unwrap_or_default(),
            status.unwrap_or_default(),
            describe(order.input.as_ref()),
            describe(order.output.as_ref()),
        );
    }
    Ok(())
}

fn describe(leg: Option<&ExchangeLeg>) -> String {
    leg.map(|leg| {
        let asset = leg.asset.as_deref().unwrap_or("?");
        format!("{} {asset}", leg.amount.unwrap_or_default())
    })
    .unwrap_or_default()
}
