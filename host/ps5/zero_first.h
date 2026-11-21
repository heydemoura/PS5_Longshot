/* (PS5) Direct memory for the guest window reads zero from the first moment the game can reach it.
   SPDX-License-Identifier: GPL-3.0-or-later

   The kernel hands direct memory out as it was left, so a unit's memory has to be zeroed. Mapped at the guest address and
   zeroed there, every thread of the game could reach it before and while the zeros were written: a thread that stored a
   pointer in the unit in that time (it does not fault: the unit is mapped) had its store wiped by the memset, and the game
   read a null pointer later; a thread that read there saw what the memory held before. So the memory is zeroed through a
   mapping of its own first, and only then mapped where the game looks.

   Seen on a PS5 Pro (2026-11-21): 7 of 120 starts ended as the world came up after the save loaded, each a read at
   null + a small offset in the game's own code, in five different functions on two threads; three of them in one loop over
   a list of pointers that held a null entry.

   The kernel's two calls are passed in, so the order is tested off the console (tests/test_ps5_zero_first.py). */
#pragma once
#include <stdint.h>
#include <string.h>

/* 0: mapped, *at says where. fixed: at *at and nowhere else, as the game is to see it; not fixed: anywhere, for this
   thread alone */
typedef int (*ps5_zf_map)(void **at, uint64_t size, int64_t phys, int fixed);
typedef int (*ps5_zf_unmap)(void *at, uint64_t size);
/* (a test's, or none) called once a mapping, before its zeros are written: with the order off the game can reach the
   memory by then, with it on nothing is mapped where the game looks yet */
typedef void (*ps5_zf_wait)(void);

/* Maps size bytes of direct memory phys at want, all zeros. first: zeroed before they are mapped there (0: after, as it
   was). Returns 1: zeroed first; 2: zeroed at want, after it was mapped (first off, or no mapping of its own to be had);
   0: not mapped at want, *err the kernel's answer */
static inline int ps5_map_zeroed(void *want, uint64_t size, int64_t phys, int first, ps5_zf_map map, ps5_zf_unmap unmap,
                                 ps5_zf_wait wait, int *err) {
    int zeroed = 0;
    if (first) {
        void *own = NULL;
        if (map(&own, size, phys, 0) == 0 && own) {
            if (wait) wait();
            memset(own, 0, size);
            unmap(own, size);
            zeroed = 1;
        }
    }
    void *at = want;
    const int r = map(&at, size, phys, 1);
    if (r || at != want) {
        if (err) *err = r;
        return 0;
    }
    if (zeroed) return 1;
    if (wait) wait();
    memset(want, 0, size);
    return 2;
}
