/* PS5: the guest window's swap (included by host/ps5/win32_ps5.c, which owns the window's units and their lock).

   The title has 12,016 MiB of direct memory for the game's pages, the platform heap and the driver, and the game alone
   reached 7.4 GB of them (2026-11-18): with the driver's 3 GB and the heap's 1.4 GB nothing was left, and the next page,
   texture or pipeline found none. A Switch 2 has more. Here the game's cold pages go to a file, as EglantinePS5's guest
   swap does (src/guest_memory_service.inc there: cold slots found by taking their access away, written with pwrite, the
   direct memory given back, read back in the fault handler through a view of their own), on a page table of our own:

   - a page is the window's 64 KiB unit. unit_state says whether it is mapped (U_COMMITTED), only reserved (U_RESERVED)
     or in the file (U_OUT); unit_sw has the swap's flags, unit_pin who holds it in memory, unit_slot its place in the
     file. All of them change under window_lock only.
   - reads and writes are told apart: a unit read back is mapped read-only and keeps its file copy (SW_CLEAN), so it can
     leave again with no write; its first write faults, makes it writable and the copy stale (the place is kept for its
     next trip out).
   - cold: while memory is getting short (room under HOS_PS5_SWAP_WATCH_MB) a part of the resident units each second has
     its CPU access taken away (SW_ARMED); a touch gives it back. A unit still armed after HOS_PS5_SWAP_COLD_S seconds was
     not used in that time.
   - out: under HOS_PS5_SWAP_LOW_MB of room, the swap thread takes units (cold and clean first, then cold, then armed,
     then any when it is critical), makes them read-only, writes those whose copy is stale, unmaps them and gives their
     direct memory back to the kernel, until HOS_PS5_SWAP_HIGH_MB are free. A write during the trip waits for it to end.
   - in: every path that maps a unit (the fault handler, commit_units: the first-touch commit, hos_ps5_commit_now, the
     file reads' and writes' buffers, VirtualProtect) reads an out unit back first. Nothing reads zeros for a unit in the
     file.
   - held in memory (unit_pin): the GPU's pools (hos_ps5_commit_now, from the Vulkan import: the GPU does not fault), a
     file read or write's buffer while the kernel uses it (hos_ps5_io_hold: the kernel's access to an armed or read-only
     page would fail), units the game or the renderer protect themselves (VirtualProtect).
   - the file: HOS_PS5_SWAP_DIR (/mnt/ext1/longshot) guest-swap.bin, HOS_PS5_SWAP_MB (4096) or less when the disk has less
     room (its free space less 2 GiB), written with zeros once, a part at a time while memory is not short, and kept for
     the next runs (a file grown at its end is slow on exFAT: EglantinePS5's notes). With the file full, places are taken
     from units whose copy is stale; with none, only clean units leave.
   - waiting: an allocation that found no memory (the heap's, operator new's, a guest commit's) asks the swap thread for
     a pass at once (hos_ps5_swap_need). */

#include <fcntl.h>
#include <unistd.h>
#include <sys/statvfs.h>
#include <sys/stat.h>

#define SW_ARMED 0x01    /* CPU access taken away: cold if still so after the cold time */
#define SW_CLEAN 0x02    /* the file's copy is the unit's (mapped read-only) */
#define SW_BUSY 0x04     /* on its way out (mapped read-only) */
#define SW_WANTED 0x08   /* a write waits for the trip to end */
#define SW_SLOT 0x10     /* unit_slot is its place */
#define PIN_GPU 0x80
#define PIN_PROT 0x40
#define PIN_IO 0x3f
#define SWAP_BATCH 256   /* units a pass takes at most (16 MiB) */

static uint8_t *unit_sw, *unit_pin;
static uint32_t *unit_slot;
static uint16_t *unit_tick;
static int swap_fd = -1, swap_live;
static uint64_t slot_count, slot_hint;
static volatile uint64_t slots_ready;
static uint64_t *slot_bits;
static uint32_t *slot_owner;            /* the unit + 1 a place holds, 0 none */
static uint16_t swap_now_tick;
static volatile long window_owner;
static pthread_mutex_t swap_cv_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t swap_cv = PTHREAD_COND_INITIALIZER, swap_done_cv = PTHREAD_COND_INITIALIZER;
static volatile uint64_t swap_urgent_bytes, swap_passes, swap_contig_bytes;
static int release_broken;
static uint64_t sw_low, sw_high, sw_watch, sw_cold, sw_contig;
uint64_t hos_ps5_direct_free(void);
static struct {
    uint64_t out_units, ins, in_faults, outs, written, read, released, armed_now, rearmed, stolen, no_slot, in_us_max, contig_passes,
        ahead, in_calls, write_waits;
    double in_ms;
} sws;

static void window_lock_take(void) {
    pthread_mutex_lock(&window_lock);
    window_owner = (long)hos_ps5_tid();
}
static void window_lock_give(void) {
    window_owner = 0;
    pthread_mutex_unlock(&window_lock);
}

/* The page table, made with the window (reserve_window). The free list is made whole here too (8 MiB: every unit's), so
   nothing under window_lock ever allocates: an allocation that waits for memory asks the swap for a pass, which needs
   the lock. All or none: unit_sw NULL means no swap and nothing held */
static int swap_tables(void) {
    uint8_t *sw = calloc(GUEST_UNITS, 1), *pin = calloc(GUEST_UNITS, 1);
    uint32_t *slot = calloc(GUEST_UNITS, sizeof *slot);
    uint16_t *tick = calloc(GUEST_UNITS, sizeof *tick);
    int64_t *fl = free_units ? NULL : malloc(GUEST_UNITS * sizeof *fl);
    if (!sw || !pin || !slot || !tick || (!free_units && !fl)) {
        free(sw), free(pin), free(slot), free(tick), free(fl);
        return 0;
    }
    if (fl) {
        memcpy(fl, free_units, free_count * sizeof *fl);
        free_units = fl, free_cap = GUEST_UNITS;
    }
    unit_pin = pin, unit_slot = slot, unit_tick = tick;
    __atomic_store_n(&unit_sw, sw, __ATOMIC_RELEASE);
    return 1;
}

static void *unit_addr(uint64_t u) { return (void *)(HOS_PS5_GUEST_LO + u * UNIT); }

static void unit_protect(uint64_t u, uint64_t n, int cpu) { sceKernelMprotect(unit_addr(u), n * UNIT, cpu | GPU_RW); }

