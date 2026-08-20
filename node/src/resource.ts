import type { Core } from './core.js';

/** Base class of the API resources, such as `client.brokerage.otcOrders`. */
export abstract class APIResource {
  /** @internal */
  protected readonly _client: Core;

  /** @internal */
  constructor(client: Core) {
    this._client = client;
  }
}
