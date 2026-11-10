# API changes

Whitebird may change its API at any time. This page lists what stays stable for your code.

## Experimental operations

Operations marked *experimental* in the [endpoint reference](../reference/endpoints.md) may
change without notice, and their response models may be incomplete. Response fields that are not
guaranteed are never required.

## What your code can rely on

The clients are written to survive API changes between releases:

- unknown response fields are ignored, and Python models keep them in `model_extra`;
- enums are open: a value added on the server is passed through instead of failing;
- timestamps are accepted as RFC 3339, ISO 8601 without zone (UTC) or epoch milliseconds;
- amounts are decimals, so precision is never lost to floating point;
- each service host can be overridden (see [Environments](environments.md)), and any endpoint can
  be called with [raw requests](raw-requests.md).

## Versioning

The four SDKs are released together under one version and follow
[semantic versioning](https://semver.org):

- a breaking change to the client or to a stable (non-experimental) method comes only with a new
  major version;
- experimental operations may change shape in a minor version, following Whitebird;
- a method whose endpoint Whitebird retires is marked *deprecated* first and removed in the next
  major version.

Every change is listed in each package's changelog.
