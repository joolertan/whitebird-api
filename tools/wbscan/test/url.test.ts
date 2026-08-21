import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { parseUrl, pathKey } from '../src/extract/url.ts';
import type { Part } from '../src/extract/values.ts';

const parse = (parts: Part[], placeholders = new Map<string, string[]>()) => parseUrl({ kind: 'str', parts }, placeholders);

describe('parseUrl', () => {
  it('splits host, path and query and normalizes slashes', () => {
    assert.deepEqual(parse(['https://API.whitebird.io:443//api/v1/orders/?page=0&size=20#top']), [
      { ok: true, url: { host: 'api.whitebird.io', path: '/api/v1/orders', query: ['page', 'size'] } },
    ]);
  });

  it('names params from their source, falling back to id / positional names', () => {
    const [named] = parse(['https://x.whitebird.io/users/', { name: 'userId' }, '/files/', {}]);
    assert.equal(named.ok && named.url.path, '/users/{userId}/files/{id}');
    const [positional] = parse(['https://x.whitebird.io/a/', {}, '/b/', {}]);
    assert.equal(positional.ok && positional.url.path, '/a/{id1}/b/{id2}');
  });

  it('moves serialized query holes out of the path', () => {
    const [r] = parse(['https://x.whitebird.io/list', { query: ['page', 'sort'] }]);
    assert.deepEqual(r.ok && r.url, { host: 'x.whitebird.io', path: '/list', query: ['page', 'sort'] });
  });

  it('expands {{PLACEHOLDER}} templates', () => {
    const results = parse(['{{BASE}}/api/v1/orders'], new Map([['BASE', ['https://a.whitebird.io', 'https://b.whitebird.io']]]));
    assert.deepEqual(results.map((r) => r.ok && r.url.host), ['a.whitebird.io', 'b.whitebird.io']);
  });

  it('rejects unknown hosts and dynamic path tails', () => {
    assert.deepEqual(parse([{ name: 'base' }, '/api/v1/x']), [{ ok: false, partial: '{base}/api/v1/x' }]);
    assert.deepEqual(parse(['https://x.whitebird.io/api/v1', {}]), [{ ok: false, partial: 'https://x.whitebird.io/api/v1{?}' }]);
  });
});

describe('pathKey', () => {
  it('reduces every parameter to {}', () => {
    assert.equal(pathKey('/a/{id}/b/{userId}'), '/a/{}/b/{}');
  });
});
