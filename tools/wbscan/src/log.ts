/** Progress goes to stderr so stdout stays clean for reports. */
export function log(message: string): void {
  process.stderr.write(`${message}\n`);
}
