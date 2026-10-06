// Quotes a conversion of 10 USDT to BTC in the Whitebird wallet and places the order only when
// run with --confirm.
import { createClient } from './client.ts';

const client = createClient();

const quote = await client.exchange.quotes.create({
  input: { type: 'INTERNAL_BALANCE', asset: 'USDT', amount: 10 },
  output: { type: 'INTERNAL_BALANCE', asset: 'BTC' },
});
const { input, output } = quote;
console.log(`${input?.amount} ${input?.asset} -> ${output?.amount} ${output?.asset}`);
console.log(`rate ${quote.exchangeRateValue}, valid until ${quote.expirationDate}`);

if (!process.argv.includes('--confirm')) {
  console.log('Dry run: pass --confirm to place the order.');
} else if (quote.id) {
  const order = await client.exchange.orders.create({
    quoteId: quote.id,
    destination: 'ACCOUNTING',
  });
  console.log(`Order ${order.id} is ${order.status}`);
}
