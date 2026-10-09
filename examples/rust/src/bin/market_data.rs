//! Public market data, no account needed: instruments, an order book and hourly candles.
//!
//! `cargo run --bin market_data -- [SYMBOL] [--demo]`

use std::time::{SystemTime, UNIX_EPOCH};

use whitebird::models::{MarketCategory, MarketKlineInterval};
use whitebird::params::{MarketInstrumentsParams, MarketKlinesParams, MarketOrderbookParams};
use whitebird::{Client, Environment};
use whitebird_examples::Error;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let demo = args.iter().any(|arg| arg == "--demo");
    let symbol = args
        .iter()
        .find(|arg| !arg.starts_with("--"))
        .map_or("BTCUSDT", String::as_str);

    let environment = if demo {
        Environment::Demo
    } else {
        Environment::Production
    };
    let client = Client::builder().environment(environment).build()?;
    let market = client.market();

    let instruments = market
        .instruments(
            MarketInstrumentsParams::builder()
                .category(MarketCategory::Spot)
                .build(),
        )
        .await?;
    println!("{} spot instruments", instruments.len());
    for instrument in instruments.iter().take(5) {
        println!(
            "  {:<10} {}/{}  min {} {}",
            instrument.symbol,
            instrument.base_coin,
            instrument.quote_coin,
            instrument.min_order_qty,
            instrument.base_coin
        );
    }

    let book = market
        .orderbook(
            MarketOrderbookParams::builder()
                .symbol(symbol)
                .limit(5)
                .build(),
        )
        .await?;
    println!("\n{symbol} order book");
    for (bid, ask) in book.bids.iter().zip(&book.asks) {
        println!(
            "  {:>12} {:>12} | {:<12} {:<12}",
            bid[1], bid[0], ask[0], ask[1]
        );
    }

    // Without `start` the series would begin at the oldest candle.
    let now = i64::try_from(SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis())?;
    let day_ago = now - 24 * 3600 * 1000;
    let candles = market
        .klines(
            MarketKlinesParams::builder()
                .symbol(symbol)
                .interval(MarketKlineInterval::V1H)
                .start(day_ago)
                .limit(24)
                .build(),
        )
        .await?;
    println!("\n{symbol} hourly candles, last 24 h");
    for candle in &candles {
        println!(
            "  open {:<12} high {:<12} low {:<12} close {:<12} volume {}",
            candle.open, candle.high, candle.low, candle.close, candle.volume
        );
    }
    Ok(())
}
