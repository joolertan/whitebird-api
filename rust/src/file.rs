use std::io;
use std::path::Path;

use bytes::Bytes;

/// A file uploaded in a multipart request, with its name and media type.
///
/// Parameters that take files accept a `File` or anything convertible into one, such as the
/// `Vec<u8>` of its contents. Without a name, the file is named after the form field, with the
/// extension of its detected type; without a media type, it is guessed from the name, then from
/// the first bytes (PDF, PNG, JPEG, GIF, WebP, ZIP), and falls back to
/// `application/octet-stream`.
///
/// # Examples
///
/// ```no_run
/// use whitebird::File;
///
/// let scan = File::read("passport.pdf")?;
/// let photo = File::new(b"\xFF\xD8\xFF...".to_vec())
///     .with_name("selfie.jpg")
///     .with_content_type("image/jpeg");
/// # Ok::<(), std::io::Error>(())
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct File {
    /// Contents of the file.
    pub data: Bytes,
    /// Name sent as the `filename` of the part.
    pub name: Option<String>,
    /// Media type sent as the `Content-Type` of the part.
    pub content_type: Option<String>,
}

impl File {
    /// Creates a file from its contents.
    pub fn new(data: impl Into<Bytes>) -> Self {
        Self {
            data: data.into(),
            name: None,
            content_type: None,
        }
    }

    /// Reads the file at `path`, named after its last component.
    ///
    /// # Errors
    ///
    /// Returns the error of reading the file.
    pub fn read(path: impl AsRef<Path>) -> io::Result<Self> {
        let path = path.as_ref();
        let file = Self::new(std::fs::read(path)?);
        Ok(match path.file_name().and_then(|name| name.to_str()) {
            Some(name) => file.with_name(name),
            None => file,
        })
    }

    /// Sets the file name.
    #[must_use]
    pub fn with_name(mut self, name: impl Into<String>) -> Self {
        self.name = Some(name.into());
        self
    }

    /// Sets the media type, such as `application/pdf`.
    #[must_use]
    pub fn with_content_type(mut self, content_type: impl Into<String>) -> Self {
        self.content_type = Some(content_type.into());
        self
    }

    /// The name to send: the given one, or `field` with the extension of the detected type.
    pub(crate) fn file_name(&self, field: &str) -> String {
        match &self.name {
            Some(name) => name.clone(),
            None => match sniff(&self.data).and_then(extension_of) {
                Some(extension) => format!("{field}.{extension}"),
                None => field.to_owned(),
            },
        }
    }

    /// The media type to send: the given one, or one guessed from the name or the contents.
    pub(crate) fn media_type(&self) -> &str {
        self.content_type
            .as_deref()
            .or_else(|| self.name.as_deref().and_then(by_extension))
            .or_else(|| sniff(&self.data))
            .unwrap_or("application/octet-stream")
    }
}

impl From<Vec<u8>> for File {
    fn from(data: Vec<u8>) -> Self {
        Self::new(data)
    }
}

impl From<Bytes> for File {
    fn from(data: Bytes) -> Self {
        Self::new(data)
    }
}

impl From<&'static [u8]> for File {
    fn from(data: &'static [u8]) -> Self {
        Self::new(data)
    }
}

/// Media types by file extension, for the kinds of documents the API takes.
const TYPES: [(&str, &str); 22] = [
    ("pdf", "application/pdf"),
    ("png", "image/png"),
    ("jpg", "image/jpeg"),
    ("jpeg", "image/jpeg"),
    ("gif", "image/gif"),
    ("webp", "image/webp"),
    ("heic", "image/heic"),
    ("heif", "image/heif"),
    ("bmp", "image/bmp"),
    ("tif", "image/tiff"),
    ("tiff", "image/tiff"),
    ("txt", "text/plain"),
    ("csv", "text/csv"),
    ("json", "application/json"),
    ("xml", "application/xml"),
    ("doc", "application/msword"),
    (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ("xls", "application/vnd.ms-excel"),
    (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ("zip", "application/zip"),
    ("mp4", "video/mp4"),
    ("mov", "video/quicktime"),
];

fn by_extension(name: &str) -> Option<&'static str> {
    let (_, extension) = name.rsplit_once('.')?;
    TYPES
        .iter()
        .find(|(known, _)| known.eq_ignore_ascii_case(extension))
        .map(|(_, media_type)| *media_type)
}

fn extension_of(media_type: &str) -> Option<&'static str> {
    TYPES
        .iter()
        .find(|(_, known)| *known == media_type)
        .map(|(extension, _)| *extension)
}

/// Recognizes common formats by their signature.
fn sniff(data: &[u8]) -> Option<&'static str> {
    Some(match data {
        [b'%', b'P', b'D', b'F', b'-', ..] => "application/pdf",
        [0x89, b'P', b'N', b'G', b'\r', b'\n', 0x1a, b'\n', ..] => "image/png",
        [0xff, 0xd8, 0xff, ..] => "image/jpeg",
        [b'G', b'I', b'F', b'8', b'7' | b'9', b'a', ..] => "image/gif",
        [
            b'R',
            b'I',
            b'F',
            b'F',
            _,
            _,
            _,
            _,
            b'W',
            b'E',
            b'B',
            b'P',
            ..,
        ] => "image/webp",
        [b'P', b'K', 3, 4, ..] => "application/zip",
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_types_are_guessed() {
        let pdf = File::from(b"%PDF-1.7 ...".to_vec());
        assert_eq!(pdf.file_name("files"), "files.pdf");
        assert_eq!(pdf.media_type(), "application/pdf");

        let named = File::new(&b"plain"[..]).with_name("Scan.JPG");
        assert_eq!(named.file_name("files"), "Scan.JPG");
        assert_eq!(named.media_type(), "image/jpeg");

        let unknown = File::new(&b"?"[..]);
        assert_eq!(unknown.file_name("file"), "file");
        assert_eq!(unknown.media_type(), "application/octet-stream");

        let explicit = File::new(&b"%PDF-"[..]).with_content_type("application/x-pdf");
        assert_eq!(explicit.media_type(), "application/x-pdf");
    }
}
