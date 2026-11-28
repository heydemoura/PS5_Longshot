"""The per-thread cache in front of the sync objects' table, sized by HOS_OBJ_CACHE (host/nn_os_sync.c obj_cache_entries,
obj_lookup_cached, obj_nockpt; 16384 entries unless HOS_OBJ_CACHE says otherwise).

Compiles the real object table (find_locked, bury_locked, find_dead_locked, insert_locked, find_unlocked, obj_lookup, drop),
the cache and obj_nockpt from host/nn_os_sync.c with a single-threaded harness (the SRW locks and the clock stubbed;
obj_lookup counted). Checks:

- obj_cache_entries: unset, empty or not a number is 16384; 0 is 0 (the fixed cache); "sets" exactly is OBJ_CACHE_SETS
  (the set-associative cache); otherwise 256 to 65536, rounded up to a power of two;
- with HOS_OBJ_CACHE=0, a thread that walks 4,000 objects (a table whose slots each have a mutex, as OoT's f_80364080 searches one) keeps
  missing in the fixed 1024-entry cache pass after pass; unset (16384) the passes after the first miss under 2 % (47 of 4,000
  here: the sets that hold five of the walk's slots); with HOS_OBJ_CACHE=256 they miss again (the size is honoured);
  HOS_OBJ_CACHE=sets (2048 sets of 8 ways of object numbers, two hashes) under 2 % as well;
- with the cache sized, or in sets, an entry is not used once its object belongs to another address or kind (re-initialized as
  another kind, buried, its spare taken by another address): every lookup still returns the table's object for that
  address and kind, compared with obj_lookup itself over 200,000 random operations;
- the log line comes once 65,536 lookups are counted and 10 s have passed, with the lookups and misses, made by
  obj_cache_flush as obj() calls it (nothing obj_nockpt reaches logs: it runs under mw_lock, test_multiwait_wake.py);
- an object reassigned in the middle of a lookup (mode reuse, every way a lookup reads an object without table_lock:
  sets, the sized default, the fixed cache, and no cache, HOS_NO_OBJ_CACHE=1, the table's unlocked path): an address's
  object is dropped, buried, after the grave's 30 s made a spare and taken by another address (of the same kind, or of
  another), or taken by another and then, dropped again, by its first address as another kind; the table's own paths
  do it, before the lookup and right after each of the lookup's atomic loads in turn (the harness watches every load
  the cut code makes outside table_lock, where a writer could come in), until the lookup has no load left. The lookup
  must return its address's object of that kind as the table has it, or the old object only if it read the object's
  identity wholly before the reassignment (no load of it after);
- HOS_OBJ_CACHE=sets: 64 guest-like threads made and ended one after another, each looking 4,000 objects up (mode
  churn): the large allocations still live after them (the harness counts calloc and free of 64 KB and up) are those
  before them: each thread's 128 KB is freed as it ends;
- HOS_OBJ_CACHE=sets, Windows (mode fiber): a thread makes its cache on a guest fiber (a Win32 fiber, as hos_thread.c
  runs the game's), switches back, deletes the fiber and looks the same objects up again: the cache is still live and
  hits, nothing is written in freed memory (the harness keeps a freed allocation of 64 KB and up filled with 0xA5), and
  it is freed as the thread ends;
- a lookup reads the cache mode inline, a load and a compare, as `main`'s two static reads were: the fixture's assembly
  has no call to obj_cache_mode_read (712ee7e7 made one on every lookup, in every mode, before the cache).

`--bench` prints a micro-benchmark (not a frame rate): ns per lookup for that 4,000-object walk among 60,000 objects, with
the fixed cache (HOS_OBJ_CACHE=0), with 4096, 16384 and 65536 entries, and with HOS_OBJ_CACHE=sets; built without the
reuse mode's watch on every atomic load (OBJ_BENCH), so the loads cost what they do in the game.

python -I -B tests/test_obj_cache_size.py [--cc COMPILER] [--bench]   (--cc, else $CC; then on Windows LONGSHOT_MINGW's
clang, else cc, clang or gcc on PATH; elsewhere cc, clang or gcc on PATH, else LONGSHOT_MINGW's clang: the pinned llvm-mingw
has no Linux sanitizer runtimes, which the fixtures link with -fsanitize=address,undefined)
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import block, between  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
ENV_ON_H = ROOT / "gpu" / "vk" / "compat" / "longshot_env.h"

HARNESS = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <time.h>
typedef struct HosThread HosThread;
#ifdef _WIN32
#include <windows.h>   /* (the real locks, and the thread-exit callbacks: the churn mode's threads are real) */
#else
#include <pthread.h>
typedef int SRWLOCK;
typedef int CONDITION_VARIABLE;
typedef long long LONG64;
#define SRWLOCK_INIT 0
/* (one thread at a time here; the locks held counted, as on Windows below) */
static _Thread_local int locks_held;
static void AcquireSRWLockExclusive(SRWLOCK *l) { (void)l, locks_held++; }
static void ReleaseSRWLockExclusive(SRWLOCK *l) { (void)l, locks_held--; }
static void AcquireSRWLockShared(SRWLOCK *l) { (void)l, locks_held++; }
static void ReleaseSRWLockShared(SRWLOCK *l) { (void)l, locks_held--; }
static void InitializeSRWLock(SRWLOCK *l) { *l = 0; }
static void InitializeConditionVariable(CONDITION_VARIABLE *c) { *c = 0; }
static LONG64 InterlockedAdd64(volatile LONG64 *v, LONG64 d) { return *v += d; }
static LONG64 InterlockedIncrement64(volatile LONG64 *v) { return ++*v; }
static LONG64 InterlockedExchange64(volatile LONG64 *v, LONG64 x) { LONG64 o = *v; *v = x; return o; }
static LONG64 InterlockedCompareExchange64(volatile LONG64 *v, LONG64 x, LONG64 c) { LONG64 o = *v; if (o == c) *v = x; return o; }
#endif
#ifdef _WIN32
/* the cut code's SRW locks this thread holds (the reuse mode's reassignments come only outside them: the table's writers
   hold table_lock); the real locks, counted */
static _Thread_local int locks_held;
static void held_acquire_x(SRWLOCK *l) { AcquireSRWLockExclusive(l); locks_held++; }
static void held_release_x(SRWLOCK *l) { locks_held--; ReleaseSRWLockExclusive(l); }
static void held_acquire_s(SRWLOCK *l) { AcquireSRWLockShared(l); locks_held++; }
static void held_release_s(SRWLOCK *l) { locks_held--; ReleaseSRWLockShared(l); }
#define AcquireSRWLockExclusive held_acquire_x
#define ReleaseSRWLockExclusive held_release_x
#define AcquireSRWLockShared held_acquire_s
#define ReleaseSRWLockShared held_release_s
#endif
static void hos_fatal(void *c, const char *fmt, ...) { (void)c; fprintf(stderr, "FATAL: %s\n", fmt); exit(3); }
/* calloc and free counted for the allocations of 64 KB and up (a thread's cache, the objects' chunks, the table): the
   real ones. A freed one is kept, filled with 0xA5 and never given back, so a write through a pointer left to it shows
   (freed_written) */
static struct { void *p; size_t n; int freed; } big[8192];
static long long big_live;
static void *counted_calloc(size_t n, size_t s) {
    void *p = calloc(n, s);
    if (p && n * s >= 65536)
        for (int i = 0; i < 8192; i++)
            if (!big[i].p) { big[i].p = p, big[i].n = n * s, big[i].freed = 0, big_live += (long long)(n * s); break; }
    return p;
}
static void counted_free(void *p) {
    for (int i = 0; p && i < 8192; i++)
        if (big[i].p == p) {
            if (!big[i].freed) big_live -= (long long)big[i].n, big[i].freed = 1, memset(p, 0xA5, big[i].n);
            return;
        }
    free(p);
}
static long long freed_written(void) {   /* (bytes changed in the freed allocations kept) */
    long long w = 0;
    for (int i = 0; i < 8192; i++)
        if (big[i].p && big[i].freed)
            for (size_t j = 0; j < big[i].n; j++) w += ((const unsigned char *)big[i].p)[j] != 0xA5;
    return w;
}
#define calloc(n, s) counted_calloc((n), (s))
#define free(p) counted_free(p)
/* Every atomic load of the cut code passes here once it has read. While watching (the reuse mode), the loads outside the
   locks are counted: right after the watch_at-th, watch_do reassigns an object, and the loads of that object's identity
   (its fields before owner, watch_lo to watch_hi) made after it are counted in watch_after. (--bench builds without it,
   OBJ_BENCH: the loads as the game makes them, no call after each) */
typedef struct Obj Obj;
static int watch_on, watch_at, watch_seen, watch_fired, watch_after;
static const char *watch_lo, *watch_hi;
static void (*watch_do)(void);
static void watch_point(const void *p) {
    if (!watch_on || locks_held) return;
    if (watch_fired) {
        watch_after += (const char *)p >= watch_lo && (const char *)p < watch_hi;
        return;
    }
    if (++watch_seen != watch_at) return;
    watch_fired = 1, watch_on = 0;
    watch_do();
    watch_on = 1;
}
#ifndef OBJ_BENCH
#define __atomic_load_n(p, m) __extension__ ({ __typeof__(*(p)) v_ = __atomic_load_n((p), (m)); watch_point((const void *)(p)); v_; })
#endif
static int64_t clock_ns = 1;
static int64_t hos_now_ns(void) { return clock_ns; }
static int logs;
static long long logged_looks, logged_misses;
static void hos_log(const char *fmt, ...);
enum { K_MUTEX = 1, K_COND, K_EVENT, K_SEMA, K_QUEUE, K_RWLOCK, K_MULTIWAIT, K_HOLDER, K_TIMER };
'''

MAIN = r'''
#include <stdarg.h>
static void hos_log(const char *fmt, ...) {
    va_list a;
    va_start(a, fmt);
    (void)va_arg(a, int);                     /* entries */
    (void)va_arg(a, long long);               /* made */
    logged_looks = va_arg(a, long long);
    logged_misses = va_arg(a, long long);
    va_end(a);
    logs++;
}
static void require(int ok, const char *m) { if (!ok) { fprintf(stderr, "FAIL: %s\n", m); exit(1); } }
static uint64_t rng = 88172645463325252ull;
static uint64_t next(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }
static double now_s(void) { struct timespec t; timespec_get(&t, TIME_UTC); return t.tv_sec + t.tv_nsec * 1e-9; }
static long lookups_made;
/* (obj_lookup counted: the real one, through a wrapper the cut code calls) */
static Obj *counted_lookup(uint64_t key, int kind) { lookups_made++; return obj_lookup_real(key, kind); }

/* (reuse) an object reassigned by the table's own paths: dropped, buried as another address takes its slot, 30 s later
   (the grave's time) the next spare as the next burial goes past it, and taken */
static uint64_t free_keys = 0x7000000000ull;
static uint64_t key_at_slot(uint64_t slot, uint64_t from) {   /* (an address whose probe starts at that slot) */
    for (uint64_t k = from;; k += 8) if (slot_of(k, cap) == slot) return k;
}
static uint64_t new_key(uint64_t slot) { const uint64_t k = key_at_slot(slot, free_keys); free_keys = k + 8; return k; }
static uint64_t slot_holding(Obj *o) {
    for (uint64_t i = 0; i < cap; i++) if (table[i] == o) return i;
    require(0, "the object is in the table");
    return 0;
}
static Obj *table_obj(uint64_t key) {
    AcquireSRWLockExclusive(&table_lock);
    Obj *o = find_locked(key);
    ReleaseSRWLockExclusive(&table_lock);
    return o;
}
static void make_spare(Obj *o) {
    drop(o->key);   /* (the cut code's own: a tombstone) */
    const uint64_t slot = slot_holding(o);
    const uint64_t p = new_key(slot);
    require(obj_lookup_real(p, K_EVENT)->key == p, "reuse: the slot's next address");
    clock_ns += 31000000000ll;
    drop(p);
    const uint64_t q = new_key(slot);
    require(obj_lookup_real(q, K_EVENT)->key == q, "reuse: the slot's address after it");
    require(spares == o, "reuse: the dropped object is the next spare");
}
static const char *const how_names[] = {"another address's, of the same kind", "another address's, of another kind",
                                        "its own address's again, of another kind (another's in between)"};
static Obj *moved;
static uint64_t moved_from, moved_to;
static int moved_how;   /* (an index in how_names) */
static void reassign(void) {
    Obj *o = moved;
    const uint32_t id = o->id;
    make_spare(o);
    moved_to = free_keys, free_keys += 8;
    require(obj_lookup_real(moved_to, moved_how ? K_EVENT : K_MUTEX) == o && o->key == moved_to && o->id == id,
            "reuse: the spare (its number) went to the new address");
    if (moved_how == 2) {
        make_spare(o);
        require(obj_lookup_real(moved_from, K_EVENT) == o && o->key == moved_from && o->kind == K_EVENT && o->id == id,
                "reuse: then back to its first address, as an event");
    }
}
static int reuse_mode(const char *label) {
    for (int i = 0; i < 20000; i++) obj_nockpt(0x100000000ull + (next() % (1ull << 34)) * 8, K_EVENT);
    int failed = 0;
    for (int how = 0; how < 3; how++) {
        int at = 0, how_failed = 0;
        for (;; at++) {   /* (at 0: before the lookup; at n: right after the lookup's n-th load) */
            const uint64_t a = free_keys;
            free_keys += 8;
            Obj *o = obj_nockpt(a, K_MUTEX);
            require(o && o->key == a && obj_nockpt(a, K_MUTEX) == o, "reuse: the address's entry hits");
            moved = o, moved_from = a, moved_how = how, watch_do = reassign;
            watch_lo = (const char *)o, watch_hi = (const char *)&o->owner;
            watch_at = at, watch_seen = 0, watch_fired = 0, watch_after = 0;
            if (!at) reassign(), watch_fired = 1;
            else watch_on = 1;
            Obj *got = obj_nockpt(a, K_MUTEX);
            watch_on = 0;
            if (!watch_fired) break;   /* (the lookup made fewer loads: every place has been tried) */
            /* (the old object only if its identity was read wholly before it changed: the lookup's moment is then before) */
            const int read_before = at && got == o && !watch_after;
            if (!read_before && !(got->key == a && got->kind == K_MUTEX && got == table_obj(a))) {
                char when[64];
                if (at) snprintf(when, sizeof when, "right after the lookup's load %d", at);
                else snprintf(when, sizeof when, "before the lookup");
                fprintf(stderr, "FAIL: HOS_OBJ_CACHE %s: a lookup of %#llx (a mutex) returned the object now %s (%#llx, kind %d), "
                        "reassigned %s (%d loads of its identity after that)\n", label, (unsigned long long)a, how_names[how],
                        (unsigned long long)got->key, got->kind, when, watch_after);
                failed = how_failed = 1;
                break;
            }
            require(obj_nockpt(a, K_MUTEX) == table_obj(a), "reuse: the address's next lookup returns the table's object");
        }
        require(how_failed || at > 2, "reuse: the lookup's loads were watched");
        if (!how_failed)
            printf("reuse: HOS_OBJ_CACHE %s: reassigned to %s before the lookup and after each of its %d loads: ok\n", label,
                   how_names[how], at - 1);
    }
    if (failed) return 1;
    printf("ok: HOS_OBJ_CACHE %s: an object reassigned at any point of a lookup is not returned for its old address\n", label);
    return 0;
}
/* (churn) guest-like threads made and ended one after another, each looking the walk's 4,000 mutexes up */
#ifdef _WIN32
static DWORD WINAPI churn_thread(void *p) {
#else
static void *churn_thread(void *p) {
#endif
    for (int s = 0; s < 4000; s++) {
        Obj *m = obj_nockpt(0x2000000000ull + (uint64_t)s * 0x80, K_MUTEX);
        if (!m || m->key != 0x2000000000ull + (uint64_t)s * 0x80) *(int *)p = 1;
    }
    return 0;
}
static int churn_mode(void) {
    for (int s = 0; s < 4000; s++) obj_nockpt(0x2000000000ull + (uint64_t)s * 0x80, K_MUTEX);
    const long long before = big_live;
    int bad = 0;
    for (int t = 0; t < 64; t++) {
#ifdef _WIN32
        HANDLE h = CreateThread(NULL, 0, churn_thread, &bad, 0, NULL);
        require(h != NULL, "churn: CreateThread");
        WaitForSingleObject(h, INFINITE);
        CloseHandle(h);
#else
        pthread_t th;
        require(!pthread_create(&th, NULL, churn_thread, &bad), "churn: pthread_create");
        pthread_join(th, NULL);
#endif
    }
    require(!bad, "churn: every thread's lookups return their objects");
    printf("churn: 64 threads made and ended: %lld bytes in allocations of 64 KB and up before, %lld after\n", before, big_live);
    require(big_live == before, "churn: each ended thread's sets cache is freed");
    puts("ok: HOS_OBJ_CACHE sets: 64 threads made and ended leave no cache behind");
    return 0;
}
/* (fiber, Windows) a thread's cache made on a guest fiber (a Win32 fiber its thread runs, as hos_thread.c runs the
   game's), the fiber deleted (the game's FinalizeFiber) while the thread runs on */
#ifdef _WIN32
static void *fiber_home;
static long long fiber_with, fiber_deleted;
static long fiber_missed;
static void WINAPI fiber_main(void *p) {
    (void)p;
    for (int s = 0; s < 4000; s++) obj_nockpt(0x2000000000ull + (uint64_t)s * 0x80, K_MUTEX);
    SwitchToFiber(fiber_home);
}
static DWORD WINAPI fiber_thread(void *p) {
    (void)p;
    fiber_home = ConvertThreadToFiber(NULL);
    void *f = CreateFiber(256 << 10, fiber_main, NULL);
    require(fiber_home && f, "fiber: ConvertThreadToFiber and CreateFiber");
    SwitchToFiber(f);   /* (the thread's first lookups, on the fiber) */
    fiber_with = big_live;
    DeleteFiber(f);
    fiber_deleted = big_live;
    const long before = lookups_made;
    for (int s = 0; s < 4000; s++) {
        Obj *m = obj_nockpt(0x2000000000ull + (uint64_t)s * 0x80, K_MUTEX);
        require(m && m->key == 0x2000000000ull + (uint64_t)s * 0x80, "fiber: a lookup returns its object");
    }
    fiber_missed = lookups_made - before;
    ConvertFiberToThread();
    return 0;
}
#endif
static int fiber_mode(void) {
#ifdef _WIN32
    for (int s = 0; s < 4000; s++) obj_nockpt(0x2000000000ull + (uint64_t)s * 0x80, K_MUTEX);
    const long long before = big_live;
    HANDLE h = CreateThread(NULL, 0, fiber_thread, NULL, 0, NULL);
    require(h != NULL, "fiber: CreateThread");
    WaitForSingleObject(h, INFINITE);
    CloseHandle(h);
    const long long written = freed_written();
    printf("fiber: a thread's cache made on a guest fiber: %lld bytes in allocations of 64 KB and up with it, %lld once the "
           "fiber is deleted (the thread running on); the thread's next 4000 lookups reached the table %ld times; %lld bytes "
           "written in freed memory; %lld after the thread ended, %lld before it\n",
           fiber_with, fiber_deleted, fiber_missed, written, big_live, before);
    require(fiber_deleted == fiber_with, "fiber: deleting a guest fiber leaves the cache of the thread that runs on");
    require(!written, "fiber: nothing is written in freed memory");
    require(fiber_missed <= 10, "fiber: the thread's lookups after it hit its own cache");
    require(big_live == before, "fiber: the cache is freed as its thread ends");
    puts("ok: HOS_OBJ_CACHE sets: a guest fiber deleted while its thread runs on leaves the thread's cache, freed as the thread ends");
#else
    puts("ok: fiber: Windows only (a guest fiber is a Win32 fiber there; here the cache is the thread's, a pthread key's)");
#endif
    return 0;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "check";
    const char *size = getenv("HOS_OBJ_CACHE");
    if (!strcmp(mode, "reuse"))
        return reuse_mode(getenv("HOS_NO_OBJ_CACHE") ? "off (HOS_NO_OBJ_CACHE=1: the table's unlocked path)" : size ? size : "unset");
    if (!strcmp(mode, "churn")) return churn_mode();
    if (!strcmp(mode, "fiber")) return fiber_mode();
    if (!strcmp(mode, "entries")) {
        struct { const char *v; int want; } cases[] = {{NULL, 16384}, {"", 16384}, {"x", 16384}, {"0", 0}, {"-5", 16384}, {"12abc", 16384},
            {"1", 256}, {"256", 256}, {"1000", 1024}, {"1024", 1024}, {"16384", 16384}, {"16385", 32768}, {"100000", 65536},
            {"sets", OBJ_CACHE_SETS}, {"set", 16384}, {"setsx", 16384}, {"Sets", 16384}};
        for (size_t i = 0; i < sizeof cases / sizeof *cases; i++)
            if (obj_cache_entries(cases[i].v) != cases[i].want) {
                fprintf(stderr, "FAIL: obj_cache_entries(%s) = %d, not %d\n", cases[i].v ? cases[i].v : "NULL",
                        obj_cache_entries(cases[i].v), cases[i].want);
                return 1;
            }
        puts("ok: obj_cache_entries");
        return 0;
    }
    /* the game's other objects: the table as large as in a scene */
    const int others = !strcmp(mode, "bench") ? 60000 : 20000;
    for (int i = 0; i < others; i++) obj_nockpt(0x100000000ull + (next() % (1ull << 34)) * 8, K_EVENT);
    /* the walk: 4,000 slots of 0x80 bytes, each with its mutex, passed again and again */
    enum { SLOTS = 4000 };
    const uint64_t base = 0x2000000000ull;
    if (!strcmp(mode, "bench")) {
        double best = 1e9;
        for (int rep = 0; rep < 7; rep++) {
            const double t0 = now_s();
            for (int pass = 0; pass < 50; pass++)
                for (int s = 0; s < SLOTS; s++) {
                    Obj *m = obj_nockpt(base + (uint64_t)s * 0x80, K_MUTEX);
                    if (!m) return 2;
                }
            const double ns = (now_s() - t0) / (50.0 * SLOTS) * 1e9;
            if (ns < best) best = ns;
        }
        printf("bench: HOS_OBJ_CACHE %-6s: %.1f ns a lookup (a walk of %d mutexes among %d objects; best of 7)\n",
               size ? size : "unset", best, SLOTS, others);
        return 0;
    }
    long before = 0;
    for (int pass = 0; pass < 4; pass++) {
        before = lookups_made;
        for (int s = 0; s < SLOTS; s++) {
            Obj *m = obj_nockpt(base + (uint64_t)s * 0x80, K_MUTEX);
            require(m && m->key == base + (uint64_t)s * 0x80 && m->kind == K_MUTEX, "a lookup returns its object");
        }
    }
    const long last_pass = lookups_made - before;
    const int entries = obj_cache_entries(size);
    if (!entries) require(last_pass > SLOTS / 2, "=0: the fixed cache misses a 4,000-object walk pass after pass");
    else if (entries == OBJ_CACHE_SETS) require(last_pass <= 10, "sets: the walk's later passes miss at most 10 of 4,000 (the four-way default misses 47)");
    else if (entries >= 16384) require(last_pass <= SLOTS / 50, "sized: the walk's later passes miss under 2 %");
    else require(last_pass > SLOTS / 2, "a cache smaller than the walk misses again: the size is honoured");
    /* objects changing under the cache: compared with the table itself */
    enum { KEYS = 3000 };
    for (int it = 0; it < 200000; it++) {
        const uint64_t key = 0x3000000000ull + (next() % KEYS) * 0x40;
        const int kind = 1 + (int)(next() % 3);
        const uint64_t op = next() % 100;
        if (op < 3) {   /* finalized: a tombstone (drop, the cut code's own) */
            drop(key);
            continue;
        }
        Obj *got = obj_nockpt(key, kind);
        require(got && got->key == key && got->kind == kind, "a lookup returns an object of that address and kind");
        AcquireSRWLockExclusive(&table_lock);
        Obj *t = find_locked(key);
        ReleaseSRWLockExclusive(&table_lock);
        require(t == got, "the cache returns the table's object for the address");
    }
    if (entries) {
        logs = 0;
        clock_ns += 20000000000ll;
        for (int i = 0; i < 70000; i++) {   /* (obj()'s way: the lookup, then the counts when they are due) */
            obj_nockpt(base + (uint64_t)(i % SLOTS) * 0x80, K_MUTEX);
            if (__atomic_load_n(&obj_cache_due, __ATOMIC_RELAXED)) obj_cache_flush();
        }
        require(logs == 1 && logged_looks >= 65536 && logged_misses >= 0 && logged_misses <= logged_looks,
                "the log line, once, with its counts");
    }
    printf("ok: HOS_OBJ_CACHE %s: the walk's last pass reached obj_lookup %ld times\n", size ? size : "unset", last_pass);
    return 0;
}
'''


def source_pieces(src):
    obj = between(src, "typedef struct Obj {", "static int wake_lat_on(void);")
    table = between(src, "/* ---- the object table: open addressing on the guest address", "static Obj *obj_lookup(uint64_t key, int kind);")
    cache = between(src, "#define OBJ_CACHE_SETS 1", "static Obj *obj_lookup(uint64_t key, int kind) {")
    real = block(src, "static Obj *obj_lookup(uint64_t key, int kind) {")
    drop = block(src, "static void drop(uint64_t key) {")   # (the harness's tombstones: drop's own way)
    return [obj, table,
            "static Obj *obj_lookup_real(uint64_t key, int kind);\nstatic Obj *counted_lookup(uint64_t key, int kind);\n"
            "static int obj_cache_due;\n"
            "#define obj_lookup counted_lookup",
            cache.replace("static Obj *obj_nockpt(uint64_t key, int kind);", ""),
            "#undef obj_lookup",
            real.replace("static Obj *obj_lookup(uint64_t key, int kind) {", "static Obj *obj_lookup_real(uint64_t key, int kind) {", 1),
            drop]


def compiler(given, environ=None, windows=None, which=shutil.which, isfile=os.path.isfile):
    """--cc, then $CC; then on Windows the configured llvm-mingw's clang (LONGSHOT_MINGW), else a C compiler on PATH; on
    any other OS a C compiler on PATH first (cc, clang, gcc), else LONGSHOT_MINGW's clang. The fixtures link with
    -fsanitize=address,undefined off Windows, and the pinned llvm-mingw strips its Linux compiler runtimes, so a Linux
    developer with LONGSHOT_MINGW set (dev.py env) got a link failure. environ, windows, which and isfile are for the
    choice's own check (compiler_choice_check)"""
    environ = os.environ if environ is None else environ
    windows = os.name == "nt" if windows is None else windows
    for c in (given, environ.get("CC")):
        if c:
            return which(c) or (c if isfile(c) else None)

    def mingw():
        m = environ.get("LONGSHOT_MINGW")
        c = os.path.join(m, "bin", "clang.exe" if windows else "clang") if m else None
        return c if c and isfile(c) else None

    def on_path():
        return next((which(n) for n in ("cc", "clang", "gcc") if which(n)), None)

    return (mingw() or on_path()) if windows else (on_path() or mingw())


