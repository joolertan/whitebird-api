# How the SDKs keep up with the API

Whitebird ships its web application several times a week, and the backend changes with it. Since
there is no published API reference, this project reconstructs the API from the application
itself and watches it for changes.

## Where the API description comes from

`spec/` holds one OpenAPI 3.1 document per backend service. Every operation records where its
shape comes from (`x-wb-source`):

| Source | Meaning |
|---|---|
| `openapi` | imported from the service's own published OpenAPI document |
| `live` | checked against the running service (public endpoints only) |
| `bundle` | reconstructed from the web application's code; marked *experimental* |

Response fields that were inferred from how the application uses a response, rather than seen in
one, are marked as inferred and are never required.

## The daily drift check

`tools/wbscan` downloads the current build of whitebird.io, deobfuscates it with
[webcrack](https://github.com/j4k0xb/webcrack) and finds every HTTP request in it with a small
static evaluator, without running the code. The result is compared with `spec/`; a scheduled
workflow opens an issue labelled `api-drift` listing:

- new and removed hosts, and a changed OAuth client;
- new endpoints, and endpoints the application no longer calls;
- request fields the application sends but the specification lacks.

The specification is then updated, the SDKs are regenerated and a new version is released.

## What your code can rely on

The clients are written to survive API changes between releases:

- unknown response fields are ignored, and Python models keep them in `model_extra`;
- enums are open: a value added on the server is passed through instead of failing;
- timestamps are accepted as RFC 3339, ISO 8601 without zone (UTC) or epoch milliseconds;
- amounts are decimals, so precision is never lost to floating point;
- each service host can be overridden (see [Environments](environments.md)), and any endpoint can
  be called with [raw requests](raw-requests.md).

## Versioning

Each SDK follows [semantic versioning](https://semver.org). Until 1.0, a minor version may change
generated signatures when the API changes shape. Changes to stable (non-experimental) methods are
listed in each package's changelog.
