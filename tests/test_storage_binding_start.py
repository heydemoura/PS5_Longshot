"""A handle table is read from where its storage buffer's binding starts, not from the address the game gave.

The translation counts a storage offset from the address rounded down to the binding's alignment and takes the rest of
the address into the offset. Read from the address as given, a table in a buffer that does not start on that boundary
was read that rest too far: on the PS5 (256 bytes) OoT's lights buffer starts at ..000 on one frame and ..040 on the
next, the lights' shadow maps were bound as nothing on every other frame, and the water and the window glass flickered.

python -I -B tests/test_storage_binding_start.py [--cxx g++]
"""
import argparse
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r"""#include "storage_binding.h"
using mkwvk::storage_binding_start;
// the two addresses the console logged for OoT's lights buffer, and the offsets its table's first element was read at
constexpr uint64_t even = 0x54ae56000ull, odd = 0x54ae6e040ull;
static_assert(storage_binding_start(even, 256) == even);
static_assert(storage_binding_start(odd, 256) == 0x54ae6e000ull);
// the same element of the table on both frames: 0x60 from the buffer's own start
static_assert(storage_binding_start(even, 256) + 0x60 == even + 0x60);
static_assert(storage_binding_start(odd, 256) + 0xa0 == odd + 0x60);
// at a PC's alignments these addresses have no rest: nothing changes there
static_assert(storage_binding_start(odd, 16) == odd && storage_binding_start(odd, 64) == odd);
static_assert(storage_binding_start(0x1234567fu, 16) == 0x12345670u);
// no buffer, and an alignment that is no power of two: as given
static_assert(storage_binding_start(0, 256) == 0 && storage_binding_start(odd, 0) == odd && storage_binding_start(odd, 1) == odd);
static_assert(storage_binding_start(odd, 48) == odd);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cxx", default="g++")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "start.cpp"
        src.write_text(FIXTURE, encoding="utf-8")
        subprocess.run([args.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-fsyntax-only", "-I", str(ROOT / "gpu" / "vk"), str(src)], check=True)
    read = lambda *p: ROOT.joinpath(*p).read_text(encoding="utf-8")
    shader, draw = read("gpu", "vk", "vk_shader.cpp"), read("gpu", "vk", "vk_draw.cpp")
    fn = shader[shader.index("uint64_t stage_storage_base("):]
    fn = fn[:fn.index("\n}\n")]
    checks = [
        ("return storage_binding_start(address, storage_binding_align());" in fn, "a handle program's storage buffer is read from the address as given"),
        ("h.min_ssbo_alignment = storage_binding_align();" in shader and "p.min_ssbo_alignment = storage_binding_align();" in shader, "the translation is not told the same alignment"),
        ("const uint64_t salign = storage_binding_align();" in draw, "the descriptor writer does not bind by the same alignment"),
        ("storage_binding.h" not in read("nx2", "translator_files.py"), "the pure header joined the translator's stamp"),
    ]
    bad = [why for ok, why in checks if not ok]
    for why in bad:
        print("FAIL: " + why)
    if bad:
        raise SystemExit(1)
    print("PASS: a handle table is read from where its storage buffer's binding starts")


if __name__ == "__main__":
    main()
