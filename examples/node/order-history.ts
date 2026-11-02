// Prints the latest exchange orders, newest first: `node order-history.ts [count]`.
import { createClient } from './client.ts';

const client = createClient();
const count = Number(process.argv[2] ?? 10);

// Pages of 20 are fetched as the loop goes; leaving the loop stops fetching.
const orders = client.exchange.orders.history({ size: 20, sort: 'creationDate,desc' });
let printed = 0;
for await (const order of orders) {
  const { input, output } = order;
  console.log(
    `${order.creationDate}  #${order.number}  ${order.status}  ` +
      `${input?.amount} ${input?.asset} -> ${output?.amount} ${output?.asset}`,
  );
  if (++printed === count) break;
}
