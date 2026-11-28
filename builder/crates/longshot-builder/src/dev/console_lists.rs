//! `longshot-builder dev console-lists (--index <folder> | --romfs <folder>) --repo <dir> --out <folder>
//! [--leave-out <name>,...] [--jobs N]`: the two shader lists a console title folder carries, so its first start can
//! build the game's pipelines before play (ps5/builder/shader_lists.py), with no game folder built around them.
//!
//! - `longshot_shaders.bin`: the census of the game's shader programs (build-assets' `census`), read from the romfs
//!   index `longshot-builder index` wrote into `--index` (a .nup), or from an unpacked `--romfs` folder. It holds
//!   game code: it is made on the player's PC and stays there.
//! - `longshot_pipeline_states_shipped.bin`: `--repo`'s `assets/pipeline_states` merged (no game code), less the
//!   sources whose file name starts with one of `--leave-out`'s (the console leaves out lists converted from
//!   another emulator's records, never drawn by Longshot itself); version 1 lists are converted as a release's are.

use build_assets::romfs::{Folder, Romfs};
use build_assets::{census, pipelines};
use build_core::{Error, Result};
use game_files::romfs::{INDEX_FILE, Index};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const USAGE: &str = "usage: longshot-builder dev console-lists (--index <folder> | --romfs <folder>) --repo <dir> \
                     --out <folder> [--leave-out <name>,...] [--jobs N]";

/// The romfs of a .nup, by its index (as build-driver's assets read it).
struct Indexed {
    index: Index,
    identity: String,
}

impl Romfs for Indexed {
    fn files(&self) -> Result<Vec<String>> {
        Ok(self.index.entries().iter().map(|e| e.name.clone()).collect())
    }

    fn read(&self, rel: &str) -> Result<Vec<u8>> {
        self.index.read(rel)
    }

    fn fingerprint(&self, rel: &str) -> Result<String> {
        let e = self.index.find(rel).ok_or_else(|| Error::format("romfs", format!("no file {rel}")))?;
        Ok(format!("{} {} {} {} {}", self.identity, e.offset, e.size, e.stored, e.codec))
    }

    fn has(&self, rel: &str) -> bool {
        self.index.find(rel).is_some()
    }
}

struct Args {
    index: Option<PathBuf>,
    romfs: Option<PathBuf>,
    repo: PathBuf,
    out: PathBuf,
    leave_out: Vec<String>,
    jobs: usize,
}

fn parse(args: &[String]) -> std::result::Result<Args, String> {
    let (mut index, mut romfs, mut repo, mut out) = (None, None, None, None);
    let (mut leave_out, mut jobs) = (Vec::new(), 1usize);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let v = it.next().ok_or_else(|| format!("{a} needs a value"))?;
        match a.as_str() {
            "--index" => index = Some(PathBuf::from(v)),
            "--romfs" => romfs = Some(PathBuf::from(v)),
            "--repo" => repo = Some(PathBuf::from(v)),
            "--out" => out = Some(PathBuf::from(v)),
            "--leave-out" => leave_out = v.split(',').filter(|s| !s.is_empty()).map(str::to_owned).collect(),
            "--jobs" => jobs = v.parse().map_err(|_| format!("--jobs: not a number: {v}"))?,
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    if index.is_some() == romfs.is_some() {
        return Err("one of --index and --romfs is needed".into());
    }
    match (repo, out) {
        (Some(repo), Some(out)) => Ok(Args { index, romfs, repo, out, leave_out, jobs: jobs.max(1) }),
        _ => Err("--repo and --out are each needed".into()),
    }
}

fn open(a: &Args) -> Result<Box<dyn Romfs>> {
    if let Some(dir) = &a.index {
        let idx = dir.join(INDEX_FILE);
        let head = std::fs::read(&idx).map_err(|e| Error::io(&idx, e))?;
        let identity = build_core::key::sha1_hex(&head[..head.len().min(0x40)]);
        return Ok(Box::new(Indexed { index: Index::open(&idx)?, identity }));
    }
    Ok(Box::new(Folder::new(a.romfs.as_deref().unwrap_or(Path::new(".")))?))
}

fn write(path: &Path, data: &[u8]) -> Result<()> {
    let new = path.with_extension("new");
    std::fs::write(&new, data).map_err(|e| Error::io(&new, e))?;
    std::fs::rename(&new, path).map_err(|e| Error::io(path, e))
}

fn lists(a: &Args) -> Result<String> {
    std::fs::create_dir_all(&a.out).map_err(|e| Error::io(&a.out, e))?;
    let romfs = open(a)?;
    let found = census::census(romfs.as_ref(), a.jobs, &|_| {})?;
    for s in &found.skipped {
        crate::output::stdout(format_args!("note: {s}"));
    }
    let seed = found.bytes();
    write(&a.out.join(census::FILE), &seed)?;

    let dir = a.repo.join("assets").join("pipeline_states");
    let (mut kept, mut left) = (Vec::new(), 0usize);
    if dir.is_dir() {
        for path in pipelines::sources(&dir)? {
            let what = path.display().to_string();
            let data = pipelines::load(&path, None)?;
            let recs: Vec<Vec<u8>> = pipelines::parse(&data, &what, true)?.into_iter().map(<[u8]>::to_vec).collect();
            let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            if a.leave_out.iter().any(|p| name.starts_with(p.as_str())) {
                left += recs.len();
            } else {
                kept.push(recs);
            }
        }
    }
    let (kept, lost) = pipelines::as_v2(kept, true);
    let recs = pipelines::unique(kept);
    let n = recs.len();
    if n > 0 {
        let (data, _) = pipelines::pack(recs);
        write(&a.out.join(pipelines::SHIPPED), &data)?;
    }
    Ok(format!(
        "shader lists: {} ({} MB, {} programs), {} ({n} pipelines; {} left out)",
        census::FILE,
        seed.len() >> 20,
        found.programs.len(),
        pipelines::SHIPPED,
        left + lost
    ))
}

/// Runs the command on its arguments (after `console-lists`).
pub fn run(args: &[String]) -> ExitCode {
    let args = match parse(args) {
        Ok(a) => a,
        Err(e) => {
            crate::output::error(format_args!("longshot-builder dev console-lists: {e}\n{USAGE}"));
            return ExitCode::from(2);
        }
    };
    match lists(&args) {
        Ok(line) => {
            crate::output::stdout(format_args!("{line}"));
            ExitCode::SUCCESS
        }
        Err(e) => {
            crate::output::error(format_args!("longshot-builder dev console-lists: {e}"));
            ExitCode::FAILURE
        }
    }
}
