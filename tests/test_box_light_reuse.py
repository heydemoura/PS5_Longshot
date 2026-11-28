"""A reused box light must not inherit another light's shape and dimming.

Run the real game/oot_box_light.c callback behind the actual lowered hook
(sitehooks.py), in an authored pool/owner fixture. A missing hook runs the
unchanged acquire behaviour and fails on stale parameters, not compilation.
No game assets, dumps or lifted game functions are included.

    python -I -B tests/test_box_light_reuse.py
    python -I -B tests/test_box_light_reuse.py --baseline-root <main checkout>

The fixture models acquisition followed by owner setup. It does not model
thread scheduling, resource loading or the renderer; those need the game run.
"""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

RUN_ALL_ARGS = ["--cc", "{clang}", "--cargo", "{cargo}"]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sitecheck  # noqa: E402
import sitehooks  # noqa: E402

HEAD = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define A64_NOESC
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); exit(1); } } while (0)
typedef struct { uint64_t x[32], pc; unsigned char nzcv[4]; int halted;
                 uint64_t vreg[32][2]; unsigned char tail[80]; } Ctx;
static unsigned char memory[2 * 640];
static const uint64_t base = 0x10000000ULL;
static int pool_locked, light_locked, writes, original_calls;
static unsigned char *address(uint64_t a, size_t n) {
    CHECK(a >= base && a + n <= base + sizeof memory);
    return memory + (a - base);
}
static uint64_t recomp_load64(Ctx *c, uint64_t a) {
    uint64_t v; (void)c; memcpy(&v, address(a, 8), 8); return v;
}
static uint8_t recomp_load8(Ctx *c, uint64_t a) { (void)c; return *address(a, 1); }
static void recomp_store32(Ctx *c, uint64_t a, uint32_t v) {
    (void)c; CHECK(pool_locked && light_locked); writes++; memcpy(address(a, 4), &v, 4);
}
static void recomp_store8(Ctx *c, uint64_t a, uint8_t v) {
    (void)c; CHECK(pool_locked && light_locked); writes++; *address(a, 1) = v;
}
static void put64(unsigned off, uint64_t v) { memcpy(memory + off, &v, 8); }
static void putf(unsigned off, float v) { memcpy(memory + off, &v, 4); }
static float getf(unsigned off) { float v; memcpy(&v, memory + off, 4); return v; }
'''

MAIN = r'''
/* The original acquire callback does nothing. The site follows that callback,
   before either lock is released; owner parameter loading happens later. */
