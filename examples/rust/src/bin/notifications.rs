//! Prints account notifications as they arrive, until Ctrl+C. The stream reconnects by itself.
//!
//! `cargo run --bin notifications` (credentials in `WHITEBIRD_*` variables)

use futures::TryStreamExt;
use whitebird::models::NotificationStreamMessageType as Type;
use whitebird_examples::Error;

#[tokio::main]
async fn main() -> Result<(), Error> {
    let client = whitebird_examples::client()?;
    let mut events = client.notifications().stream();

    println!("Listening, press Ctrl+C to stop.");
    loop {
        let event = tokio::select! {
            _ = tokio::signal::ctrl_c() => return Ok(()),
            event = events.try_next() => event?,
        };
        let Some(event) = event else { return Ok(()) };
        // Skip events that are not notifications instead of stopping.
        let message = match event.json() {
            Ok(message) => message,
            Err(error) => {
                eprintln!("skipped event {:?}: {error}", event.id);
                continue;
            }
        };
        let payload = message.payload.unwrap_or_default();
        match message.r#type {
            Some(Type::OrderUpdate) => println!("order update: {payload}"),
            Some(Type::BalanceOperationUpdate) => println!("deposit or withdrawal: {payload}"),
            Some(other) => println!("{other}: {payload}"),
            None => println!("{}", event.data),
        }
    }
}
