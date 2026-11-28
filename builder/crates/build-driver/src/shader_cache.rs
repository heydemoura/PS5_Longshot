//! Find a shared pipeline cache after its SPIR-V store moves. Older builders included
//! the store's pathname in the driver fingerprint; leave their folders in place.
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

const LEGACY_STORE: &str = "longshot_legacy_shader_store.txt";

fn opened(path: &Path, magic: &[u8; 4]) -> Option<(File, [u8; 16])> {
    if !path.metadata().ok()?.is_file() {
        return None;
    }
    let mut bytes = [0; 16];
    let mut file = File::open(path).ok()?;
    file.read_exact(&mut bytes).ok()?;
    (&bytes[..4] == magic).then_some((file, bytes))
}

fn store_dir(get: &impl Fn(&str) -> Option<String>) -> Option<PathBuf> {
    if let Some(value) = get("LONGSHOT_SHADER_STORE").filter(|v| !v.is_empty()) {
        return (value != "0").then(|| PathBuf::from(value));
    }
    if cfg!(windows) {
        return get("LOCALAPPDATA").filter(|v| !v.is_empty()).map(|p| Path::new(&p).join("Longshot/shaders"));
    }
    get("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| get("HOME").map(PathBuf::from).filter(|p| p.is_absolute()).map(|p| p.join(".cache")))
        .map(|p| p.join("longshot/shaders"))
}

/// Minimum pack/index lengths a preparation saw. The renderer appends both files;
/// restoring a shorter copy of the same generation must invalidate its receipt.
pub(super) fn store_bounds(cache: &Path, get: impl Fn(&str) -> Option<String>) -> Option<(u64, u64)> {
    let (programs, head) = opened(&cache.join("longshot_programs.bin"), b"HSP1")?;
    if programs.metadata().ok()?.len() <= 16 {
        return None;
    }
    let id = &head[4..8];
    if id == [0; 4] {
        return Some((0, 0));
    } // SPIR-V embedded in each program.
    let dir = store_dir(&get)?;
    let store = |name: &str, magic| {
        let (file, h) = opened(&dir.join(name), magic)?;
        (h[4..8] == 1u32.to_le_bytes() && &h[8..12] == id).then_some(file)
    };
    let mut pack = store("spirv.pack", b"LSPK")?;
    let mut index = store("spirv.idx", b"LSPI")?;
    let index_len = index.metadata().ok()?.len();
    let pack_len = pack.metadata().ok()?.len();
    if index_len < 48 || !(index_len - 16).is_multiple_of(32) || pack_len < 44 || !pack_len.is_multiple_of(4) {
        return None;
    }
    // Each index entry points past its 24-byte blob header. Check the last
    // complete append, including its recorded hashes and SPIR-V word count.
    let mut entry = [0u8; 32];
    index.seek(SeekFrom::Start(index_len - 32)).ok()?;
    index.read_exact(&mut entry).ok()?;
    let offset = u64::from_le_bytes(entry[16..24].try_into().ok()?);
    let words = u64::from_le_bytes(entry[24..32].try_into().ok()?);
    if offset < 40 || words == 0 || offset.checked_add(words.checked_mul(4)?)? > pack_len {
        return None;
    }
    let mut blob = [0u8; 24];
    pack.seek(SeekFrom::Start(offset - 24)).ok()?;
    pack.read_exact(&mut blob).ok()?;
    (blob[..16] == entry[..16] && blob[16..] == entry[24..]).then_some((pack_len, index_len))
}

pub(super) fn store_matches(cache: &Path, get: impl Fn(&str) -> Option<String>) -> bool {
    store_bounds(cache, get).is_some()
}

// The provenance file is only a hint. It never supplies a path to open: recompute the
// old builder's hash for the actual GPU/driver and compare it to the candidate's name.
fn old_driver(cache: &Path, driver: &str, store: &str) -> Option<String> {
    let legacy = format!("{driver}LONGSHOT_SHADER_STORE={store}\n");
    (cache.file_name()?.to_str()? == &build_core::key::sha1_hex(legacy.as_bytes())[..16]).then_some(legacy)
}

fn recorded_store(cache: &Path) -> Option<String> {
    let marker = cache.join(LEGACY_STORE);
    let meta = marker.symlink_metadata().ok()?;
    if !meta.is_file() || meta.len() > 32768 {
        return None;
    }
    let mut old = String::new();
    File::open(marker).ok()?.take(32769).read_to_string(&mut old).ok()?;
    (old.len() <= 32768).then_some(old)
}

fn legacy_store(cache: &Path, driver: &str, get: &impl Fn(&str) -> Option<String>) -> Option<String> {
    let matches = |s: &String| s != "0" && !s.is_empty() && old_driver(cache, driver, s).is_some();
    get("LONGSHOT_SHADER_STORE")
        .filter(matches)
        .or_else(|| recorded_store(cache).filter(matches))
        // Older builders only kept a hash of the original path and driver. Hash candidate
        // locations; never open the old path or accept a matching SPIR-V generation alone.
        .or_else(|| get("LONGSHOT_PREVIOUS_SHADER_STORE").filter(matches))
        .or_else(|| {
            store_dir(&|k| (k != "LONGSHOT_SHADER_STORE").then(|| get(k)).flatten())?
                .to_str()
                .map(str::to_owned)
                .filter(matches)
        })
}

pub(super) fn legacy_driver(cache: &Path, driver: &str, get: impl Fn(&str) -> Option<String>) -> Option<String> {
    let store = legacy_store(cache, driver, &get)?;
    old_driver(cache, driver, &store)
}

pub(super) fn remember_legacy(cache: &Path, driver: &str, get: impl Fn(&str) -> Option<String>) {
    if let Some(store) = legacy_store(cache, driver, &get)
        && store_matches(cache, &get)
    {
        let _ = build_core::fsx::write_if_changed(&cache.join(LEGACY_STORE), store.as_bytes());
    }
}

/// The shared shader cache for this translator and GPU, including a compatible
/// cache created by a builder that included the store location in its key.
pub fn cache_for(cache: &Path, translator: &str, get: impl Fn(&str) -> Option<String>) -> Option<PathBuf> {
    resolve(cache, translator, &super::driver_id(&get)?, &get)
}

fn resolve(cache: &Path, translator: &str, driver: &str, get: &impl Fn(&str) -> Option<String>) -> Option<PathBuf> {
    let canonical = super::cache_dir(cache, translator, driver)?;
    let parent = canonical.parent()?;
    let legacy = get("LONGSHOT_SHADER_STORE")
        .filter(|s| s != "0")
        .and_then(|store| super::cache_dir(cache, translator, &format!("{driver}LONGSHOT_SHADER_STORE={store}\n")));
    if let Some(old) = &legacy
        && old.symlink_metadata().is_ok_and(|m| m.is_dir())
        && store_matches(old, get)
    {
        return legacy;
    }
    if store_matches(&canonical, get) {
        return Some(canonical);
    }
    if let Ok(entries) = fs::read_dir(parent) {
        for entry in entries.flatten() {
            let dir = entry.path();
            if entry.file_type().is_ok_and(|t| t.is_dir())
                && legacy_driver(&dir, driver, get).is_some()
                && store_matches(&dir, get)
            {
                return Some(dir);
            }
        }
    }
    // A different, populated store can still be in use by another installation.
    if canonical.join("longshot_programs.bin").exists() && legacy.is_some() {
        return legacy;
    }
    Some(canonical)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(dir: &Path, name: &str, magic: &[u8; 4], a: u32, b: u32) {
        fs::create_dir_all(dir).unwrap();
        let mut head = magic.to_vec();
        head.extend(a.to_le_bytes());
        head.extend(b.to_le_bytes());
        head.extend([0; 4]);
        match name {
            "spirv.pack" | "spirv.idx" => {
                head.extend([0xaa; 16]);
                if name == "spirv.idx" {
                    head.extend(40u64.to_le_bytes());
                }
                head.extend(1u64.to_le_bytes());
                if name == "spirv.pack" {
                    head.extend(7u32.to_le_bytes());
                }
            }
            _ => head.push(1),
        }
        fs::write(dir.join(name), head).unwrap();
    }

    fn store(dir: &Path, id: u32) {
        file(dir, "spirv.pack", b"LSPK", 1, id);
        file(dir, "spirv.idx", b"LSPI", 1, id);
    }

    #[test]
    fn moved_store_keeps_a_verified_legacy_driver_cache() {
        let root = crate::scratch("shader-cache-store-relocation");
        let cache = root.join("builder");
        let original = root.join("original-store");
        let moved = root.join("moved-store");
        let get = |p: &Path, k: &str| (k == "LONGSHOT_SHADER_STORE").then(|| p.display().to_string());
        let old = super::super::cache_dir(
            &cache,
            "translator",
            &format!("gpu\nLONGSHOT_SHADER_STORE={}\n", original.display()),
        )
        .unwrap();
        let canonical = super::super::cache_dir(&cache, "translator", "gpu\n").unwrap();
        store(&original, 42);
        file(&old, "longshot_programs.bin", b"HSP1", 42, 1);
        let env = |k: &str| get(&original, k);
        assert_eq!(resolve(&cache, "translator", "gpu\n", &env), Some(old.clone()));
        remember_legacy(&old, "gpu\n", env);
        fs::rename(&original, &moved).unwrap();
        let env = |k: &str| get(&moved, k);
        assert_eq!(resolve(&cache, "translator", "gpu\n", &env), Some(old.clone()));
        assert!(legacy_driver(&old, "gpu\n", env).is_some());
        assert!(legacy_driver(&old, "other GPU\n", env).is_none());
        file(&moved, "spirv.idx", b"LSPI", 1, 77);
        assert_eq!(resolve(&cache, "translator", "gpu\n", &env), Some(canonical.clone()));
        store(&moved, 77);
        assert!(!store_matches(&old, env));
        store(&moved, 42);
        fs::write(old.join(LEGACY_STORE), b"unrelated-location").unwrap();
        assert_eq!(resolve(&cache, "translator", "gpu\n", &env), Some(canonical));
    }

    #[test]
    fn move_before_upgrading_can_reuse_an_unmarked_custom_store_cache() {
        let root = crate::scratch("shader-cache-pre-upgrade-move");
        let cache = root.join("builder");
        let original = root.join("custom-store");
        let moved = root.join("relocated");
        let driver = "gpu and driver\n";
        let legacy = format!("{driver}LONGSHOT_SHADER_STORE={}\n", original.display());
        let old = super::super::cache_dir(&cache, "translator", &legacy).unwrap();
        let env = |k: &str| (k == "LONGSHOT_SHADER_STORE").then(|| moved.display().to_string());
        let hint = |k: &str| match k {
            "LONGSHOT_PREVIOUS_SHADER_STORE" => Some(original.display().to_string()),
            _ => env(k),
        };
        store(&original, 42);
        file(&old, "longshot_programs.bin", b"HSP1", 42, 1);
        fs::rename(&original, &moved).unwrap(); // The old builder has never written a marker.
        assert!(!old.join(LEGACY_STORE).exists());
        assert_ne!(resolve(&cache, "translator", driver, &env), Some(old.clone()));
        assert_ne!(resolve(&cache, "translator", "another GPU\n", &hint), Some(old.clone()));
        store(&moved, 7);
        assert_ne!(resolve(&cache, "translator", driver, &hint), Some(old.clone()));
        store(&moved, 42);
        assert_eq!(resolve(&cache, "translator", driver, &hint), Some(old.clone()));
        assert_eq!(legacy_driver(&old, driver, hint), Some(legacy));
        remember_legacy(&old, driver, hint);
        assert_eq!(resolve(&cache, "translator", driver, &env), Some(old));
    }

    #[test]
    fn truncated_or_inconsistent_store_does_not_validate_the_program_cache() {
        let root = crate::scratch("shader-store-bounds");
        let dir = root.join("pack");
        let get = |k: &str| (k == "LONGSHOT_SHADER_STORE").then(|| dir.display().to_string());
        file(&root, "longshot_programs.bin", b"HSP1", 7, 0);
        for (name, len) in [("spirv.pack", 16), ("spirv.idx", 16), ("spirv.idx", 47), ("spirv.pack", 40)] {
            store(&dir, 7);
            assert_eq!(store_bounds(&root, get), Some((44, 48)));
            fs::OpenOptions::new().write(true).open(dir.join(name)).unwrap().set_len(len).unwrap();
            assert_eq!(store_bounds(&root, get), None);
        }
        store(&dir, 7);
        let idx = dir.join("spirv.idx");
        let mut entry = fs::read(&idx).unwrap();
        entry[32..40].copy_from_slice(&u64::MAX.to_le_bytes());
        fs::write(&idx, entry).unwrap();
        assert_eq!(store_bounds(&root, get), None);
        store(&dir, 7);
        let mut entry = fs::read(&idx).unwrap();
        entry[16] ^= 8;
        fs::write(idx, entry).unwrap();
        assert_eq!(store_bounds(&root, get), None);
    }

    #[test]
    fn inline_programs_need_no_external_store() {
        let cache = crate::scratch("shader-cache-inline");
        file(&cache, "longshot_programs.bin", b"HSP1", 0, 1);
        assert!(store_matches(&cache, |_| None));
        file(&cache, "longshot_programs.bin", b"bad!", 0, 1);
        assert!(!store_matches(&cache, |_| None));
    }
}
