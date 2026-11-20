#!/usr/bin/env bash
# The payload SDK's platform layer, patched for this title (ps5/sdk-patches/*.py, applied to the pinned revision's source):
# the patched objects are compiled with the platform's own Makefile and put in place of the installed
# libps5platform.a's. Run after ps5/builder/build_kit.sh; a second run rebuilds the same objects.
#
#   heap_pieces.py   a heap segment made of several pieces of direct memory, mapped back to back, when no one piece
#                    is free that is large enough (2026-11-18: 1254 MiB free in 3616 pieces, the largest 9 MiB, and a
#                    16 MiB malloc waited for good)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
rev=$(sed -n 's/^SDK_REV=//p' "$root/ps5/builder/build_kit.sh")
src=$root/.local/projects/PS5_PayloadSDK
sdk=$root/.local/ps5-deps/ps5-payload-sdk
lib=$sdk/target/lib/libps5platform.a
[[ -f $lib && -d $src/.git ]] || { echo "sdk-patches: no SDK at $sdk or source at $src (ps5/builder/build_kit.sh)" >&2; exit 2; }
tree=$(mktemp -d)
trap 'rm -rf -- "$tree"' EXIT
git -C "$src" archive "$rev" | tar -x -C "$tree"
for p in "$here"/*.py; do python3 "$p" "$tree/platform" || { echo "sdk-patches: $(basename "$p") did not apply" >&2; exit 1; }; done
export PATH="$sdk/bin:$PATH"
make -s -C "$tree/platform" build/ps5/heap.o CC="$sdk/bin/clang"
[[ -f $lib.orig ]] || cp "$lib" "$lib.orig"
cp "$lib.orig" "$lib"
"$sdk/bin/llvm-ar" r "$lib" "$tree/platform/build/ps5/heap.o"
echo "sdk-patches: heap.o replaced in $lib"