static void slot_free(uint64_t u) {
    if (!(unit_sw[u] & SW_SLOT)) return;
    const uint32_t s = unit_slot[u];
    slot_bits[s / 64] &= ~(1ULL << (s % 64));
    slot_owner[s] = 0;
    unit_sw[u] &= (uint8_t)~(SW_SLOT | SW_CLEAN);
}

/* A place for unit u: its own, a free one (after the last given, so a run of units gets a run of places), or with
   steal one a resident unit's whose copy is stale. -1: none */
static int64_t slot_get(uint64_t u, int steal) {
    if (unit_sw[u] & SW_SLOT) return unit_slot[u];
    const uint64_t ready = __atomic_load_n(&slots_ready, __ATOMIC_ACQUIRE);
    for (uint64_t k = 0; k < ready; k++) {
        uint64_t s = (slot_hint + k) % ready;
        if (slot_bits[s / 64] == ~0ULL) { k += 63 - s % 64; continue; }
        if (slot_bits[s / 64] >> (s % 64) & 1) continue;
        slot_bits[s / 64] |= 1ULL << (s % 64);
        slot_owner[s] = (uint32_t)(u + 1);
        unit_slot[u] = (uint32_t)s, unit_sw[u] |= SW_SLOT;
        slot_hint = s + 1;
        return (int64_t)s;
    }
    if (!steal) return -1;
    for (uint64_t k = 0; k < ready; k++) {
        const uint64_t s = (slot_hint + k) % ready;
        const uint32_t o = slot_owner[s];
        if (!o) continue;
        const uint64_t v = o - 1;
        if (unit_state[v] != U_COMMITTED || (unit_sw[v] & (SW_CLEAN | SW_BUSY))) continue;
        unit_sw[v] &= (uint8_t)~SW_SLOT;
        slot_owner[s] = (uint32_t)(u + 1);
        unit_slot[u] = (uint32_t)s, unit_sw[u] |= SW_SLOT;
        slot_hint = s + 1;
        sws.stolen++;
        return (int64_t)s;
    }
    return -1;
}

static void free_push(int64_t phys) {   /* (never grows: swap_tables made it whole) */
    if (free_count == free_cap) {
        if (!sceKernelReleaseDirectMemory(phys, UNIT)) sws.released++;
        return;
    }
    free_units[free_count++] = phys;
}

/* Direct memory back to the kernel, where the heap and the driver can have it; kept for the next commits if the kernel
   will not take part of a block back */
static void phys_give_back(int64_t phys) {
    if (!release_broken) {
        const int r = sceKernelReleaseDirectMemory(phys, UNIT);
        if (!r) { sws.released++; return; }
        release_broken = 1;
        hos_ps5_klog_now("[ps5] swap: the kernel took no part of a block back (%#x): units out are kept for the game's next pages", (unsigned)r);
    }
    free_push(phys);
}

/* The kept units of decommits and trips out, back to the kernel (up to want bytes); window_lock held */
static uint64_t free_give_back(uint64_t want) {
    uint64_t n = 0;
    while (free_count && n < want && !release_broken) {
        const int64_t phys = free_units[free_count - 1];
        if (sceKernelReleaseDirectMemory(phys, UNIT)) { release_broken = 1; break; }
        free_count--, n += UNIT, sws.released++;
    }
    return n;
}

/* The unit's mapping gone and the range reserved again, as hos_ps5_decommit does; its memory to phys_give_back */
static int unit_unmap(uint64_t u) {
    void *place = unit_addr(u);
    if (sceKernelMunmap(place, UNIT) != 0) {
        hos_ps5_klog_now("[ps5] swap: %#llx would not unmap", (unsigned long long)(uintptr_t)unit_addr(u));
        return 0;
    }
    /* (unmapped: out whatever follows, its copy is the file's. Not reserved again, the range is only open to the kernel's
       own choice of address until it is read back over it, MAP_FIXED) */
    if (sceKernelReserveVirtualRange(&place, UNIT, PS5_KERNEL_MAP_FIXED, UNIT) != 0 || place != unit_addr(u))
        hos_ps5_klog_now("[ps5] swap: %#llx could not be reserved again", (unsigned long long)(uintptr_t)unit_addr(u));
    return 1;
}

/* Clean units out with no write (their copy is the file's): for a unit read back when memory is short. A part of the
   window a call (the fault handler's lock hold kept short). window_lock held */
static uint64_t evict_clean_locked(uint64_t want, int64_t *keep_phys) {
    uint64_t got = 0;
    if (!swap_live) return 0;
    static uint64_t hand;
    for (uint64_t k = 0; k < 65536 && got < want; k++) {
        const uint64_t u = (hand + k) % GUEST_UNITS;
        if (unit_state[u] != U_COMMITTED || unit_pin[u] || (unit_sw[u] & (SW_CLEAN | SW_BUSY)) != SW_CLEAN) continue;
        if (!unit_unmap(u)) continue;
        __atomic_store_n(&unit_state[u], U_OUT, __ATOMIC_RELEASE);
        unit_sw[u] &= (uint8_t)~SW_ARMED;
        committed_bytes -= UNIT, sws.out_units++, sws.outs++;
        if (keep_phys && !got) *keep_phys = unit_phys[u];
        else phys_give_back(unit_phys[u]);
        got++;
    }
    hand += 65536;
    return got;
}

static uint64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + (uint64_t)ts.tv_nsec / 1000;
}

void hos_ps5_swap_need(size_t bytes);

#define VIEWS 8          /* read-backs under way at once, each with a view of its own */
#define AHEAD 16         /* units one read-back brings (1 MiB): the unit and the ones after it in the window and the file */
static void *views[VIEWS];
static volatile int reads_in_flight;   /* (read-backs reading the file now: the writes give way to them) */
static uint8_t view_used[VIEWS];

/* A unit's memory for a read-back: the free list's, a new piece, or a clean unit's taken out for it; -1 none */
static int64_t phys_for_in_locked(int may_evict) {
    uint64_t got = 0;
    if (free_count) return free_units[--free_count];
    if (!may_evict && block_next >= block_end) return -1;   /* (read-ahead takes no new block of direct memory) */
    int64_t phys = direct_take(UNIT, &got);
    if (phys >= 0) return phys;
    phys = -1;
    if (may_evict && evict_clean_locked(1, &phys) && phys >= 0) return phys;
    return -1;
}

