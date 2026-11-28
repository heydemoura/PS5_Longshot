"""The PS5 build turns the GPU pages' bitmap on, with the page marks' table cut to the console's guest window.

- nx2/rtgen.py's A64_DIRTY_PAGES can be given by the build (#ifndef), and is the 512 GB window's elsewhere;
- ps5/build.py gives the lifted game and the host the same A64_DIRTY_PAGES, and it covers HOS_GUEST_SPACE's pages exactly
  (a smaller table would put two pages on one mark, a larger one wastes the writable segment);
- the lifted game is built with A64_MARK_BITMAP unless RX_MARK_BITMAP=0;
- host/hos_mem.c refuses to compile with a table smaller than the window;
- a store's page index stays within the table for every address of the window, and two pages never share one.

python -I -B tests/test_ps5_mark_bitmap.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    sys.path.insert(0, str(ROOT / "nx2"))
    import rtgen
    build = (ROOT / "ps5" / "build.py").read_text(encoding="utf-8")
    hos_h = (ROOT / "host" / "hos.h").read_text(encoding="utf-8")
    mem = (ROOT / "host" / "hos_mem.c").read_text(encoding="utf-8")
    ps5 = hos_h[hos_h.index("#ifdef __PROSPERO__\n/* (PS5) 64 GiB of the 39-bit space"):]
    space = int(re.search(r"#define HOS_GUEST_SPACE (0x[0-9A-Fa-f]+)ULL", ps5[:ps5.index("#else")]).group(1), 16)
    given = re.search(r'DIRTY_PAGES = \["-DA64_DIRTY_PAGES=\(1ull<<(\d+)\)"\]', build)
    pages = 1 << int(given.group(1)) if given else 0
    jobs = build[build.index("def jobs_for(rt64, gen_dir):"):]
    game_fl = jobs[jobs.index("game_fl = "):jobs.index("# (no lifted game")]
    host_fl = jobs[jobs.index("host_fl = "):]
    host_fl = host_fl[:host_fl.index("\n\n")]
    at = lambda a: (a >> 12) & (pages - 1)   # (rtgen's A64_DIRTY_AT)
    checks = [
        ("#ifndef A64_DIRTY_PAGES" in rtgen.ADDR64 and "#define A64_DIRTY_PAGES (1ull << 27)" in rtgen.ADDR64,
         "rtgen's A64_DIRTY_PAGES cannot be given by the build"),
        (pages == space >> 12, "ps5/build.py's A64_DIRTY_PAGES is not the guest window's pages (%d against %d)" % (pages, space >> 12)),
        ("+ DIRTY_PAGES + MARK_BITMAP" in game_fl, "the lifted game is not built with the table's size and the bitmap"),
        ("COMMON + DIRTY_PAGES + [" in host_fl, "the host is not built with the table's size"),
        ('os.environ.get("RX_MARK_BITMAP", "").strip().lower() in ("0", "off", "false", "no") else ["-DA64_MARK_BITMAP"]' in build,
         "RX_MARK_BITMAP=0 does not build without the bitmap"),
        ("_Static_assert((HOS_GUEST_SPACE >> 12) <= A64_DIRTY_PAGES" in mem, "a table smaller than the window compiles"),
        (pages > 0 and at(space - 1) == pages - 1 and at(0) == 0 and at(space - 4096) != at(space - 8192)
         and len({at(a) for a in range(0, space, space // 4096)}) == 4096, "two pages of the window share a mark"),
    ]
    bad = [why for ok, why in checks if not ok]
    if bad:
        raise SystemExit("FAIL: " + "; ".join(bad))
    print("PASS: the PS5 build marks GPU pages only, its page marks' table the size of the console's guest window")


if __name__ == "__main__":
    main()
