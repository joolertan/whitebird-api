// Prints notifications as they arrive, until Ctrl+C.
import { createClient } from './client.ts';

const client = createClient();
const stop = new AbortController();
process.once('SIGINT', () => stop.abort());

console.log('Waiting for notifications, Ctrl+C to stop');
// The stream reconnects on its own and ends when the signal aborts.
for await (const { data } of client.notifications.stream({ signal: stop.signal })) {
  console.log(`${data.createdAt}  ${data.type}`, data.payload);
}
