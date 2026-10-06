// Prints the non-zero balances of the Whitebird wallet with their value in USD.
import { createClient } from './client.ts';

const client = createClient();

const { balances = [] } = await client.accounting.accounts.getEnhanced({ type: 'USER_BALANCE' });
for (const { currency, amount, usdAmount } of balances) {
  if (amount) console.log(`${currency?.padEnd(8)} ${amount}  (${usdAmount ?? '?'} USD)`);
}
