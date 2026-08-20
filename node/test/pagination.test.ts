import { describe, expect, it } from 'vitest';
import type { Core } from '../src/core.js';
import { type Page, Paginator, type Whitebird } from '../src/index.js';
import { client, MockApi } from './helpers.js';

const LIST = 'POST /api/v1/brokerage/otc-order/page';
const TOTAL = 5;

/** Serves `TOTAL` orders in pages of the requested size, like Spring Data. */
function orders(request: Request): Response {
  const url = new URL(request.url);
  const number = Number(url.searchParams.get('page') ?? 0);
  const size = Number(url.searchParams.get('size') ?? 20);
  const ids = Array.from({ length: TOTAL }, (_, i) => `otc-${i}`);
  const content = ids.slice(number * size, (number + 1) * size).map((uuid) => ({ uuid }));
  const totalPages = Math.ceil(TOTAL / size);
  return Response.json({
    content,
    totalElements: TOTAL,
    totalPages,
    number,
    size,
    numberOfElements: content.length,
    first: number === 0,
    last: number >= totalPages - 1,
    empty: content.length === 0,
  });
}

const pageNumbers = (api: MockApi) =>
  api.calls(LIST).map((request) => new URL(request.url).searchParams.get('page'));

describe('paginated methods', () => {
  it('resolve to the first page with its metadata', async () => {
    const api = new MockApi({ [LIST]: orders });

    const page = await client(api).brokerage.otcOrders.list({ size: 2, status: 'NEW' });

    expect(page).toMatchObject({ totalElements: 5, totalPages: 3, number: 0 });
    expect(page.content.map((order) => order.uuid)).toEqual(['otc-0', 'otc-1']);
    expect(await api.calls(LIST)[0]?.json()).toEqual({ status: 'NEW' });
  });

  it('iterate over every item, fetching pages lazily', async () => {
    const api = new MockApi({ [LIST]: orders });
    const uuids: string[] = [];

    for await (const order of client(api).brokerage.otcOrders.list({ size: 2 })) {
      uuids.push(order.uuid ?? '');
    }

    expect(uuids).toEqual(['otc-0', 'otc-1', 'otc-2', 'otc-3', 'otc-4']);
    expect(pageNumbers(api)).toEqual(['0', '1', '2']);
  });

  it('stop fetching when the loop breaks', async () => {
    const api = new MockApi({ [LIST]: orders });

    for await (const order of client(api).brokerage.otcOrders.list({ size: 2 })) {
      if (order.uuid === 'otc-2') break;
    }

    expect(pageNumbers(api)).toEqual(['0', '1']);
  });

  it('do not fetch anything until awaited or iterated', () => {
    const api = new MockApi({ [LIST]: orders });
    client(api).brokerage.otcOrders.list();
    expect(api.requests).toHaveLength(0);
  });

  it('iterate over pages and reuse an awaited first page', async () => {
    const api = new MockApi({ [LIST]: orders });
    const paginator = client(api).brokerage.otcOrders.list({ size: 2, page: 1 });

    await paginator;
    const sizes: number[] = [];
    for await (const page of paginator.pages()) sizes.push(page.content.length);

    expect(sizes).toEqual([2, 1]);
    expect(pageNumbers(api)).toEqual(['1', '2']);
  });

  it('put page numbers in the body when the endpoint expects them there', async () => {
    const api = new MockApi({
      'POST /search': async (request) => {
        const { page } = await request.json();
        return Response.json({ content: [page], totalElements: 2, size: 1 });
      },
    });
    const core = (client(api) as unknown as { _client: Core })._client;
    const paginator = core.paginate<number>({
      service: 'brokerage',
      method: 'POST',
      path: '/search',
      body: { size: 1, query: 'btc' },
      pageIn: 'body',
    });

    const items: number[] = [];
    for await (const item of paginator) items.push(item);

    expect(items).toEqual([0, 1]);
    expect(await api.calls('POST /search')[1]?.json()).toEqual({ size: 1, query: 'btc', page: 1 });
  });
});

describe('Paginator', () => {
  const collect = async (pages: Partial<Page<number>>[]) => {
    const requested: number[] = [];
    const paginator = new Paginator<number>(async (index) => {
      requested.push(index);
      return pages[index] as Page<number>;
    });
    const items: number[] = [];
    for await (const item of paginator) items.push(item);
    return { items, requested };
  };

  it('stops on `last`', async () => {
    const result = await collect([
      { content: [1], last: false },
      { content: [2], last: true },
      { content: [3] },
    ]);
    expect(result).toEqual({ items: [1, 2], requested: [0, 1] });
  });

  it('falls back to totalPages, then totalElements, then an empty page', async () => {
    expect((await collect([{ content: [1], totalPages: 1 }])).requested).toEqual([0]);
    expect(
      (
        await collect([
          { content: [1, 2], totalElements: 3 },
          { content: [3], totalElements: 3 },
        ])
      ).items,
    ).toEqual([1, 2, 3]);
    expect(
      (await collect([{ content: [1], size: 1, totalElements: 9 }, { content: [] }])).items,
    ).toEqual([1]);
  });

  it('is typed as a promise of the first page', async () => {
    const whitebird: Pick<Whitebird, 'brokerage'> = client(new MockApi({ [LIST]: orders }));
    const page: Page<{ uuid?: string }> = await whitebird.brokerage.otcOrders.list();
    expect(page.content).toHaveLength(5);
  });
});
