"""On the console a guest unit's direct memory reads zero before the game can reach it (host/ps5/zero_first.h).

The kernel hands direct memory out as it was left. Mapped at the guest address and zeroed there, another thread of the
game could store in the unit between the mapping and the zeros, and the store was wiped: the game read a null pointer
later. The header's order is run here on Linux, the kernel's two calls stood in for by a memory file:
- when the memory is mapped where the game looks, it already reads zero, and the mapping it was zeroed through is gone;
- with the order off (HOS_PS5_ZERO_FIRST=0), or with no mapping of its own to be had, it is zeroed where the game looks,
  as before, and the answer says so;
- a mapping the kernel refuses is told, with the kernel's answer;
- a test's wait (HOS_PS5_ZERO_WAIT_US) comes once a mapping, before the zeros, in either order;
- the race itself: a thread that stores in the unit as soon as it can keeps its store with the order on, every time; with
  the order off it loses it (counted, and expected on a machine with more than one processor).
Then the source: commit_units maps through the header, and nothing zeroes a unit at the guest address after it.

python -I -B tests/test_ps5_zero_first.py [--cc cc]
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_ALL_ARGS = ["--cc", "gcc"]

CHECK = r"""
#define _GNU_SOURCE
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>
#include "zero_first.h"

#define SIZE (1u << 20)
#define CHECKED(x) do { if (!(x)) { printf("FAIL line %d: %s\n", __LINE__, #x); exit(1); } } while (0)

static int fd, maps, unmaps, refuse_own, refuse_fixed, dirty_at_fixed, own_left, fixed_made, waits, wait_saw_fixed, wait_saw_dirty;
static void *own_at;

