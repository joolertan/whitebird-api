mod common;

use common::*;
use futures::{StreamExt, TryStreamExt};
use serde_json::{Value, json};
use whitebird::models::{OtcOrderStatus, SubaccountResponse};
use whitebird::params::{BrokerageOtcOrdersListParams, BrokerageSubaccountsListParams};
use whitebird::{Client, ErrorKind, Method, PageParam, Service};
use wiremock::matchers::{body_json, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

const LIST_PATH: &str = "/api/v1/brokerage/subaccount/page";

/// Serves `total` subaccounts in pages of `size`, the way Spring Data does.
async fn subaccounts(server: &MockServer, total: u32, size: u32) {
    let pages = total.div_ceil(size);
    for number in 0..pages {
        let content: Vec<Value> = (number * size..total.min((number + 1) * size))
            .map(|i| subaccount(&format!("sub-{i}")))
            .collect();
        Mock::given(method("GET"))
            .and(path(LIST_PATH))
            .and(query_param("page", number.to_string()))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "content": content,
                "number": number,
                "size": size,
                "totalElements": total,
                "totalPages": pages,
                "numberOfElements": content.len(),
                "first": number == 0,
                "last": number + 1 == pages,
                "empty": content.is_empty(),
            })))
            .mount(server)
            .await;
    }
}

fn names(items: &[SubaccountResponse]) -> Vec<&str> {
    items
        .iter()
        .map(|item| item.display_name.as_deref().unwrap())
        .collect()
}

async fn pages_requested(server: &MockServer) -> Vec<String> {
    server
        .received_requests()
        .await
        .unwrap()
        .iter()
        .filter_map(|request| {
            request
                .url
                .query_pairs()
                .find(|(key, _)| key == "page")
                .map(|(_, value)| value.into_owned())
        })
        .collect()
}

fn client(server: &MockServer) -> Client {
    signed_in(server, &jwt("me", 3600))
}

#[tokio::test]
async fn walks_every_page_and_stops_after_the_last() {
    let server = MockServer::start().await;
    subaccounts(&server, 5, 2).await;

    let items: Vec<_> = client(&server)
        .brokerage()
        .subaccounts()
        .list(BrokerageSubaccountsListParams::builder().size(2).build())
        .try_collect()
        .await
        .unwrap();

    assert_eq!(names(&items), ["sub-0", "sub-1", "sub-2", "sub-3", "sub-4"]);
    assert_eq!(pages_requested(&server).await, ["0", "1", "2"]);
    let requests = server.received_requests().await.unwrap();
    assert_eq!(requests[0].url.query(), Some("size=2&page=0"));
}

#[tokio::test]
async fn fetches_lazily_and_stops_when_dropped() {
    let server = MockServer::start().await;
    subaccounts(&server, 10, 2).await;

    let items: Vec<_> = client(&server)
        .brokerage()
        .subaccounts()
        .list(BrokerageSubaccountsListParams::builder().size(2).build())
        .take(3)
        .try_collect()
        .await
        .unwrap();

    assert_eq!(names(&items), ["sub-0", "sub-1", "sub-2"]);
    assert_eq!(pages_requested(&server).await, ["0", "1"]);
}

#[tokio::test]
async fn stops_on_an_empty_page_without_metadata() {
    let server = MockServer::start().await;
    Mock::given(query_param("page", "0"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({ "content": [subaccount("only")] })),
        )
        .mount(&server)
        .await;
    Mock::given(query_param("page", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "content": [] })))
        .mount(&server)
        .await;

    let items: Vec<_> = client(&server)
        .brokerage()
        .subaccounts()
        .list(BrokerageSubaccountsListParams::default())
        .try_collect()
        .await
        .unwrap();

    assert_eq!(names(&items), ["only"]);
}

#[tokio::test]
async fn starts_from_the_requested_page_and_exposes_page_metadata() {
    let server = MockServer::start().await;
    subaccounts(&server, 5, 2).await;
    let listing = client(&server).brokerage().subaccounts().list(
        BrokerageSubaccountsListParams::builder()
            .page(1)
            .size(2)
            .build(),
    );

    let first = listing.first_page().await.unwrap();
    let last = listing.page(2).await.unwrap();
    let pages: Vec<_> = listing.pages().try_collect().await.unwrap();

    assert_eq!(names(&first.content), ["sub-2", "sub-3"]);
    assert_eq!(first.total_elements, Some(5));
    assert_eq!(first.total_pages, Some(3));
    assert!(!first.is_last());
    assert_eq!(names(&last.content), ["sub-4"]);
    assert!(last.is_last());
    assert_eq!(pages.len(), 2);
    assert_eq!(pages[1].number, Some(2));
    assert_eq!(pages_requested(&server).await, ["1", "2", "1", "2"]);
}

#[tokio::test]
async fn sends_filters_in_the_body_and_the_page_in_the_query() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/v1/brokerage/otc-order/page"))
        .and(query_param("size", "50"))
        .and(query_param("sort", "creationDate,desc"))
        .and(body_json(json!({ "status": "NEW", "fromAsset": "USDT" })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "content": [{ "uuid": "order-1", "status": "NEW" }],
            "number": 0,
            "totalPages": 1,
        })))
        .expect(1)
        .mount(&server)
        .await;

    let orders: Vec<_> = client(&server)
        .brokerage()
        .otc_orders()
        .list(
            BrokerageOtcOrdersListParams::builder()
                .size(50)
                .sort(vec!["creationDate,desc".into()])
                .status(OtcOrderStatus::New)
                .from_asset("USDT")
                .build(),
        )
        .try_collect()
        .await
        .unwrap();

    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].status, Some(OtcOrderStatus::New));
}

#[tokio::test]
async fn paginates_by_a_body_field_through_the_escape_hatch() {
    let server = MockServer::start().await;
    for (number, items) in [(0, json!([1, 2])), (1, json!([3]))] {
        Mock::given(method("POST"))
            .and(path("/api/v2/search"))
            .and(body_json(json!({ "page": number, "size": 2, "q": "btc" })))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "content": items,
                "number": number,
                "totalPages": 2,
            })))
            .expect(1)
            .mount(&server)
            .await;
    }

    let items: Vec<_> = client(&server)
        .request(Service::Brokerage, Method::POST, "/api/v2/search")
        .json(json!({ "page": 0, "size": 2, "q": "btc" }))
        .paginate::<u32>(PageParam::Body("page"))
        .try_collect()
        .await
        .unwrap();

    assert_eq!(items, [1, 2, 3]);
}

#[tokio::test]
async fn yields_the_error_and_ends() {
    let server = MockServer::start().await;
    Mock::given(query_param("page", "0"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "content": [subaccount("a")],
            "last": false,
        })))
        .mount(&server)
        .await;
    Mock::given(query_param("page", "1"))
        .respond_with(ResponseTemplate::new(404).set_body_json(json!({ "message": "Gone" })))
        .mount(&server)
        .await;

    let results: Vec<_> = client(&server)
        .brokerage()
        .subaccounts()
        .list(BrokerageSubaccountsListParams::default())
        .collect()
        .await;

    assert_eq!(results.len(), 2);
    assert!(results[0].is_ok());
    let error = results[1].as_ref().unwrap_err();
    assert_eq!(error.kind(), Some(ErrorKind::NotFound));
}
