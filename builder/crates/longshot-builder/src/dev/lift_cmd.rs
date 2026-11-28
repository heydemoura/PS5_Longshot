//! `longshot-builder dev lift`: the whole lift, from the game's image to gen/ ([`lift::whole`]; builder.md, "The gen/
//! folder"). The options:
//!
//! - `--work <dir>`: the folder holding `image/` (read) and `gen/` (written); or `--image <dir>` and `--gen <dir>`;
//! - `--repo <dir>`: the repository whose `host/` and `game/` (HLE functions, entry hooks; `game/`'s site tables) the lift
//!   reads (`--host <dir>` and `--tables <dir>` name them one by one); `--exefs <dir>` or `--npdm <file>`: the game's
//!   `main.npdm`, for the boot constants;
//! - `--entries <file>` (more entries, hex a line), `--devirt <file>` (`<blr> <target>` a line);
//! - the guest profile: `--profile-generate`, or `--profile <file>` [`--hot-share F`] [`--hot-max N`];
//! - the IR: `--no-polls`, `--no-mark-bitmap`, `--sync-all-at-calls`, `--mark-each`, `--no-simplify`; `--split-over
//!   N` (a function of more mid IR instructions is split into pieces for the compiler; 0: none) and `--split-piece N`
//!   (about how many a piece holds); the hot units' float slow paths have their middle path by default, as a build
//!   has them (the log counts them): `--no-fp-nan-mid` lifts without it, as a build with `LONGSHOT_FP_NAN_MID=0`,
//!   `--fp-nan-mid-skip <entries>` leaves those functions out (`LONGSHOT_FP_NAN_MID_SKIP`'s list), and
//!   `--fp-nan-mid` (the default) is kept for scripts that name it; the hot units made for LLVM's hot-cold splitting
//!   as the build makes them, by `LONGSHOT_HOT_SPLIT` (on unless `0`) and `LONGSHOT_HOT_SPLIT_SKIP`, or `--hot-split`
//!   (on), `--no-hot-split` (off) and `--hot-split-skip <list>` over them;
//! - `--unit-cost N` (a unit's budget, `--hot-unit-cost N` a hot one's, `--cold-unit-cost N` a cold one's, 0: no cold
//!   units), `--jobs N`, `--store <dir>`, `--force`;
//! - `--output json|text`: the events as JSON Lines, or text (the default).
//!
//! gen/edits.txt lists each function the edits change, its edits' key and what changes it.

use build_core::report::{Format, Reporter};
use lift::whole::{self, LiftOptions, Profiling};
use std::path::PathBuf;
use std::process::ExitCode;

struct Args {
    o: LiftOptions,
    json: bool,
}

