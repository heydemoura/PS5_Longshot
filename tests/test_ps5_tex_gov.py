"""The console's textures' governor (gpu/vk/tex_gov.h, vk_texture.cpp, RX_VK_TEX_GOV).

The line of the reserve kept textures small for a whole session over memory that was never short.

python -I -B tests/test_ps5_tex_gov.py [--cxx g++]
"""
import argparse
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r"""#include "tex_gov.h"
using namespace mkwvk::tex_gov;
constexpr unsigned long long M = 1ull << 20;
constexpr Lines G{700 * M, 1000 * M, 192 * M};
constexpr long long MS = 1 << 20;
static_assert(step(Gov{}, 1500 * M, 0, 10000, G).level == 0, "memory far from the floor and still: its own size");
static_assert(step(Gov{}, 1500 * M, 100 * MS, 10000, G).level == 0, "falling, but still above the line in two seconds: its own size");
static_assert(step(Gov{}, 1500 * M, 450 * MS, 10000, G).level == 1, "will be under the line in two seconds: a level down already");
static_assert(step(Gov{}, 1800 * M, 700 * MS, 10000, G).level == 3, "will be half way to the floor in two seconds: 128 at once");
static_assert(step(Gov{}, 2500 * M, 600 * MS, 10000, G).level == 0, "a second of the game taking 600 MiB with 2,500 free: nothing yet");
static_assert(step(Gov{1, 10000}, 650 * M, 0, 10100, G).level == 1 && step(Gov{1, 10000}, 650 * M, 0, 10300, G).level == 2, "under the line: a level each 300 ms");
static_assert(step(Gov{3, 0}, 650 * M, 0, 10000, G).level == 3, "and no further than 128 by the line alone");
static_assert(step(Gov{0, 0}, 300 * M, 0, 10000, G).level == 4, "the last quarter above the floor: 32");
static_assert(step(Gov{4, 0}, 800 * M, 0, 2999, G).level == 4 && step(Gov{4, 0}, 800 * M, 0, 3000, G).level == 3, "out of it for three seconds: 128 again");
static_assert(step(Gov{3, 0}, 999 * M, 0, 9000, G).level == 3 && step(Gov{3, 0}, 1000 * M, 0, 2999, G).level == 3, "back up only from the high line, after three seconds");
static_assert(step(Gov{3, 0}, 1000 * M, 0, 3000, G).level == 2 && step(Gov{1, 0}, 1200 * M, 0, 3000, G).level == 0, "a level at a time");
static_assert(step(Gov{2, 0}, 1200 * M, 50 * MS, 9000, G).level == 2, "not back up while it falls");
static_assert(cap_of(0) == 0 && cap_of(1) == 512 && cap_of(2) == 256 && cap_of(3) == 128 && cap_of(4) == 32, "the levels' caps");
static_assert(larger(256, 128) && larger(0, 512) && !larger(128, 128) && !larger(128, 256) && !larger(0, 0), "made again larger only above the cap it was made under");
static_assert(smaller(128, 0, 4 * M, 1) && smaller(128, 512, 1 * M, 1) && smaller(32, 128, 80 * 1024, 1), "a larger one is made again under the cap");
static_assert(!smaller(128, 128, 4 * M, 1) && !smaller(128, 0, 100 * 1024, 1) && !smaller(0, 0, 64 * M, 1) && !smaller(128, 0, 600 * 1024, 6), "not one made under it, one that fits it, or with no cap");
int main() {}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cxx", default="g++")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "gov.cpp"
        src.write_text(FIXTURE, encoding="utf-8")
        subprocess.run([args.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-fsyntax-only", "-I", str(ROOT / "gpu" / "vk"), str(src)], check=True)
    tex = (ROOT / "gpu" / "vk" / "vk_texture.cpp").read_text(encoding="utf-8")
    checks = [
        ("const uint32_t cap = gov ? ps5_gov_cap() : room < reserve / 2 ? 32 : 128;" in tex, "a new texture's cap is not the governor's with RX_VK_TEX_GOV, the line's without"),
        ("x.pressed_cap = (uint16_t)cap;" in tex, "a texture made small does not keep the cap it was made under"),
        ("gov && !tex_gov::larger(gov_cap, tex->pressed_cap)" in tex, "the governor makes a texture again at the cap it already has"),
        ("hos_ps5_direct_room() < ps5_gov_low()" in tex, "the loaded textures are made smaller on the forecast alone"),
    ]
    bad = [why for ok, why in checks if not ok]
    if bad:
        raise SystemExit("FAIL: " + "; ".join(bad))
    print("PASS: the console's textures' governor: its own size far from the floor, down by the fall, back a level at a time")


if __name__ == "__main__":
    main()
