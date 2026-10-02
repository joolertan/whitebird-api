use std::sync::Arc;

use futures_util::StreamExt;
use serde::de::DeserializeOwned;
use tokio::runtime::Runtime;

use super::Client;
use crate::sse::Event;
use crate::{Page, Result};

/// Lazy iterator over the items of a paginated endpoint; blocking counterpart of
/// [`crate::Paginator`].
#[must_use = "iterators are lazy and do nothing unless consumed"]
pub struct Paginator<T> {
    inner: crate::Paginator<T>,
    runtime: Arc<Runtime>,
}

impl<T> Paginator<T> {
    pub(crate) fn new(client: &Client, inner: crate::Paginator<T>) -> Self {
        Self {
            inner,
            runtime: Arc::clone(&client.runtime),
        }
    }
}

impl<T: DeserializeOwned + Send + 'static> Paginator<T> {
    /// Fetches page `number` (0-based).
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or the page cannot be decoded.
    pub fn page(&self, number: u32) -> Result<Page<T>> {
        self.runtime.block_on(self.inner.page(number))
    }

    /// Fetches the page the iteration starts from.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or the page cannot be decoded.
    pub fn first_page(&self) -> Result<Page<T>> {
        self.runtime.block_on(self.inner.first_page())
    }

    /// Turns the paginator into an iterator over whole pages.
    pub fn pages(self) -> Pages<T> {
        Pages {
            inner: self.inner.pages(),
            runtime: self.runtime,
        }
    }
}

impl<T: DeserializeOwned + Send + 'static> Iterator for Paginator<T> {
    type Item = Result<T>;

    fn next(&mut self) -> Option<Self::Item> {
        self.runtime.block_on(self.inner.next())
    }
}

/// Lazy iterator over the pages of a paginated endpoint, created by [`Paginator::pages`].
#[must_use = "iterators are lazy and do nothing unless consumed"]
pub struct Pages<T> {
    inner: crate::Pages<T>,
    runtime: Arc<Runtime>,
}

impl<T> Iterator for Pages<T> {
    type Item = Result<Page<T>>;

    fn next(&mut self) -> Option<Self::Item> {
        self.runtime.block_on(self.inner.next())
    }
}

/// Iterator of server-sent events that reconnects automatically; blocking counterpart of
/// [`crate::sse::EventStream`].
#[must_use = "iterators are lazy and do nothing unless consumed"]
pub struct EventStream<T> {
    inner: crate::sse::EventStream<T>,
    runtime: Arc<Runtime>,
}

impl<T> EventStream<T> {
    pub(crate) fn new(client: &Client, inner: crate::sse::EventStream<T>) -> Self {
        Self {
            inner,
            runtime: Arc::clone(&client.runtime),
        }
    }
}

impl<T> Iterator for EventStream<T> {
    type Item = Result<Event<T>>;

    fn next(&mut self) -> Option<Self::Item> {
        self.runtime.block_on(self.inner.next())
    }
}
