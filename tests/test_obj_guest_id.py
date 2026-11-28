"""LockMutex/UnlockMutex's lookup (host/nn_os_sync.c obj_mutex) with HOS_MUTEX_GUEST_ID: the mutex's object number kept in
its guest MutexType (MUTEX_GUEST_WORD), read where the per-thread cache misses (=1, obj_miss) or before it (=2,
obj_by_word), finds the object the shared table has, and changes no guest byte but those words.

The real object table, per-thread caches, lookup and mutex_guest_id are compiled as tests/test_obj_cache_size.py cuts them
(its harness: the SRW locks and the clock stubbed, obj_lookup counted), with obj_mutex and obj_by_word cut from the source,
over a 16 MB guest window of random bytes. HOS_MUTEX_GUEST_ID unset (the default, 2), 0, off, no, false, 1, 2, values
that are neither (3, 12, on) and unset again, each in a fresh process with the child's environment set explicitly, run with the
sized cache (unset), the fixed one (HOS_OBJ_CACHE=0), the sets (HOS_OBJ_CACHE=sets) and none (HOS_NO_OBJ_CACHE=1).
Checked:
- the value read: 2 unset (on by default), off for env_on's off values (0, off, no, false), 1 for exactly 1, 2 for 2
  and everything else; read
  once: the variable flipped (off to 2, on to 0) after the first lookups changes nothing that follows;
- every lookup answers the table's object for that address and kind: 64 mutexes over and over, then 100,000 random
  operations on 300 addresses with objects finalized (as FinalizeMutex buries them), made again and re-made as another
  kind in between;
- off (0, off, no, false): guest memory is never written (nor marked), and without the sets cache objects are not
  numbered, as before;
- on: the first lookup of a mutex writes its number at MUTEX_GUEST_WORD (and marks it), with every cache (objects are
  numbered); later ones go to the table never again; with 2 they do not reach the per-thread cache either (its lookups
  are not counted); with 1 and no cache, the word alone answers;
- a word that does not name the mutex's object (0, the game's bytes, a number past those given, another mutex's
  number, any other object's) is never taken: the right object all the same, and the word is fixed; with 1 and no cache,
  a table lookup exactly when the word was wrong;
- an object made past the numbered ones (no number) leaves its word alone; a mutex whose word lies outside the guest
  window, whose address plus the word's offset wraps past 2^64 (into the window's first bytes), or whose word is off a
  4-byte boundary, is found all the same and its bytes are left alone;
- a mutex in the guest's first MiB (a null pointer's page, never committed) is found all the same, and no guest byte of
  it is read or written;
- only the mutexes' words ever change, and each written word is marked after its store (the mark sees the new number);
- each lookup runs the core checkpoint once, before it looks anything up (no cache probe, table lookup or guest read
  before the checkpoint);
- LockMutex and UnlockMutex, their HLE bodies cut from the source, look their mutex up through obj_mutex (never obj()),
  and pass the table's mutex on;
- first-object: with the sets cache and without, the switch is read when the first sync object is made (an event before
  any LockMutex): a flip after it changes nothing.
Broken variants that must fail (each built and run with HOS_MUTEX_GUEST_ID=2 and the sized cache, the last with =0 and the
sets cache): the word's object taken without obj_is's check, obj_mutex ignoring the switch, obj_mutex without its core
checkpoint, the checkpoint after the lookup, LockMutex or UnlockMutex through obj(), the mark before the store, no guard
for the first MiB, the sets cache reading its mode before the switch at the first object, and unset read as off (run
unset).

python -I -B tests/test_obj_guest_id.py [--cc COMPILER]   (test_obj_cache_size.compiler: --cc, else $CC; then on Windows
LONGSHOT_MINGW's clang before cc, clang or gcc on PATH, elsewhere those before it)
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import block  # noqa: E402
import test_obj_cache_size as cache_test  # noqa: E402  (its cut of the table and the caches, and its harness)

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r'''
/* (obj_mutex's checkpoint, counted, with what had been looked up when it ran: lookup_marks, in MAIN) */
static long ckpts, ckpt_snap, range_calls;
static long lookup_marks(void);
static void hos_vcore_checkpoint(void) { ckpts++; ckpt_snap = lookup_marks(); }
#define WINDOW (1u << 24)
static uint8_t *win;
static void *hos_guest_range(uint64_t a, uint64_t n) {
    range_calls++;
    return a < WINDOW && n <= WINDOW - a ? win + a : NULL;
}
/* (a mark counted, with the word's bytes when it is made: a mark before the store would see the old ones) */
static int marks;
static uint32_t marked_value;
#define HOS_WRITTEN(p, n) ((void)(n), memcpy(&marked_value, (const void *)(p), 4), marks++)
static int numbered = 1;   /* (0: obj_new_locked's fallback with no number, as past OBJ_CHUNKS) */
'''

MAIN = r'''
#include <stdarg.h>
static void hos_log(const char *fmt, ...) { (void)fmt; logs++; }
static void require(int ok, const char *m) { if (!ok) { fprintf(stderr, "FAIL: %s\n", m); exit(1); } }
static uint64_t rng = 0x853C49E6748FEA9Bull;
static uint64_t rnd(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }
static long lookups_made;
static Obj *counted_lookup(uint64_t key, int kind) { lookups_made++; return obj_lookup_real(key, kind); }
static uint8_t shadow[WINDOW];
static Obj *table_of(uint64_t key) {
    AcquireSRWLockExclusive(&table_lock);
    Obj *t = find_locked(key);
    ReleaseSRWLockExclusive(&table_lock);
    return t;
}
/* (what a lookup has done so far: table lookups, per-thread cache probes, guest reads) */
static long lookup_marks(void) { return lookups_made + (long)obj_cache_tl_looks + range_calls; }
static uint32_t word(uint64_t key) { uint32_t w; memcpy(&w, win + key + MUTEX_GUEST_WORD, 4); return w; }
static Obj *lock(uint64_t key) {   /* (LockMutex's lookup) the table's object of that address, a mutex */
    const long ck = ckpts, done = lookup_marks();
    const int m0 = marks;
    ckpt_snap = -1;
    Obj *o = obj_mutex(key, K_MUTEX);
    require(ckpts == ck + 1, "each LockMutex lookup runs the core checkpoint once");
    require(ckpt_snap == done, "the core checkpoint comes before the lookup (nothing looked up or read before it)");
    require(o && o->key == key && o->kind == K_MUTEX && o == table_of(key), "the lookup answers the table's mutex");
    if (marks != m0) require(marked_value == word(key), "a word is marked after its store (the mark sees the new number)");
    return o;
}
/* (LockMutex and UnlockMutex themselves, cut from the source: their lookup is obj_mutex, never obj()) */
static void hle(uint64_t key, int g) {
    Ctx c;
    memset(&c, 0, sizeof c);
    c.x[0] = key;
    const long calls = obj_calls, ck = ckpts;
    hle_locked = hle_unlocked = NULL;
    hle_lock_mutex(&c);
    require(obj_calls == calls && ckpts == ck + 1, "LockMutex looks its mutex up through obj_mutex, not obj()");
    require(hle_locked && hle_locked == table_of(key) && hle_locked->kind == K_MUTEX, "LockMutex locks the table's mutex");
    if (g && hle_locked->id) require(word(key) == hle_locked->id, "LockMutex's lookup keeps the number in the word");
    hle_unlock_mutex(&c);
    require(obj_calls == calls && ckpts == ck + 2, "UnlockMutex looks its mutex up through obj_mutex, not obj()");
    require(hle_unlocked == hle_locked, "UnlockMutex unlocks the table's mutex");
}
static void set_word(uint64_t key, uint32_t w) { memcpy(win + key + MUTEX_GUEST_WORD, &w, 4); }
static void finalize(uint64_t key) { drop(key); }   /* (FinalizeMutex's way: drop, cut with the table) */

int main(int argc, char **argv) {
    require(argc == 2 || argc == 3, "usage: obj_guest_id <the mode HOS_MUTEX_GUEST_ID should be read as> [first-object]");
    if (argc == 3) {   /* (first-object: an event made before any LockMutex reads the switch, with every cache) */
        Obj *e = obj_nockpt(0x900000, K_EVENT);
        require(e && e->kind == K_EVENT, "first-object: the event is made");
#ifdef _WIN32
        _putenv(atoi(argv[1]) ? "HOS_MUTEX_GUEST_ID=0" : "HOS_MUTEX_GUEST_ID=2");
#else
        setenv("HOS_MUTEX_GUEST_ID", atoi(argv[1]) ? "0" : "2", 1);
#endif
        require(mutex_guest_id() == atoi(argv[1]), "the switch is read when the first sync object is made (a flip after it "
                "changes nothing), with the sets cache too");
        printf("ok: first-object: HOS_MUTEX_GUEST_ID read as %d at the first object, %s\n", atoi(argv[1]),
               getenv("HOS_OBJ_CACHE") ? getenv("HOS_OBJ_CACHE") : "the sized cache");
        return 0;
    }
    const int g = mutex_guest_id();
    require(g == atoi(argv[1]), "HOS_MUTEX_GUEST_ID read as expected (unset: 2; 0, off, no, false: off; exactly 1: 1; "
            "else 2)");
    const int no_cache = env_on("HOS_NO_OBJ_CACHE");
    const int sets = obj_cache_mode_read() == OBJ_CACHE_SETS;
    win = (uint8_t *)malloc(WINDOW);
    for (size_t i = 0; i < WINDOW; i++) win[i] = (uint8_t)rnd();
    memcpy(shadow, win, WINDOW);
    uint64_t keys[64], rkeys[300];
    for (int i = 0; i < 64; i++) keys[i] = 0x100000 + (uint64_t)i * 0x10000 + (rnd() & 0xffc0);   /* (past the first MiB) */
    for (int i = 0; i < 300; i++) rkeys[i] = 0x800000 + (uint64_t)i * 0x2000 + (rnd() & 0x1fc0);
    for (int i = 0; i < 64; i++) {
        Obj *o = lock(keys[i]);
        if (g) require(o->id && word(keys[i]) == o->id, "the first lookup writes the mutex's number in its word");
        if (!g && !sets) require(!o->id, "off, without the sets cache: objects are not numbered, as before");
    }
    if (g) require(marks == 64, "each word written is marked");
    for (int i = 0; i < 64; i++) hle(keys[i], g);
    /* (read once: the variable flipped after the first lookups; everything below checks the mode first read) */
#ifdef _WIN32
    _putenv(g ? "HOS_MUTEX_GUEST_ID=0" : "HOS_MUTEX_GUEST_ID=2");
#else
    setenv("HOS_MUTEX_GUEST_ID", g ? "0" : "2", 1);
#endif
    require(mutex_guest_id() == g, "HOS_MUTEX_GUEST_ID is read once: a flip after the first lookup changes nothing");
    long before = lookups_made;
    const uint64_t looks = obj_cache_tl_looks;
    for (int k = 0; k < 20000; k++) lock(keys[rnd() % 64]);
    if (g) require(lookups_made == before, "a word that names the mutex's object: no table lookup");
    if (g == 2) require(obj_cache_tl_looks == looks, "=2: the word answers before the per-thread cache");
    if (g) {
        for (int k = 0; k < 5000; k++) {   /* (words that do not name the mutex's object) */
            const int i = (int)(rnd() % 64);
            uint32_t bad;
            switch (k % 5) {
            case 0: bad = 0; break;
            case 1: bad = (uint32_t)rnd(); break;
            case 2: bad = obj_count + 1 + (uint32_t)(rnd() % 1000); break;
            case 3: bad = lock(keys[(i + 1) % 64])->id; break;
            default: bad = (uint32_t)(rnd() % (obj_count + 1)); break;
            }
            set_word(keys[i], bad);
            before = lookups_made;
            Obj *o = lock(keys[i]);
            require(word(keys[i]) == o->id, "a wrong word is fixed");
            if (g == 1 && no_cache) require(lookups_made == before + (bad != o->id), "=1, no cache: a table lookup when the word is wrong");
        }
    }
    /* objects finalized, made again, re-made as another kind: still the table's */
    for (int k = 0; k < 100000; k++) {
        const uint64_t key = rkeys[rnd() % 300];
        const uint64_t op = rnd() % 100;
        if (op < 3) finalize(key);
        else if (op < 6) {
            Obj *o = obj_nockpt(key, K_EVENT);   /* (the same memory as an event, as a game's stack reused) */
            require(o->key == key && o->kind == K_EVENT && o == table_of(key), "a lookup of another kind is the table's");
        } else {
            Obj *o = lock(key);
            if (g && o->id) require(word(key) == o->id, "the word names the mutex's object after its lookup");
        }
    }
    /* an object with no number: its word is left alone */
    numbered = 0;
    const uint64_t fresh_key = 0xF00000;   /* (above every address before) */
    const uint32_t keep = word(fresh_key);
    Obj *u = lock(fresh_key);
    require(!u->id && word(fresh_key) == keep, "an object with no number leaves the word alone");
    numbered = 1;
    /* outside the guest window, wrapping past 2^64, and off a 4-byte boundary (no atomic access): found all the same, the
       bytes left alone */
    lock(WINDOW + 0x40);
    lock((uint64_t)WINDOW - 0x18);   /* (its word would lie past the window's end) */
    lock(0xFFFFFFFFFFFFFFF0ull);     /* (its address plus MUTEX_GUEST_WORD wraps round to guest address 8) */
    lock(0xE00002);
    /* a null pointer and the rest of the first MiB (never committed: a read there faults): no guest byte read */
    const long reads = range_calls;
    lock(0);
    lock(0x40);
    lock(0xFFFC0);
    require(range_calls == reads, "a mutex in the first MiB: its guest bytes are never read");
    /* only the mutexes' words changed */
    if (g) {
        for (int i = 0; i < 64; i++) memcpy(shadow + keys[i] + MUTEX_GUEST_WORD, win + keys[i] + MUTEX_GUEST_WORD, 4);
        for (int i = 0; i < 300; i++) memcpy(shadow + rkeys[i] + MUTEX_GUEST_WORD, win + rkeys[i] + MUTEX_GUEST_WORD, 4);
    } else {
        require(marks == 0, "off: no guest write marked");
    }
    require(!memcmp(shadow, win, WINDOW), g ? "only the mutexes' words changed" : "off: guest memory untouched");
    printf("ok: HOS_MUTEX_GUEST_ID %d, %s: the table's mutexes; %d words written\n", g,
           no_cache ? "no cache" : getenv("HOS_OBJ_CACHE") ? getenv("HOS_OBJ_CACHE") : "the sized cache", marks);
    return 0;
}
'''


# (LockMutex's and UnlockMutex's HLE bodies are cut from the source and compiled as plain functions over these: obj()
# counted, so a body that looks its mutex up through it fails; the lock and unlock record the object they were given)
HLE_STUBS = r'''
typedef struct { uint64_t x[31]; } Ctx;
#define ARG(n) (c->x[n])
#define watched(k) ((void)(k), 0)
#define watch_log(...) ((void)0)
static Obj *hle_locked, *hle_unlocked;
static long obj_calls;
static Obj *obj(uint64_t key, int kind) { obj_calls++; hos_vcore_checkpoint(); return obj_nockpt(key, kind); }
static void mutex_lock(Obj *m, uint64_t guest, Ctx *c) { (void)guest, (void)c; hle_locked = m; }
static int mutex_unlock(Obj *m) { hle_unlocked = m; return 1; }
'''
# (obj_mutex's checkpoint and lookup, as the source has them: the "checkpoint after the lookup" variant swaps them)
LOOKUP_LINES = ("    hos_vcore_checkpoint();   /* (as obj: before the lookup) */\n"
                "    const int g = mutex_guest_id();\n"
                "    Obj *o = g ? obj_by_word(key, kind, g) : obj_nockpt(key, kind);\n")

# (HOS_MUTEX_GUEST_ID's value in the child's environment, None: unset; and the mode it must be read as. Unset is 2, the
# default, and comes again last: a later fresh process reads it the same)
VALUES = ((None, 2), ("0", 0), ("off", 0), ("no", 0), ("false", 0), ("1", 1), ("2", 2), ("3", 2), ("12", 2), ("on", 2),
          (None, 2))
CACHES = (None, "0", "sets", "none")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cc")
    a = ap.parse_args()
    cc = cache_test.compiler(a.cc)
    if not cc:
        raise SystemExit("SKIP: no C compiler (--cc, CC, LONGSHOT_MINGW, or cc, clang or gcc on PATH)")
    src = (ROOT / "host/nn_os_sync.c").read_text(encoding="utf-8")
    new = "static Obj *obj_new_locked(void) {"
    pieces = [p.replace(new, new + "\n    if (!numbered) return (Obj *)calloc(1, sizeof(Obj));", 1)
              for p in cache_test.source_pieces(src)]
    if not any("if (!numbered)" in p for p in pieces):
        raise SystemExit("FAIL: obj_new_locked is not in the cut (update the check with the source)")
    if not any("static int mutex_guest_id(void) {" in p for p in pieces):
        raise SystemExit("FAIL: mutex_guest_id is not in test_obj_cache_size's cut (update the check with the source)")
    hle_heads = (("HLE(_ZN2nn2os9LockMutexEPNS0_9MutexTypeE) {", "static void hle_lock_mutex(Ctx *c) {"),
                 ("HLE(_ZN2nn2os11UnlockMutexEPNS0_9MutexTypeE) {", "static void hle_unlock_mutex(Ctx *c) {"))
    word = ["static Obj *obj_by_word(uint64_t key, int kind, int mode);",
            block(src, "static Obj *obj_mutex(uint64_t key, int kind) {"),
            block(src, "static __attribute__((noinline)) Obj *obj_by_word(uint64_t key, int kind, int mode) {"),
            HLE_STUBS] + [block(src, head).replace(head, name, 1) for head, name in hle_heads]
    def fixture(word_parts):
        return (cache_test.ENV_ON_H.read_text(encoding="utf-8") + cache_test.HARNESS + PRELUDE + "\n".join(pieces) + "\n" +
                "\n".join(word_parts) + MAIN)

    def build(tmp, name, code):
        c = os.path.join(tmp, name + ".c")
        exe = os.path.join(tmp, name + (".exe" if os.name == "nt" else ""))
        Path(c).write_text(code, encoding="utf-8")
        flags = ["-O2", "-std=gnu11", "-w"]
        if os.name != "nt":
            flags += ["-fsanitize=address,undefined", "-pthread"]
        r = subprocess.run([cc, *flags, c, "-o", exe], capture_output=True, text=True)
        if r.returncode:
            sys.stderr.write(r.stdout + r.stderr)
            raise SystemExit("FAIL: the fixture %s does not build" % name)
        return exe

    def run(exe, value, mode, cache, first=False):
        env = dict(os.environ)
        for name in ("HOS_MUTEX_GUEST_ID", "HOS_OBJ_CACHE", "HOS_NO_OBJ_CACHE", "HOS_KEEP_DEAD_OBJECTS",
                     "HOS_SYNC_TABLE_LOCK"):
            env.pop(name, None)   # (each read by the cut code: a developer's own value would change what runs)
        if value is not None:
            env["HOS_MUTEX_GUEST_ID"] = value
        if cache == "none":
            env["HOS_NO_OBJ_CACHE"] = "1"
        elif cache:
            env["HOS_OBJ_CACHE"] = cache
        return subprocess.run([exe, str(mode)] + (["first-object"] if first else []), capture_output=True, text=True,
                              env=env, timeout=600)

    # (broken variants: the anchor in the fixture's cut source, what replaces it, and what the variant breaks)
    broken = (
        ("return o && obj_is(o, key, kind) ? o : NULL;", "return o;", "the word's object taken without obj_is"),
        ("Obj *o = g ? obj_by_word(key, kind, g) : obj_nockpt(key, kind);", "Obj *o = obj_nockpt(key, kind);",
         "obj_mutex ignoring HOS_MUTEX_GUEST_ID"),
        ("    hos_vcore_checkpoint();   /* (as obj: before the lookup) */", "",
         "obj_mutex without its core checkpoint"),
        (LOOKUP_LINES, LOOKUP_LINES.split("\n", 1)[1] + LOOKUP_LINES.split("\n", 1)[0] + "\n",
         "the checkpoint after the lookup"),
        ('Obj *m = obj_mutex(ARG(0), K_MUTEX);\n    if (watched(ARG(0))) watch_log("lock", ',
         'Obj *m = obj(ARG(0), K_MUTEX);\n    if (watched(ARG(0))) watch_log("lock", ', "LockMutex through obj()"),
        ('Obj *m = obj_mutex(ARG(0), K_MUTEX);\n    if (watched(ARG(0))) watch_log("unlock", ',
         'Obj *m = obj(ARG(0), K_MUTEX);\n    if (watched(ARG(0))) watch_log("unlock", ', "UnlockMutex through obj()"),
        ("        __atomic_store_n(w, o->id, __ATOMIC_RELAXED);\n        HOS_WRITTEN(w, 4);",
         "        HOS_WRITTEN(w, 4);\n        __atomic_store_n(w, o->id, __ATOMIC_RELAXED);", "the mark before the store"),
        ("key < MUTEX_GUEST_LOW || ", "", "no guard for the first MiB"),
        ("const int numbered = mutex_guest_id() || obj_cache_mode_read() == OBJ_CACHE_SETS;",
         "const int numbered = obj_cache_mode_read() == OBJ_CACHE_SETS || mutex_guest_id();",
         "the sets cache's mode read before the switch at the first object", ("0", 0, "sets", True)),
        ("m = !v ? 2 : ", "m = !v ? 0 : ", "unset read as off", (None, 2, None)),
    )
    failed = []   # (every run, then the failures together)
    with tempfile.TemporaryDirectory(prefix="obj-guest-id-") as tmp:
        exe = build(tmp, "obj_guest_id", fixture(word))
        for value, mode in VALUES:
            for cache in CACHES:
                r = run(exe, value, mode, cache)
                sys.stdout.write(r.stdout)
                if r.returncode:
                    sys.stdout.write(r.stderr)
                    failed.append("HOS_MUTEX_GUEST_ID %s with the cache %s" % ("unset" if value is None else "=" + value,
                                                                               cache or "unset"))
        for value, mode in (("0", 0), ("2", 2)):   # (first-object: the switch read at the first sync object)
            for cache in (None, "sets"):
                r = run(exe, value, mode, cache, first=True)
                sys.stdout.write(r.stdout)
                if r.returncode:
                    sys.stdout.write(r.stderr)
                    failed.append("first-object, HOS_MUTEX_GUEST_ID=%s with the cache %s" % (value, cache or "unset"))
        code = fixture(word)
        for n, (anchor, repl, what, *how) in enumerate(broken):
            if code.count(anchor) != 1:
                raise SystemExit("FAIL: the broken variant's anchor %r is not once in the cut (update the check with the "
                                 "source)" % anchor)
            r = run(build(tmp, "broken%d" % n, code.replace(anchor, repl)), *(how[0] if how else ("2", 2, None)))
            if r.returncode == 0:
                failed.append("the broken variant (%s) passes" % what)
            elif "FAIL: " not in r.stderr:   # (a crash or a sanitizer's start-up abort is not the check catching it)
                failed.append("the broken variant (%s) fails without a check's FAIL line: %s"
                              % (what, (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1]))
            else:
                print("ok: the broken variant (%s) fails: %s" % (what, (r.stderr.strip().splitlines() or ["?"])[-1]))
    if failed:
        raise SystemExit("FAIL: " + "; ".join(failed))
    print("PASS: the mutex lookup answers the table's object with and without the guest word; only the words change")


if __name__ == "__main__":
    main()
