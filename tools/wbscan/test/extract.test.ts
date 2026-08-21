import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { describe, it } from 'node:test';
import { extractInventory } from '../src/extract/index.ts';
import type { Endpoint, Inventory } from '../src/extract/inventory.ts';

const dir = new URL('./fixtures/build/', import.meta.url);
const sources = readdirSync(dir).map((name) => ({ name, code: readFileSync(new URL(name, dir), 'utf8') }));
const build = { entry: 'env.js', fetchedAt: '2026-01-01T00:00:00.000Z', chunks: sources.map((s) => s.name) };
const inventory = extractInventory(sources, build);

function lineOf(chunk: string, text: string): number {
  const code = sources.find((s) => s.name === chunk)!.code;
  return code.slice(0, code.indexOf(text)).split('\n').length;
}

function find(service: string, method: string, path: string): Endpoint {
  const found = inventory.endpoints.find((e) => e.service === service && e.method === method && e.path === path);
  assert.ok(found, `missing ${service} ${method} ${path}; have:\n${inventory.endpoints.map((e) => `${e.service} ${e.method} ${e.path}`).join('\n')}`);
  return found;
}

function absent(service: string, method: string, path: string): void {
  assert.ok(!inventory.endpoints.some((e) => e.service === service && e.method === method && e.path === path), `unexpected ${method} ${path}`);
}

describe('extractInventory', () => {
  it('records URL-valued env vars of the build-time config only', () => {
    assert.deepEqual(Object.keys(inventory.env), [
      'VITE_AUTH_TOKEN_URL',
      'VITE_AUTH_URL',
      'VITE_STRAPI_URL',
      'VITE_TRADING_BASE_URL_TRADING_SPOT_DEMO',
      'VITE_TRADING_BASE_URL_TRADING_SPOT_LIVE',
    ]);
  });

  it('resolves env getters across chunks and names params after destructured keys', () => {
    const e = find('auth-server', 'GET', '/api/v1/users/{userId}');
    assert.deepEqual(e.query, ['expand']);
    assert.deepEqual(e.headers, ['x-mfa-token']);
    assert.deepEqual(e.sources, [{ chunk: 'api.js', line: lineOf('api.js', 'await http.get(`${t}/users/'), symbol: 'getUser' }]);
  });

  it('follows constants assigned in lazy-init wrappers', () => {
    assert.equal(find('auth-server', 'POST', '/api/v1/user/logout').body.kind, 'none');
  });

  it('detects body kinds and FormData fields', () => {
    assert.deepEqual(find('auth-server', 'POST', '/oauth/token').body, {
      kind: 'form-urlencoded',
      fields: ['grant_type', 'password', 'username'],
    });
    assert.deepEqual(find('kyc-service', 'POST', '/api/v1/documents').body, { kind: 'multipart', fields: ['comment', 'file'] });
    assert.deepEqual(find('exchange-core', 'POST', '/api/v1/client/statement/export').body, { kind: 'json', fields: ['format'] });
  });

  it('collects query keys from URLSearchParams, params objects and literal query strings', () => {
    assert.deepEqual(find('exchange-core', 'GET', '/api/v1/orders').query, ['status']);
    assert.deepEqual(find('exchange-core', 'GET', '/api/v1/limits').query, ['currency', 'fiatOpType', 'page', 'size']);
  });

  it('expands optional path segments and keeps `?? ""` holes as params', () => {
    assert.equal(find('exchange-core', 'GET', '/api/v1/invoice/{invoiceId}/receipt').responseType, 'blob');
    find('exchange-core', 'GET', '/api/v1/invoice/{invoiceId}/email/receipt');
  });

  it('resolves values destructured from call results', () => {
    assert.deepEqual(find('exchange-core', 'PUT', '/api/v1/assets/{id}').body, { kind: 'json', fields: ['name'] });
  });

  it('enumerates lookup tables, keeping url and method paired', () => {
    find('auth-server', 'POST', '/api/v1/card/unbind/{id}');
    find('trading-service', 'DELETE', '/accounts/{id}');
    absent('auth-server', 'DELETE', '/api/v1/card/unbind/{id}');
    absent('trading-service', 'POST', '/accounts/{id}');
  });

  it('prunes branches whose guard is false for the chosen table entry', () => {
    assert.equal(find('exchange-core', 'GET', '/api/v1/client/balance/report').responseType, 'blob');
    absent('exchange-core', 'POST', '/api/v1/client/balance/report');
    absent('exchange-core', 'GET', '/api/v1/client/statement/export');
  });

  it('detects server-sent events', () => {
    assert.equal(find('notification-service', 'GET', '/api/v1/events/stream').responseType, 'event-stream');
    assert.equal(find('auth-server', 'GET', '/api/v1/sse/connect').responseType, 'event-stream');
  });

  it('resolves {{PLACEHOLDER}} action configs and folds demo hosts into the service', () => {
    const e = find('trading-service', 'POST', '/api/v1/orders');
    assert.ok(e.sources.every((s) => s.symbol === 'createOrder'));
    assert.deepEqual(inventory.services['trading-service'], {
      production: 'https://trading-service.whitebird.io',
      demo: 'https://trading-demo-service.whitebird.io',
    });
  });

  it('excludes CMS and static URLs', () => {
    assert.ok(!inventory.endpoints.some((e) => e.service === 'content' || e.service === 'whitebird.io'));
  });

  it('extracts the OAuth client', () => {
    assert.deepEqual(inventory.oauth, {
      tokenUrl: 'https://auth-server.whitebird.io/oauth/token',
      basic: 'Y2xpZW50OnNlY3JldA==',
      clientId: 'client',
      envVars: ['VITE_AUTH_BASIC_TOKEN', 'VITE_AUTH_TOKEN_URL'],
      grantTypes: ['password'],
      notes: inventory.oauth.notes,
    });
    assert.match(inventory.oauth.notes, /hardcoded in api\.js:\d+/);
  });

  it('reports dynamic path tails and URLs not used by any detected request', () => {
    const partials = inventory.unresolved.map((u) => u.partial);
    assert.ok(partials.includes('https://auth-server.whitebird.io/api/v1{?}'), partials.join('\n'));
    assert.ok(partials.includes('https://mtsbank-service.whitebird.io/api/v1/card/bind?token'), partials.join('\n'));
  });

  it('is deterministic and sorted', () => {
    const again: Inventory = extractInventory([...sources].reverse(), build);
    assert.equal(JSON.stringify(again), JSON.stringify(inventory));
    const services = inventory.endpoints.map((e) => e.service);
    assert.deepEqual(services, [...services].sort((a, b) => a.localeCompare(b)));
  });
});
