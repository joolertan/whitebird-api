export interface HtmlScripts {
  entry: string;
  preloads: string[];
}

function attr(tag: string, name: string): string | undefined {
  return new RegExp(`\\s${name}\\s*=\\s*["']([^"']*)["']`, 'i').exec(tag)?.[1];
}

/** Entry module and modulepreload chunks referenced by a Vite `index.html`, as absolute URLs. */
export function scriptsFromHtml(html: string, pageUrl: string): HtmlScripts {
  let entry: string | undefined;
  for (const [tag] of html.matchAll(/<script\b[^>]*>/gi)) {
    const src = attr(tag, 'src');
    if (src && attr(tag, 'type') === 'module') entry ??= new URL(src, pageUrl).href;
  }
  if (!entry) throw new Error('index.html: no <script type="module" src> entry found');
  const preloads: string[] = [];
  for (const [tag] of html.matchAll(/<link\b[^>]*>/gi)) {
    const href = attr(tag, 'href');
    if (href && attr(tag, 'rel') === 'modulepreload') preloads.push(new URL(href, pageUrl).href);
  }
  return { entry, preloads };
}

/**
 * JS chunks referenced from a chunk: static and dynamic imports (`./x-hash.js`, relative to the
 * chunk) and Vite's preload dependency lists (`assets/x-hash.js`, relative to the site root).
 * Only files in the chunk's own directory are returned.
 */
export function chunkRefs(js: string, chunkUrl: string): string[] {
  const dir = new URL('./', chunkUrl);
  const refs = new Set<string>();
  for (const [, ref] of js.matchAll(/["'`]((?:\.{1,2}\/|\/)?(?:[\w-]+\/)*[\w.-]+\.js)["'`]/g)) {
    const base = ref!.startsWith('.') ? chunkUrl : dir.origin + '/';
    const url = new URL(ref!, base);
    if (url.origin === dir.origin && url.pathname.startsWith(dir.pathname) && !url.pathname.slice(dir.pathname.length).includes('/')) {
      refs.add(url.href);
    }
  }
  return [...refs].sort();
}

export function fileName(url: string): string {
  return new URL(url).pathname.split('/').pop()!;
}

/** Content hash of the entry chunk (`index-By9aTaK4.js` -> `By9aTaK4`), used as the build id. */
export function buildId(entryUrl: string): string {
  const name = fileName(entryUrl);
  return /^index-([\w-]+)\.js$/.exec(name)?.[1] ?? name.replace(/\.js$/, '');
}