/* Unit u back from the file, with up to AHEAD - 1 units after it that are out and lie after it in the file too (what
   the game touches next is mostly there: one read of up to 1 MiB, not one of 64 KiB a fault). The units are marked busy
   (on their way in) and window_lock is given up for the read, so that a slow read stops no other thread's fault (a
   read-back held the lock: 13 ms each, 2.9 s the most, every fault behind it, 2026-11-18). A thread that faults on a unit
   on its way in waits for it. Each is mapped read-only and clean, u writable if write. window_lock held on entry and
   return (given up while it waits for memory with wait; without, a commit's own wait does: 0 at once) */
static int swap_in_locked(uint64_t u, int write, int wait) {
    const uint64_t t0 = now_us();
    while (unit_state[u] == U_OUT && (unit_sw[u] & SW_BUSY)) {   /* (on its way in by another thread) */
        window_lock_give();
        sceKernelUsleep(200);
        window_lock_take();
    }
    if (unit_state[u] != U_OUT) return unit_state[u] == U_COMMITTED;
    int64_t phys[AHEAD];
    for (int tries = 0;; tries++) {
        if ((phys[0] = phys_for_in_locked(1)) >= 0) break;
        if (!wait) return 0;
        if (tries == 0) hos_ps5_klog_now("[ps5] swap: no memory to read %#llx back: waiting for some", (unsigned long long)(uintptr_t)unit_addr(u));
        if (tries >= 500) {
            hos_ps5_klog_now("[ps5] swap: no memory to read %#llx back after 10 s", (unsigned long long)(uintptr_t)unit_addr(u));
            return 0;
        }
        window_lock_give();
        hos_ps5_swap_need(UNIT);
        sceKernelUsleep(20000);
        window_lock_take();
        if (unit_state[u] != U_OUT || (unit_sw[u] & SW_BUSY)) return swap_in_locked(u, write, wait);
    }
    /* (the ones after it: out, not busy, the next place in the file, memory for each with no wait) */
    const uint32_t s0 = unit_slot[u];
    int k = 1;
    while (k < AHEAD && u + k < GUEST_UNITS && unit_state[u + k] == U_OUT && !(unit_sw[u + k] & SW_BUSY) &&
           (unit_sw[u + k] & SW_SLOT) && unit_slot[u + k] == s0 + (uint32_t)k && !unit_pin[u + k]) {
        if ((phys[k] = phys_for_in_locked(0)) < 0) break;
        k++;
    }
    for (int i = 0; i < k; i++) unit_sw[u + i] |= SW_BUSY;
    int v = -1;
    while (v < 0) {
        for (int i = 0; i < VIEWS && v < 0; i++)
            if (views[i] && !view_used[i]) v = i, view_used[i] = 1;
        if (v < 0) {
            window_lock_give();
            sceKernelUsleep(200);
            window_lock_take();
        }
    }
    window_lock_give();
    /* (the read, the lock given up) */
    int mapped = 0;
    for (; mapped < k; mapped++) {
        void *at = (char *)views[v] + (size_t)mapped * UNIT;
        if (sceKernelMapDirectMemory(&at, UNIT, PS5_KERNEL_PROT_CPU_READ | PS5_KERNEL_PROT_CPU_WRITE, PS5_KERNEL_MAP_FIXED,
                                     phys[mapped], UNIT) || at != (char *)views[v] + (size_t)mapped * UNIT)
            break;
    }
    size_t want = (size_t)mapped * UNIT, done = 0;
    __atomic_add_fetch(&reads_in_flight, 1, __ATOMIC_ACQ_REL);
    while (done < want) {
        const ssize_t r = pread(swap_fd, (char *)views[v] + done, want - done, (off_t)s0 * UNIT + (off_t)done);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) break;
        done += (size_t)r;
    }
    const int read_errno = errno;
    __atomic_sub_fetch(&reads_in_flight, 1, __ATOMIC_ACQ_REL);
    {
        void *at = views[v];
        sceKernelMunmap(at, (size_t)AHEAD * UNIT);
        sceKernelReserveVirtualRange(&at, (size_t)AHEAD * UNIT, PS5_KERNEL_MAP_FIXED, UNIT);
    }
    window_lock_take();
    view_used[v] = 0;
    const int whole = (int)(done / UNIT);   /* (units read whole) */
    int ok = 0;
    for (int i = 0; i < k; i++) {
        const uint64_t w = u + i;
        unit_sw[w] &= (uint8_t)~SW_BUSY;
        if (i >= whole || i >= mapped) {   /* (not read: its memory back, still out) */
            free_push(phys[i]);
            if (i == 0)
                hos_ps5_klog_now("[ps5] swap: %#llx could not be read back from the file (place %u, errno %d)",
                                 (unsigned long long)(uintptr_t)unit_addr(w), unit_slot[w], read_errno);
            continue;
        }
        void *at = unit_addr(w);
        const int wr = i == 0 && write;
        if (sceKernelMapDirectMemory(&at, UNIT, PS5_KERNEL_PROT_CPU_READ | (wr ? PS5_KERNEL_PROT_CPU_WRITE : 0) | GPU_RW,
                                     PS5_KERNEL_MAP_FIXED, phys[i], UNIT) || at != unit_addr(w)) {
            hos_ps5_klog_now("[ps5] swap: %#llx would not map again", (unsigned long long)(uintptr_t)unit_addr(w));
            free_push(phys[i]);
            continue;
        }
        unit_phys[w] = phys[i];
        unit_sw[w] = (uint8_t)((unit_sw[w] & SW_SLOT) | (wr ? 0 : SW_CLEAN));
        __atomic_store_n(&unit_state[w], U_COMMITTED, __ATOMIC_RELEASE);
        committed_bytes += UNIT, sws.out_units--, sws.ins++, sws.read += UNIT;
        if (i) sws.ahead++;
        if (!i) ok = 1;
    }
    const uint64_t us = now_us() - t0;
    sws.in_ms += us / 1e3, sws.in_calls++;
    if (us > sws.in_us_max) sws.in_us_max = us;
    return ok;
}

/* Units [u, end) held for the kernel or the GPU: read back, waited for if on their way out, writable and not armed.
   window_lock held */