static void acquire_site(Ctx *c) {
    original_calls++;
@HOOK@
}
static void acquire(uint64_t object) {
    Ctx c; memset(&c, 0x5a, sizeof c);
    c.x[20] = object;
    c.x[0] = base + 640;  /* x20 must survive the original callback's result. */
    Ctx before = c;
    pool_locked = light_locked = 1;
    acquire_site(&c);
    CHECK(!memcmp(&c, &before, sizeof c));
    light_locked = pool_locked = 0;
}
static void stale(uint64_t vt, float factor) {
    memset(memory, 0xa6, sizeof memory);
    put64(0, vt);
    put64(248, 0);  /* Released: no owner; parameters remain. */
    memset(memory + 264, 0, 4);
    const float values[12] = {9, -2, 6, 17, 4, 17, 17, .5f, 17, 17, 4, 17};
    memcpy(memory + 580, values, sizeof values);
    putf(628, factor);
    memory[632] = 0xa4;
}
static void neutral(void) {
    for (unsigned i = 580; i < 632; i += 4) {
        const float expected = (i >= 592 && i <= 600) || i == 628 ? 1.0f : 0.0f;
        CHECK(getf(i) == expected);
    }
    CHECK(memory[632] == 0xa5);  /* Set dirty, preserve every other flag. */
}
static void unchanged_except_parameters(const unsigned char *before) {
    CHECK(!memcmp(memory, before, 580));
    CHECK(!memcmp(memory + 633, before + 633, sizeof memory - 633));
}
static void new_owner(int parameters_present) {
    put64(248, 0x11223344);
    unsigned channel = 2; memcpy(memory + 264, &channel, 4);
    putf(568, 7); putf(572, 15); putf(576, -3);
    if (parameters_present) {
        const float values[13] = {3, 2, 1, 6, 8, 10, 1, 2, 3, 4, 5, 6, .375f};
        memcpy(memory + 580, values, sizeof values);
    }
}
int main(void) {
    unsigned char before[sizeof memory];
    /* This is the regression: release a dimmer, reuse it for an owner without
       parameters. Main compiles and reaches this assertion with old values. */
    stale(0x84baa5f8ULL, .1f); memcpy(before, memory, sizeof memory);
    acquire(base);
    neutral(); unchanged_except_parameters(before);
    new_owner(0); neutral();
    CHECK(original_calls == 1);

    /* A valid owner still supplies its shape and dimming AFTER acquisition. */
    stale(0x84baa5f8ULL, .25f); acquire(base); new_owner(1);
    CHECK(getf(592) == 6 && getf(596) == 8 && getf(600) == 10 && getf(628) == .375f);
    memcpy(before, memory, sizeof memory);
    /* No acquisition occurs during an ordinary update of the same owner. */
    new_owner(0);
    CHECK(!memcmp(memory + 580, before + 580, 52));
    CHECK(original_calls == 2);

    /* Repeated reuse cannot leak rotation, blend vectors or dirty flags either. */
    for (int i = 0; i < 8; i++) {
        stale(0x84baa5f8ULL, (float)(i + 2)); acquire(base); neutral();
    }

    /* Another class and a failed allocation are untouched. */
    stale(0x12345678ULL, .1f); memcpy(before, memory, sizeof memory);
    int was = writes; acquire(base); acquire(0);
    CHECK(writes == was && !memcmp(memory, before, sizeof memory));

    /* The generated site's disabled path retains the original behaviour. */
@OFF@
    puts("PASS: box reuse resets only parameters, under the locks; later owner parameters and registers survive");
    return 0;
}
'''


def fixture(root):
    sites = sitecheck.load(sitehooks.BUILD, game=root / "game/sites", defs=root / "game/sites/sites.toml")
    x = next((s for s in sites if s.name == "light.boxAcquired"), None)
    if x is None:
        # The original acquisition has no reset. Execute the same regression.
        return HEAD + MAIN.replace("@HOOK@", "    (void)c;").replace("@OFF@", "")
    assert (x.function, x.at, x.offset) == ("f_831eaa90", "before", 0x80)
    assert x.signature_offset == 0x7c
    assert [w for w, mask in x.signature if mask == 0xffffffff] == [
        0xd63f0100, 0x9100c280, 0x9429de57, 0x91002260, 0x9429de55]
    assert x.args == ("x20:ptr:box",)
    assert x.guards == ((0x5c, ((0x9100c280, 0xffffffff), (0x9429de5c, 0xffffffff))),)
    host = (root / "game/oot_box_light.c").read_text(encoding="utf-8")
    host = "".join(line for line in host.splitlines(keepends=True) if not line.startswith("#include"))
    # slot() reads the real registration; sitehooks lowers the real definition.
    defs = sitecheck.slot(x)
    off = '''
    hos_site_on_light_boxAcquired = 0;
    stale(0x84baa5f8ULL, .1f); memcpy(before, memory, sizeof memory);
    was = writes; acquire(base);
    CHECK(writes == was && !memcmp(memory, before, sizeof memory));
'''
    return HEAD + host + defs + MAIN.replace("@HOOK@", sitehooks.line(x)).replace("@OFF@", off)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default="clang")
    parser.add_argument("--cargo")
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--check-source", action="store_true", help="validate the fixture and tables without compiling")
    args = parser.parse_args()
    sitehooks.use(args.cc, args.cargo)
    text = fixture(args.baseline_root or ROOT)
    if args.check_source:
        print("PASS: fixture prepared from current sources; compiled behaviour not yet tested")
        return
    with tempfile.TemporaryDirectory(prefix="box-light-reuse-") as tmp:
        source, executable = Path(tmp) / "fixture.c", Path(tmp) / "fixture.exe"
        source.write_text(text, encoding="utf-8")
        subprocess.run([args.cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                        str(source), *sitehooks.objects(tmp, text), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
