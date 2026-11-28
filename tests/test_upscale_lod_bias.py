"""Texture mip LOD bias rules for temporal upscalers (gpu/vk/upscaler_rows.h upscale_lod_bias_for).
Verifies that FSR 3.1, FSR 4, FSR 4 Native AA and DRS Auto (share == 0) apply the -1.0 mip LOD bias
offset matching AMD's FSR 3.1 / FSR 4 specification (log2(render / output) - 1.0), while respecting
RX_VK_DLSS_LOD_OFFSET and preserving XeSS, Game and Off behaviour. No game or GPU required.

python -I -B tests/test_upscale_lod_bias.py [--cxx g++]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from srccut import block  # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "gpu/vk/upscaler_rows.h"
#include <cmath>
#include <cstdio>

namespace ur = mkwvk::upscaler_rows;

static int failed = 0;
#define CHECK(what, cond) do { if (!(cond)) { std::printf("FAIL %s (line %d)\n", what, __LINE__); failed++; } } while (0)

static bool near(float a, float b) { return std::fabs(a - b) < 1e-3f; }

int main() {
    const auto expected = [](float r, float offset) { return std::fmin(0.0f, std::log2(r / 0.75f) + offset); };

    // DLSS, FSR 3.1 and FSR 4 share log2(render / output) + offset (default -1.0) on top of the game's -0.5 at 3/4.
    for (int up : {ur::DLSS, ur::FSR3, ur::FSR4}) {
        for (int share : {3333, 5000, 5833, 6667, 7500, 9167, 10000}) {
            const float r = share / 10000.0f;
            CHECK("fixed share -1.0 offset", near(ur::upscale_lod_bias_for(up, share, -1.0f), expected(r, -1.0f)));
            CHECK("fixed share -0.5 offset", near(ur::upscale_lod_bias_for(up, share, -0.5f), expected(r, -0.5f)));
            CHECK("fixed share 0.0 offset", near(ur::upscale_lod_bias_for(up, share, 0.0f), expected(r, 0.0f)));
            CHECK("never positive", ur::upscale_lod_bias_for(up, share, 0.0f) <= 0.0f &&
                                        ur::upscale_lod_bias_for(up, share, -1.0f) < 0.0f);
        }
        // Specific spec values at -1.0 offset (Quality 67%, Performance 50%, Ultra Performance 33%, Native 100%)
        CHECK("Quality (67%) bias", near(ur::upscale_lod_bias_for(up, 6667, -1.0f), -1.1699f));
        CHECK("Performance (50%) bias", near(ur::upscale_lod_bias_for(up, 5000, -1.0f), -1.5850f));
        CHECK("Ultra Performance (33%) bias", near(ur::upscale_lod_bias_for(up, 3333, -1.0f), -2.1700f));
        CHECK("Native (100%) bias", near(ur::upscale_lod_bias_for(up, 10000, -1.0f), -0.5850f));

        // DRS Auto (share == 0, and defensive share < 0 fallback to 0.75)
        CHECK("DRS Auto (share == 0) at -1.0 offset", near(ur::upscale_lod_bias_for(up, 0, -1.0f), -1.0f));
        CHECK("DRS Auto (share == 0) at -0.5 offset", near(ur::upscale_lod_bias_for(up, 0, -0.5f), -0.5f));
        CHECK("DRS Auto (share == 0) at 0.0 offset", ur::upscale_lod_bias_for(up, 0, 0.0f) == 0.0f);
        CHECK("negative share falls back to 0.75", near(ur::upscale_lod_bias_for(up, -1, -1.0f), -1.0f));

        // Monotonic: smaller render share -> more negative LOD bias
        CHECK("monotonic with share",
              ur::upscale_lod_bias_for(up, 3333, -1.0f) < ur::upscale_lod_bias_for(up, 5000, -1.0f) &&
                  ur::upscale_lod_bias_for(up, 5000, -1.0f) < ur::upscale_lod_bias_for(up, 6667, -1.0f) &&
                  ur::upscale_lod_bias_for(up, 6667, -1.0f) < ur::upscale_lod_bias_for(up, 7500, -1.0f) &&
                  ur::upscale_lod_bias_for(up, 7500, -1.0f) < ur::upscale_lod_bias_for(up, 10000, -1.0f));
    }

    // Native AA modes (DLAA, FSR 4 Native AA): full-scale (r = 1.0) regardless of saved share
    for (int up : {ur::DLAA, ur::FSR4_AA}) {
        CHECK("native AA at -1.0 offset",
              near(ur::upscale_lod_bias_for(up, 0, -1.0f), expected(1.0f, -1.0f)) &&
                  near(ur::upscale_lod_bias_for(up, 6667, -1.0f), expected(1.0f, -1.0f)));
        CHECK("native AA at 0.0 offset",
              ur::upscale_lod_bias_for(up, 0, 0.0f) == 0.0f &&
                  ur::upscale_lod_bias_for(up, 6667, 0.0f) == 0.0f);
    }

    // XeSS keeps its guide's log2(render / output) without the -1.0 offset; XeSS Native AA stays 0
    CHECK("XeSS without -1.0 offset",
          near(ur::upscale_lod_bias_for(ur::XESS, 5000, -1.0f), std::log2(5000.0f / 7500.0f)) &&
              ur::upscale_lod_bias_for(ur::XESS, 0, -1.0f) == 0.0f &&
              ur::upscale_lod_bias_for(ur::XESS, 10000, -1.0f) == 0.0f &&
              ur::upscale_lod_bias_for(ur::XESS_AA, 0, -1.0f) == 0.0f);

    // Game's own upscaler and Off add no bias
    CHECK("Game and Off add no bias",
          ur::upscale_lod_bias_for(ur::GAME, 6667, -1.0f) == 0.0f &&
              ur::upscale_lod_bias_for(ur::GAME, 0, -1.0f) == 0.0f &&
              ur::upscale_lod_bias_for(ur::OFF, 5000, -1.0f) == 0.0f &&
              ur::upscale_lod_bias_for(ur::OFF, 0, -1.0f) == 0.0f);

    if (failed) return 1;
    std::puts("upscale lod bias: all checks passed");
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default=os.environ.get("CXX") or shutil.which("clang++") or
                        shutil.which("g++") or shutil.which("c++") or "c++")
    args = parser.parse_args()
    draw = (ROOT / "gpu" / "vk" / "vk_draw.cpp").read_text(encoding="utf-8")
    bias_fn = block(draw, "float upscale_lod_bias() {")
    for need in ("RX_VK_UPSCALE_LOD_BIAS", "RX_VK_DLSS_LOD_OFFSET",
                 "upscaler_rows::upscale_lod_bias_for(up, resolution_scale(), offset)"):
        if need not in bias_fn:
            raise SystemExit("vk_draw.cpp upscale_lod_bias: missing %s" % need)
    with tempfile.TemporaryDirectory(prefix="upscale-lod-bias-") as tmp:
        src = Path(tmp) / "upscale_lod_bias_test.cpp"
        src.write_text(PROGRAM, encoding="utf-8")
        exe = Path(tmp) / ("upscale-lod-bias.exe" if os.name == "nt" else "upscale-lod-bias")
        subprocess.run(
            [args.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT), str(src), "-o", str(exe)],
            check=True,
        )
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
