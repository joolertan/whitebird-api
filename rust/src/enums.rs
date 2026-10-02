//! Open enums: values unknown to this version are kept instead of failing to decode.

/// Declares an enum of string values with a fallback variant holding any other value.
macro_rules! string_enum {
    (
        $(#[$meta:meta])*
        pub enum $name:ident {
            $($(#[$variant_meta:meta])* $variant:ident = $wire:literal,)*
            _ => $unknown:ident $(,)?
        }
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        #[non_exhaustive]
        pub enum $name {
            $(
                #[doc = concat!("`", $wire, "`")]
                $(#[$variant_meta])*
                $variant,
            )*
            /// A value this version of the SDK does not know.
            $unknown(String),
        }

        impl $name {
            /// Returns the value as sent on the wire.
            pub fn as_str(&self) -> &str {
                match self {
                    $(Self::$variant => $wire,)*
                    Self::$unknown(value) => value,
                }
            }
        }

        impl From<&str> for $name {
            fn from(value: &str) -> Self {
                match value {
                    $($wire => Self::$variant,)*
                    other => Self::$unknown(other.to_owned()),
                }
            }
        }

        impl ::std::fmt::Display for $name {
            fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
                f.write_str(self.as_str())
            }
        }

        impl ::serde::Serialize for $name {
            fn serialize<S: ::serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.serialize_str(self.as_str())
            }
        }

        impl<'de> ::serde::Deserialize<'de> for $name {
            fn deserialize<D: ::serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                String::deserialize(deserializer).map(|value| Self::from(value.as_str()))
            }
        }
    };
}

/// Declares an enum of integer values with a fallback variant holding any other value.
#[allow(
    unused_macros,
    reason = "used by generated code when the spec has integer enums"
)]
macro_rules! integer_enum {
    (
        $(#[$meta:meta])*
        pub enum $name:ident {
            $($(#[$variant_meta:meta])* $variant:ident = $wire:literal,)*
            _ => $unknown:ident $(,)?
        }
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        #[non_exhaustive]
        pub enum $name {
            $(
                #[doc = concat!("`", $wire, "`")]
                $(#[$variant_meta])*
                $variant,
            )*
            /// A value this version of the SDK does not know.
            $unknown(i64),
        }

        impl $name {
            /// Returns the value as sent on the wire.
            pub fn value(self) -> i64 {
                match self {
                    $(Self::$variant => $wire,)*
                    Self::$unknown(value) => value,
                }
            }
        }

        impl From<i64> for $name {
            fn from(value: i64) -> Self {
                match value {
                    $($wire => Self::$variant,)*
                    other => Self::$unknown(other),
                }
            }
        }

        impl ::std::fmt::Display for $name {
            fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
                ::std::fmt::Display::fmt(&self.value(), f)
            }
        }

        impl ::serde::Serialize for $name {
            fn serialize<S: ::serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.serialize_i64(self.value())
            }
        }

        impl<'de> ::serde::Deserialize<'de> for $name {
            fn deserialize<D: ::serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                i64::deserialize(deserializer).map(Self::from)
            }
        }
    };
}

#[allow(
    unused_imports,
    reason = "used by generated code when the spec has integer enums"
)]
pub(crate) use integer_enum;
pub(crate) use string_enum;

#[cfg(test)]
mod tests {
    string_enum! {
        pub enum Side {
            Buy = "BUY",
            InProgress = "in-progress",
            _ => Unknown,
        }
    }

    integer_enum! {
        pub enum Priority {
            V1 = 1,
            V2 = 2,
            _ => Unknown,
        }
    }

    #[test]
    fn known_and_unknown_strings_round_trip() {
        let values: Vec<Side> = serde_json::from_str(r#"["BUY", "in-progress", "HOLD"]"#).unwrap();
        assert_eq!(
            values,
            [Side::Buy, Side::InProgress, Side::Unknown("HOLD".into())]
        );
        assert_eq!(
            serde_json::to_string(&values).unwrap(),
            r#"["BUY","in-progress","HOLD"]"#
        );
        assert_eq!(Side::from("BUY").to_string(), "BUY");
    }

    #[test]
    fn known_and_unknown_integers_round_trip() {
        let values: Vec<Priority> = serde_json::from_str("[1, 2, 7]").unwrap();
        assert_eq!(values, [Priority::V1, Priority::V2, Priority::Unknown(7)]);
        assert_eq!(serde_json::to_string(&values).unwrap(), "[1,2,7]");
    }
}