fn parse(args: &[String]) -> Result<Args, String> {
    let mut o = LiftOptions::new(PathBuf::new(), PathBuf::new());
    let (mut work, mut repo, mut exefs) = (None::<PathBuf>, None::<PathBuf>, None::<PathBuf>);
    let (mut share, mut max, mut profile, mut generate) = (0.95, 4000, None::<PathBuf>, false);
    let mut json = false;
    let (mut mid_on, mut mid_off) = (false, false);
    // (unset: the environment's, as the build reads it)
    let (mut hot_split, mut hot_split_skip) = (None::<&str>, None::<String>);
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let mut value = |what: &str| it.next().cloned().ok_or(format!("{arg} needs {what}"));
        let number = |v: String| v.parse::<u64>().map_err(|_| format!("{arg} needs a number"));
        let read = |e: build_core::Error| e.to_string();
        match arg.as_str() {
            "--work" => work = Some(value("a folder")?.into()),
            "--image" => o.image = value("a folder")?.into(),
            "--gen" => o.gen_dir = value("a folder")?.into(),
            "--repo" => repo = Some(value("a folder")?.into()),
            "--host" => o.host = Some(value("a folder")?.into()),
            "--tables" => o.tables = Some(value("a folder")?.into()),
            "--exefs" => exefs = Some(value("a folder")?.into()),
            "--npdm" => o.npdm = Some(value("a file")?.into()),
            "--entries" => o.entries = whole::read_entries(&PathBuf::from(value("a file")?)).map_err(read)?,
            "--devirt" => o.devirt = whole::read_pairs(&PathBuf::from(value("a file")?)).map_err(read)?,
            "--profile-generate" => generate = true,
            "--profile" => profile = Some(value("a file")?.into()),
            "--hot-share" => {
                share = value("a share")?.parse().map_err(|_| "--hot-share needs a number from 0 to 1")?;
                if !(0.0..=1.0).contains(&share) {
                    return Err("--hot-share needs a number from 0 to 1".into());
                }
            }
            "--hot-max" => max = number(value("a number")?)? as usize,
            "--no-polls" => o.codegen.polls = false,
            "--no-mark-bitmap" => o.codegen.mark_bitmap = false,
            "--sync-all-at-calls" => o.codegen.callee_saved = false,
            "--mark-each" => o.mark_each = true,
            "--no-simplify" => o.no_simplify = true,
            "--fp-nan-mid" => mid_on = true,
            "--no-fp-nan-mid" => mid_off = true,
            "--fp-nan-mid-skip" => {
                let list = value("a list of entries")?;
                let skip = build_driver::stages::fp_nan_mid(Some("1"), Some(&list)).map_err(|e| e.to_string())?;
                mid_on = true;
                o.fp_nan_mid.get_or_insert_with(Default::default).extend(skip.unwrap_or_default());
            }
            "--split-over" => o.split.over = number(value("a number")?)? as usize,
            "--split-piece" => o.split.piece = number(value("a number")?)?.max(1) as usize,
            "--unit-cost" => o.sizing.budget = number(value("a number")?)?.max(1),
            "--hot-unit-cost" => o.sizing.hot_budget = number(value("a number")?)?.max(1),
            "--cold-unit-cost" => o.sizing.cold_budget = number(value("a number")?)?,
            "--jobs" => o.jobs = number(value("a number")?)?.max(1) as usize,
            "--store" => o.store = Some(value("a folder")?.into()),
            "--force" => o.force = true,
            "--hot-split" => hot_split = Some("1"),
            "--no-hot-split" => hot_split = Some("0"),
            "--hot-split-skip" => hot_split_skip = Some(value("a list of entries")?),
            "--output" => {
                let v = it.next().ok_or_else(|| "--output needs a value".to_string())?;
                json = match crate::output::Output::parse(v)? {
                    crate::output::Output::Json => true,
                    crate::output::Output::Text | crate::output::Output::User => false,
                    crate::output::Output::RecompLauncher => return Err("dev lift: --output json or text".into()),
                }
            }
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    if mid_off {
        if mid_on {
            return Err("--no-fp-nan-mid and --fp-nan-mid or --fp-nan-mid-skip: one or the other".into());
        }
        o.fp_nan_mid = None;
    }
    if let Some(w) = work {
        if o.image.as_os_str().is_empty() {
            o.image = w.join("image");
        }
        if o.gen_dir.as_os_str().is_empty() {
            o.gen_dir = w.join("gen");
        }
    }
    if let Some(r) = repo {
        o.host.get_or_insert_with(|| r.join("host"));
        o.tables.get_or_insert_with(|| r.join("game"));
    }
    if let Some(e) = exefs {
        o.npdm.get_or_insert_with(|| e.join("main.npdm"));
    }
    if o.image.as_os_str().is_empty() || o.gen_dir.as_os_str().is_empty() {
        return Err("--work, or --image and --gen, are needed".into());
    }
    let on = hot_split.map(str::to_string).or_else(|| build_driver::env("LONGSHOT_HOT_SPLIT"));
    let skip = hot_split_skip.or_else(|| build_driver::env("LONGSHOT_HOT_SPLIT_SKIP"));
    o.hot_split = build_driver::stages::hot_split(on.as_deref(), skip.as_deref())
        .map_err(|w| format!("--hot-split-skip (or LONGSHOT_HOT_SPLIT_SKIP): {w:?} is not a function's entry (hex)"))?;
    o.profiling = match (generate, profile) {
        (true, Some(_)) => return Err("--profile-generate and --profile: one or the other".into()),
        (true, None) => Profiling::Generate,
        (false, Some(path)) => Profiling::Use { path, share, max },
        (false, None) => Profiling::Off,
    };
    Ok(Args { o, json })
}

/// The command; `args` are those after `lift`.
pub fn run(args: &[String]) -> ExitCode {
    let a = match parse(args) {
        Ok(a) => a,
        Err(e) => {
            crate::output::error(format_args!("longshot-builder dev lift: {e}"));
            return ExitCode::from(2);
        }
    };
    let json = a.json || crate::output::chosen() == crate::output::Output::Json;
    let r = Reporter::stdout(if json { Format::Json } else { Format::Text });
    match whole::run(&a.o, &r) {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            r.error(&e);
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::parse;
    use std::collections::BTreeSet;

    fn mid(args: &[&str]) -> Result<Option<BTreeSet<u64>>, String> {
        let mut all: Vec<String> = ["--work", "w"].iter().map(|s| s.to_string()).collect();
        all.extend(args.iter().map(|s| s.to_string()));
        parse(&all).map(|a| a.o.fp_nan_mid)
    }

    #[test]
    fn the_float_middle_path_is_on_by_default_as_in_a_build_and_off_with_no_fp_nan_mid() {
        assert_eq!(mid(&[]).unwrap(), Some(BTreeSet::new()), "on by default");
        assert_eq!(mid(&["--fp-nan-mid"]).unwrap(), Some(BTreeSet::new()));
        assert_eq!(mid(&["--fp-nan-mid-skip", "f_81c7f880"]).unwrap(), Some(BTreeSet::from([0x81c7_f880])));
        assert_eq!(mid(&["--no-fp-nan-mid"]).unwrap(), None, "off");
        assert!(mid(&["--no-fp-nan-mid", "--fp-nan-mid-skip", "f_1"]).is_err());
        assert!(mid(&["--fp-nan-mid", "--no-fp-nan-mid"]).is_err());
    }
}