def compiler_choice_check():
    """compiler()'s order over a fake PATH and file system: --cc and CC first everywhere; llvm-mingw before PATH on Windows
    only; a native compiler before llvm-mingw elsewhere, llvm-mingw when there is none. Each Linux case is also asked as
    Windows (the broken variant: the old order, llvm-mingw first everywhere) and must then answer differently"""
    files = {os.path.join("/mingw", "bin", "clang"), os.path.join("/mingw", "bin", "clang.exe")}
    path = {"cc": "/usr/bin/cc", "gcc": "/usr/bin/gcc"}
    isfile = files.__contains__
    mingw_env = {"LONGSHOT_MINGW": "/mingw"}

    def pick(given, env, windows, which=path.get):
        return compiler(given, environ=env, windows=windows, which=which, isfile=isfile)

    cases = [   # (what, given, env, windows, which, wanted)
        ("--cc first", "gcc", dict(mingw_env, CC="cc"), False, path.get, "/usr/bin/gcc"),
        ("CC before llvm-mingw", None, dict(mingw_env, CC="gcc"), True, path.get, "/usr/bin/gcc"),
        ("Linux: a native compiler before llvm-mingw", None, mingw_env, False, path.get, "/usr/bin/cc"),
        ("Linux: llvm-mingw when PATH has none", None, mingw_env, False, {}.get, os.path.join("/mingw", "bin", "clang")),
        ("Windows: llvm-mingw first", None, mingw_env, True, path.get, os.path.join("/mingw", "bin", "clang.exe")),
        ("Windows: PATH without llvm-mingw", None, {}, True, path.get, "/usr/bin/cc"),
    ]
    for what, given, env, windows, which, wanted in cases:
        got = pick(given, env, windows, which)
        if got != wanted:
            raise SystemExit("FAIL: the fixture's compiler choice, %s: %r, not %r" % (what, got, wanted))
    if pick(None, mingw_env, True) == pick(None, mingw_env, False):
        raise SystemExit("FAIL: the compiler choice does not tell Windows (llvm-mingw first) from Linux (native first)")
    print("ok: the fixture's compiler: --cc, CC, then llvm-mingw first on Windows only, a native one first elsewhere")


