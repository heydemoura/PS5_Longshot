//! A command's own answers outside a build's reporter (`longshot-builder`'s `dev` commands and its notices, and
//! `longshot-packager`): printed as they are, or with `--output json` ([`set_json`]) said as `log` events on stdout, a
//! child program's output among them, so a reader of the JSON Lines gets nothing else.

use super::{Diagnostics, Event, Format, Reporter};
use std::sync::OnceLock;
use tracing_subscriber::{filter::Targets, prelude::*};

/// The tracing target of what is said here.
pub const TARGET: &str = "longshot::console";

/// Whether this run speaks JSON, once decided.
static JSON: OnceLock<bool> = OnceLock::new();

/// This run's output decided: JSON Lines, or text. The first call's holds.
pub fn set_json(json: bool) {
    let _ = JSON.set(json);
}

/// Whether this run speaks JSON ([`set_json`]'s; not before it).
pub fn json() -> bool {
    JSON.get().copied().unwrap_or(false)
}

/// The program's sink for its diagnostics, set once, first thing: those of target `own` (the program's crate) and of
/// this module, as events with JSON, else as `<name>: <text>` on stderr.
pub fn init(name: &'static str, own: &'static str) {
    let events = Reporter::stdout(Format::Json);
    let sink = Diagnostics(move |event| {
        if json() {
            events.send(event);
        } else if let Event::Log { text, .. } = event {
            eprintln!("{name}: {text}");
        }
    });
    let wanted = Targets::new().with_target(own, tracing::Level::INFO).with_target(TARGET, tracing::Level::INFO);
    tracing_subscriber::registry().with(sink.with_filter(wanted)).init();
}

/// Whether the command requested JSON, including when another argument could not be parsed.
pub fn json_requested(args: &[String]) -> bool {
    let end = args.iter().position(|arg| arg == "--").unwrap_or(args.len());
    args[..end].iter().any(|arg| arg == "--output=json")
        || args[..end].windows(2).any(|pair| pair[0] == "--output" && pair[1] == "json")
}

/// A diagnostic, formatted consistently with the selected output.
pub fn log(text: impl Into<String>) {
    let text = text.into();
    tracing::info!(target: TARGET, "{text}");
}

/// A line of a command's answer: on stdout, or a `log` event.
pub fn stdout(text: std::fmt::Arguments<'_>) {
    if json() {
        log(text.to_string());
    } else {
        println!("{text}");
    }
}

/// A command's error: on stderr, or a `log` event of level `ERROR`.
pub fn error(text: std::fmt::Arguments<'_>) {
    if json() {
        tracing::error!(target: TARGET, code = "command_error", "{text}");
    } else {
        eprintln!("{text}");
    }
}

/// Child diagnostics share the launcher's line format, including the originating stream.
pub fn child_line(source: &'static str, stream: &'static str, bytes: &[u8]) {
    let text = String::from_utf8_lossy(bytes);
    let code = if source == "game" { "game_output" } else { "tool_output" };
    tracing::info!(target: TARGET, code, source, stream, "{}", text.trim_end_matches('\r'));
}

/// A finished child's output passed on: as it was, or with JSON a `log` event a line.
pub fn tool_output(output: &std::process::Output) {
    use std::io::Write;
    if json() {
        for (stream, bytes) in [("stdout", &output.stdout), ("stderr", &output.stderr)] {
            for line in bytes.split(|b| *b == b'\n').filter(|line| !line.is_empty()) {
                child_line("tool", stream, line);
            }
        }
    } else {
        let _ = std::io::stdout().write_all(&output.stdout);
        let _ = std::io::stderr().write_all(&output.stderr);
    }
}

/// A child run to its end, its output the program's own, or with JSON said a `log` event a line as it comes.
pub fn tool_status(command: &mut std::process::Command) -> std::io::Result<std::process::ExitStatus> {
    if !json() {
        return crate::child::status(command);
    }
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    let mut child = crate::child::spawn(command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()))?;
    fn drain(reader: impl std::io::Read, stream: &'static str) -> std::io::Result<()> {
        for line in BufReader::new(reader).split(b'\n') {
            child_line("tool", stream, &line?);
        }
        Ok(())
    }
    let stdout = child.stdout.take().expect("piped");
    let stderr = child.stderr.take().expect("piped");
    std::thread::scope(|scope| {
        let err = scope.spawn(|| drain(stderr, "stderr"));
        let out = drain(stdout, "stdout");
        let status = child.wait();
        err.join().expect("stderr reader")?;
        out?;
        status
    })
}
