//! `longshot-packager`: what packages a release of longshot-builder (docs/dev/reference/releases.md). It is built and run
//! by the release's jobs (MagicBeanAdmin/Release's longshot.yml, `scripts/longshot_builder.py packager`) and is never
//! shipped. Everything it does is the library crates' code, the one the builder it packages runs.
//!
//! ```text
//! longshot-packager tools check | install | add | make ...   (tools.lock: build-driver's tools::cli)
//! longshot-packager release sources | unpack | attach | dry-run | last-objects | fetch-bases | deltas | files |
//!                           recomplauncher ...               (release_cmd.rs)
//! longshot-packager compile --work <dir> (--third-party-objects <dir> | --dry-run) ...   (compile.rs)
//! longshot-packager rust-key [--root <dir>]
//!                      the key of the tree's Rust sources (default: the current folder), written beside the prebuilt
//!                      `liblongshot_rs.a` the release built (build-compile's `rust`)
//! ```
//!
//! `--output json` (anywhere before a `--`) makes every command speak as `longshot-builder dev` does: JSON Lines events
//! on stdout and nothing else (build-core's `report::console`), a child's output a `log` event a line, a command line
//! not taken or a panic an `error` event.
//!
//! Exit status 0 done, 1 something is wrong, 2 a command line it does not take.

use build_core::report::{Event, Format, Reporter, console};
use build_driver::modhook::{Output, take_output};
use std::process::ExitCode;

mod compile;
mod release_cmd;
mod release_deltas;
mod release_objects;

const USAGE: &str = "usage: longshot-packager tools [--lock <file>] check|install|add|make ...
       longshot-packager release sources|unpack|attach|dry-run|last-objects|fetch-bases|deltas|files|recomplauncher ...
                                                                                            (release --help)
       longshot-packager compile --work <dir> (--third-party-objects <dir> | --dry-run) [--root <tree>] [--store <dir>]
                                 [--jobs N] [--upscaler dlss,fsr4,xess|none] [--cache <folder>]
       longshot-packager rust-key [--root <dir>]
Every command takes --output json|text (json: the events as JSON Lines on stdout and nothing else)";

/// `rust-key [--root <dir>]`: the key of the Rust sources of the tree (default: the current folder), on stdout.
fn rust_key(args: &[String]) -> ExitCode {
    let root = match args {
        [] => std::path::PathBuf::from("."),
        [flag, dir] if flag == "--root" => std::path::PathBuf::from(dir),
        _ => {
            console::error(format_args!("{USAGE}"));
            return ExitCode::from(2);
        }
    };
    match build_compile::rust::source_key(&root) {
        Ok(key) => {
            console::stdout(format_args!("{key}"));
            ExitCode::SUCCESS
        }
        Err(e) => {
            console::error(format_args!("longshot-packager rust-key: {e}"));
            ExitCode::FAILURE
        }
    }
}

/// A command line it does not take, with `--output json` asked: an `error` event (`usage`); exit status 2.
fn usage_event(why: String) -> ExitCode {
    Reporter::stdout(Format::Json).send(Event::Error { code: "usage", text: format!("longshot-packager: {why}") });
    ExitCode::from(2)
}

/// A bug's panic said as an `error` event (`internal_error`), redacted as the logs are; exit status 1.
fn panics_as_events() {
    std::panic::set_hook(Box::new(|info| {
        let who = build_driver::redact::Who::here();
        let text = build_driver::log::for_log(&who, &format!("longshot-packager stopped on an internal error: {info}"));
        Reporter::stdout(Format::Json).send(Event::Error { code: "internal_error", text });
        std::process::exit(1);
    }));
}

fn main() -> ExitCode {
    console::init("longshot-packager", "longshot_packager");
    let given: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    let mut args: Vec<String> = given.iter().map(|a| a.to_string_lossy().into_owned()).collect();
    let json = console::json_requested(&args);
    if json {
        panics_as_events();
    }
    if let Some(a) = given.iter().find(|a| a.to_str().is_none()) {
        let why = format!("an argument is not valid Unicode: {}", a.to_string_lossy());
        if json {
            return usage_event(why);
        }
        eprintln!("longshot-packager: {why}\n{USAGE}");
        return ExitCode::from(2);
    }
    if json {
        match take_output(&mut args) {
            Ok(asked) => console::set_json(asked == Some(Output::Json)),
            Err(why) => return usage_event(why),
        }
    }
    match args.first().map(String::as_str) {
        Some("tools") => build_driver::tools::cli::run("longshot-packager tools", &args[1..]),
        Some("release") => release_cmd::run(&args[1..]),
        Some("compile") => compile::run(&args[1..]),
        Some("rust-key") => rust_key(&args[1..]),
        Some("--help" | "-h" | "help") => {
            console::stdout(format_args!("{USAGE}"));
            ExitCode::SUCCESS
        }
        _ => {
            console::error(format_args!("{USAGE}"));
            ExitCode::from(2)
        }
    }
}