static void units_hold_locked(uint64_t u, uint64_t end, uint8_t pin) {
    if (!unit_sw) return;
    for (; u < end; u++) {
        while (unit_sw[u] & SW_BUSY) {
            unit_sw[u] |= SW_WANTED;
            window_lock_give();
            sceKernelUsleep(500);
            window_lock_take();
        }
        /* (held before it is read back: a unit the GPU imports is never the swap's again, even if the read-back failed.
           An I/O hold at its ceiling stays for good: io_release leaves it) */
        if (pin == PIN_IO) {
            if ((unit_pin[u] & PIN_IO) < PIN_IO) unit_pin[u]++;
        } else unit_pin[u] |= pin;
        if (unit_state[u] == U_OUT && !swap_in_locked(u, 1, 1)) continue;
        if (unit_state[u] != U_COMMITTED) continue;
        if (unit_sw[u] & (SW_ARMED | SW_CLEAN)) {
            unit_sw[u] &= (uint8_t)~(SW_ARMED | SW_CLEAN);
            if (!(unit_pin[u] & PIN_PROT)) unit_protect(u, 1, PS5_KERNEL_PROT_CPU_READ | PS5_KERNEL_PROT_CPU_WRITE);
        }
    }
}

static void unit_range(const void *addr, size_t size, uint64_t *u, uint64_t *end) {
    const uintptr_t a = (uintptr_t)addr;
    *u = (a - HOS_PS5_GUEST_LO) / UNIT, *end = (a + size - HOS_PS5_GUEST_LO + UNIT - 1) / UNIT;
}

/* (ReadFile, WriteFile) the buffer backed, read back and held while the kernel reads or writes it */
void hos_ps5_io_hold(void *addr, size_t size) {
    const uintptr_t a = (uintptr_t)addr;
    if (!size || !window_reserved || !in_window(a, a + size)) return;
    hos_ps5_commit_now_plain(addr, size);
    if (!unit_sw) return;
    uint64_t u, end;
    unit_range(addr, size, &u, &end);
    window_lock_take();
    units_hold_locked(u, end, PIN_IO);
    window_lock_give();
}
void hos_ps5_io_release(void *addr, size_t size) {
    const uintptr_t a = (uintptr_t)addr;
    if (!size || !unit_sw || !window_reserved || !in_window(a, a + size)) return;
    uint64_t u, end;
    unit_range(addr, size, &u, &end);
    window_lock_take();
    for (; u < end; u++)
        if ((unit_pin[u] & PIN_IO) && (unit_pin[u] & PIN_IO) < PIN_IO) unit_pin[u]--;
    window_lock_give();
}

/* The signal handler's: a fault on a unit of the swap's (out, armed, clean and written, on its way out). 1: the access
   is tried again. 0: not the swap's (the other handlers have it) */
int hos_ps5_swap_fault(uintptr_t addr, int write) {
    if (!unit_sw || !window_reserved || !in_window(addr, addr + 1)) return 0;
    const uint64_t u = (addr - HOS_PS5_GUEST_LO) / UNIT;
    const uint8_t s0 = __atomic_load_n(&unit_state[u], __ATOMIC_ACQUIRE);
    if (s0 == U_RESERVED) return 0;   /* (never backed: the first-touch commit's) */
    if (window_owner == (long)hos_ps5_tid()) {   /* (this thread holds the lock: a fault the swap cannot take) */
        hos_ps5_klog_now("[ps5] swap: a fault at %#llx with the window's lock held", (unsigned long long)addr);
        return 0;
    }
    window_lock_take();
    int ok = 1;
    for (;;) {
        const uint8_t s = unit_state[u];
        if (s == U_RESERVED) { ok = 0; break; }
        if (s == U_OUT) {
            sws.in_faults++;
            ok = swap_in_locked(u, write, 1);
            break;
        }
        if (unit_sw[u] & SW_BUSY) {
            if (!write) break;   /* (read-only on its way out: readable) */
            unit_sw[u] |= SW_WANTED;
            window_lock_give();
            sceKernelUsleep(500);
            window_lock_take();
            continue;
        }
        if (unit_pin[u] & PIN_PROT) { ok = 0; break; }   /* (protected by its owner: not the swap's fault) */
        if (!(unit_sw[u] & (SW_ARMED | SW_CLEAN))) {
            /* (none of the swap's flags: a thread that faulted on what another just fixed is retried if the access is
               allowed now; otherwise the fault is not the swap's) */
            void *start = NULL, *end = NULL;
            uint32_t prot = 0;
            ok = sceKernelQueryMemoryProtection((void *)addr, &start, &end, &prot) == 0 &&
                 (prot & (write ? PS5_KERNEL_PROT_CPU_WRITE : PS5_KERNEL_PROT_CPU_READ));
            break;
        }
        if (unit_sw[u] & SW_ARMED) {
            unit_sw[u] &= (uint8_t)~SW_ARMED;
            sws.rearmed++;
        }
        if (write) unit_sw[u] &= (uint8_t)~SW_CLEAN;
        unit_protect(u, 1, PS5_KERNEL_PROT_CPU_READ | ((unit_sw[u] & SW_CLEAN) ? 0 : PS5_KERNEL_PROT_CPU_WRITE));
        break;
    }
    window_lock_give();
    return ok;
}

/* (hos_ps5_decommit) Units [u, end) given back by the game: a unit in the file reserved again (its place freed), the
   others' places, flags and holds dropped (a unit on its way out waited for). window_lock held */
static void decommit_units_locked(uint64_t lo, uint64_t end) {
    if (!unit_sw) return;
    /* (no unit of the range on its way out, all at once under one hold of the lock: a wait gives the lock up, and the
       swap may take a unit already seen meanwhile, so the scan starts again after one) */
    for (int again = 1; again;) {
        again = 0;
        for (uint64_t u = lo; u < end; u++)
            if (unit_sw[u] & SW_BUSY) {
                unit_sw[u] |= SW_WANTED;
                window_lock_give();
                sceKernelUsleep(500);
                window_lock_take();
                again = 1;
                break;
            }
    }
    for (uint64_t u = lo; u < end; u++) {
        if (unit_state[u] == U_OUT) {
            slot_free(u);
            __atomic_store_n(&unit_state[u], U_RESERVED, __ATOMIC_RELEASE);
            sws.out_units--;
        } else {
            slot_free(u);
            /* (writable again: the decommit may zero it in place, under the lock) */
            if (unit_state[u] == U_COMMITTED && (unit_sw[u] & (SW_ARMED | SW_CLEAN)) && !(unit_pin[u] & PIN_PROT))
                unit_protect(u, 1, PS5_KERNEL_PROT_CPU_READ | PS5_KERNEL_PROT_CPU_WRITE);
        }
        unit_sw[u] = 0, unit_pin[u] = 0;
    }
}

/* (commit_units) a unit backed afresh: none of the swap's state from before */
static void unit_fresh_locked(uint64_t u) {
    if (!unit_sw) return;
    slot_free(u);
    unit_sw[u] = 0, unit_pin[u] = 0;
}