static int file_reads_zero(int64_t phys, uint64_t size) {
    static unsigned char buf[SIZE];
    CHECKED(pread(fd, buf, size, phys) == (ssize_t)size);
    for (uint64_t i = 0; i < size; i++) if (buf[i]) return 0;
    return 1;
}
/* (the kernel: a fixed mapping is where the game looks; what the memory holds at that moment is what the game can see) */
static int fake_map(void **at, uint64_t size, int64_t phys, int fixed) {
    maps++;
    if (fixed) {
        if (refuse_fixed) return -12;
        dirty_at_fixed = !file_reads_zero(phys, size);
        own_left = own_at != NULL;
        void *p = mmap(*at, size, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_FIXED, fd, phys);
        fixed_made = p == *at;
        return p == *at ? 0 : -1;
    }
    if (refuse_own) return -12;
    void *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, phys);
    if (p == MAP_FAILED) return -1;
    *at = own_at = p;
    return 0;
}
static int fake_unmap(void *at, uint64_t size) {
    unmaps++;
    if (at == own_at) own_at = NULL;
    return munmap(at, size);
}
static void fake_wait(void) { waits++, wait_saw_fixed = fixed_made, wait_saw_dirty = !file_reads_zero(0, SIZE); }
static void dirty(int64_t phys) {
    static unsigned char buf[SIZE];
    memset(buf, 0xAB, sizeof buf);
    CHECKED(pwrite(fd, buf, SIZE, phys) == (ssize_t)SIZE);
}
static unsigned char *reserve(void) {
    void *p = mmap(NULL, SIZE, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECKED(p != MAP_FAILED);
    return p;
}
static int all_zero(const unsigned char *p) {
    for (unsigned i = 0; i < SIZE; i++) if (p[i]) return 0;
    return 1;
}

/* the race: a thread stores at the end of the unit as soon as the store does not fault (the fault is tried again, as the
   port's handler does for a unit another thread is backing) */
static void again(int sig) { (void)sig; }
static volatile unsigned long long *volatile target;
static pthread_barrier_t go;
static void *storer(void *unused) {
    (void)unused;
    pthread_barrier_wait(&go);
    *target = 0x1122334455667788ull;
    return NULL;
}
static int lost_stores(int first, int rounds) {
    int lost = 0;
    for (int i = 0; i < rounds; i++) {
        unsigned char *want = reserve();
        dirty(0);
        target = (volatile unsigned long long *)(want + SIZE - 8);
        pthread_t t;
        pthread_barrier_init(&go, NULL, 2);
        CHECKED(pthread_create(&t, NULL, storer, NULL) == 0);
        pthread_barrier_wait(&go);
        int err = 0;
        CHECKED(ps5_map_zeroed(want, SIZE, 0, first, fake_map, fake_unmap, NULL, &err) == (first ? 1 : 2));
        pthread_join(t, NULL);
        pthread_barrier_destroy(&go);
        lost += *target != 0x1122334455667788ull;
        munmap(want, SIZE);
    }
    return lost;
}

int main(void) {
    fd = memfd_create("direct", 0);
    CHECKED(fd >= 0 && ftruncate(fd, 2 * SIZE) == 0);
    int err = 0;

    /* the order on: zero already when it is mapped where the game looks, the own mapping gone by then */
    unsigned char *want = reserve();
    dirty(0), maps = unmaps = 0;
    CHECKED(ps5_map_zeroed(want, SIZE, 0, 1, fake_map, fake_unmap, NULL, &err) == 1);
    CHECKED(!dirty_at_fixed && !own_left && maps == 2 && unmaps == 1 && all_zero(want));
    /* it is that memory, not other: a store shows in the file */
    want[5] = 7;
    CHECKED(!file_reads_zero(0, SIZE));
    munmap(want, SIZE);

    /* memory further on in the block, the first megabyte left as it is */
    want = reserve();
    dirty(0), dirty(SIZE);
    CHECKED(ps5_map_zeroed(want, SIZE, SIZE, 1, fake_map, fake_unmap, NULL, &err) == 1);
    CHECKED(!dirty_at_fixed && all_zero(want) && !file_reads_zero(0, SIZE));
    munmap(want, SIZE);

    /* the order off: mapped with what it held, zeroed there, and the answer says so */
    want = reserve();
    dirty(0), maps = unmaps = 0;
    CHECKED(ps5_map_zeroed(want, SIZE, 0, 0, fake_map, fake_unmap, NULL, &err) == 2);
    CHECKED(dirty_at_fixed && maps == 1 && unmaps == 0 && all_zero(want));
    munmap(want, SIZE);

    /* no mapping of its own to be had: as with the order off, never left unzeroed */
    want = reserve();
    dirty(0), refuse_own = 1;
    CHECKED(ps5_map_zeroed(want, SIZE, 0, 1, fake_map, fake_unmap, NULL, &err) == 2);
    CHECKED(dirty_at_fixed && all_zero(want));
    refuse_own = 0;
    munmap(want, SIZE);

    /* the kernel refuses the mapping at the guest address: told, with its answer */
    want = reserve();
    dirty(0), refuse_fixed = 1, err = 0;
    CHECKED(ps5_map_zeroed(want, SIZE, 0, 1, fake_map, fake_unmap, NULL, &err) == 0 && err == -12);
    refuse_fixed = 0;
    munmap(want, SIZE);

    /* the test's wait: once, before the zeros; with the order on nothing is mapped where the game looks by then */
    want = reserve();
    dirty(0), waits = fixed_made = 0;
    CHECKED(ps5_map_zeroed(want, SIZE, 0, 1, fake_map, fake_unmap, fake_wait, &err) == 1);
    CHECKED(waits == 1 && !wait_saw_fixed && wait_saw_dirty && all_zero(want));
    munmap(want, SIZE);
    want = reserve();
    dirty(0), waits = fixed_made = 0;
    CHECKED(ps5_map_zeroed(want, SIZE, 0, 0, fake_map, fake_unmap, fake_wait, &err) == 2);
    CHECKED(waits == 1 && wait_saw_fixed && wait_saw_dirty && all_zero(want));
    munmap(want, SIZE);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = again;
    sigaction(SIGSEGV, &sa, NULL);
    const int rounds = 300;
    const int with = lost_stores(1, rounds), without = lost_stores(0, rounds);
    printf("stores lost in %d rounds: %d with the order on, %d with it off\n", rounds, with, without);
    CHECKED(with == 0);
    if (sysconf(_SC_NPROCESSORS_ONLN) > 1) CHECKED(without > 0);
    return 0;
}
"""


def block(text, signature):
    start = text.index(signature)
    brace = text.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cc", default="cc")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="ps5-zero-first-") as tmp:
        src, exe = Path(tmp) / "check.c", Path(tmp) / "check"
        src.write_text(CHECK, encoding="utf-8")
        subprocess.run([args.cc, "-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "host" / "ps5"), str(src),
                        "-o", str(exe), "-lpthread"], check=True)
        ran = subprocess.run([str(exe)], capture_output=True, text=True)
        sys.stdout.write(ran.stdout)
        if ran.returncode:
            raise SystemExit("FAIL: the order of zero_first.h (exit %d) %s" % (ran.returncode, ran.stderr.strip()))
    host = (ROOT / "host" / "ps5" / "win32_ps5.c").read_text(encoding="utf-8")
    commit = block(host, "static int commit_units(uintptr_t lo, uintptr_t hi)")
    own = block(host, "static int guest_map(void **at, uint64_t size, int64_t phys, int fixed)")
    on = block(host, "static int zero_first_on(void)")
    checks = [
        ("ps5_map_zeroed(want, got, phys, zero_first_on(), guest_map, guest_unmap, zero_wait, &r)" in commit,
         "commit_units does not map a unit's memory through zero_first.h"),
        ("memset(" not in commit and "sceKernelMapDirectMemory" not in commit,
         "commit_units maps or zeroes a unit by itself, at the guest address"),
        ("fixed ? PS5_KERNEL_MAP_FIXED : 0" in own and "(fixed ? GPU_RW : 0)" in own,
         "the mapping the memory is zeroed through is not this thread's own (a fixed place or GPU bits)"),
        ('getenv("HOS_PS5_ZERO_FIRST")' in on and '!strcmp(e, "0")' in on and "return !off;" in on,
         "the order is not on unless HOS_PS5_ZERO_FIRST=0"),
        ("commit_late += how == 2" in commit and "zeroed after they were mapped" in commit,
         "the commits zeroed in the game's sight are not counted and said"),
    ]
    bad = [why for ok, why in checks if not ok]
    if bad:
        raise SystemExit("FAIL: " + "; ".join(bad))
    print("PASS: on the console a guest unit's memory reads zero before the game can reach it")


if __name__ == "__main__":
    main()
