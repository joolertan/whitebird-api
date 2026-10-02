use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

use hmac::{Hmac, KeyInit, Mac};
use sha1::Sha1;

use crate::error::{Error, Result};

const STEP_SECONDS: u64 = 30;

/// TOTP generator (RFC 6238 with HMAC-SHA1, 6 digits and 30-second steps), the scheme of
/// authenticator apps.
///
/// # Examples
///
/// ```
/// use whitebird::{Client, Totp};
///
/// # fn main() -> whitebird::Result<()> {
/// let client = Client::builder()
///     .credentials("me@example.com", "password")
///     .otp(Totp::new("JBSWY3DPEHPK3PXP")?)
///     .build()?;
/// # Ok(())
/// # }
/// ```
#[derive(Clone)]
pub struct Totp {
    key: Vec<u8>,
}

impl Totp {
    /// Creates a generator from the base32 secret shown when two-factor authentication was set
    /// up. Spaces, dashes, lowercase letters and `=` padding are accepted.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Config`] if the secret is not valid base32.
    pub fn new(secret: &str) -> Result<Self> {
        decode_base32(secret)
            .map(|key| Self { key })
            .ok_or_else(|| Error::Config("the TOTP secret is not valid base32".into()))
    }

    /// Returns the code for the current time.
    pub fn code(&self) -> String {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.as_secs());
        self.code_at(now)
    }

    /// Returns the code for the given Unix time in seconds.
    pub fn code_at(&self, unix_time: u64) -> String {
        format!("{:06}", hotp(&self.key, unix_time / STEP_SECONDS))
    }
}

impl fmt::Debug for Totp {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Totp").finish_non_exhaustive()
    }
}

/// HOTP value (RFC 4226) truncated to 6 digits.
fn hotp(key: &[u8], counter: u64) -> u32 {
    let mut mac = Hmac::<Sha1>::new_from_slice(key).expect("HMAC accepts keys of any length");
    mac.update(&counter.to_be_bytes());
    let digest = mac.finalize().into_bytes();
    let offset = usize::from(digest[digest.len() - 1] & 0x0f);
    let value = u32::from_be_bytes([
        digest[offset],
        digest[offset + 1],
        digest[offset + 2],
        digest[offset + 3],
    ]);
    (value & 0x7fff_ffff) % 1_000_000
}

fn decode_base32(secret: &str) -> Option<Vec<u8>> {
    let mut key = Vec::with_capacity(secret.len() * 5 / 8);
    let mut buffer = 0u64;
    let mut bits = 0;
    for c in secret.bytes().filter(|c| !matches!(c, b' ' | b'-' | b'=')) {
        let value = match c.to_ascii_uppercase() {
            upper @ b'A'..=b'Z' => upper - b'A',
            digit @ b'2'..=b'7' => digit - b'2' + 26,
            _ => return None,
        };
        buffer = (buffer << 5) | u64::from(value);
        bits += 5;
        if bits >= 8 {
            bits -= 8;
            key.push((buffer >> bits).to_be_bytes()[7]);
            buffer &= (1 << bits) - 1;
        }
    }
    (!key.is_empty()).then_some(key)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc_6238_vectors() {
        // Secret "12345678901234567890"; the RFC lists 8-digit codes, these are their last 6 digits.
        let totp = Totp::new("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ").unwrap();
        for (time, code) in [
            (59, "287082"),
            (1_111_111_109, "081804"),
            (1_111_111_111, "050471"),
            (1_234_567_890, "005924"),
            (2_000_000_000, "279037"),
            (20_000_000_000, "353130"),
        ] {
            assert_eq!(totp.code_at(time), code, "t = {time}");
        }
    }

    #[test]
    fn rfc_4226_vectors() {
        let expected = [
            755_224, 287_082, 359_152, 969_429, 338_314, 254_676, 287_922, 162_583, 399_871,
            520_489,
        ];
        for (counter, code) in (0..).zip(expected) {
            assert_eq!(hotp(b"12345678901234567890", counter), code);
        }
    }

    #[test]
    fn lenient_secret_format() {
        let canonical = Totp::new("JBSWY3DPEHPK3PXP").unwrap();
        let spaced = Totp::new("jbsw y3dp ehpk 3pxp====").unwrap();
        assert_eq!(canonical.key, spaced.key);
        assert_eq!(canonical.key, b"Hello!\xde\xad\xbe\xef");
        assert!(Totp::new("not base32!").is_err());
        assert!(Totp::new("").is_err());
    }
}
