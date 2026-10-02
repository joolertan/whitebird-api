//! Incremental parser for the `text/event-stream` format (WHATWG HTML, "Server-sent events").

use std::collections::VecDeque;
use std::mem;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RawEvent {
    pub(crate) event: String,
    pub(crate) id: Option<String>,
    pub(crate) data: String,
}

#[derive(Debug, Default)]
pub(crate) struct Parser {
    buffer: Vec<u8>,
    seen_first_line: bool,
    event: String,
    data: String,
    id: Option<String>,
    last_event_id: Option<String>,
    retry: Option<Duration>,
    events: VecDeque<RawEvent>,
}

impl Parser {
    pub(crate) fn push(&mut self, chunk: &[u8]) {
        self.buffer.extend_from_slice(chunk);
        let buffer = mem::take(&mut self.buffer);
        let mut start = 0;
        while let Some(offset) = buffer[start..]
            .iter()
            .position(|&b| b == b'\n' || b == b'\r')
        {
            let end = start + offset;
            let terminator = match (buffer[end], buffer.get(end + 1)) {
                (b'\r', Some(b'\n')) => 2,
                // A trailing CR may be the first half of CRLF: wait for the next chunk.
                (b'\r', None) => break,
                _ => 1,
            };
            self.line(&buffer[start..end]);
            start = end + terminator;
        }
        self.buffer = buffer[start..].to_vec();
    }

    pub(crate) fn next_event(&mut self) -> Option<RawEvent> {
        self.events.pop_front()
    }

    pub(crate) fn take_retry(&mut self) -> Option<Duration> {
        self.retry.take()
    }

    pub(crate) fn last_event_id(&self) -> Option<&str> {
        self.last_event_id.as_deref()
    }

    /// Drops the partial event of a broken connection, keeping the ID of the last dispatched
    /// one.
    pub(crate) fn reset(&mut self) {
        *self = Self {
            id: self.last_event_id.clone(),
            last_event_id: self.last_event_id.take(),
            ..Self::default()
        };
    }

    fn line(&mut self, line: &[u8]) {
        let line = String::from_utf8_lossy(line);
        let mut line = line.as_ref();
        if !mem::replace(&mut self.seen_first_line, true) {
            line = line.strip_prefix('\u{feff}').unwrap_or(line);
        }
        if line.is_empty() {
            self.dispatch();
            return;
        }
        if line.starts_with(':') {
            return;
        }
        let (field, value) = match line.split_once(':') {
            Some((field, value)) => (field, value.strip_prefix(' ').unwrap_or(value)),
            None => (line, ""),
        };
        match field {
            "event" => value.clone_into(&mut self.event),
            "data" => {
                self.data.push_str(value);
                self.data.push('\n');
            }
            "id" if !value.contains('\0') => {
                self.id = (!value.is_empty()).then(|| value.to_owned());
            }
            "retry" if !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()) => {
                self.retry = value.parse().ok().map(Duration::from_millis);
            }
            _ => {}
        }
    }

    fn dispatch(&mut self) {
        self.last_event_id.clone_from(&self.id);
        let event = mem::take(&mut self.event);
        let mut data = mem::take(&mut self.data);
        if data.pop().is_none() {
            return;
        }
        self.events.push_back(RawEvent {
            event: if event.is_empty() {
                "message".to_owned()
            } else {
                event
            },
            id: self.id.clone(),
            data,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(chunks: &[&str]) -> (Vec<RawEvent>, Parser) {
        let mut parser = Parser::default();
        for chunk in chunks {
            parser.push(chunk.as_bytes());
        }
        let events = std::iter::from_fn(|| parser.next_event()).collect();
        (events, parser)
    }

    fn event(event: &str, id: Option<&str>, data: &str) -> RawEvent {
        RawEvent {
            event: event.into(),
            id: id.map(Into::into),
            data: data.into(),
        }
    }

    #[test]
    fn parses_fields_comments_and_multiline_data() {
        let (events, _) = parse(&[
            ": connected\n\nevent: order-updates\nid: 7\ndata: {\"a\":\ndata:1}\n\ndata: plain\n\n",
        ]);
        assert_eq!(
            events,
            [
                event("order-updates", Some("7"), "{\"a\":\n1}"),
                event("message", Some("7"), "plain")
            ]
        );
    }

    #[test]
    fn handles_every_line_ending_and_split_chunks() {
        let (events, _) = parse(&["\u{feff}data: a\r", "\ndata: b\r\r", "data:c\n", "\n"]);
        assert_eq!(
            events,
            [event("message", None, "a\nb"), event("message", None, "c")]
        );
    }

    #[test]
    fn ignores_events_without_data_and_reads_retry() {
        let (events, mut parser) = parse(&["event: ping\n\nretry: 1500\nretry: soon\nid: 3\n\n"]);
        assert!(events.is_empty());
        assert_eq!(parser.take_retry(), Some(Duration::from_millis(1500)));
        assert_eq!(parser.last_event_id(), Some("3"));
    }

    #[test]
    fn reset_keeps_the_id_of_the_last_dispatched_event_only() {
        let (events, mut parser) = parse(&["id: 9\ndata: done\n\nid: 10\ndata: partial"]);
        assert_eq!(events, [event("message", Some("9"), "done")]);
        parser.reset();
        assert_eq!(parser.last_event_id(), Some("9"));
        parser.push(b"\n\ndata: next\n\n");
        assert_eq!(
            parser.next_event(),
            Some(event("message", Some("9"), "next"))
        );
    }
}
