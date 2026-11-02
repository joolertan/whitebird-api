// Prints the non-zero balances of the Whitebird wallet with their value in USD.
import { createClient } from './client.ts';

const client = createClient();

const { balances = [] } = await client.accounting.accounts.getEnhanced({ type: 'USER_BALANCE' });
// Amounts are decimal strings with every digit the API sent, such as "0.00000000".
for (const { currency, amount, usdAmount } of balances) {
  if (amount !== undefined && Number(amount) !== 0) {
    console.log(`${(currency ?? '?').padEnd(8)} ${amount}  (${usdAmount ?? '?'} USD)`);
  }
}
