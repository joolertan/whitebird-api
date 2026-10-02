use std::borrow::Cow;
use std::fmt;
use std::str::FromStr;

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::value::RawValue;

/// Exact decimal number used for amounts and prices.
///
/// Wraps [`rust_decimal::Decimal`] (28 significant digits). It is decoded from the JSON text of
/// the number, never through `f64`, so `0.123456789012345678` stays exact; strings holding a
/// number are accepted too. It serializes as a JSON string, which the API accepts for every
/// decimal field.
///
/// Decoding relies on [`serde_json`]; other serde formats are not supported.
///
/// # Examples
///
/// ```
/// use whitebird::Decimal;
///
/// let amount: Decimal = serde_json::from_str("0.123456789012345678").unwrap();
/// assert_eq!(amount.to_string(), "0.123456789012345678");
///
/// let total = amount.get() * rust_decimal::Decimal::TWO;
/// assert_eq!(total.to_string(), "0.246913578024691356");
/// ```
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Decimal(rust_decimal::Decimal);

impl Decimal {
    /// Zero.
    pub const ZERO: Self = Self(rust_decimal::Decimal::ZERO);

    /// Returns the wrapped [`rust_decimal::Decimal`] for arithmetic.
    pub fn get(self) -> rust_decimal::Decimal {
        self.0
    }
}

impl From<rust_decimal::Decimal> for Decimal {
    fn from(value: rust_decimal::Decimal) -> Self {
        Self(value)
    }
}

impl From<Decimal> for rust_decimal::Decimal {
    fn from(value: Decimal) -> Self {
        value.0
    }
}

macro_rules! from_integer {
    ($($integer:ty),*) => {
        $(impl From<$integer> for Decimal {
            fn from(value: $integer) -> Self {
                Self(value.into())
            }
        })*
    };
}

from_integer!(i32, i64, u32, u64);

impl FromStr for Decimal {
    type Err = rust_decimal::Error;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let value = value.trim();
        rust_decimal::Decimal::from_str(value)
            .or_else(|_| rust_decimal::Decimal::from_scientific(value))
            .map(Self)
    }
}

impl fmt::Display for Decimal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.0, f)
    }
}

impl Serialize for Decimal {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for Decimal {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        // The raw JSON text keeps every digit; going through `f64` would not.
        let raw = Box::<RawValue>::deserialize(deserializer)?;
        let text: Cow<'_, str> = if raw.get().starts_with('"') {
            Cow::Owned(serde_json::from_str(raw.get()).map_err(D::Error::custom)?)
        } else {
            Cow::Borrowed(raw.get())
        };
        text.parse()
            .map_err(|_| D::Error::custom(format_args!("invalid decimal {text:?}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_without_losing_digits() {
        let value: Decimal = serde_json::from_str("1234567.123456789012345").unwrap();
        assert_eq!(value.to_string(), "1234567.123456789012345");
        let value: Decimal = serde_json::from_str(r#""0.000000000000000001""#).unwrap();
        assert_eq!(value.to_string(), "0.000000000000000001");
        let value: Decimal = serde_json::from_str("1e-7").unwrap();
        assert_eq!(value.to_string(), "0.0000001");
    }

    #[test]
    fn decodes_from_value_and_inside_containers() {
        let value: Vec<Option<Decimal>> =
            serde_json::from_value(serde_json::json!([1.5, null, "2"])).unwrap();
        assert_eq!(
            value,
            [Some("1.5".parse().unwrap()), None, Some(Decimal::from(2))]
        );
    }

    #[test]
    fn serializes_as_string() {
        let value: Decimal = "10.50".parse().unwrap();
        assert_eq!(serde_json::to_string(&value).unwrap(), r#""10.50""#);
    }

    #[test]
    fn rejects_non_numbers() {
        assert!(serde_json::from_str::<Decimal>("true").is_err());
        assert!(serde_json::from_str::<Decimal>(r#""abc""#).is_err());
    }
}
