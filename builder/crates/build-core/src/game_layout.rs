//! Installed game files. Flat folders remain readable by developer tools and older releases.
use std::path::{Path, PathBuf};

/// A new build carries its game image in data/. Development and earlier builds keep it at the root.
pub fn organized(game: &Path) -> bool {
    game.join("data/image.bin").is_file()
}

/// Resolve a packaged asset for a game or a flat preparation workspace.
pub fn data(game: &Path) -> PathBuf {
    if organized(game) { game.join("data") } else { game.to_owned() }
}

/// Logs and reports belonging to this run.
pub fn diagnostics(game: &Path) -> PathBuf {
    if organized(game) { game.join("diagnostics") } else { game.to_owned() }
}

/// Per-build defaults. Player preferences live in the profile.
pub fn config(game: &Path) -> PathBuf {
    if organized(game) { game.join("config") } else { game.to_owned() }
}
