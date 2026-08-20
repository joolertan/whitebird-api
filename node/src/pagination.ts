/** One page of a paginated endpoint (a Spring Data page). */
export interface Page<T> {
  /** Items of this page. */
  content: T[];
  /** Number of items across all pages. */
  totalElements: number;
  /** Number of pages. */
  totalPages: number;
  /** Zero-based index of this page. */
  number: number;
  /** Requested page size. */
  size: number;
  /** Number of items in this page. */
  numberOfElements: number;
  /** Whether this is the first page. */
  first: boolean;
  /** Whether this is the last page. */
  last: boolean;
  /** Whether this page has no items. */
  empty: boolean;
}

/**
 * Result of a paginated method. Await it for the first page with its metadata, or iterate it
 * with `for await` to get every item; following pages are fetched lazily, only as the loop
 * reaches them, so `break` stops fetching.
 *
 * @example
 * ```ts
 * const page = await client.brokerage.otcOrders.list({ size: 50 });
 * console.log(page.totalElements);
 *
 * for await (const order of client.brokerage.otcOrders.list({ size: 50 })) {
 *   console.log(order.uuid);
 * }
 *
 * for await (const page of client.brokerage.otcOrders.list().pages()) {
 *   console.log(page.number, page.content.length);
 * }
 * ```
 */
export class Paginator<T> implements PromiseLike<Page<T>>, AsyncIterable<T> {
  readonly #fetchPage: (page: number) => Promise<Page<T>>;
  readonly #start: number;
  #first: Promise<Page<T>> | undefined;

  /**
   * @param fetchPage - Fetches the page with the given zero-based index.
   * @param start - Index of the first page.
   */
  constructor(fetchPage: (page: number) => Promise<Page<T>>, start = 0) {
    this.#fetchPage = fetchPage;
    this.#start = start;
  }

  // biome-ignore lint/suspicious/noThenProperty: awaiting a paginator yields its first page by design.
  then<Fulfilled = Page<T>, Rejected = never>(
    onFulfilled?: ((page: Page<T>) => Fulfilled | PromiseLike<Fulfilled>) | null,
    onRejected?: ((reason: unknown) => Rejected | PromiseLike<Rejected>) | null,
  ): Promise<Fulfilled | Rejected> {
    return this.#firstPage().then(onFulfilled, onRejected);
  }

  /** Iterates over whole pages instead of items. */
  async *pages(): AsyncGenerator<Page<T>, void, undefined> {
    let index = this.#start;
    let page = await this.#firstPage();
    const size = page.size ?? page.content.length;
    for (;;) {
      yield page;
      if (isLastPage(page, index, size)) return;
      index += 1;
      page = await this.#fetchPage(index);
    }
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<T, void, undefined> {
    for await (const page of this.pages()) yield* page.content;
  }

  #firstPage(): Promise<Page<T>> {
    this.#first ??= this.#fetchPage(this.#start);
    return this.#first;
  }
}

/** Decides from whatever metadata the server sent, since some endpoints omit part of it. */
function isLastPage(page: Partial<Page<unknown>>, index: number, size: number): boolean {
  if (!page.content?.length) return true;
  if (typeof page.last === 'boolean') return page.last;
  if (typeof page.totalPages === 'number') return index + 1 >= page.totalPages;
  if (typeof page.totalElements === 'number') return (index + 1) * size >= page.totalElements;
  return true;
}