/* ---- the swap thread -------------------------------------------------------------------------------------------- */
/* A pass asked for at once, up to 300 ms waited for: guest is a guest commit's (the units out go to its free list when the
   kernel takes no part of a block back), else the heap's or operator new's, which only memory given back to the kernel
   helps. At most 256 MiB a request */
static void swap_need_for(size_t bytes, int guest) {
    if (!swap_live || (release_broken && !guest)) return;
    pthread_mutex_lock(&swap_cv_lock);
    uint64_t want = bytes + (64ULL << 20);
    if (want > (256ULL << 20)) want = 256ULL << 20;
    if (swap_urgent_bytes < want) swap_urgent_bytes = want;
    if (swap_contig_bytes < bytes) swap_contig_bytes = bytes;   /* (one piece that large: contig_pass) */
    const uint64_t pass = swap_passes;
    pthread_cond_signal(&swap_cv);
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_nsec += 300000000;
    if (until.tv_nsec >= 1000000000) until.tv_sec++, until.tv_nsec -= 1000000000;
    while (swap_passes == pass && pthread_cond_timedwait(&swap_done_cv, &swap_cv_lock, &until) == 0) {}
    pthread_mutex_unlock(&swap_cv_lock);
}
void hos_ps5_swap_need(size_t bytes) { swap_need_for(bytes, 0); }
static void swap_need_guest(size_t bytes) { swap_need_for(bytes, 1); }

/* Each second while memory is getting short: a part of the resident units armed (their CPU access taken away) */
static void arm_tick(void) {
    static uint64_t hand;
    const uint64_t resident = committed_bytes / UNIT;
    uint64_t budget = resident / 16 > 256 ? resident / 16 : 256, armed = 0;
    window_lock_take();
    for (uint64_t k = 0; k < GUEST_UNITS && budget; k++) {
        const uint64_t u = (hand + k) % GUEST_UNITS;
        if (unit_state[u] != U_COMMITTED || unit_pin[u] || (unit_sw[u] & (SW_ARMED | SW_BUSY))) continue;
        uint64_t n = 1;
        while (n < budget && u + n < GUEST_UNITS && unit_state[u + n] == U_COMMITTED && !unit_pin[u + n] &&
               !(unit_sw[u + n] & (SW_ARMED | SW_BUSY)))
            n++;
        for (uint64_t i = 0; i < n; i++) unit_sw[u + i] |= SW_ARMED, unit_tick[u + i] = swap_now_tick;
        unit_protect(u, n, 0);
        armed += n, budget -= n, k += n - 1;
        hand = u + n;
    }
    sws.armed_now += armed;
    window_lock_give();
}

typedef struct { uint64_t u; int64_t slot; int write; } Victim;

/* Victims already on their way out (busy, read-only): the stale ones written, then each unmapped and its memory given
   back, unless a write waited, a hold came, or it was given back meanwhile. The bytes given back */
static uint64_t evict_run(Victim *v, int n) {
    /* (the writes, a run of units with a run of places at a time) */
    for (int i = 0; i < n;) {
        if (!v[i].write) { i++; continue; }
        int j = i + 1;
        while (j < n && v[j].write && v[j].u == v[j - 1].u + 1 && v[j].slot == v[j - 1].slot + 1 && j - i < AHEAD) j++;
        /* (a read-back waits on the game's thread, a write on no one: the writes wait while one reads, up to 250 ms a run.
           Behind the writes on the same drive the read-backs took 56 ms on average and 2.9 s the most, and the game
           stood still, 2026-11-18) */
        for (int waited = 0; __atomic_load_n(&reads_in_flight, __ATOMIC_ACQUIRE) && waited < 250; waited++) {
            sceKernelUsleep(1000);
            sws.write_waits++;
        }
        const size_t bytes = (size_t)(j - i) * UNIT;
        size_t done = 0;
        while (done < bytes) {
            const ssize_t r = pwrite(swap_fd, (char *)unit_addr(v[i].u) + done, bytes - done, (off_t)v[i].slot * UNIT + (off_t)done);
            if (r < 0 && errno == EINTR) continue;
            if (r <= 0) break;
            done += (size_t)r;
        }
        if (done < bytes) {
            static int told;
            if (told++ < 8) hos_ps5_klog_now("[ps5] swap: the file refused %zu bytes (errno %d)", bytes, errno);
            for (int k = i; k < j; k++) v[k].write = -1;
        } else sws.written += bytes;
        i = j;
    }
    uint64_t gone = 0;
    window_lock_take();
    for (int i = 0; i < n; i++) {
        const uint64_t u = v[i].u;
        const int failed = v[i].write < 0, wanted = (unit_sw[u] & SW_WANTED) != 0, held = unit_pin[u] != 0;
        unit_sw[u] &= (uint8_t)~(SW_BUSY | SW_WANTED);
        if (unit_state[u] != U_COMMITTED) {   /* (given back meanwhile: none of its state stays. decommit waits for a trip) */
            slot_free(u);
            unit_sw[u] = 0;
            continue;
        }
        if (!failed) unit_sw[u] |= SW_CLEAN;   /* (read-only since before the write: the copy is the unit's) */
        if (failed || wanted || held) {
            /* (stays, not armed (its protection is no longer none): writable unless clean, or as its holder wants it) */
            unit_sw[u] &= (uint8_t)~SW_ARMED;
            if (held) unit_sw[u] &= (uint8_t)~SW_CLEAN;
            if (!(unit_pin[u] & PIN_PROT))
                unit_protect(u, 1, PS5_KERNEL_PROT_CPU_READ | ((unit_sw[u] & SW_CLEAN) ? 0 : PS5_KERNEL_PROT_CPU_WRITE));
            continue;
        }
        if (!unit_unmap(u)) continue;
        __atomic_store_n(&unit_state[u], U_OUT, __ATOMIC_RELEASE);
        unit_sw[u] &= (uint8_t)~SW_ARMED;
        committed_bytes -= UNIT, sws.out_units++, sws.outs++;
        phys_give_back(unit_phys[u]);
        gone += UNIT;
    }
    window_lock_give();
    return gone;
}

