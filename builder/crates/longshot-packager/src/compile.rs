//! `longshot-packager compile`: the two compiles of `longshot-builder compile` a release's jobs need, which need no game,
//! with the builder's arguments and through the same code (`build_driver::build::compile_only`), so a set's objects have
//! the keys a player's build asks for:
//!
//! - `--third-party-objects <dir>`: the release's third-party set, written to `<dir>`;
//! - `--dry-run`: what a player's build would compile and which of its files are missing, nothing compiled (`release
//!   dry-run` runs it for a tree whose own builder can't run here: Windows', cross-built on Linux).

use build_core::report::{Event, Format, Reporter, console};
use build_driver::{CompileMode, Stages, Upscaler, build};
use std::path::PathBuf;
use std::process::ExitCode;

/// The command line after `compile`.
#[derive(Debug, Default)]
struct Args {
    work: Option<PathBuf>,
    root: Option<PathBuf>,
    store: Option<PathBuf>,
    jobs: Option<usize>,
    upscalers: Vec<Upscaler>,
    cache: Option<String>,
    output: Option<build_driver::modhook::Output>,
    third_party_objects: Option<PathBuf>,
    dry_run: bool,
}

fn parse(args: &[String]) -> Result<Args, String> {
    let mut a = Args::default();
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let mut value = || it.next().ok_or_else(|| format!("{arg} needs a value"));
        match arg.as_str() {
            "--work" => a.work = Some(value()?.into()),
            "--root" => a.root = Some(value()?.into()),
            "--store" => a.store = Some(value()?.into()),
            "--jobs" => {
                a.jobs = build_core::jobs::requested(Some(value()?))
                    .map_err(|_| "--jobs: 1 or more, or 0 for automatic".to_string())?
            }
            "--upscaler" => {
                for u in Upscaler::parse_list(value()?).map_err(|e| e.to_string())? {
                    if !a.upscalers.contains(&u) {
                        a.upscalers.push(u);
                    }
                }
            }
            "--cache" => a.cache = Some(value()?.clone()),
            "--output" => a.output = Some(build_driver::modhook::Output::parse(value()?)?),
            "--third-party-objects" => a.third_party_objects = Some(value()?.into()),
            "--dry-run" => a.dry_run = true,
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    if a.work.is_none() {
        return Err("--work is needed".into());
    }
    if a.third_party_objects.is_some() == a.dry_run {
        return Err("--third-party-objects <dir> or --dry-run is needed, one of them".into());
    }
    Ok(a)
}

fn compile(a: &Args, r: &Reporter) -> build_core::Result<()> {
    let t = std::time::Instant::now();
    let p = build::stage_places(a.root.as_deref(), a.work.as_deref(), None, a.store.as_deref(), a.jobs)?;
    let mode = match &a.third_party_objects {
        Some(dir) => CompileMode::ThirdPartySet(std::path::absolute(dir).unwrap_or_else(|_| dir.clone())),
        None => CompileMode::DryRun,
    };
    let b = build::compile_only(&p, &a.upscalers, Some(mode), &Stages::real(), r)?;
    r.log(format!("compile: {} of {} units compiled now", b.compiled, b.units));
    r.log(format!("compile took {:.1} s", t.elapsed().as_secs_f64()));
    Ok(())
}

/// Runs the command on its arguments (after `compile`).
pub fn run(args: &[String]) -> ExitCode {
    let a = match parse(args) {
        Ok(a) => a,
        Err(e) => {
            console::error(format_args!("longshot-packager compile: {e}"));
            return ExitCode::from(2);
        }
    };
    if let Some(cache) = &a.cache {
        build_driver::set_flag("LONGSHOT_CACHE", cache);
    }
    // (`--output json` is taken before the command is: main.rs)
    let format = if console::json() { Format::Json } else { a.output.map_or(Format::Text, |o| o.format()) };
    let r = Reporter::stdout(format);
    match compile(&a, &r) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            r.send(Event::Error { code: e.code(), text: e.to_string() });
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(s: &str) -> Result<Args, String> {
        parse(&s.split_whitespace().map(String::from).collect::<Vec<_>>())
    }

    /// The lines the release's jobs and `release dry-run` give it.
    #[test]
    fn the_command_lines_it_takes() {
        let t = args("--work w --root r --store s --jobs 4 --third-party-objects o").unwrap();
        assert_eq!((t.jobs, t.third_party_objects, t.dry_run), (Some(4), Some("o".into()), false));
        let d = args("--work w --dry-run --upscaler fsr4 --output text --cache c --root r").unwrap();
        assert_eq!((d.upscalers, d.cache.as_deref(), d.dry_run), (vec![Upscaler::Fsr4], Some("c"), true));
        assert!(args("--work w --dry-run --upscaler none").unwrap().upscalers.is_empty());
        assert!(args("--dry-run").is_err(), "--work is needed");
        assert!(args("--work w").is_err(), "no game's compile here: longshot-builder's");
        assert!(args("--work w --dry-run --third-party-objects o").is_err());
        assert!(args("--work w --only renderer").is_err());
    }
}
