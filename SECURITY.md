# Security policy

## Reporting a vulnerability

Please report vulnerabilities in these SDKs privately through
[GitHub security advisories](https://github.com/joolertan/whitebird-api/security/advisories/new),
not in public issues. You will get an answer within a week.

Vulnerabilities in Whitebird's own services are out of scope here; report them to Whitebird.

## Supported versions

Fixes are released for the latest minor version of each package.

## Handling credentials

- The SDKs never log passwords, TOTP secrets, tokens or the `x-mfa-token`.
- Tokens are kept in memory unless you configure a token store. The Python `FileTokenStore`
  writes its file with `0600` permissions; apply the same care to stores you write yourself.
- The OAuth client credentials in `spec/environments.yaml` are public client credentials, not
  secrets.
