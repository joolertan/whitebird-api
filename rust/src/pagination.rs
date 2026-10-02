use std::pin::Pin;
use std::task::{Context, Poll, ready};

use futures_core::Stream;
use futures_util::StreamExt;
use futures_util::stream::{self, BoxStream};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

use crate::error::Result;
use crate::request::RequestBuilder;

/// One page of a paginated listing, as returned by the API (a Spring Data `Page`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Page<T> {
    /// Items on this page.
    #[serde(default = "Vec::new")]
    pub content: Vec<T>,
    /// Number of items on all pages.
    pub total_elements: Option<u64>,
    /// Number of pages.
    pub total_pages: Option<u32>,
    /// Index of this page, starting at 0.
    pub number: Option<u32>,
    /// Requested page size.
    pub size: Option<u32>,
    /// Number of items on this page.
    pub number_of_elements: Option<u32>,
    /// Whether this is the first page.
    pub first: Option<bool>,
    /// Whether this is the last page.
    pub last: Option<bool>,
    /// Whether this page has no items.
    pub empty: Option<bool>,
}

impl<T> Page<T> {
    /// Returns `true` if no page follows this one.
    pub fn is_last(&self) -> bool {
        if self.content.is_empty() {
            return true;
        }
        match (self.last, self.number, self.total_pages) {
            (Some(last), _, _) => last,
            (None, Some(number), Some(total)) => number + 1 >= total,
            _ => false,
        }
    }
}

/// Where a paginated endpoint takes the page number.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PageParam {
    /// A query parameter with this name.
    Query(&'static str),
    /// A field of the JSON or form body with this name.
    Body(&'static str),
}

/// Lazy stream over the items of a paginated endpoint.
///
/// Pages are requested one at a time as the stream is polled and the stream ends after the last
/// page, so dropping it early saves the remaining requests. Use [`pages`](Self::pages) to see
/// page metadata while iterating or [`page`](Self::page) to fetch a single page.
///
/// # Examples
///
/// ```no_run
/// use futures::TryStreamExt;
/// use whitebird::params::BrokerageSubaccountsListParams;
///
/// # async fn run(client: whitebird::Client) -> whitebird::Result<()> {
/// let mut subaccounts = client.brokerage().subaccounts().list(BrokerageSubaccountsListParams::default());
/// while let Some(subaccount) = subaccounts.try_next().await? {
///     println!("{:?}", subaccount.display_name);
/// }
///
/// let listing = client.brokerage().subaccounts().list(BrokerageSubaccountsListParams::default());
/// let first = listing.first_page().await?;
/// println!("{:?} subaccounts", first.total_elements);
/// # Ok(())
/// # }
/// ```
#[must_use = "streams do nothing unless polled"]
pub struct Paginator<T> {
    request: RequestBuilder,
    param: PageParam,
    start: u32,
    pages: Option<Pages<T>>,
    buffer: std::vec::IntoIter<T>,
}

impl<T: DeserializeOwned + Send + 'static> Paginator<T> {
    pub(crate) fn new(request: RequestBuilder, param: PageParam, start: u32) -> Self {
        Self {
            request,
            param,
            start,
            pages: None,
            buffer: Vec::new().into_iter(),
        }
    }

    /// Fetches page `number` (0-based).
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or the page cannot be decoded.
    pub async fn page(&self, number: u32) -> Result<Page<T>> {
        fetch(self.request.clone(), self.param, number).await
    }

    /// Fetches the page the iteration starts from: the `page` parameter, or the first page.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or the page cannot be decoded.
    pub async fn first_page(&self) -> Result<Page<T>> {
        self.page(self.start).await
    }

    /// Turns the paginator into a stream of whole pages.
    pub fn pages(self) -> Pages<T> {
        Pages::new(self.request, self.param, self.start)
    }
}

impl<T: DeserializeOwned + Send + 'static> Stream for Paginator<T> {
    type Item = Result<T>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = &mut *self;
        loop {
            if let Some(item) = this.buffer.next() {
                return Poll::Ready(Some(Ok(item)));
            }
            let pages = this
                .pages
                .get_or_insert_with(|| Pages::new(this.request.clone(), this.param, this.start));
            match ready!(pages.poll_next_unpin(cx)) {
                Some(Ok(page)) => this.buffer = page.content.into_iter(),
                Some(Err(error)) => return Poll::Ready(Some(Err(error))),
                None => return Poll::Ready(None),
            }
        }
    }
}

impl<T> Unpin for Paginator<T> {}

/// Lazy stream over the pages of a paginated endpoint, created by [`Paginator::pages`].
#[must_use = "streams do nothing unless polled"]
pub struct Pages<T>(BoxStream<'static, Result<Page<T>>>);

impl<T: DeserializeOwned + Send + 'static> Pages<T> {
    fn new(request: RequestBuilder, param: PageParam, start: u32) -> Self {
        let pages = stream::unfold(Some(start), move |next| {
            let request = request.clone();
            async move {
                let number = next?;
                Some(match fetch(request, param, number).await {
                    Ok(page) => {
                        let next = (!page.is_last()).then_some(number + 1);
                        (Ok(page), next)
                    }
                    Err(error) => (Err(error), None),
                })
            }
        });
        Self(pages.fuse().boxed())
    }
}

impl<T> Stream for Pages<T> {
    type Item = Result<Page<T>>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.0.poll_next_unpin(cx)
    }
}

async fn fetch<T: DeserializeOwned>(
    request: RequestBuilder,
    param: PageParam,
    number: u32,
) -> Result<Page<T>> {
    request.with_page(param, number).send().await?.json().await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn page(json: &str) -> Page<u32> {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn detects_the_last_page() {
        assert!(page(r#"{"content": [1], "last": true}"#).is_last());
        assert!(!page(r#"{"content": [1], "last": false}"#).is_last());
        assert!(page(r#"{"content": [1], "number": 2, "totalPages": 3}"#).is_last());
        assert!(!page(r#"{"content": [1], "number": 1, "totalPages": 3}"#).is_last());
        assert!(!page(r#"{"content": [1], "totalElements": 10}"#).is_last());
        assert!(page(r#"{"content": [], "last": false}"#).is_last());
        assert!(page(r#"{"totalElements": 0}"#).is_last());
    }
}
