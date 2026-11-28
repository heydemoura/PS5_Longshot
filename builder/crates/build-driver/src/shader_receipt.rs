//! Successful shader preparations shared by installations using the same pipeline cache.
use std::{
    fs,
    path::{Path, PathBuf},
};

const RECEIPTS: &str = "longshot_preparations";

fn receipt(cache: &Path, key: &str) -> PathBuf {
    cache.join(RECEIPTS).join(key)
}

/// Fingerprint the renderer's small completion record, and the two expensive artifacts by
/// length and write time. A partial run, a cleared cache or a replacement forces another
/// preparation. Avoid reading a gigabyte of Vulkan pipelines just to open the launcher.
fn artifacts(cache: &Path) -> Option<String> {
    use build_core::key::Key;
    let mut key = Key::new();
    let prepared = cache.join("longshot_prepared.bin");
    let meta = fs::metadata(&prepared).ok()?;
    if meta.len() != 48 {
        return None;
    }
    let prepared: [u8; 48] = fs::read(prepared).ok()?.try_into().ok()?;
    // prepare_check updates the seed file time and the recording time when an identical
    // seed is copied. Its translator, seed hash and size, and driver warmup stamp survive.
    key.part("translator and seed", &prepared[..24]).part("warmup", &prepared[40..]);
    for name in ["longshot_programs.bin", "longshot_vk_pipelines.bin"] {
        let meta = fs::metadata(cache.join(name)).ok()?;
        if !meta.is_file() || meta.len() == 0 {
            return None;
        }
        let time = meta.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?;
        key.part(name, &meta.len().to_le_bytes()).part("modified", &time.as_nanos().to_le_bytes());
    }
    Some(key.hex())
}

/// Only the original installation can use an old local record, until shared receipts exist.
pub(super) fn can_adopt_legacy(cache: &Path) -> bool {
    matches!(cache.join(RECEIPTS).symlink_metadata(), Err(e) if e.kind() == std::io::ErrorKind::NotFound)
        && artifacts(cache).is_some()
}

pub(super) fn matches(cache: &Path, key: &str, (pack_len, index_len): (u64, u64)) -> bool {
    let Some(found) = fs::read_to_string(receipt(cache, key)).ok() else { return false };
    let mut fields = found.split_whitespace();
    let (Some(proof), Some(pack), Some(index), None) = (fields.next(), fields.next(), fields.next(), fields.next())
    else {
        return false;
    };
    pack.parse::<u64>().is_ok_and(|prior| pack_len >= prior)
        && index.parse::<u64>().is_ok_and(|prior| index_len >= prior)
        && artifacts(cache).is_some_and(|now| now == proof)
}

pub(super) fn publish(cache: &Path, key: &str, (pack_len, index_len): (u64, u64)) {
    if let Some(proof) = artifacts(cache) {
        let snapshot = format!("{proof} {pack_len} {index_len}\n");
        let _ = build_core::fsx::write_if_changed(&receipt(cache, key), snapshot.as_bytes());
    }
}

/// Serialize preparation and receipt publication across different instances. The early
/// worker yields to an existing preparer and retries through the ordinary post-build step.
pub(super) fn lock(cache: &Path, wait: bool) -> std::io::Result<fs::File> {
    let file = preparation_lease(cache, wait)?;
    fs::create_dir_all(cache)?;
    Ok(file)
}

// Eviction removes the driver directory. Its lock must survive that removal so a
// waiting preparer and the next preparer always acquire the same filesystem lock.
pub(crate) fn preparation_lock_path(cache: &Path) -> PathBuf {
    cache.with_added_extension("preparation.lock")
}

pub(crate) fn preparation_lease(cache: &Path, wait: bool) -> std::io::Result<fs::File> {
    if let Some(parent) = cache.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(preparation_lock_path(cache))?;
    if wait {
        file.lock()?;
    } else {
        file.try_lock()?;
    }
    Ok(file)
}