/* One pass out: up to SWAP_BATCH units, the best first. Returns the bytes given back */
static uint64_t evict_pass(uint64_t want, int critical) {
    static Victim v[SWAP_BATCH];
    static uint64_t hand;
    int n = 0;
    window_lock_take();
    /* (one scan from the hand, each candidate put in its kind's list, best first: 0 cold and clean, 1 cold, 2 armed and
       clean, 3 armed, 4 clean, 5 any; the last two only when it is critical. The lists' first SWAP_BATCH are kept) */
    static uint64_t list[6][SWAP_BATCH];
    int count[6] = {0};
    /* (cold ones only, unless it is critical: units armed a while but not yet cold went out and came straight back,
       15721 touched again for 20032 taken out, 2026-11-18) */
    const int kinds = critical ? 6 : 2;
    uint64_t need = want / UNIT + 1;
    if (need > SWAP_BATCH) need = SWAP_BATCH;
    for (uint64_t k = 0; k < GUEST_UNITS && count[0] < (int)need; k++) {
        const uint64_t u = (hand + k) % GUEST_UNITS;
        if (unit_state[u] != U_COMMITTED || unit_pin[u] || (unit_sw[u] & SW_BUSY)) continue;
        const int armed = (unit_sw[u] & SW_ARMED) != 0, clean = (unit_sw[u] & SW_CLEAN) != 0;
        const int cold = armed && (uint16_t)(swap_now_tick - unit_tick[u]) >= sw_cold;
        const int kind = cold ? (clean ? 0 : 1) : armed ? (clean ? 2 : 3) : (clean ? 4 : 5);
        if (kind < kinds && count[kind] < SWAP_BATCH) list[kind][count[kind]++] = u;
    }
    for (int kind = 0; kind < kinds && n < (int)need; kind++)
        for (int i = 0; i < count[kind] && n < (int)need; i++) {
            const uint64_t u = list[kind][i];
            const int clean = (unit_sw[u] & SW_CLEAN) != 0;
            int64_t slot = -1;
            if (!clean && (slot = slot_get(u, critical)) < 0) { sws.no_slot++; continue; }
            unit_sw[u] |= SW_BUSY;
            unit_sw[u] &= (uint8_t)~SW_WANTED;
            v[n].u = u, v[n].slot = clean ? -1 : slot, v[n].write = !clean;
            n++;
        }
    /* (the writes in the order of the units, for runs: kinds mix them) */
    for (int i = 1; i < n; i++)
        for (int j = i; j > 0 && v[j - 1].u > v[j].u; j--) {
            const Victim t = v[j];
            v[j] = v[j - 1], v[j - 1] = t;
        }
    if (n) hand = v[n - 1].u + 1;
    else hand += GUEST_UNITS / 8;
    /* (read-only for the trip: a write waits) */
    for (int i = 0; i < n; i++) unit_protect(v[i].u, 1, PS5_KERNEL_PROT_CPU_READ);
    window_lock_give();
    if (!n) return 0;
    return evict_run(v, n);
}

/* ---- large pieces: the direct memory the title shares ends up in small pieces (1254 MiB free in 3616, the largest
   9 MiB, 2026-11-18: units given back one at a time from all over, and the heap's 16 MiB and the driver's large buffers
   found none). A window of physical memory that is free, or the game's units the swap may take, or units its free list
   keeps, is emptied whole: the cheapest such window (clean units cost least, stale ones a write) ---- */
#define CONTIG_MAX 1024   /* (64 MiB a pass at most) */
static uint8_t *pclass;   /* a 64 KiB piece of direct memory: 0 someone else's, 1 free, 2 a clean unit, 3 a unit, 4 kept */
static uint32_t *powner;  /* the unit + 1 a piece backs */
static uint64_t pcount;


static uint64_t contig_pass(uint64_t target) {
    static Victim v[CONTIG_MAX];
    uint64_t w = (target + UNIT - 1) / UNIT;
    if (w > CONTIG_MAX) w = CONTIG_MAX;
    const uint64_t total = (uint64_t)sceKernelGetDirectMemorySize() / UNIT;
    if (!pclass) {
        pclass = malloc(total), powner = malloc(total * sizeof *powner), pcount = total;
        if (!pclass || !powner) { free(pclass), free(powner), pclass = NULL, powner = NULL; return 0; }
    }
    if (total < pcount) pcount = total;
    memset(pclass, 1, pcount);
    {   /* (who holds what: every allocated range is someone else's until the units say otherwise) */
        struct { int64_t start, end; int type; } q;
        int sceKernelDirectMemoryQuery(int64_t offset, int flags, void *info, size_t size);
        int64_t off = 0;
        while ((uint64_t)off < pcount * UNIT && sceKernelDirectMemoryQuery(off, 1, &q, sizeof q) == 0 && q.end > q.start) {
            for (int64_t ph = q.start / (int64_t)UNIT; ph < (q.end + (int64_t)UNIT - 1) / (int64_t)UNIT && (uint64_t)ph < pcount; ph++)
                pclass[ph] = 0;
            off = q.end;
        }
    }
    window_lock_take();
    memset(powner, 0, pcount * sizeof *powner);
    for (uint64_t u = 0; u < GUEST_UNITS; u++) {
        if (unit_state[u] != U_COMMITTED) continue;
        const uint64_t ph = (uint64_t)unit_phys[u] / UNIT;
        if (ph >= pcount) continue;
        powner[ph] = (uint32_t)(u + 1);
        if (!unit_pin[u] && !(unit_sw[u] & SW_BUSY)) pclass[ph] = (unit_sw[u] & SW_CLEAN) ? 2 : 3;
    }
    for (size_t i = 0; i < free_count; i++)
        if ((uint64_t)free_units[i] / UNIT < pcount) pclass[free_units[i] / UNIT] = 4;
    /* (the windows at 2 MiB steps, none with a piece of someone else's: the one that costs least) */
    uint64_t best = UINT64_MAX, best_at = 0;
    for (uint64_t at = 0; at + w <= pcount; at += 32) {
        uint64_t cost = 0, k = 0;
        for (; k < w; k++) {
            const uint8_t c = pclass[at + k];
            if (!c) break;
            cost += c == 3 ? 8 : c == 2 ? 1 : 0;
        }
        if (k < w) { at = (at + k) & ~31ULL; continue; }   /* (past the piece that stops it) */
        if (cost < best) best = cost, best_at = at;
        if (!cost) break;
    }
    if (best == UINT64_MAX) {
        window_lock_give();
        static int told;
        if (told++ < 4) hos_ps5_klog_now("[ps5] swap: no %llu MiB of direct memory the swap could make free in one piece",
                                         (unsigned long long)(w * UNIT >> 20));
        return 0;
    }
    /* (the free list's pieces in it given back; its units on their way out) */
    size_t kept = 0;
    uint64_t back = 0;
    for (size_t i = 0; i < free_count; i++) {
        const uint64_t ph = (uint64_t)free_units[i] / UNIT;
        if (ph >= best_at && ph < best_at + w && !sceKernelReleaseDirectMemory(free_units[i], UNIT)) { back += UNIT; sws.released++; continue; }
        free_units[kept++] = free_units[i];
    }
    free_count = kept;
    int n = 0;
    for (uint64_t ph = best_at; ph < best_at + w; ph++) {
        if (pclass[ph] != 2 && pclass[ph] != 3) continue;
        const uint64_t u = powner[ph] - 1;
        if (unit_state[u] != U_COMMITTED || unit_pin[u] || (unit_sw[u] & SW_BUSY)) continue;
        const int clean = (unit_sw[u] & SW_CLEAN) != 0;
        int64_t slot = -1;
        if (!clean && (slot = slot_get(u, 1)) < 0) { sws.no_slot++; continue; }
        unit_sw[u] |= SW_BUSY;
        unit_sw[u] &= (uint8_t)~SW_WANTED;
        v[n].u = u, v[n].slot = clean ? -1 : slot, v[n].write = !clean;
        n++;
    }
    for (int i = 1; i < n; i++)
        for (int j = i; j > 0 && v[j - 1].u > v[j].u; j--) {
            const Victim t = v[j];
            v[j] = v[j - 1], v[j - 1] = t;
        }
    for (int i = 0; i < n; i++) unit_protect(v[i].u, 1, PS5_KERNEL_PROT_CPU_READ);
    window_lock_give();
    const uint64_t gone = n ? evict_run(v, n) : 0;
    sws.contig_passes++;
    return back + gone;
}

