//! `longshot-builder dev`: the lifter's commands, for developers (docs/dev/reference/builder.md, "The parts").
//!
//! - `discover`: the image's functions, their counts and the time they take (discover.rs);
//! - `console-lists`: the two shader lists a console title folder carries (console_lists.rs);
//! - `sites`: the build's hook sites, read, resolved and placed over the image's own discovery (sites_cmd.rs);
//! - `lift`: the whole lift, from the image to gen/ (lift_cmd.rs, the `lift` crate's `whole`);
//! - `profile`: a run's counts as a guest profile (profile_cmd.rs);
//! - `site-hooks`: hook sites as the lift lowers them, each a function a C test calls (site_hooks.rs);
//! - `words`: single instructions as functions, for the semantics check (words.rs);
//! - `decode <hex word> ...`: each instruction word as the decoder sees it;
//! - `tools`: tools.lock, the build tools the builder carries: check, install, add, make (build-driver's `tools::cli`).
//!
//! A release's packaging is not here: it is longshot-packager's (builder/crates/longshot-packager).

use std::process::ExitCode;

mod console_lists;
mod discover;
mod lift_cmd;
mod profile_cmd;
mod site_hooks;
mod sites_cmd;
mod words;

const USAGE: &str = "usage: longshot-builder dev discover --image <dir>
       longshot-builder dev sites --image <dir> --tables <game>
       longshot-builder dev console-lists (--index <folder> | --romfs <folder>) --repo <dir> --out <folder>
                                          [--leave-out <name>,...] [--jobs N]
       longshot-builder dev lift (--work <dir> | --image <dir> --gen <dir>) [--repo <dir> | --host <dir> --tables <dir>]
                                 [--exefs <dir> | --npdm <file>] [--entries <file>] [--devirt <file>]
                                 [--profile-generate | --profile <file> [--hot-share F] [--hot-max N]]
                                 [--no-polls] [--no-mark-bitmap] [--sync-all-at-calls] [--mark-each]
                                 [--memory-base <hex>] [--dirty-pages N]
                                 [--no-simplify] [--split-over N] [--split-piece N] [--unit-cost N] [--hot-unit-cost N]
                                 [--cold-unit-cost N] [--jobs N] [--store <dir>] [--force]
                                 [--output json|text]
       longshot-builder dev profile --map <profile_map.txt> --raw <counts> ... [--into <profile>] --out <profile>
       longshot-builder dev site-hooks --tables <game> --build <build id> --out <file.ll>
                                       <name>=<site>[@<pc>[:<callee>]][/<ident>] ...
       longshot-builder dev words --in <file> --pc <hex> --out <file.ll> --list <file> [--seq <file>] [--count-checks]
       longshot-builder dev decode <hex word> ...
       longshot-builder dev tools [--lock <file>] check|install|add|make ...";

fn decode(words: &[String]) -> ExitCode {
    let mut ok = true;
    for a in words {
        let Ok(w) = u32::from_str_radix(a.trim_start_matches("0x"), 16) else {
            crate::output::error(format_args!("longshot-builder dev decode: not a hex word: {a}"));
            ok = false;
            continue;
        };
        match a64::decode(w) {
            Some(insn) => crate::output::stdout(format_args!("{w:08x}  {insn}  {:?}", insn.flow())),
            None => crate::output::stdout(format_args!("{w:08x}  (unallocated)")),
        }
    }
    if ok { ExitCode::SUCCESS } else { ExitCode::FAILURE }
}

/// `longshot-builder dev <args>`: `args` are the words after `dev`.
pub fn run(args: &[String]) -> ExitCode {
    let mut args = args.to_vec();
    if crate::output::json_requested(&args) {
        match crate::output::take(&mut args) {
            Ok(asked) => {
                crate::output::set(asked);
            }
            Err(error) => return crate::usage_error(error, true),
        }
    }
    match args.first().map(String::as_str) {
        Some("console-lists") => console_lists::run(&args[1..]),
        Some("discover") => discover::run(&args[1..]),
        Some("sites") => sites_cmd::run(&args[1..]),
        Some("site-hooks") => site_hooks::run(&args[1..]),
        Some("lift") => lift_cmd::run(&args[1..]),
        Some("words") => words::run(&args[1..]),
        Some("profile") => profile_cmd::run(&args[1..]),
        Some("decode") if args.len() > 1 => decode(&args[1..]),
        Some("tools") => build_driver::tools::cli::run("longshot-builder dev tools", &args[1..]),
        Some("--help" | "-h" | "help") => {
            crate::output::stdout(format_args!("{USAGE}"));
            ExitCode::SUCCESS
        }
        _ => {
            crate::output::error(format_args!("{USAGE}"));
            ExitCode::from(2)
        }
    }
}