def identity_read_once(body):
    """obj_is's body reads the object's identity as one word: exactly one atomic load, of o->ident, and no read of
    o->key or o->kind (comments left out)"""
    code = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
    loads = re.findall(r"__atomic_load_n\(([^,]+),", code)
    return loads == ["&o->ident"] and not re.search(r"o->(key|kind)\b", code)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cc")
    ap.add_argument("--bench", action="store_true")
    a = ap.parse_args()
    compiler_choice_check()
    cc = compiler(a.cc)
    if not cc:
        raise SystemExit("SKIP: no C compiler (--cc, CC, LONGSHOT_MINGW, or cc, clang or gcc on PATH)")
    src = (ROOT / "host/nn_os_sync.c").read_text(encoding="utf-8")
    if "obj_cache_flush()" not in block(src, "static Obj *obj(uint64_t key, int kind) {"):
        raise SystemExit("FAIL: obj() does not add the sized cache's counts up (obj_cache_flush)")
    nock = block(src, "static Obj *obj_nockpt(uint64_t key, int kind) {")
    if ("obj_lookup_cached(key, kind, mode)" not in nock or "obj_cache_mode_read()" not in nock or
            'getenv("HOS_OBJ_CACHE")' not in block(src, "static __attribute__((noinline, cold)) int obj_cache_mode_first(void) {")):
        raise SystemExit("FAIL: obj_nockpt does not take HOS_OBJ_CACHE's sized cache")
    # (obj_is reads an object's identity in one load of its ident word. The reuse mode below sees only atomic loads: a split
    # read, the kind atomically and the key plainly, would pass it, so the body is checked here, with a split variant)
    is_head = "static inline int obj_is(Obj *o, uint64_t key, int kind) {"
    split = is_head + ("\n    return __atomic_load_n(&o->kind, __ATOMIC_ACQUIRE) == kind && o->key == key;\n}")
    for body, want in ((block(src, is_head), True), (split, False)):
        if identity_read_once(body) != want:
            raise SystemExit("FAIL: obj_is does not read the identity in one load of o->ident" if want else
                             "FAIL: the identity check takes a split read (the kind, then the key) for one load")
    print("ok: obj_is reads the identity in one load; a split read of the kind and the key is refused")
    code = ENV_ON_H.read_text(encoding="utf-8") + HARNESS + "\n".join(source_pieces(src)) + MAIN
    with tempfile.TemporaryDirectory() as tmp:
        c = os.path.join(tmp, "obj_cache.c")
        exe = os.path.join(tmp, "obj_cache.exe" if os.name == "nt" else "obj_cache")
        Path(c).write_text(code, encoding="utf-8")
        flags = ["-O2", "-std=gnu11", "-w"] + (["-DOBJ_BENCH"] if a.bench else [])
        if os.name != "nt":
            flags += ["-fsanitize=address,undefined", "-pthread"]
        r = subprocess.run([cc, *flags, c, "-o", exe], capture_output=True, text=True)
        if r.returncode:
            sys.stderr.write(r.stdout + r.stderr)
            raise SystemExit("FAIL: the fixture does not build")
        # (the lookup's own path: the mode word read inline, no call on every lookup; its first read, the switches, out of
        # line. 712ee7e7's obj_cache_mode_read was a function of its own that every lookup called, in every mode)
        s = os.path.join(tmp, "obj_cache.s")
        r = subprocess.run([cc, *flags, "-S", c, "-o", s], capture_output=True, text=True)
        if r.returncode:
            sys.stderr.write(r.stdout + r.stderr)
            raise SystemExit("FAIL: the fixture's assembly does not build")
        failed = []   # (every check and run, then the failures together)
        if re.search(r"\bcall[lq]?\s+\*?obj_cache_mode_read\b", Path(s).read_text(encoding="utf-8", errors="replace")):
            print("FAIL: a lookup calls obj_cache_mode_read: the mode is to be read inline (a load and a compare)")
            failed.append("the mode read inline")
        else:
            print("ok: the cache mode is read inline (no call to obj_cache_mode_read)")
        runs = [("entries", None)] + ([("bench", v) for v in ("0", "4096", None, "65536", "sets")] if a.bench else
                                      [("check", v) for v in (None, "0", "256", "sets")] +
                                      [("reuse", v) for v in ("sets", None, "0", "off")] +
                                      [("churn", "sets"), ("fiber", "sets")])
        for mode, value in runs:
            env = dict(os.environ)
            for name in ("HOS_OBJ_CACHE", "HOS_NO_OBJ_CACHE", "HOS_KEEP_DEAD_OBJECTS", "HOS_SYNC_TABLE_LOCK",
                         "HOS_MUTEX_GUEST_ID"):
                env.pop(name, None)   # (each read by the cut code: a developer's own value would change what runs)
            if value == "off":   # (no cache: HOS_NO_OBJ_CACHE=1, the table's unlocked path)
                env["HOS_NO_OBJ_CACHE"] = "1"
            elif value:
                env["HOS_OBJ_CACHE"] = value
            r = subprocess.run([exe, mode], capture_output=True, text=True, env=env, timeout=600)
            sys.stdout.write(r.stdout)
            if r.returncode:
                sys.stdout.write(r.stderr)
                failed.append("%s with %s" % (mode, "HOS_NO_OBJ_CACHE=1" if value == "off" else
                                              "HOS_OBJ_CACHE=" + value if value else "HOS_OBJ_CACHE unset"))
    if failed:
        raise SystemExit("FAIL: " + "; ".join(failed))
    print("ok: test_obj_cache_size")


if __name__ == "__main__":
    main()
