#!/usr/bin/env bash
# The PS5 RADV's diagnostics this title links: EglantinePS5's stall dump (tools/patch-radv-series lead-mesh-01..04 there).
# When a submission stops making progress the winsys writes what it carried (its draws, dispatches, waits, the packets
# around where the GPU stopped) to the kernel log; gpu/vk/vk_replay.cpp's hang watch asks for it 10 s into a stall
# (eden_radv_stall_dump), before the game ends at RX_VK_GPU_HANG_SEC.
#
# PS5_Vulkan's tools/build-radv.sh builds from .deps/work/radv-src, a git archive of PS5_Mesa that is not a repository:
# these are applied to it in place (patch, not git apply, which skips paths under the submodule's own tree), then the
# release archive is rebuilt and put where ps5/build.py links it. Run after build_kit.sh; a second run changes nothing.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
vulkan=${PS5_VULKAN:-$here/../deps/PS5_Vulkan}
work=$vulkan/.deps/work
src=$work/radv-src
archive=$vulkan/.deps/native/radv-release/lib/libvulkan_radeon.ps5.a
[[ -d $src && -f $work/radv-build-ps5-release/build.ninja ]] || { echo "no RADV build at $work: run ps5/builder/build_kit.sh" >&2; exit 2; }
changed=0
# (the patches applied, by name, kept in the source tree: a later patch edits around an earlier one, and patch's own
# "previously applied" check then fails for the earlier)
applied=$src/.longshot-radv-patches
touch "$applied"
for p in "$here"/*.patch; do
    if grep -qxF "$(basename "$p")" "$applied"; then
        echo "radv-patches: $(basename "$p") already in"
        continue
    fi
    out=$(patch -d "$src" -p1 --forward --dry-run < "$p" 2>&1) && ok=1 || ok=0
    if [[ $ok == 1 ]]; then
        patch -d "$src" -p1 --forward -s < "$p"
        echo "radv-patches: applied $(basename "$p")"
        basename "$p" >> "$applied"
        changed=1
    elif grep -q 'previously applied' <<< "$out" && ! grep -q -E 'FAILED|malformed' <<< "$(grep -v 'previously applied' <<< "$out")"; then
        echo "radv-patches: $(basename "$p") already in"
        basename "$p" >> "$applied"
    else
        echo "radv-patches: $(basename "$p") does not apply to $src" >&2
        exit 1
    fi
done
# (ninja every time: it rebuilds only what changed, here or in radv-src by other means, such as
# ~/longshot/ps5/scripts/radv_eg_port.py's memory fixes; the archive is replaced when the built one is newer)
export PATH="$work/radv-clc-bin:$HOME/.local/bin:$PATH"
ninja -C "$work/radv-build-ps5-release" src/amd/vulkan/libvulkan_radeon.a > "$work/radv-build-ps5-release.patches.log" 2>&1 ||
    { tail -40 "$work/radv-build-ps5-release.patches.log" >&2; exit 1; }
built=$work/radv-build-ps5-release/src/amd/vulkan/libvulkan_radeon.a
if [[ $changed == 1 || ! -f $archive || $built -nt $archive ]]; then
    cp "$built" "$archive"
    echo "radv-patches: rebuilt $archive"
else
    echo "radv-patches: $archive up to date"
fi
