// Public market data, no account needed. Run with `demo` to use the sandbox trading contour.
import { Whitebird } from 'whitebird-api';

const symbol = 'BTCUSDT';
const client = new Whitebird({ environment: process.argv[2] === 'demo' ? 'demo' : 'production' });

const instruments = await client.market.instruments();
const symbols = instruments.map((instrument) => instrument.symbol);
console.log(`${instruments.length} instruments: ${symbols.slice(0, 8).join(', ')}, ...`);

const book = await client.market.orderbook({ symbol, limit: 5 });
console.log(`\n${symbol} order book`);
for (const [price, size] of book.asks.toReversed()) console.log(`  ask ${price}  ${size}`);
for (const [price, size] of book.bids) console.log(`  bid ${price}  ${size}`);

const candles = await client.market.klines({
  symbol,
  interval: '1h',
  start: Date.now() - 12 * 60 * 60 * 1000,
});
console.log(`\n${symbol} hourly candles`);
for (const candle of candles) {
  const time = new Date(candle.start).toISOString().slice(0, 16);
  console.log(`  ${time}  O ${candle.open}  H ${candle.high}  L ${candle.low}  C ${candle.close}`);
}
