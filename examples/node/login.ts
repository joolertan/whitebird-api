// Logs in with a TOTP code and keeps the session in .session.json (see client.ts): later runs,
// and the other examples, resume it instead of logging in again.
import { createClient } from './client.ts';

const client = createClient();

const user = await client.user.me();
console.log(`Logged in as ${user.email}`);
console.log(`  status: ${user.userStatus}`);
console.log(`  two-factor authentication: ${user.mfaEnabled ? 'on' : 'off'}`);
console.log(`  roles: ${user.roles?.join(', ')}`);
