//! Unofficial client for the [Whitebird](https://whitebird.io) crypto exchange API.
//!
//! The client signs in with the account email and password (plus a TOTP code when two-factor
//! authentication is on), keeps the OAuth session fresh and exposes the endpoints as typed
//! methods grouped by resource: `client.brokerage().otc_orders().list(..)`.
//!
//! ```no_run
//! use futures::TryStreamExt;
//! use whitebird::params::BrokerageOtcOrdersListParams;
//! use whitebird::{Client, Environment};
//!
//! # async fn run() -> whitebird::Result<()> {
//! let client = Client::builder()
//!     .credentials("me@example.com", "password")
//!     .otp("123456")
//!     .environment(Environment::Demo)
//!     .build()?;
//!
//! let subaccount = client.brokerage().subaccounts().me().await?;
//! println!("{:?}", subaccount.balances);
//!
//! let params = BrokerageOtcOrdersListParams::builder().size(50).build();
//! let mut orders = client.brokerage().otc_orders().list(params);
//! while let Some(order) = orders.try_next().await? {
//!     println!("{:?} {:?}", order.uuid, order.status);
//! }
//! # Ok(())
//! # }
//! ```
//!
//! # Feature flags
//!
//! - `rustls-tls` (default): TLS through rustls.
//! - `native-tls`: TLS through the platform library.
//! - `otp`: [`Totp`] computes two-factor codes from the base32 secret.
//! - `blocking`: a synchronous [`blocking::Client`] with the same resources.
//!
//! # Tolerance to API changes
//!
//! Unknown response fields are ignored, enums keep values this version does not know in an
//! `Unknown` variant, [`Timestamp`] accepts every date format the API is known to use and
//! [`Decimal`] amounts are decoded without going through floating point. Endpoints missing from
//! the SDK can be called with [`Client::request`].
#![cfg_attr(docsrs, feature(doc_cfg))]
#![warn(missing_docs)]

mod auth;
#[cfg(feature = "blocking")]
#[cfg_attr(docsrs, doc(cfg(feature = "blocking")))]
pub mod blocking;
mod client;
mod decimal;
mod enums;
mod error;
mod generated;
mod otp;
mod pagination;
mod request;
mod retry;
pub mod sse;
mod timestamp;

pub use auth::{MemoryTokenStore, TokenStore, Tokens};
pub use bytes::Bytes;
pub use client::{Client, ClientBuilder};
pub use decimal::Decimal;
pub use error::{ApiError, Error, ErrorKind, Result};
pub use generated::{Environment, Service, models, params, resources};
pub use otp::CodeProvider;
#[cfg(feature = "otp")]
#[cfg_attr(docsrs, doc(cfg(feature = "otp")))]
pub use otp::Totp;
pub use pagination::{Page, PageParam, Pages, Paginator};
pub use request::{Envelope, Multipart, RequestBuilder, RequestOptions, Response};
pub use reqwest::{Method, StatusCode, header};
pub use timestamp::{ParseTimestampError, Timestamp};
pub use {chrono, rust_decimal};

#[cfg(all(doctest, feature = "otp"))]
#[doc = include_str!("../README.md")]
struct ReadmeDoctests;
