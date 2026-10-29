import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';
import { diffInventory, loadPublished, loadSpec, renderMarkdown } from '../src/diff.ts';
import type { Endpoint, Inventory } from '../src/extract/inventory.ts';

const fixtures = (path: string) => fileURLToPath(new URL(`./fixtures/${path}`, import.meta.url));

function endpoint(service: string, method: string, path: string, extra: Partial<Endpoint> = {}): Endpoint {
  return {
    service,
    method,
    path,
    query: [],
    body: { kind: 'none', fields: [] },
    headers: [],
    responseType: 'json',
    sources: [{ chunk: 'app.js', line: 1, symbol: null }],
    ...extra,
  };
}

const inventory: Inventory = {
  build: { entry: 'index-abc.js', fetchedAt: '2026-01-01T00:00:00.000Z', chunks: ['index-abc.js'] },
  env: { VITE_KYC_URL: 'https://kyc-service.whitebird.io/api/v1' },
  oauth: {
    tokenUrl: 'https://authorization-server.whitebird.io/oauth/token',
    basic: 'Y2xpZW50OnNlY3JldA==',
    clientId: 'client',
    envVars: [],
    grantTypes: ['password'],
    notes: '',
  },
  services: {
    'admin-service': { production: 'https://admin-service.whitebird.io', demo: null },
    'authorization-server': { production: 'https://authorization-server.whitebird.io', demo: null },
    'exchange-core': { production: 'https://exchange-core.whitebird.io', demo: null },
    'trading-service': { production: 'https://trading-service.whitebird.io', demo: 'https://trading-demo-service.whitebird.io' },
  },
  endpoints: [
    endpoint('authorization-server', 'POST', '/oauth/token', {
      body: { kind: 'form-urlencoded', fields: ['grant_type', 'password', 'username'] },
    }),
    endpoint('admin-service', 'POST', '/api/v1/audit/record'),
    endpoint('exchange-core', 'POST', '/api/v1/admin/approve/{requestId}'),
    endpoint('exchange-core', 'GET', '/api/v1/orders', { query: ['assets', 'page', 'size', 'status'] }),
    endpoint('exchange-core', 'POST', '/api/v1/quote', { body: { kind: 'json', fields: ['amount', 'currency'] } }),
    endpoint('exchange-core', 'POST', '/api/v1/subaccount/add/{id}'),
    endpoint('trading-service', 'POST', '/api/v1/orders'),
  ],
  unresolved: [],
  paths: ['/registration/initiate'],
};

describe('diffInventory', async () => {
  const spec = await loadSpec(fixtures('spec'));
  const published = await loadPublished(fixtures('openapi'));
  const report = diffInventory(inventory, spec, published);

  it('loads spec/environments.yaml and spec/services', () => {
    assert.deepEqual(
      spec.services.map((s) => s.service),
      ['auth', 'exchange', 'old-service'],
    );
    assert.deepEqual(spec.environments?.oauth, {
      basic: 'b2xkQ2xpZW50OnNlY3JldA==',
      tokenUrl: 'https://authorization-server.whitebird.io/oauth/token',
    });
  });

  it('reports new and removed hosts; env URLs keep a host', () => {
    assert.deepEqual(report.newHosts, ['trading-demo-service.whitebird.io', 'trading-service.whitebird.io']);
    assert.deepEqual(report.removedHosts, [
      'legacy-demo-service.whitebird.io',
      'legacy-service.whitebird.io',
      'old-service.whitebird.io',
    ]);
  });

  it('reports new endpoints; params match literal segments; excluded.yaml is honoured', () => {
    assert.deepEqual(report.newEndpoints, [{ service: 'trading-service', method: 'POST', path: '/api/v1/orders' }]);
  });

  it('ties spec files to services by host; keeps openapi-sourced operations still published', () => {
    assert.deepEqual(
      report.goneEndpoints.map((e) => `${e.service} ${e.method} ${e.path}`),
      ['exchange-core DELETE /api/v1/orders/{orderId}', 'exchange-core GET /api/v1/admin/legacy'],
    );
  });

  it('keeps operations whose path is in the code or that were verified live out of gone', () => {
    assert.deepEqual(
      report.unconfirmedEndpoints.map((e) => `${e.service} ${e.reason} ${e.path}`),
      ['authorization-server mentioned /api/v1/registration/initiate', 'exchange-core live /api/v1/rates'],
    );
  });

  it('reports request fields the spec lacks, not spec-only ones; `name[]` matches `name`', () => {
    assert.deepEqual(report.changedEndpoints, [
      { service: 'exchange-core', method: 'GET', path: '/api/v1/orders', query: ['status'] },
      { service: 'exchange-core', method: 'POST', path: '/api/v1/quote', body: ['currency'] },
    ]);
  });

  it('reports a changed OAuth client', () => {
    assert.deepEqual(report.oauth, [{ field: 'basic', spec: 'b2xkQ2xpZW50OnNlY3JldA==', inventory: 'Y2xpZW50OnNlY3JldA==' }]);
  });

  it('renders Markdown', () => {
    assert.equal(report.drift, true);
    const md = renderMarkdown(report);
    assert.match(md, /## New endpoints \(1\)\n\n### trading-service \(1\)\n\n- `POST \/api\/v1\/orders`/);
    assert.match(md, /query \+status/);
  });

  it('handles an empty spec dir', async () => {
    const empty = diffInventory(inventory, await loadSpec(fixtures('missing')));
    assert.equal(empty.newEndpoints.length, inventory.endpoints.length);
    assert.equal(empty.goneEndpoints.length, 0);
    assert.equal(empty.drift, true);
  });
});
