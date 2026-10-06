// Saves the PDF statement of the last 30 days of exchange orders: `node export-report.ts [file]`.
import { writeFile } from 'node:fs/promises';
import { createClient } from './client.ts';

const client = createClient();
const file = process.argv[2] ?? 'exchange-report.pdf';

// The API takes timestamps like 2026-11-07T00:00:00+0000.
const timestamp = (date: Date) => date.toISOString().replace(/\.\d+Z$/, '+0000');
const end = new Date();
const start = new Date(end.getTime() - 30 * 24 * 60 * 60 * 1000);

const pdf = await client.reports.downloadExchange({
  limit: 100,
  completionDateFrame: { start: timestamp(start), end: timestamp(end) },
});
await writeFile(file, pdf);
console.log(`Saved ${pdf.byteLength} bytes to ${file}`);
