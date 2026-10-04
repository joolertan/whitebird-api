#!/usr/bin/env bash
# Renders the Node.js and Go API references into docs/reference/generated/ for mkdocs.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/docs/reference/generated"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
rm -rf "$out" && mkdir -p "$out"

npm ci --prefix "$root/scripts/docs" --no-audit --no-fund
"$root/scripts/docs/node_modules/.bin/typedoc" \
  --plugin typedoc-plugin-markdown \
  --tsconfig "$root/node/tsconfig.json" \
  --entryPoints "$root/node/src/index.ts" "$root/node/src/otp.ts" \
  --out "$tmp/node" --readme none --outputFileStrategy modules \
  --excludePrivate --excludeInternal --hidePageHeader --hideBreadcrumbs \
  --disableSources --skipErrorChecking
# Drop each page's own "# <module>" title; the including page has one.
tail -n +2 "$tmp/node/index.md" > "$out/node.md"
tail -n +2 "$tmp/node/otp.md" > "$out/node-otp.md"

gomarkdoc=github.com/princjef/gomarkdoc/cmd/gomarkdoc@v1.1.0
(cd "$root/go" && go run "$gomarkdoc" --output "$tmp/go.md" . && go run "$gomarkdoc" --output "$tmp/go-otp.md" ./otp)
for name in go go-otp; do sed '1,3d' "$tmp/$name.md" > "$out/$name.md"; done
