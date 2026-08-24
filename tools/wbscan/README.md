# wbscan

Reverse-engineers the HTTP API of [whitebird.io](https://whitebird.io) from its public frontend
bundles and checks the repository's OpenAPI specs (`spec/services/*.yaml`) for drift.

The site is a Vite/Rolldown React SPA without source maps. `wbscan` downloads its chunks,
unminifies them with [webcrack](https://github.com/j4k0xb/webcrack) and statically extracts every
HTTP request it can resolve into `inventory.json`. Bundle code is only parsed, never executed.

## Setup

Node.js >= 22.18 (TypeScript runs through Node's type stripping, no build step).

```sh
cd tools/wbscan
npm ci
```

webcrack's sandbox dependency `isolated-vm` is replaced by a stub (`stubs/isolated-vm`,
see `overrides` in `package.json`): it is a native module that does not build on every Node
version, and wbscan must not evaluate bundle code anyway.

## Commands

```sh
node src/cli.ts scan                 # fetch + deobfuscate + extract (npm run scan)
node src/cli.ts fetch                # index.html + all chunks -> <cache>/<build>/raw/
node src/cli.ts deobfuscate          # webcrack -> <cache>/<build>/deobfuscated/
node src/cli.ts extract              # -> <cache>/<build>/inventory.json
node src/cli.ts openapi              # public /v3/api-docs -> <cache>/openapi/<service>.json
node src/cli.ts diff --check         # Markdown drift report (npm run diff -- --check)
node src/cli.ts diff --json          # the same report as JSON
```

Common options: `--cache-dir <dir>` (default `<repo>/.cache/wbscan`), `--build <id>` (default:
the latest fetched build), `--force` (redo cached steps), `--out <file>` (extract: also write the
inventory there), `--spec <dir>` (diff: default `<repo>/spec`; `spec/` or `spec/services/`),
`--inventory <file>`, `--openapi-dir <dir>`.

Exit codes: `0` success / no drift, `1` drift found with `--check`, `2` error.

### Cache layout

```
.cache/wbscan/
  latest                      id of the most recently fetched build
  <build>/                    build id = hash of the entry chunk (index-<hash>.js)
    meta.json                 entry, fetchedAt, chunk list
    raw/                      index.html and chunks as served
    deobfuscated/<chunk>.js   webcrack output; inventory line numbers refer to these files
    inventory.json
  openapi/<service>.json      specs published by the services themselves
```

`fetch` skips the download when the build is already cached; `deobfuscate` skips chunks that
already have output. A daily run therefore only does real work when the site was redeployed.

## Inventory

`inventory.json` has stable keys and sorted contents:

- `build`: entry chunk, fetch time, chunk list.
- `env`: URL-valued `VITE_*` variables of the build-time env config (Vite inlines
  `import.meta.env` as an object literal).
- `oauth`: token URL, the public client's `Authorization: Basic` value, decoded client id, env
  vars that hold them, grant types seen in the code.
- `services`: `<name>` -> `{production, demo}` origins. The name is the `*.whitebird.io`
  subdomain (full host otherwise). Demo hosts are folded into their production service using the
  `*_DEMO` / `*_LIVE` env pairs.
- `endpoints`: `{service, method, path, query, body: {kind, fields}, headers, responseType, sources}`.
  Path parameters are named after their source property (`{userId}`) or `{id}` / `{id1}`, `{id2}`.
  An empty `body.fields` means the fields are not statically known (body passed in as a value).
- `unresolved`: request sites whose URL or method could not be resolved, and API URLs that occur
  in the code without a detected request (redirect targets, iframes, or missed clients), each with
  a best-effort `partial` URL.

### How URLs are resolved

A small symbolic evaluator follows bindings (including Rolldown's `var x; init = () => { x = ... }`
pattern), cross-chunk `import`/`export` lists, object literals, template literals and calls to small
functions, keeping unknown values as named holes. On top of that:

- Calls whose argument is a `VITE_*` key (`getEnv(keys.VITE_X)`) and members named like a key
  (`env.VITE_X`) resolve through the env config.
- A lookup `table[key]` with an unknown key is evaluated once per table entry, using the same entry
  for every lookup by that key, and conditions like `entry.method === 'get'` prune the branches that
  cannot run for it. This keeps URL and method paired for provider/report dispatch tables.
- `{{BASE_URL_X}}` templates of the trading terminal resolve through objects with matching keys.
- Requests are axios-style calls (`client.post(url, data, config)`), request config objects
  (`{url, method, data, params, headers, responseType}`), action configs (`{path, method}`),
  `fetch`, fetch-event-source style calls (`onmessage`) and `EventSource`.

Hosts other than `*.whitebird.io` count as API hosts only if an env URL variable points to them;
the website itself, the CMS (`content.`) and landing pages (`info.`) are excluded, as are static
assets.

## Drift check (`diff`)

`diff` reads the spec directory (`--spec`, default `<repo>/spec`):

- `environments.yaml`: `services.<id>.{production, demo}` hosts and the public OAuth client
  (`oauth.basic`; token URL = `production` of `oauth.service` + `oauth.token_path`),
- `services/*.yaml`: one OpenAPI 3.1 document per service. A file belongs to the inventory service
  whose production or demo host is in its `servers`; `x-wb-service` is only its display name.

Operations match inventory endpoints on (method, path with every parameter reduced to `{}`); a
parameter segment on either side also matches a literal segment, so `/subaccount/add/{type}`
covers `/subaccount/add/organization`.

The report lists:

- new hosts (called by the frontend, unknown to `environments.yaml` and the specs' `servers`) and
  removed hosts (known to the spec, no longer mentioned anywhere in the frontend, env included),
- a changed OAuth client (token URL or `Basic` value),
- new endpoints (in the inventory, not in any spec),
- gone endpoints (in a spec, not in the inventory). An operation marked `x-wb-source: openapi`
  (imported from a service's published spec) is not reported while that service's published
  `/v3/api-docs` still contains it, since the frontend need not call every documented operation.
  Run `openapi` before `diff` so the published specs are current,
- changed request shapes: query parameter names, body kind and top-level body fields. Only compared
  for operations not marked `x-wb-source: openapi`, and body fields only when the inventory knows
  them.

## CI

A daily job runs, from `tools/wbscan`:

```sh
npm ci
npm run scan
node src/cli.ts openapi
node src/cli.ts diff --check > drift.md
```

Cache `.cache/wbscan` between runs (keyed e.g. on the date) to skip webcrack for unchanged chunks;
a full deobfuscation takes several minutes and ~3 GB of memory for the largest chunk. On exit
code `1`, publish `drift.md` (job summary, issue or PR) and update the specs.

## Development

```sh
npm test          # node --test, fixtures in test/fixtures
npm run typecheck # tsc --noEmit (strict)
```
