use build_core::report::{Event, Reporter, Stage};
use build_core::{Error, IoContext, Result};
use build_driver::GameSource;
use game_files::nup::{self, Nup, Part};
use std::{
    fs, io,
    path::{Path, PathBuf},
};

pub(crate) fn run(game: &GameSource, out: &Path, report: &Reporter) -> Result<()> {
    let started = std::time::Instant::now();
    let out = out.canonicalize().at(out)?;
    let dest = out.join("romfs");
    report.stage(Stage::Read, "Extracting game RomFS…");
    match game {
        GameSource::Nup(path) => {
            let nup = Nup::open(path)?;
            let title = nup.pick_title(None)?;
            let total =
                nup.part(title, Part::Romfs).iter().fold(0u64, |sum, (_, i)| sum.saturating_add(nup.files[*i].size));
            fs::create_dir(&dest).at(&dest)?;
            let extracted = nup::extract(&nup, title, &out, Some(Part::Romfs), &mut progress(report, total))?;
            if !extracted.damaged.is_empty() {
                report.warn(
                    "romfs_files_damaged",
                    format!("{} RomFS files in the original package are damaged.", extracted.damaged.len()),
                );
            }
        }
        GameSource::Folder { romfs, .. } => copy_dump(romfs, &out, &dest, report)?,
    }
    report.progress(1.0);
    report.send(Event::Done {
        folder: dest.display().to_string(),
        seconds: started.elapsed().as_secs_f64(),
        compile_seconds: 0.0,
        previous_seconds: None,
    });
    Ok(())
}

fn progress(report: &Reporter, total: u64) -> impl FnMut(u64) + '_ {
    let mut done = 0u64;
    let mut percent = -1;
    move |bytes| {
        done = done.saturating_add(bytes);
        let fraction = done.min(total) as f64 / total.max(1) as f64;
        let next = (fraction * 100.0) as i32;
        if next != percent {
            report.progress(fraction);
            percent = next;
        }
    }
}

fn copy_dump(source: &Path, out: &Path, dest: &Path, report: &Reporter) -> Result<()> {
    let source = source.canonicalize().at(source)?;
    if out.starts_with(&source) {
        return Err(Error::player("game_files_overlap", "Choose an output folder outside the original RomFS."));
    }
    let mut dirs = vec![PathBuf::new()];
    let mut entries = Vec::new();
    let mut total = 0u64;
    let mut index = 0;
    while index < dirs.len() {
        let relative = dirs[index].clone();
        index += 1;
        let from = source.join(&relative);
        for entry in fs::read_dir(&from).at(&from)? {
            let entry = entry.at(&from)?;
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path).at(&path)?;
            #[cfg(windows)]
            let linked = {
                use std::os::windows::fs::MetadataExt;
                metadata.file_attributes() & 0x400 != 0
            };
            #[cfg(not(windows))]
            let linked = metadata.is_symlink();
            if linked || !(metadata.is_dir() || metadata.is_file()) {
                return Err(Error::player("dump_has_links", "The original RomFS contains a link or a special file."));
            }
            let rel = relative.join(entry.file_name());
            if metadata.is_dir() {
                dirs.push(rel);
            } else {
                total = total.saturating_add(metadata.len());
                entries.push(rel);
            }
        }
    }
    fs::create_dir(dest).at(dest)?;
    for rel in dirs.into_iter().skip(1) {
        let path = dest.join(rel);
        fs::create_dir(&path).at(&path)?;
    }
    let mut advance = progress(report, total);
    let mut buffer = vec![0; 1 << 20];
    for rel in entries {
        use io::{Read as _, Write as _};
        let from = source.join(&rel);
        let to = dest.join(rel);
        let mut input = fs::File::open(&from).at(&from)?;
        let mut output = fs::File::create_new(&to).at(&to)?;
        loop {
            let read = input.read(&mut buffer).at(&from)?;
            if read == 0 {
                break;
            }
            output.write_all(&buffer[..read]).at(&to)?;
            advance(read as u64);
        }
    }
    Ok(())
}
