//! Keep the game's output on the builder's JSON Lines stream.
use serde_json::Value;
use std::{
    io::{self, BufRead, BufReader, Write},
    process::{Command, Stdio},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

pub(super) struct Output([JoinHandle<()>; 2]);

impl Output {
    pub(super) fn attach(game: &mut Command) -> io::Result<Self> {
        let (stdout, out) = io::pipe()?;
        let (stderr, err) = io::pipe()?;
        game.stdout(Stdio::from(out)).stderr(Stdio::from(err));
        Ok(Self([
            thread::Builder::new().spawn(move || relay(stdout, "stdout"))?,
            thread::Builder::new().spawn(move || relay(stderr, "stderr"))?,
        ]))
    }

    pub(super) fn finish(self) {
        // An app opened by the game can inherit its streams and outlive the game.
        let deadline = Instant::now() + Duration::from_millis(250);
        while self.0.iter().any(|reader| !reader.is_finished()) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        for reader in self.0 {
            if reader.is_finished() {
                let _ = reader.join();
            }
        }
    }
}

fn startup_event(line: &[u8], stream: &str) -> Option<Value> {
    if stream != "stdout" {
        return None;
    }
    let event = serde_json::from_slice::<Value>(line).ok()?;
    (event["v"] == build_core::report::VERSION
        && matches!(event["type"].as_str(), Some("startup_progress" | "game_ready")))
    .then_some(event)
}

fn relay(reader: io::PipeReader, stream: &'static str) {
    for line in BufReader::new(reader).split(b'\n').map_while(Result::ok) {
        if let Some(event) = startup_event(&line, stream) {
            let _ = writeln!(io::stdout().lock(), "{event}");
        } else {
            crate::output::child_line("game", stream, &line);
        }
        // Keep draining even if the launcher disconnected, so the game can keep writing.
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn forwards_startup_events_and_wraps_other_game_output() {
        let ready = br#"{"v":1,"type":"startup_progress","stage":"pipelines","permille":50}"#;
        assert_eq!(startup_event(ready, "stdout").unwrap()["permille"], 50);
        assert!(startup_event(ready, "stderr").is_none());
        assert!(startup_event(b"a driver's message", "stdout").is_none());
        assert!(startup_event(br#"{"v":1,"type":"done"}"#, "stdout").is_none());
    }
}
