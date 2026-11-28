#!/usr/bin/env bash
# The game's Rust (longshot-rs, game/crates) built for the console: work/rust/x86_64-unknown-freebsd/game/liblongshot_rs.a,
# which ps5/build.py links (PS5_RUST_LIB names another) and ps5/builder/make_kit.py puts in the kit.
#
#   bash ps5/tools/build-rust.sh
#
# The console's title has no thread-local storage segment (the SDK's link takes none: -femulated-tls for the C and C++),
# so Rust's standard library is built here from its sources with the same model, -Z tls-model=emulated, instead of the
# one rustup ships for FreeBSD (whose thread locals are the linker's). That takes the toolchain's rust-src component
# (rustup component add rust-src) and RUSTC_BOOTSTRAP=1, which lets a stable compiler take -Z flags. host/ps5/rust_std_ps5.c
# has the three libc calls the standard library asks for that no module a title loads exports.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
command -v cargo > /dev/null || { echo "build-rust: cargo was not found (Rust, from https://rustup.rs)" >&2; exit 1; }
RUSTC_BOOTSTRAP=1 RUSTFLAGS="-Z tls-model=emulated" cargo build --profile game -p longshot-rs \
    --target x86_64-unknown-freebsd -Z build-std=std,panic_abort --target-dir "${PS5_RUST_TARGET_DIR:-$root/work/rust}"
lib="${PS5_RUST_TARGET_DIR:-$root/work/rust}/x86_64-unknown-freebsd/game/liblongshot_rs.a"
[[ -f $lib ]] || { echo "build-rust: cargo left no $lib" >&2; exit 1; }
echo "build-rust: $lib ($(( $(stat -c %s "$lib") >> 20 )) MB)"
