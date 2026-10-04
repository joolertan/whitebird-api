# Pagination

Listings such as order history, balance operations or notifications are paginated on the server
(Spring Data pages: `content`, `totalElements`, `totalPages`, ...). Their methods return an object
that is iterable over **items** in the language's own way. Pages are fetched lazily: the next one
is requested only when the loop reaches it, so breaking out of the loop stops fetching.

`page` (zero-based) chooses where to start and `size` how many items each request returns.

## Iterating over items

=== "Python"

    ```python
    for operation in client.balance.operations.list(size=100):
        print(operation.id, operation.status)

    async for operation in async_client.balance.operations.list(size=100):
        ...
    ```

=== "Node.js"

    ```ts
    for await (const operation of client.balance.operations.list({ size: 100 })) {
      console.log(operation.id, operation.status);
    }
    ```

=== "Go"

    Paginated methods return an `iter.Seq2[T, error]`. An error ends the iteration.

    ```go
    for operation, err := range client.Balance.Operations.List(ctx, whitebird.BalanceOperationsListParams{Size: 100}) {
        if err != nil {
            return err
        }
        fmt.Println(operation.ID, operation.Status)
    }
    ```

=== "Rust"

    Paginated methods return a `Paginator<T>`, which implements `Stream<Item = Result<T>>`.

    ```rust
    use futures::TryStreamExt;

    let mut operations = client.balance().operations().list(BalanceOperationsListParams::builder().size(100).build());
    while let Some(operation) = operations.try_next().await? {
        println!("{:?} {:?}", operation.id, operation.status);
    }
    ```

## Taking a few items

=== "Python"

    ```python
    from itertools import islice

    latest = list(islice(client.exchange.orders.history(), 10))
    ```

=== "Node.js"

    ```ts
    const latest = [];
    for await (const order of client.exchange.orders.history()) {
      latest.push(order);
      if (latest.length === 10) break;
    }
    ```

=== "Go"

    ```go
    var latest []whitebird.ExchangeOrder
    for order, err := range client.Exchange.Orders.History(ctx, whitebird.ExchangeOrdersHistoryParams{}) {
        if err != nil {
            return err
        }
        if latest = append(latest, order); len(latest) == 10 {
            break
        }
    }
    ```

=== "Rust"

    ```rust
    use futures::{StreamExt, TryStreamExt};

    let latest: Vec<_> = client.exchange().orders().history(Default::default()).take(10).try_collect().await?;
    ```

## Pages and totals

When you need the page metadata, ask for pages instead of items, or fetch a single page:

=== "Python"

    ```python
    orders = client.exchange.orders.history(size=50)
    first = orders.page(0)
    print(first.total_elements, first.total_pages)

    for page in orders.pages():
        print(page.number, len(page.content))
    ```

=== "Node.js"

    Awaiting the result gives the first page.

    ```ts
    const first = await client.exchange.orders.history({ size: 50 });
    console.log(first.totalElements, first.totalPages);

    for await (const page of client.exchange.orders.history({ size: 50 }).pages()) {
      console.log(page.number, page.content.length);
    }
    ```

=== "Go"

    Every paginated method has a `...Page` twin returning one `*Page[T]`.

    ```go
    page, err := client.Exchange.Orders.HistoryPage(ctx, whitebird.ExchangeOrdersHistoryParams{Size: 50})
    fmt.Println(page.TotalElements, page.TotalPages)
    ```

=== "Rust"

    ```rust
    let orders = client.exchange().orders().history(params);
    let first = orders.first_page().await?;
    println!("{:?} {:?}", first.total_elements, first.total_pages);

    let mut pages = orders.pages();
    while let Some(page) = pages.try_next().await? {
        println!("{:?} {}", page.number, page.content.len());
    }
    ```
