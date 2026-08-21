export const API_DOMAIN = '.whitebird.io';

/** Hosts on the API domain that serve the website, CMS content or landing pages rather than APIs. */
const NON_API_HOSTS = new Set(['whitebird.io', 'www.whitebird.io', 'content.whitebird.io', 'info.whitebird.io']);

const STATIC_ASSET = /\.(png|jpe?g|gif|svg|webp|avif|ico|css|js|mjs|map|woff2?|ttf|otf|mp4|webm|mp3)$/i;

export interface ServiceHosts {
  production: string;
  demo: string | null;
}

/** Service id of a host: the subdomain for `*.whitebird.io`, the full host otherwise. */
export function serviceNameOf(host: string): string {
  return host.endsWith(API_DOMAIN) ? host.slice(0, -API_DOMAIN.length) : host;
}

export function hostOf(url: string): string | undefined {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.hostname.toLowerCase() : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Decides which hosts are API services and folds demo twins (declared as `*_DEMO` / `*_LIVE`
 * env pairs, or named `<x>-demo-<y>`) into their production service.
 */
export class Hosts {
  private readonly extraApiHosts = new Set<string>();
  private readonly demoToLive = new Map<string, string>();

  constructor(env: Map<string, string>) {
    for (const [key, value] of env) {
      const host = hostOf(value);
      if (host && /_URL(_|$)/.test(key) && !NON_API_HOSTS.has(host)) this.extraApiHosts.add(host);
      const pair = /^(.*)_DEMO$/.exec(key);
      const live = pair && (env.get(`${pair[1]}_LIVE`) ?? env.get(`${pair[1]}_PROD`));
      const liveHost = live ? hostOf(live) : undefined;
      if (host && liveHost && host !== liveHost) this.demoToLive.set(host, liveHost);
    }
  }

  isApi(host: string, path = ''): boolean {
    if (NON_API_HOSTS.has(host) || STATIC_ASSET.test(path)) return false;
    return host.endsWith(API_DOMAIN) || this.extraApiHosts.has(host);
  }

  /** Production host for a (possibly demo) host. */
  liveHost(host: string): string {
    const mapped = this.demoToLive.get(host);
    if (mapped) return mapped;
    if (host.endsWith(API_DOMAIN)) {
      const label = host.slice(0, -API_DOMAIN.length);
      const stripped = label.replace(/-demo(?=-|$)/, '');
      if (stripped !== label) return `${stripped}${API_DOMAIN}`;
    }
    return host;
  }

  demoHost(liveHost: string): string | null {
    const demos = [...this.demoToLive].filter(([, live]) => live === liveHost).map(([demo]) => demo);
    return demos.sort()[0] ?? null;
  }

  serviceName(host: string): string {
    return serviceNameOf(this.liveHost(host));
  }

  describe(liveHost: string): ServiceHosts {
    const demo = this.demoHost(liveHost);
    return { production: `https://${liveHost}`, demo: demo ? `https://${demo}` : null };
  }
}
