"""On the console frame generation steps down with the free memory, and off gives its images back.

Its images stayed for good: the menu's Off stopped the frames and kept about 250 MiB at 3840x2160, and nothing let go of
them or of the generator when the console ran out of memory. gpu/vk/fg_by.h level_next and level_held are the steps (the
row's generator, the renderer's own, none) and their hysteresis; compiled here alone.

python -I -B tests/test_ps5_framegen_memory.py [--cxx g++]
"""
import argparse
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"


def compiled(fixture, cxx):
    with tempfile.TemporaryDirectory(prefix="fg-by-") as tmp:
        cpp = Path(tmp) / "fixture.cpp"
        cpp.write_text(fixture, encoding="utf-8")
        for define in ([], ["-D__PROSPERO__"]):
            subprocess.run([cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-fsyntax-only", "-I", str(VK)] + define +
                           [str(cpp)], check=True)


def checked(checks, passed):
    bad = [why for ok, why in checks if not ok]
    if bad:
        raise SystemExit("FAIL: " + "; ".join(bad))
    print("PASS: " + passed)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default="g++")
    a = p.parse_args()
    framegen = (VK / "vk_framegen.cpp").read_text(encoding="utf-8")
    menu = (VK / "menu" / "framegen.cpp").read_text(encoding="utf-8")   # (the rows' texts: a module since upstream v3.1.0)
    compiled(r"""#include "fg_by.h"
using namespace mkwvk::fg_by;
static_assert(level_next(0, 1024, 1024) == 0 && level_next(0, 1023, 1024) == 1, "down to the light one under the line");
static_assert(level_next(1, 512, 1024) == 1 && level_next(1, 511, 1024) == 2, "down to none under half the line");
static_assert(level_next(0, 100, 1024) == 2, "very short at once: none, not a step at a time");
static_assert(level_next(2, 1535, 1024) == 2 && level_next(2, 1536, 1024) == 1, "up from none at one and a half times the line");
static_assert(level_next(2, 9999, 1024) == 1, "up a step at a time");
static_assert(level_next(1, 2047, 1024) == 1 && level_next(1, 2048, 1024) == 0, "up to the row's generator at twice the line");
// (no going back and forth: what a step up takes again leaves it above the line it would go down at)
static_assert(level_next(1, 1536 - 256, 1024) == 1, "the frames' images made again (256 MiB at 3840x2160) do not turn it off again");
static_assert(level_next(0, 2048 - 130, 1024) == 0, "a generator made again does not make it light again");
constexpr int held(int level, unsigned long long room, int times) {
    int ups = 0;
    for (int i = 0; i < times; i++) level = level_held(level, room, 1024, &ups);
    return level;
}
static_assert(held(2, 4000, kUpHold - 1) == 2 && held(2, 4000, kUpHold) == 1, "a step up only once asked kUpHold times running");
static_assert(held(0, 100, 1) == 2, "a step down at once");
constexpr int swing() {   // (Hyrule Castle: the free memory between 460 and 1130 MiB every few askings)
    int level = 2, ups = 0, changes = 0;
    for (int i = 0; i < 200; i++) {
        const int n = level_held(level, i % 6 < 3 ? 1130 : 460, 1024, &ups);
        changes += n != level, level = n;
    }
    return changes;
}
static_assert(swing() == 0, "memory going up and down does not turn the frames on and off");
int main() { return 0; }
""", a.cxx)
    checked([
        ("fg_by::level_held(level, room, line, &ups)" in framegen, "frame generation does not step down by the memory left"),
        ("release_when_off()" in framegen, "frame generation off keeps its images"),
        ("if (by_short) by = fg_by::kBuiltIn;" in framegen, "memory short does not leave the frames to the renderer's own"),
        ("framegen_memory_level() >= 2" in menu and "framegen_by_short()" in menu, "the menu does not say that memory stepped it down"),
    ], "the console's frame generation by the memory left: three steps, a wait before a step up, off lets its images go")


if __name__ == "__main__":
    main()
