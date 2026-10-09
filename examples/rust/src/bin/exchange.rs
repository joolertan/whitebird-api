//! Prices a conversion between two wallet assets and, with `--confirm`, places the order.
//!
//! `cargo run --bin exchange -- 10 USDT BTC [--confirm]` (credentials in `WHITEBIRD_*` variables)

use whitebird::Decimal;
use whitebird::models::{ExchangeDestination, ExchangeLegType, ExchangeQuoteLegRequest};
use whitebird::params::{ExchangeOrdersCreateParams, ExchangeQuotesCreateParams};
use whitebird_examples::Error;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let confirm = args.iter().any(|arg| arg == "--confirm");
    let words: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with("--"))
        .collect();
    let [amount, from, to] = words[..] else {
        return Err("usage: exchange AMOUNT FROM TO [--confirm]".into());
    };
    let amount: Decimal = amount.parse()?;
    let client = whitebird_examples::client()?;

    let input = ExchangeQuoteLegRequest::builder()
        .asset(from)
        .r#type(ExchangeLegType::InternalBalance)
        .amount(amount)
        .build();
    let output = ExchangeQuoteLegRequest::builder()
        .asset(to)
        .r#type(ExchangeLegType::InternalBalance)
        .build();
    let quote = client
        .exchange()
        .quotes()
        .create(
            ExchangeQuotesCreateParams::builder()
                .input(input)
                .output(output)
                .build(),
        )
        .await?;
    let receive = quote
        .output
        .as_ref()
        .and_then(|leg| leg.amount)
        .unwrap_or_default();
    println!(
        "{amount} {from} -> {receive} {to} at {}, quote valid until {}",
        quote.exchange_rate_value.unwrap_or_default(),
        quote
            .expiration_date
            .map(|date| date.to_string())
            .unwrap_or_default()
    );

    if !confirm {
        println!("Dry run: pass --confirm to place the order.");
        return Ok(());
    }
    let order = client
        .exchange()
        .orders()
        .create(
            ExchangeOrdersCreateParams::builder()
                .quote_id(quote.id.ok_or("the quote has no id")?)
                .destination(ExchangeDestination::Accounting)
                .build(),
        )
        .await?;
    println!(
        "Order {} is {:?}",
        order.id.unwrap_or_default(),
        order.status
    );
    Ok(())
}
