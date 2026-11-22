"""A mutex's host object is found by an index kept in the game's own mutex (host/nn_os_sync.c obj_mutex_ix, HOS_MUTEX_IX).

The real functions (obj, the index's table and switch, obj_mutex_ix) are compiled with a harness whose obj_nockpt counts the
table lookups, twice: as the PC builds them (off unless HOS_MUTEX_IX=1) and as the PS5 does (on unless HOS_MUTEX_IX=0).

- a mutex's first lock looks the table up once and writes the index in the mutex + 8; later ones look nothing up;
- two mutexes have two indexes; a mutex copied to another address gets its own object and index, and the first keeps its;
- the word zeroed or holding anything else (past the table, a number nobody has) falls back to the lookup and is written again;
- an object since reused for another kind is not answered: the mutex gets another object and index;
- an object of another kind, and address 0, never read or write the game's memory;
- off: every lock looks the table up and the game's word is not written;
- FinalizeMutex puts the word back to zero.

python -I -B tests/test_ps5_mutex_index.py [--cxx clang++]
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENV_ON_H = str(ROOT / "gpu" / "vk" / "compat" / "longshot_env.h")
RUN_ALL_ARGS = ["--cxx", "{cxx}"]


def block(source, marker):
    start = source.index(marker)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


HARNESS = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
enum { K_MUTEX = 1, K_COND, K_EVENT };
typedef struct Obj { uint64_t key; int kind; uint32_t ix; } Obj;
static uint8_t guest[1 << 16];
static int guest_reads;
static void *gp(uint64_t a) { guest_reads++; return guest + a; }
#define GP(a) gp(a)
static void hos_vcore_checkpoint(void) {}
static Obj pool[64];
static int npool, lookups;
/* (the table: one object an address and kind, a new one where the address now holds another kind) */
static Obj *obj_nockpt(uint64_t key, int kind) {
    lookups++;
    for (int i = 0; i < npool; i++) if (pool[i].key == key && pool[i].kind == kind) return &pool[i];
    Obj *o = &pool[npool++];
    o->key = key, o->kind = kind;
    return o;
}
static int mx_ix_on(void);
static Obj *obj_mutex_ix(uint64_t key);
OBJ
INDEX
static int bad;
#define CHECK(x, why) do { if (!(x)) { printf("FAIL: %s\n", why); bad = 1; } } while (0)
static uint32_t word(uint64_t a) { uint32_t v; memcpy(&v, guest + a + 8, 4); return v; }
int main(int argc, char **argv) {
    const int on = argc > 1 && argv[1][0] == '1';
    const uint64_t A = 0x100, B = 0x200, C = 0x300, E = 0x400;
    if (!on) {
        Obj *a = obj(A, K_MUTEX);
        CHECK(obj(A, K_MUTEX) == a && lookups == 2, "off: a lock does not look the table up");
        CHECK(!word(A) && !guest_reads, "off: the game's mutex was read or written");
        CHECK(!mx_objs, "off: the index's table was made");
        puts(bad ? "FAILED" : "ok off");
        return bad;
    }
    Obj *a = obj(A, K_MUTEX);
    CHECK(lookups == 1 && a->ix && word(A) == a->ix, "a mutex's first lock does not number it and write the number in it");
    CHECK(obj(A, K_MUTEX) == a && obj(A, K_MUTEX) == a && lookups == 1, "a numbered mutex still looks the table up");
    Obj *b = obj(B, K_MUTEX);
    CHECK(b != a && b->ix && b->ix != a->ix && word(B) == b->ix && lookups == 2, "a second mutex is not numbered apart");
    CHECK(obj(A, K_MUTEX) == a && obj(B, K_MUTEX) == b && lookups == 2, "two numbered mutexes are not found by their numbers");
    memcpy(guest + C, guest + A, 16);   /* (the game copied a mutex: the copy carries the first one's number) */
    Obj *cc = obj(C, K_MUTEX);
    CHECK(cc != a && cc->key == C && cc->ix != a->ix && word(C) == cc->ix, "a copied mutex is answered with the first one's object");
    CHECK(obj(A, K_MUTEX) == a && word(A) == a->ix, "the copy took the first mutex's number");
    int before = lookups;
    memset(guest + A + 8, 0, 4);        /* (zeroed: the game initialized it again in place) */
    CHECK(obj(A, K_MUTEX) == a && lookups == before + 1 && word(A) == a->ix, "a zeroed word is not looked up and written again");
    const uint32_t wild[3] = {0xFFFFFFFFu, 1u << 20, 5000};   /* (past the table, at its end, a number nobody has) */
    for (int i = 0; i < 3; i++) {
        memcpy(guest + A + 8, &wild[i], 4);
        before = lookups;
        CHECK(obj(A, K_MUTEX) == a && lookups == before + 1 && word(A) == a->ix, "a word that is no number of ours is trusted");
    }
    b->kind = K_EVENT;                  /* (the address now holds an event: the object was reused for it) */
    Obj *b2 = obj(B, K_MUTEX);
    CHECK(b2 != b && b2->kind == K_MUTEX && b2->ix && b2->ix != b->ix && word(B) == b2->ix, "an object reused for another kind is answered as the mutex");
    CHECK(obj(B, K_MUTEX) == b2, "the renumbered mutex is not found by its new number");
    const int reads = guest_reads;
    Obj *e = obj(E, K_EVENT);
    CHECK(e->kind == K_EVENT && !e->ix && !word(E) && guest_reads == reads, "an event read or wrote the game's memory");
    (void)obj(0, K_MUTEX);
    CHECK(guest_reads == reads, "address 0 was read as a mutex");
    puts(bad ? "FAILED" : "ok on");
    return bad;
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default=os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++") or "g++")
    args = parser.parse_args()
    source = (ROOT / "host" / "nn_os_sync.c").read_text(encoding="utf-8")
    index = source[source.index("#define MX_IX_OFFSET 8"):source.index("/* obj without the checkpoint, for lookups under a host lock")]
    text = HARNESS.replace("OBJ", block(source, "static Obj *obj(uint64_t key, int kind) {"), 1).replace("INDEX", index, 1)
    final = block(source, "HLE(_ZN2nn2os13FinalizeMutexEPNS0_9MutexTypeE) {")
    failed = []
    if "if (ARG(0) && mx_ix_on()) __atomic_store_n((uint32_t *)GP(ARG(0) + MX_IX_OFFSET), 0u, __ATOMIC_RELAXED);" not in final:
        failed.append("FinalizeMutex does not put the index's word back to zero")
    if final.index("MX_IX_OFFSET") > final.index("drop(ARG(0));"):
        failed.append("FinalizeMutex clears the word after the object is dropped")
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TMP", "TEMP")}
    with tempfile.TemporaryDirectory(prefix="mutex-index-") as tmp:
        folder = Path(tmp)
        (folder / "fixture.cpp").write_text(text, encoding="utf-8")
        for name, flags, runs in (("pc", [], ((None, "0"), ("1", "1"), ("0", "0"))),
                                  ("ps5", ["-D__PROSPERO__"], ((None, "1"), ("0", "0"), ("off", "0"), ("1", "1")))):
            exe = folder / (name + ".exe")
            subprocess.run([args.cxx, "-std=c++17", "-include", ENV_ON_H, "-O1", "-w", *flags, str(folder / "fixture.cpp"), "-o", str(exe)],
                           check=True, cwd=folder)
            for value, want in runs:
                env = dict(keep)
                if value is not None:
                    env["HOS_MUTEX_IX"] = value
                got = subprocess.run([str(exe), want], env=env, capture_output=True, text=True, timeout=30)
                ok = got.returncode == 0 and got.stdout.strip() == "ok " + ("on" if want == "1" else "off")
                print(("ok   " if ok else "FAIL ") + "%s, HOS_MUTEX_IX=%s: %s" % (name, value, got.stdout.strip().replace("\n", "; ")))
                if not ok:
                    failed.append("%s HOS_MUTEX_IX=%s" % (name, value))
    for why in failed:
        print("FAIL: " + why)
    if failed:
        return 1
    print("PASS: a mutex's object is found by the index kept in the mutex, on by default on the PS5 and off elsewhere")
    return 0


if __name__ == "__main__":
    sys.exit(main())