static void *zero_chunk;
static int swap_open(void) {
    const char *dir = getenv("HOS_PS5_SWAP_DIR");
    if (!dir || !*dir) dir = "/mnt/ext1/longshot";
    mkdir(dir, 0777);
    char path[256];
    snprintf(path, sizeof path, "%s/guest-swap.bin", dir);
    swap_fd = open(path, O_RDWR | O_CREAT, 0644);
    if (swap_fd < 0) {
        hos_ps5_klog_now("[ps5] swap: %s would not open (errno %d): no swap", path, errno);
        return 0;
    }
    struct stat st;
    fstat(swap_fd, &st);
    const char *e = getenv("HOS_PS5_SWAP_MB");
    uint64_t want = (e && *e ? strtoull(e, NULL, 10) : 4096) << 20;
    struct statvfs sf;   /* (statvfs: the console's libraries have no statfs) */
    if (fstatvfs(swap_fd, &sf) == 0) {
        const uint64_t room = (uint64_t)sf.f_bavail * (uint64_t)sf.f_frsize + (uint64_t)st.st_size, margin = 2ULL << 30;
        if (room < want + margin) want = room > margin ? room - margin : 0;
    }
    if ((uint64_t)st.st_size > want) {   /* (a larger file from a run that asked for more: cut to this run's size) */
        if (ftruncate(swap_fd, (off_t)(want & ~(UNIT - 1))) == 0) st.st_size = (off_t)(want & ~(UNIT - 1));
        else want = (uint64_t)st.st_size;
    }
    want &= ~(UNIT - 1);
    if (want > (uint64_t)UINT32_MAX * UNIT) want = (uint64_t)UINT32_MAX * UNIT;
    if (want < (256ULL << 20)) {
        hos_ps5_klog_now("[ps5] swap: %s: %llu MiB is all the disk has room for: no swap", path, (unsigned long long)(want >> 20));
        return 0;
    }
    slot_count = want / UNIT;
    slot_bits = calloc((slot_count + 63) / 64, 8);
    slot_owner = calloc(slot_count, sizeof *slot_owner);
    zero_chunk = calloc(1, 8 << 20);
    if (!slot_bits || !slot_owner || !zero_chunk) return 0;
    slots_ready = (uint64_t)st.st_size / UNIT;
    if (slots_ready > slot_count) slots_ready = slot_count;
    /* (a round trip at the file's start: written, read back, compared) */
    uint8_t *a = malloc(UNIT), *b = malloc(UNIT);
    int ok = a && b;
    for (size_t i = 0; ok && i < UNIT; i++) a[i] = (uint8_t)(i * 131 + 7);
    ok = ok && pwrite(swap_fd, a, UNIT, 0) == (ssize_t)UNIT && pread(swap_fd, b, UNIT, 0) == (ssize_t)UNIT && !memcmp(a, b, UNIT);
    free(a), free(b);
    if (!ok) {
        hos_ps5_klog_now("[ps5] swap: %s failed its round trip (errno %d): no swap", path, errno);
        return 0;
    }
    if (!slots_ready) slots_ready = 1;
    for (int i = 0; i < VIEWS; i++) {   /* (a read-back's view each, AHEAD units) */
        views[i] = NULL;
        if (sceKernelReserveVirtualRange(&views[i], (size_t)AHEAD * UNIT, 0, UNIT) != 0) views[i] = NULL;
    }
    if (!views[0]) {
        hos_ps5_klog_now("[ps5] swap: no range for the read-back views: no swap");
        return 0;
    }
    hos_ps5_klog_now("[ps5] swap: %s, %llu MiB (%llu written before), out under %llu MiB free until %llu, watched under %llu, cold after %llu s",
                     path, (unsigned long long)(want >> 20), (unsigned long long)((uint64_t)st.st_size >> 20),
                     (unsigned long long)(sw_low >> 20), (unsigned long long)(sw_high >> 20), (unsigned long long)(sw_watch >> 20),
                     (unsigned long long)sw_cold);
    return 1;
}

/* The file written to its size, 8 MiB at a time, when memory is not short */
static void swap_prepare_some(void) {
    uint64_t ready = slots_ready;
    if (ready >= slot_count) return;
    const uint64_t n = slot_count - ready < 128 ? slot_count - ready : 128;
    if (pwrite(swap_fd, zero_chunk, n * UNIT, (off_t)(ready * UNIT)) != (ssize_t)(n * UNIT)) {
        static int told;
        if (!told++) hos_ps5_klog_now("[ps5] swap: the file stopped growing at %llu MiB (errno %d)", (unsigned long long)(ready * UNIT >> 20), errno);
        slot_count = ready;
        return;
    }
    __atomic_store_n(&slots_ready, ready + n, __ATOMIC_RELEASE);
    if (ready + n == slot_count) hos_ps5_klog_now("[ps5] swap: the file is ready (%llu MiB)", (unsigned long long)(slot_count * UNIT >> 20));
}

