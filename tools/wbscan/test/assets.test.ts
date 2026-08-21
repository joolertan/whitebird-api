import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { buildId, chunkRefs, scriptsFromHtml } from '../src/assets.ts';

describe('scriptsFromHtml', () => {
  it('finds the module entry and modulepreload chunks', () => {
    const html = `<script>var x = 1</script>
      <script type="module" crossorigin src="/assets/index-By9aTaK4.js"></script>
      <link rel="modulepreload" crossorigin href="/assets/vendor-react-BOIG4WAV.js">
      <link rel="stylesheet" href="/assets/index-CtVmZ1Zc.css">`;
    assert.deepEqual(scriptsFromHtml(html, 'https://whitebird.io/'), {
      entry: 'https://whitebird.io/assets/index-By9aTaK4.js',
      preloads: ['https://whitebird.io/assets/vendor-react-BOIG4WAV.js'],
    });
  });
});

describe('chunkRefs', () => {
  it('resolves imports relative to the chunk and preload deps relative to the site root', () => {
    const js = `import{a as b}from"./vendor-a-1.js";const m=["assets/app-pages-2.js","assets/app-pages-3.css"];
      import(\`./app-components-4.js\`);const other="https://cdn.example.com/x.js",nested="./sub/dir.js";`;
    assert.deepEqual(chunkRefs(js, 'https://whitebird.io/assets/index-0.js'), [
      'https://whitebird.io/assets/app-components-4.js',
      'https://whitebird.io/assets/app-pages-2.js',
      'https://whitebird.io/assets/vendor-a-1.js',
    ]);
  });
});

describe('buildId', () => {
  it('uses the entry chunk hash', () => {
    assert.equal(buildId('https://whitebird.io/assets/index-By9aTaK4.js'), 'By9aTaK4');
    assert.equal(buildId('https://whitebird.io/assets/main.js'), 'main');
  });
});
