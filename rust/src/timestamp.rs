use std::fmt;
use std::str::FromStr;

use chrono::{DateTime, NaiveDate, NaiveDateTime, SecondsFormat, Utc};
use serde::de::{self, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// A point in time in UTC.
///
/// Whitebird services are not consistent about dates, so parsing is lenient: RFC 3339
/// (`2024-05-01T12:00:00+03:00`), an offset without a colon (`2024-05-01T12:00:00+0300`),
/// ISO-8601 without an offset (`2024-05-01T09:00:00.123`, taken as UTC), a bare date
/// (`2024-05-01`, midnight UTC) and Unix time in milliseconds, as a number or a string.
///
/// Timestamps are sent to the second with a numeric offset:
/// `2024-05-01T09:00:00+0000`. [`Display`](fmt::Display) shows them in RFC 3339.
///
/// # Examples
///
/// ```
/// use whitebird::Timestamp;
///
/// let a: Timestamp = "2024-05-01T09:00:00".parse().unwrap();
/// let b: Timestamp = "2024-05-01T12:00:00+0300".parse().unwrap();
/// assert_eq!(a, b);
/// assert_eq!(a, Timestamp::from_unix_millis(1_714_554_000_000).unwrap());
/// assert_eq!(a.to_string(), "2024-05-01T09:00:00Z");
/// assert_eq!(serde_json::to_string(&a).unwrap(), r#""2024-05-01T09:00:00+0000""#);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Timestamp(DateTime<Utc>);

impl Timestamp {
    /// Creates a timestamp from Unix time in milliseconds; `None` if it is out of range.
    pub fn from_unix_millis(millis: i64) -> Option<Self> {
        DateTime::from_timestamp_millis(millis).map(Self)
    }

    /// Returns Unix time in milliseconds.
    pub fn unix_millis(&self) -> i64 {
        self.0.timestamp_millis()
    }

    /// Returns the timestamp as a [`chrono`] date-time.
    pub fn as_datetime(&self) -> &DateTime<Utc> {
        &self.0
    }
}

impl From<DateTime<Utc>> for Timestamp {
    fn from(datetime: DateTime<Utc>) -> Self {
        Self(datetime)
    }
}

impl From<Timestamp> for DateTime<Utc> {
    fn from(timestamp: Timestamp) -> Self {
        timestamp.0
    }
}

/// Error returned when a string is not a recognized timestamp.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("unrecognized timestamp {0:?}")]
pub struct ParseTimestampError(String);

impl FromStr for Timestamp {
    type Err = ParseTimestampError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let value = value.trim();
        if let Ok(datetime) = DateTime::parse_from_rfc3339(value) {
            return Ok(Self(datetime.to_utc()));
        }
        for format in ["%Y-%m-%dT%H:%M:%S%.f%z", "%Y-%m-%d %H:%M:%S%.f%z"] {
            if let Ok(datetime) = DateTime::parse_from_str(value, format) {
                return Ok(Self(datetime.to_utc()));
            }
        }
        for format in [
            "%Y-%m-%dT%H:%M:%S%.f",
            "%Y-%m-%d %H:%M:%S%.f",
            "%Y-%m-%dT%H:%M",
        ] {
            if let Ok(naive) = NaiveDateTime::parse_from_str(value, format) {
                return Ok(Self(naive.and_utc()));
            }
        }
        if let Ok(date) = NaiveDate::parse_from_str(value, "%Y-%m-%d") {
            return Ok(Self(date.and_time(chrono::NaiveTime::MIN).and_utc()));
        }
        value
            .parse()
            .ok()
            .and_then(Self::from_unix_millis)
            .ok_or_else(|| ParseTimestampError(value.to_owned()))
    }
}

impl fmt::Display for Timestamp {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0.to_rfc3339_opts(SecondsFormat::AutoSi, true))
    }
}

impl Serialize for Timestamp {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(&self.0.format("%Y-%m-%dT%H:%M:%S%z"))
    }
}

impl<'de> Deserialize<'de> for Timestamp {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(TimestampVisitor)
    }
}

struct TimestampVisitor;

impl TimestampVisitor {
    fn millis<E: de::Error>(millis: i64) -> Result<Timestamp, E> {
        Timestamp::from_unix_millis(millis)
            .ok_or_else(|| E::custom(format!("timestamp {millis} is out of range")))
    }
}

impl Visitor<'_> for TimestampVisitor {
    type Value = Timestamp;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("an ISO-8601 date-time or Unix time in milliseconds")
    }

    fn visit_str<E: de::Error>(self, value: &str) -> Result<Timestamp, E> {
        value.parse().map_err(E::custom)
    }

    fn visit_i64<E: de::Error>(self, value: i64) -> Result<Timestamp, E> {
        Self::millis(value)
    }

    fn visit_u64<E: de::Error>(self, value: u64) -> Result<Timestamp, E> {
        i64::try_from(value)
            .map_err(E::custom)
            .and_then(Self::millis)
    }

    #[allow(clippy::cast_possible_truncation)]
    fn visit_f64<E: de::Error>(self, value: f64) -> Result<Timestamp, E> {
        Self::millis(value as i64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(json: &str) -> Timestamp {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn accepts_every_known_format() {
        let expected = Timestamp::from_unix_millis(1_714_554_000_123).unwrap();
        assert_eq!(parse(r#""2024-05-01T09:00:00.123Z""#), expected);
        assert_eq!(parse(r#""2024-05-01T12:00:00.123+03:00""#), expected);
        assert_eq!(parse(r#""2024-05-01T12:00:00.123+0300""#), expected);
        assert_eq!(parse(r#""2024-05-01T09:00:00.123+0000""#), expected);
        assert_eq!(parse(r#""2024-05-01 09:00:00.123+0000""#), expected);
        assert_eq!(parse(r#""2024-05-01T09:00:00.123""#), expected);
        assert_eq!(parse(r#""2024-05-01 09:00:00.123""#), expected);
        assert_eq!(parse("1714554000123"), expected);
        assert_eq!(parse(r#""1714554000123""#), expected);
        assert_eq!(parse("1714554000123.0"), expected);
        assert_eq!(parse(r#""2024-05-01""#).to_string(), "2024-05-01T00:00:00Z");
    }

    #[test]
    fn serializes_like_the_web_app() {
        let timestamp = parse(r#""2024-05-01T12:00:00.123+03:00""#);
        assert_eq!(
            serde_json::to_string(&timestamp).unwrap(),
            r#""2024-05-01T09:00:00+0000""#
        );
        assert_eq!(timestamp.to_string(), "2024-05-01T09:00:00.123Z");
        assert_eq!(
            parse(&serde_json::to_string(&timestamp).unwrap()).unix_millis(),
            1_714_554_000_000
        );
    }

    #[test]
    fn rejects_garbage() {
        assert!("yesterday".parse::<Timestamp>().is_err());
        assert!(serde_json::from_str::<Timestamp>("true").is_err());
    }
}