static uint64_t env_mb(const char *name, uint64_t otherwise) {
    const char *e = getenv(name);
    return (e && *e ? strtoull(e, NULL, 10) : otherwise) << 20;
}

static void *swap_thread(void *unused) {
    (void)unused;
    pthread_setname_np(pthread_self(), "hos guest swap");
    uint64_t last_second = now_us();
    for (;;) {
        pthread_mutex_lock(&swap_cv_lock);
        if (!swap_urgent_bytes) {
            struct timespec until;
            clock_gettime(CLOCK_REALTIME, &until);
            until.tv_nsec += 250000000;
            if (until.tv_nsec >= 1000000000) until.tv_sec++, until.tv_nsec -= 1000000000;
            pthread_cond_timedwait(&swap_cv, &swap_cv_lock, &until);
        }
        const uint64_t urgent = swap_urgent_bytes;
        swap_urgent_bytes = 0;
        pthread_mutex_unlock(&swap_cv_lock);
        if (!__atomic_load_n(&window_reserved, __ATOMIC_ACQUIRE) || !unit_sw) continue;   /* (the window not made yet) */

        /* (the units kept for the game's next pages count as room when the kernel takes no part of a block back: else
           every pass would find the same shortage and take the game out to the last unit) */
        window_lock_take();
        const uint64_t kept = release_broken ? (uint64_t)free_count * UNIT : 0;
        window_lock_give();
        const uint64_t room = direct_room_now(NULL) + kept;
        if (now_us() - last_second >= 1000000) {
            last_second = now_us();
            swap_now_tick++;
            if (room < sw_watch) arm_tick();
            else if (room > sw_watch + (512ULL << 20)) swap_prepare_some();
        }
        /* (one piece large enough: for a request's size, or sw_contig while memory is getting short) */
        {
            pthread_mutex_lock(&swap_cv_lock);
            uint64_t piece = swap_contig_bytes;
            swap_contig_bytes = 0;
            pthread_mutex_unlock(&swap_cv_lock);
            /* (on its own only when the largest piece is small: eight passes a tick for 64 MiB kept the drive writing) */
            if (room < sw_watch && piece < sw_contig && hos_ps5_direct_free() < sw_contig / 4) piece = sw_contig;
            if (piece && hos_ps5_direct_free() < piece) contig_pass(piece);
        }
        uint64_t want = urgent;
        if (room < sw_low && sw_high - room > want) want = sw_high - room;
        if (want) {
            const int critical = room < sw_low / 2;
            window_lock_take();
            const uint64_t back = free_give_back(want);
            window_lock_give();
            want = back < want ? want - back : 0;
            for (int pass = 0; want && pass < 64; pass++) {
                const uint64_t gone = evict_pass(want, critical);
                if (!gone) break;
                want = gone < want ? want - gone : 0;
            }
        }
        pthread_mutex_lock(&swap_cv_lock);
        swap_passes++;
        pthread_cond_broadcast(&swap_done_cv);
        pthread_mutex_unlock(&swap_cv_lock);
    }
    return NULL;
}

void hos_ps5_swap_start(void) {
    const char *e = getenv("HOS_PS5_SWAP");
    if (!e || *e != '1') return;
    sw_low = env_mb("HOS_PS5_SWAP_LOW_MB", 768), sw_high = env_mb("HOS_PS5_SWAP_HIGH_MB", 1280);
    sw_watch = env_mb("HOS_PS5_SWAP_WATCH_MB", 2048);
    sw_contig = env_mb("HOS_PS5_SWAP_PIECE_MB", 64);   /* (the largest free piece kept at least this while memory is short) */
    const char *c = getenv("HOS_PS5_SWAP_COLD_S");
    sw_cold = c && *c ? strtoull(c, NULL, 10) : 20;
    if (sw_high < sw_low) sw_high = sw_low;
    if (!swap_open()) return;
    swap_live = 1;
    pthread_t t;
    if (pthread_create(&t, NULL, swap_thread, NULL) == 0) pthread_detach(t);
    else swap_live = 0;
}

/* The meter's line (HOS_PS5_CPU=1) */
static void swap_report(void) {
    if (!unit_sw) return;
    uint64_t clean = 0, armed = 0, gpu = 0, io = 0, prot = 0, used = 0;
    window_lock_take();
    for (uint64_t u = 0; u < GUEST_UNITS; u++) {
        if (unit_state[u] == U_COMMITTED) {
            clean += (unit_sw[u] & SW_CLEAN) != 0, armed += (unit_sw[u] & SW_ARMED) != 0;
            gpu += (unit_pin[u] & PIN_GPU) != 0, io += (unit_pin[u] & PIN_IO) != 0, prot += (unit_pin[u] & PIN_PROT) != 0;
        }
    }
    for (uint64_t s = 0; slot_bits && s < (slot_count + 63) / 64; s++) used += (uint64_t)__builtin_popcountll(slot_bits[s]);
    const typeof(sws) w = sws;
    window_lock_give();
    fprintf(stderr, "[ps5] swap: %s; %llu MiB out, resident %llu MiB clean, %llu armed, held %llu for the GPU, %llu for I/O, %llu "
            "protected; file %llu of %llu MiB used, %llu written; %llu read back (%llu on a fault, %.1f ms a read, %.1f most, %llu ahead), "
            "%llu taken out (%llu MiB written), %llu MiB given back to the kernel, %llu touched again, %llu places taken over, "
            "%llu stayed for want of a place, %llu passes for a large piece (largest free %llu MiB)\n",
            swap_live ? "on" : "off", (unsigned long long)(w.out_units * UNIT >> 20), (unsigned long long)(clean * UNIT >> 20),
            (unsigned long long)(armed * UNIT >> 20), (unsigned long long)(gpu * UNIT >> 20), (unsigned long long)(io * UNIT >> 20),
            (unsigned long long)(prot * UNIT >> 20), (unsigned long long)(used * UNIT >> 20),
            (unsigned long long)(slot_count * UNIT >> 20), (unsigned long long)(slots_ready * UNIT >> 20),
            (unsigned long long)w.ins, (unsigned long long)w.in_faults, w.in_calls ? w.in_ms / (double)w.in_calls : 0.0,
            w.in_us_max / 1e3, (unsigned long long)w.ahead, (unsigned long long)w.outs, (unsigned long long)(w.written >> 20),
            (unsigned long long)(w.released * UNIT >> 20), (unsigned long long)w.rearmed, (unsigned long long)w.stolen,
            (unsigned long long)w.no_slot, (unsigned long long)w.contig_passes,
            (unsigned long long)(hos_ps5_direct_free() >> 20));
}
