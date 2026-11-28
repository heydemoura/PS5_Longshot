/* (longshot) HOS_NATIVE_CLOTH: thirteen pieces of the game's Havok cloth on host code, bit for bit. On by default;
   HOS_NATIVE_CLOTH=0 runs all of it lifted.

   The pieces (the kernels are game/cloth_*.cpp, each written from a behavioural spec: docs/dev/reference/native-cloth/).
   Three whole functions, hooked at their entry:
     bend       f_81c7f880  the bending pass (four particles a record)
     anchor     f_81c7bd00  the skinned-anchor distance pass (a particle back towards a skinned vertex)
     pm         f_81cdff40  "Prepare Matrices" of the object-space skinning: three 4x4 products an entry
   Ten loops inside larger routines, each hooked where its loop is entered, the rest of the routine lifted:
     link       f_81c86720  the standard link constraints' loop (two particles a link)
     bend link  f_81c7d2e0  the bend link constraints' loop (a min / max distance a link)
     tether     f_81c88940  the tethers' loop (one particle pulled towards another)
     aabb       f_81b38800  hclSimClothInstance::updateAabbs: the instance's three boxes over its particles
     cull       f_81b5a3c0  the capsule collidable's cull: the list of the particles inside its box
     verlet     f_81c66a60  _integrateTask's Verlet loop: the particles' new positions
     capsule    f_81b8c000  the capsule collidable's batch kernel, mode 0: its batches of 16 particles, every one of a
     capsule tail           call, and the up to 15 particles left over, one at a time (two hooks)
     sphere     f_81c1a240  the sphere collidable's batch kernel, mode 0: its batches of 16 particles, every one of a
                            call (its particles left over run lifted)
     skin       f_81db9a40  hclObjectSpaceSkinPOperator's Object Space Deform: its block loop, 16 vertices a block by 4,
                            3, 2 or 1 bones (a hook at the block's head: blocks of the other kinds run lifted)
     bones      f_81ce6500  Mesh Bone Deform: its bone loop, a bone's frame from a triangle of particles
   link, bend link and tether are Havok timer scopes around their loop: their hook sits at the end of the loop's set-up,
   so the scope (its calls, its thread-local stream) stays lifted.

   What a hook hands back: what the code after it reads, and nothing else. A whole function: the memory it writes (pm
   also seven vector values some of its callers read). A loop done whole: the few registers the rest of its routine
   reads (the boxes' six corners, the cull's count, a collision's list position; most hand back none), and the site
   jumps to the loop's way out. A loop a kernel stops part way (link, bend link, tether, the collisions, skin): the
   loop's own state for the first record not done, and the lifted loop goes on from there.

   Each hook is a hook site (docs/dev/reference/hooks.md): the tables game/sites/sites.toml (what each is), the build's
   game/sites/<build id>/locations.toml (where) and game/sites.def (its slot) place them; this file answers them. The
   engine callbacks (hos_site_cloth_*) are on from the start; the sites at the loops' ways out (cloth.*End) are set only in
   check mode, by hos_oot_cloth_init (game_init).

     HOS_NATIVE_CLOTH=0          every call runs as lifted, as on main (each site's callback asks the switch and answers 0)
     HOS_NATIVE_CLOTH unset / 1  a call, or its records up to the first one a kernel cannot give the game's bits for, on
                                 the host; the rest runs lifted
     HOS_NATIVE_CLOTH=check      the kernel into copies of the memory it writes, then the lifted code for real, then
                                 both compared at the loop's way out (a whole function: at its return): every byte the
                                 call could write, what the hook hands back, and FPSR; for the whole functions and the
                                 link, bend link and tether loops also the callee-saved registers. The lifted result is
                                 the one kept. A call the kernel did only in part, or whose copy would not see what the
                                 game sees (its records or particle data among the particles written), is not compared
                                 (counted). Slow: for a look in the game

   What a kernel takes (game/cloth_*.cpp): the float-mode gate (CLOTH_GATE_OPEN below: FPCR at its default but FZ), and
   records whose every float operation provably gives the same bits on IEEE as on Arm (operands zero or within a range
   where nothing can be tiny, infinite or NaN); what IEEE cannot be shown to give, a kernel does lane by lane with the
   Arm's own operations or leaves to the lifted code. FPSR is left as it is: the lifted code raises none of its
   cumulative flags, so the hooks put none in (the gate's comment below). Every page a kernel wrote is marked for the
   GPU mirror (HOS_WRITTEN), as the lifted stores mark theirs. Memory: the u16 particle indices reach 1 MB from each
   base, so that whole reach must be guest memory.

   The log: an "oot: cloth" line at most every 10 s while calls come (per pass: calls all native, partly native, lifted
   and why, records native and lifted; in check mode also calls compared, not compared and MISMATCHES), and the first
   mismatch's detail.

   HOS_CLOTH_DECLINES=1 and HOS_CLOTH_CAPTURE=<folder> say why records run lifted (diagnostics: what runs natively is the
   same with them as without): see "HOS_CLOTH_DECLINES" below. */
#include "game_host.h"   /* (what the host gives the game: host/game_host.h; env_on, the switches' one rule, with it) */
#include "sites.h"
#include "cloth_aabb.h"
#include "cloth_anchor.h"
#include "cloth_bend.h"
#include "cloth_bend_link.h"
#include "cloth_bones.h"
#include "cloth_capsule.h"
#include "cloth_cull.h"
#include "cloth_link.h"
#include "cloth_matrices.h"
#include "cloth_skin.h"
#include "cloth_sphere.h"
#include "cloth_tether.h"
#include "cloth_verlet.h"
#include "cloth_why.h"   /* (the declines' reasons; the link, bend link and tether headers brought it before) */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <direct.h>
#include <windows.h>
static int cloth_wpath(const char *p, wchar_t *w, int n) {
    return MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, p, -1, w, n) != 0;
}
static void cloth_mkdir(const char *p) {
    wchar_t w[560];
    if (cloth_wpath(p, w, 560)) _wmkdir(w);
}
static FILE *cloth_fopen(const char *p) {
    wchar_t w[560];
    return cloth_wpath(p, w, 560) ? _wfopen(w, L"wb") : NULL;
}
#else
#include <pthread.h>
#include <sys/stat.h>
#define cloth_mkdir(p) mkdir((p), 0777)
static FILE *cloth_fopen(const char *p) { return fopen(p, "wb"); }
#endif

/* ---- build e0f3ccfa's addresses the callbacks read (docs/dev/reference/hooks.md: a callback naming an address belongs to
   the build whose table names its site; a second build's table brings its own values for these) */
#define OOT_CLOTH_BEND_FN 0x81c7f880ull       /* (the bending pass: cloth.bend's function, called whole from its callback) */
#define OOT_CLOTH_ANCHOR_FN 0x81c7bd00ull     /* (the skinned-anchor pass: cloth.anchor's) */
#define OOT_CLOTH_PM_FN 0x81cdff40ull         /* (Prepare Matrices: cloth.matrices') */
#define CLOTH_TABLE_PTR 0x84ca4680ull         /* the solver's constants (a pointer the game keeps; [+0x50], [+0x60], [+0x270] ... read at run time) */
#define CAP_MASK_CONST 0x83e40bc0ull          /* (the image: the capsule's and the sphere's contact dispatch's 16-byte lane mask, read at run time) */
#define CAP_TABLES 0x83eb8814ull              /* (the image: the capsule's mode 0's four 15-entry u16 jump tables, 0x78 bytes) */
#define SPH_TABLES 0x83eb9624ull              /* (the image: the sphere's mode 0's four 15-entry u16 jump tables, 0x78 bytes) */
/* ---- end of the build's addresses */

/* The kernels' float-mode gate, and the float status. The lift computes the game's floats by the host's model
   (builder/crates/sem/src/fp.rs): FPCR.FZ and DN are read, NaNs are the Arm's, and none of FPSR's cumulative flags (IOC,
   DZC, OFC, UFC, IXC, IDC) is raised. A kernel is written for FPCR at its default but FZ (round to nearest, no DN, AHP,
   FZ16, trap enables or FEAT_AFP bits): that is the gate, whatever FPSR holds. The lifted code raises no cumulative
   flag, so the host raises none either: each kernel is told that every one of them is set already (CLOTH_FPSR_IN, so
   it has none to follow), and what it reports is not put into FPSR. (The old lift raised them; the gate then also
   asked for IXC set already, and the hooks ORed the kernels' flags in.) */
#ifndef CLOTH_GATE_OPEN   /* (a test's stand-in may set its own) */
#define CLOTH_FPCR_MASK (UINT64_C(0x06C8FF07) | (UINT64_C(1) << 32))
#define CLOTH_GATE_OPEN(c) (!((c)->fpcr & CLOTH_FPCR_MASK))
#endif
#define CLOTH_FPSR_IN 0x9fu   /* (IOC, DZC, OFC, UFC, IXC, IDC) */

enum { CLOTH_OFF, CLOTH_ON, CLOTH_CHECK };
static int cloth_mode_now = -1;
static const char *cloth_end_missing;   /* (check mode: the first way-out site hos_site_set did not find, and how many) */
static int cloth_end_missing_n;

/* the switch: 0 off, 1 on (unset too), check */
static int cloth_mode_read(void) {
    const char *v = getenv("HOS_NATIVE_CLOTH");
    return !env_on_or("HOS_NATIVE_CLOTH", 1) ? CLOTH_OFF : (v && (v[0] | 0x20) == 'c') ? CLOTH_CHECK : CLOTH_ON;
}

/* the mode, read at the first call (the start line in the log then: hos_oot_cloth_init runs before the log is open) */
static int cloth_mode(void) {
    int m = __atomic_load_n(&cloth_mode_now, __ATOMIC_RELAXED);
    if (m < 0) {
        int unset = -1;
        m = cloth_mode_read();
        if (__atomic_compare_exchange_n(&cloth_mode_now, &unset, m, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED) && m) {
            hos_log("oot: cloth: thirteen pieces of the cloth on the host (HOS_NATIVE_CLOTH%s)",
                    m == CLOTH_CHECK ? "=check: each call compared with the lifted code, which is kept" : ", on by default; =0 runs them lifted");
            if (m == CLOTH_CHECK && cloth_end_missing)
                hos_log("oot: cloth: the host has no site for %d of check mode's compares (the first: %s): those passes are "
                        "not compared", cloth_end_missing_n, cloth_end_missing);
        }
        m = __atomic_load_n(&cloth_mode_now, __ATOMIC_RELAXED);
    }
    return m;
}

/* ---- counts and the log ------------------------------------------------------------------------------------------- */
enum { K_BEND, K_LINK, K_BEND_LINK, K_ANCHOR, K_TETHER, K_AABB, K_CULL, K_PM, K_VERLET, K_CAPREC, K_CAPTAIL, K_SPHREC,
       K_SKIN, K_BONES, K_COUNT };
static const char *const cloth_name[K_COUNT] = {"bend f_81c7f880", "link f_81c86720", "bend link f_81c7d2e0",
                                                "anchor f_81c7bd00", "tether f_81c88940", "aabb f_81b38800",
                                                "cull f_81b5a3c0", "pm f_81cdff40", "verlet f_81c66a60",
                                                "capsule f_81b8c000", "capsule tail f_81b8c000",
                                                "sphere f_81c1a240", "skin f_81db9a40", "bones f_81ce6500"};
enum { S_CALLS, S_ALL, S_PART, S_DECLINED, S_GATE, S_REC_NATIVE, S_REC_LIFTED, S_CHECKED, S_UNCHECKED, S_MISMATCH,
       S_COUNT };
static long cloth_total[K_COUNT][S_COUNT];
static _Thread_local long cloth_local[K_COUNT][S_COUNT];
static _Thread_local unsigned cloth_local_calls;
static int64_t cloth_last_log;
#define CLOTH_LOG_NS 10000000000LL
#define CLOTH_FLUSH_CALLS 256

/* (one line, or more when the passes would pass the logger's 1023 characters: each line starts "oot: cloth
   (HOS_NATIVE_CLOTH=...)" and holds whole passes; the total ends the last) */
static void cloth_log_line(void) {
    char line[1100], part[400];
    const int check = cloth_mode() == CLOTH_CHECK;
    long mism = 0;
    const int head = snprintf(line, sizeof line, "oot: cloth (HOS_NATIVE_CLOTH=%s)", check ? "check" : "1");
    int at = head;
    for (int k = 0; k < K_COUNT; k++) {
        long s[S_COUNT];
        for (int i = 0; i < S_COUNT; i++) s[i] = __atomic_load_n(&cloth_total[k][i], __ATOMIC_RELAXED);
        mism += s[S_MISMATCH];
        int m = snprintf(part, sizeof part,
                         "%s %s: calls %ld (native %ld, partly %ld, lifted %ld of which FPCR %ld), records %ld native %ld lifted",
                         at > head ? ";" : "", cloth_name[k], s[S_CALLS], s[S_ALL], s[S_PART], s[S_DECLINED], s[S_GATE],
                         s[S_REC_NATIVE], s[S_REC_LIFTED]);
        if (check && m < (int)sizeof part)
            m += snprintf(part + m, sizeof part - (size_t)m, ", compared %ld, not compared %ld, MISMATCHES %ld", s[S_CHECKED],
                          s[S_UNCHECKED], s[S_MISMATCH]);
        if (m >= (int)sizeof part) m = (int)sizeof part - 1;
        if (at > head && at + m > 990) {   /* (this pass on a line of its own) */
            hos_log("%s", line);
            at = head;
            memmove(part, part + 1, (size_t)m), m--;   /* (its leading ";") */
        }
        memcpy(line + at, part, (size_t)m + 1), at += m;
    }
    if (check) snprintf(line + at, sizeof line - (size_t)at, "; MISMATCHES in all %ld", mism);
    hos_log("%s", line);
}

static void cloth_dx_log(void);
static int cloth_dx_now = -1;   /* HOS_CLOTH_DECLINES / HOS_CLOTH_CAPTURE: 1 on, 0 off, -1 not read yet */

/* the thread's counts into the totals, and the line when 10 s have passed since the last */
static void cloth_flush(void) {
    for (int k = 0; k < K_COUNT; k++)
        for (int i = 0; i < S_COUNT; i++)
            if (cloth_local[k][i]) __atomic_fetch_add(&cloth_total[k][i], cloth_local[k][i], __ATOMIC_RELAXED), cloth_local[k][i] = 0;
    cloth_local_calls = 0;
    const int64_t now = hos_now_ns();
    int64_t last = __atomic_load_n(&cloth_last_log, __ATOMIC_RELAXED);
    if (!last) {   /* (the first flush starts the clock) */
        __atomic_compare_exchange_n(&cloth_last_log, &last, now, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED);
        return;
    }
    if (now - last >= CLOTH_LOG_NS && __atomic_compare_exchange_n(&cloth_last_log, &last, now, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {
        cloth_log_line();
        if (__atomic_load_n(&cloth_dx_now, __ATOMIC_ACQUIRE) > 0) cloth_dx_log();
    }
}

static inline void cloth_count(int k, int s, long v) { cloth_local[k][s] += v; }
static inline void cloth_call_done(void) {
    if (++cloth_local_calls >= CLOTH_FLUSH_CALLS) cloth_flush();
}

/* ---- HOS_CLOTH_DECLINES, HOS_CLOTH_CAPTURE: why records run lifted ---------------------------------------------------
   Diagnostics for HOS_NATIVE_CLOTH=1 (or check): what runs natively and every value the game sees are the same with
   them as without.
     HOS_CLOTH_DECLINES=1      every call a kernel does not finish is put down to one reason, counted per pass with the
                               records it leaves to the lifted code: the kernel's own reason for stopping (its result's
                               `why`, named as the log lists them), or the whole call's (the float-mode gate, memory, an
                               overlap, the count, an entry value). A line per pass every 10 s (after the "oot: cloth" line) and at the end of the run
                               (hos_oot_cloth_end, from game_close_log at the close):
       oot: cloth declines link f_81c86720: calls C (declined D), records R, lifted L (P %); <what> [lane] <how>: calls c,
            records r (p % of the pass's records), |v| lo..hi; ...       (by records, as many as the line holds)
     HOS_CLOTH_CAPTURE=<folder> (counts the declines too): up to HOS_CLOTH_CAPTURE_N (2000) declined calls a pass, at most a
                               quarter of them for one reason, into <folder>/cloth_<addr>.bin as they stood when the lifted
                               code took over (the declined record first): the registers and the memory the rest of the
                               call reads, for replay against the game's routine (layout at cloth_cap_write). Calls
                               declined whole before the kernel ran (gate, memory, entry values the hook checks) are
                               counted, not written; so are the anchor pass's (its kernel answers or declines whole
                               calls), Prepare Matrices' (its kernel never declines) and the capsule's and sphere's
                               (their stops are counted by reason, with no capture).
   Off (neither set): one predictable branch a call, and a store on the hooks' decline paths. */
#define CLOTH_DX_KEYS 96
typedef struct ClothDxKey {
    ClothWhy why;            /* the reason (its bits: the first seen) */
    long calls, records;     /* calls put down to it, and the records they left to the lifted code */
    long captured;
    uint32_t lo, hi;         /* the smallest and largest magnitude (bits) of the failing values */
} ClothDxKey;
static struct {
    long calls, declined, records, lifted, captured, cap_skipped;
    int nkeys, cap_failed;
    FILE *cap;
    ClothDxKey key[CLOTH_DX_KEYS];
} cloth_dx_tab[K_COUNT];
static const char *const cloth_addr[K_COUNT] = {"81c7f880", "81c86720", "81c7d2e0", "81c7bd00", "81c88940", "81b38800",
                                                "81b5a3c0", "81cdff40", "81c66a60", "81b8c000", "81b8c000t",
                                                "81c1a240", "81db9a40", "81ce6500"};
static char cloth_cap_dir[480];    /* HOS_CLOTH_CAPTURE ("": no capture) */
static long cloth_cap_max = 2000;  /* HOS_CLOTH_CAPTURE_N */
static char cloth_dx_busy;         /* (the tables' and the capture files' lock; taken only with the switches on, once a call) */
static void cloth_dx_lock(void) {
    while (__atomic_test_and_set(&cloth_dx_busy, __ATOMIC_ACQUIRE))
        while (__atomic_load_n(&cloth_dx_busy, __ATOMIC_RELAXED)) {}
}
static void cloth_dx_unlock(void) { __atomic_clear(&cloth_dx_busy, __ATOMIC_RELEASE); }

static int cloth_dx_read(void) {
    cloth_dx_lock();
    int d = __atomic_load_n(&cloth_dx_now, __ATOMIC_RELAXED);
    if (d < 0) {
        const char *cap = getenv("HOS_CLOTH_CAPTURE"), *nmax = getenv("HOS_CLOTH_CAPTURE_N");
        if (cap && *cap && strlen(cap) < sizeof cloth_cap_dir) {
            strcpy(cloth_cap_dir, cap);
            cloth_mkdir(cloth_cap_dir);   /* (the folder itself; its parent must exist) */
        }
        if (nmax && atol(nmax) >= 0) cloth_cap_max = atol(nmax);
        d = env_on("HOS_CLOTH_DECLINES") || cloth_cap_dir[0];
        if (d)
            hos_log("oot: cloth declines: each call a cloth kernel does not finish is put down to a reason (HOS_CLOTH_DECLINES)%s%s%s",
                    cloth_cap_dir[0] ? "; up to " : "", cloth_cap_dir[0] ? (nmax ? nmax : "2000") : "",
                    cloth_cap_dir[0] ? " declined calls a pass captured (HOS_CLOTH_CAPTURE)" : "");
        __atomic_store_n(&cloth_dx_now, d, __ATOMIC_RELEASE);
    }
    cloth_dx_unlock();
    return d;
}
/* 1 when the declines are counted (read once) */
static inline int cloth_dx(void) {
    const int d = __atomic_load_n(&cloth_dx_now, __ATOMIC_ACQUIRE);
    return __builtin_expect(d < 0, 0) ? cloth_dx_read() : d;
}

/* the reason the current call's answer gave (has: 1 noted, 2 noted and counted by the answer) */
static _Thread_local struct { int has; ClothWhy why; } cloth_dn;
/* a whole call declined (`what`: why): the answers' `return decline("...")` */
static inline int decline(const char *what) {
    if (__builtin_expect(__atomic_load_n(&cloth_dx_now, __ATOMIC_RELAXED) > 0, 0))
        cloth_dn.has = 1, cloth_dn.why.what = what, cloth_dn.why.lane = -1, cloth_dn.why.cls = CW_CALL, cloth_dn.why.bits = 0;
    return 0;
}

static int cloth_dx_has_value(int cls) { return cls >= CW_POS_SUB && cls <= CW_RSQRTS && cls != CW_INF && cls != CW_NAN; }
/* (locked) the pass's row for a reason; the last row takes what does not fit */
static ClothDxKey *cloth_dx_key(int k, const ClothWhy *w) {
    int i = 0;
    for (; i < cloth_dx_tab[k].nkeys; i++) {
        ClothDxKey *e = &cloth_dx_tab[k].key[i];
        if (e->why.cls == w->cls && e->why.lane == w->lane && (e->why.what == w->what || !strcmp(e->why.what, w->what))) return e;
    }
    if (i == CLOTH_DX_KEYS) return &cloth_dx_tab[k].key[CLOTH_DX_KEYS - 1];
    ClothDxKey *e = &cloth_dx_tab[k].key[i];
    memset(e, 0, sizeof *e);
    e->why = *w, e->lo = 0xffffffffu;
    if (i == CLOTH_DX_KEYS - 1) e->why.what = "(other reasons)", e->why.lane = -1, e->why.cls = CW_ORDER;
    cloth_dx_tab[k].nkeys = i + 1;
    return e;
}
static void cloth_dx_add(ClothDxKey *e, const ClothWhy *w, uint64_t records) {
    e->calls++, e->records += (long)records;
    const uint32_t a = w->bits & 0x7fffffffu;
    if (cloth_dx_has_value(w->cls)) e->lo = a < e->lo ? a : e->lo, e->hi = a > e->hi ? a : e->hi;
}

/* the hooks, after an answer: the call's records, those done natively, and whether the call was declined (whole or in
   part); a declined call the answer did not count is put down to the reason it noted (or to none: a fault) */
static void cloth_dx_call(int k, uint64_t records, uint64_t native, int declined) {
    cloth_dx_lock();
    cloth_dx_tab[k].calls++, cloth_dx_tab[k].records += (long)records, cloth_dx_tab[k].lifted += (long)(records - native);
    if (declined) {
        cloth_dx_tab[k].declined++;
        if (cloth_dn.has != 2) {
            ClothWhy w = cloth_dn.why;
            if (!cloth_dn.has) w.what = "(no reason noted)", w.lane = -1, w.cls = CW_UNEXPLAINED, w.bits = 0;
            cloth_dx_add(cloth_dx_key(k, &w), &w, records - native);
        }
    }
    cloth_dx_unlock();
    cloth_dn.has = 0;
}

/* an answer whose kernel stopped at record `done` of `n`, `found` its why function's answer (in cloth_dn.why): counted;
   the row to capture the call for, or NULL */
static ClothDxKey *cloth_dx_record(int k, int found, uint64_t n, uint32_t done) {
    if (!found) cloth_dn.why.what = "(the kernel stopped, its why found nothing)", cloth_dn.why.lane = -1,
                cloth_dn.why.cls = CW_UNEXPLAINED, cloth_dn.why.bits = 0;
    cloth_dx_lock();
    ClothDxKey *e = cloth_dx_key(k, &cloth_dn.why);
    cloth_dx_add(e, &cloth_dn.why, n - done);
    const int cap = cloth_cap_dir[0] && cloth_dx_tab[k].captured < cloth_cap_max && e->captured < (cloth_cap_max + 3) / 4;
    cloth_dx_unlock();
    cloth_dn.has = 2;
    return cap ? e : NULL;
}

/* ---- the capture: <folder>/cloth_<addr>.bin, little-endian ----
   file:  char magic[8] "OOTCLCAP"; u32 version 1; u32 pass (0 bend, 1 link, 2 bend link, 3 anchor, 4 tether);
          char name[24] (as the log names the pass)
   a call: char magic[4] "CASE"; u32 regions; u32 n (the call's records); u32 done (records the host did before the
          lifted code took over); u64 meta[4]; char why[96] (the reason, as the log writes it);
          u64 x[32]; u64 v[32][2]; u32 nzcv (N<<3 | Z<<2 | C<<1 | V); u32 fpcr; u32 fpsr; u32 0;
          then per region: u64 guest address; u32 bytes; u32 role; the bytes.
   The registers are the hook's at its entry (link, bend link, tether: at the end of the loop's set-up), with what the
   lifted code is given to do the rest: bend x0 = the records left and x3 their first; the loop passes x9 = the records
   left; FPSR as it was. meta[0] = the guest address of the declined record (the loop passes' object still
   names the call's first record: a replay points it here), meta[1] = the records left. Roles: 1 particle slots (the
   lowest to the highest slot the records left name), 2 particle data (the same slots), 3 the records left, 4 the
   constraint object (link, bend link, tether: its +0x28, 8 bytes), 5 [x4], 6 [x5], 7 the constants' pointer
   [0x84ca4680], 8 the constants +0x50. (Pass 3, the anchor pass, is no longer written, nor roles 9-12 and meta[2] that
   were its.) Slots and data are read where the kernel read them (check mode: its
   copy). A call over 4 MB in one region or 8 MB in all is skipped (counted). */
typedef struct ClothCapRegion {
    uint64_t a;          /* guest address */
    const void *p;       /* the bytes (host) */
    uint64_t n;
    uint32_t role;
} ClothCapRegion;

/* "<what> [lane] <how>", as the log and the capture write a reason */
static void cloth_why_text(char *out, size_t cap, const ClothWhy *w) {
    char lane[8] = "";
    if (w->lane >= 0 && w->lane < 4) snprintf(lane, sizeof lane, " [%c]", "xyzw"[w->lane]);
    snprintf(out, cap, "%s%s %s", w->what ? w->what : "?", lane, cloth_why_class_name(w->cls));
}

static void cloth_cap_write(int k, ClothDxKey *e, const Ctx *c, uint64_t n, uint32_t done, const uint64_t meta[4],
                            const ClothCapRegion *r, int nr) {
    uint64_t all = 0;
    int big = 0;
    for (int i = 0; i < nr; i++) all += r[i].n, big |= r[i].n > (4u << 20);
    char why[96];
    memset(why, 0, sizeof why);
    cloth_why_text(why, sizeof why, &cloth_dn.why);
    cloth_dx_lock();
    if (big || all > (8u << 20)) {
        cloth_dx_tab[k].cap_skipped++;
    } else if (cloth_dx_tab[k].captured < cloth_cap_max && e->captured < (cloth_cap_max + 3) / 4 && !cloth_dx_tab[k].cap_failed) {
        if (!cloth_dx_tab[k].cap) {
            char path[560];
            snprintf(path, sizeof path, "%s/cloth_%s.bin", cloth_cap_dir, cloth_addr[k]);
            cloth_dx_tab[k].cap = cloth_fopen(path);
            if (cloth_dx_tab[k].cap) {
                char head[40];
                const uint32_t ver = 1, pass = (uint32_t)k;
                memset(head, 0, sizeof head);
                memcpy(head, "OOTCLCAP", 8), memcpy(head + 8, &ver, 4), memcpy(head + 12, &pass, 4);
                snprintf(head + 16, 24, "%s", cloth_name[k]);
                fwrite(head, 1, sizeof head, cloth_dx_tab[k].cap);
            } else {
                cloth_dx_tab[k].cap_failed = 1;
                hos_log("oot: cloth capture: %s could not be written: no capture for %s", path, cloth_name[k]);
            }
        }
        FILE *f = cloth_dx_tab[k].cap;
        if (f) {
            const uint32_t h[4] = {0, (uint32_t)nr, (uint32_t)n, done};
            const uint32_t fl[4] = {(uint32_t)c->n << 3 | (uint32_t)c->z << 2 | (uint32_t)c->c << 1 | c->v, (uint32_t)c->fpcr,
                                    (uint32_t)c->fpsr, 0};
            fwrite("CASE", 1, 4, f), fwrite(h + 1, 4, 3, f), fwrite(meta, 8, 4, f), fwrite(why, 1, sizeof why, f);
            fwrite(c->x, 8, 32, f), fwrite(c->vreg, 16, 32, f), fwrite(fl, 4, 4, f);
            for (int i = 0; i < nr; i++) {
                const uint32_t ln[2] = {(uint32_t)r[i].n, r[i].role};
                fwrite(&r[i].a, 8, 1, f), fwrite(ln, 4, 2, f), fwrite(r[i].p, 1, (size_t)r[i].n, f);
            }
            fflush(f);
            cloth_dx_tab[k].captured++, e->captured++;
        }
    }
    cloth_dx_unlock();
}

/* the lowest and highest u16 at the given offsets of records [from, n) */
static void cloth_u16_span(const uint8_t *rec, uint64_t from, uint64_t n, size_t size, const int *at, int nat, uint32_t *lo,
                           uint32_t *hi) {
    *lo = 0xffffu, *hi = 0;
    for (uint64_t i = from; i < n; i++)
        for (int j = 0; j < nat; j++) {
            uint16_t v;
            memcpy(&v, rec + i * size + at[j], 2);
            if (v < *lo) *lo = v;
            if (v > *hi) *hi = v;
        }
}

/* floor(log2) of a nonzero finite magnitude given as bits */
static int cloth_exp2(uint32_t a) {
    if (a >= 0x00800000u) return (int)(a >> 23) - 127;
    int e = -150;
    while (a) a >>= 1, e++;
    return e;
}

/* the log's lines for the declines (one or two a pass) and the capture's */
static void cloth_dx_log_pass(int k) {
    __typeof__(cloth_dx_tab[0]) t;   /* (a snapshot: the lines are made from it unlocked) */
    cloth_dx_lock();
    t = cloth_dx_tab[k];
    cloth_dx_unlock();
    int order[CLOTH_DX_KEYS];
    for (int i = 0; i < t.nkeys; i++) order[i] = i;
    for (int i = 1; i < t.nkeys; i++)   /* (by records, then calls) */
        for (int j = i; j > 0; j--) {
            const ClothDxKey *a = &t.key[order[j - 1]], *b = &t.key[order[j]];
            if (a->records > b->records || (a->records == b->records && a->calls >= b->calls)) break;
            const int s = order[j - 1];
            order[j - 1] = order[j], order[j] = s;
        }
    char line[1000];
    const double all = t.records > 0 ? (double)t.records : 1.0;
    int at = snprintf(line, sizeof line, "oot: cloth declines %s: calls %ld (declined %ld), records %ld, lifted %ld (%.1f %%)",
                      cloth_name[k], t.calls, t.declined, t.records, t.lifted, 100.0 * (double)t.lifted / all);
    int lines = 1, shown = 0;
    for (int i = 0; i < t.nkeys; i++) {
        const ClothDxKey *e = &t.key[order[i]];
        char why[96], item[260], range[64] = "";
        cloth_why_text(why, sizeof why, &e->why);
        if (cloth_dx_has_value(e->why.cls) && e->lo <= e->hi)   /* (the failing magnitudes' binary exponents, floor(log2)) */
            snprintf(range, sizeof range, ", %s 2^%d..2^%d", e->why.cls == CW_RSQRTS ? "L" : "|v|", cloth_exp2(e->lo), cloth_exp2(e->hi));
        const int m = snprintf(item, sizeof item, "; %s: calls %ld, records %ld (%.1f %%)%s", why, e->calls, e->records,
                               100.0 * (double)e->records / all, range);
        if (at + m >= (int)sizeof line - 40) {
            if (lines == 2) break;
            hos_log("%s", line);
            lines++;
            at = snprintf(line, sizeof line, "oot: cloth declines %s (more)", cloth_name[k]);
        }
        memcpy(line + at, item, (size_t)m + 1), at += m, shown++;
    }
    if (shown < t.nkeys) snprintf(line + at, sizeof line - (size_t)at, "; +%d more reasons", t.nkeys - shown);
    hos_log("%s", line);
}
static void cloth_dx_log(void) {
    for (int k = 0; k < K_COUNT; k++) cloth_dx_log_pass(k);
    if (!cloth_cap_dir[0]) return;
    char line[800];
    int at = snprintf(line, sizeof line, "oot: cloth capture: %s:", cloth_cap_dir);
    cloth_dx_lock();
    for (int k = 0; k < K_COUNT && at < (int)sizeof line; k++)
        at += snprintf(line + at, sizeof line - (size_t)at, "%s %s %ld (skipped %ld)", k ? "," : "", cloth_name[k],
                       cloth_dx_tab[k].captured, cloth_dx_tab[k].cap_skipped);
    cloth_dx_unlock();
    hos_log("%s calls written", line);
}

/* game_close_log (game/oot_tools.c, from the host's close): the run's last counts and declines, when HOS_CLOTH_DECLINES or a
   capture is on */
void hos_oot_cloth_end(void) {
    if (__atomic_load_n(&cloth_dx_now, __ATOMIC_ACQUIRE) <= 0 || __atomic_load_n(&cloth_mode_now, __ATOMIC_RELAXED) <= 0) return;
    hos_log("oot: cloth: the run's last counts (HOS_CLOTH_DECLINES):");
    cloth_log_line();
    cloth_dx_log();
    cloth_dx_lock();
    for (int k = 0; k < K_COUNT; k++)
        if (cloth_dx_tab[k].cap) fflush(cloth_dx_tab[k].cap);
    cloth_dx_unlock();
}

/* ---- check mode: the copy, the registers wanted, the compare ------------------------------------------------------- */
#define CLOTH_SPAN (0x10000ull * 16)   /* any u16 slot of a particle base */
static _Thread_local uint8_t *cloth_scratch;   /* (CLOTH_SPAN bytes: the thread's copy of the particles a call writes) */
#ifdef _WIN32
static DWORD cloth_scratch_key = FLS_OUT_OF_INDEXES;
static INIT_ONCE cloth_scratch_once = INIT_ONCE_STATIC_INIT;
static VOID CALLBACK cloth_scratch_free(void *p) { free(p); }
static BOOL CALLBACK cloth_scratch_init(PINIT_ONCE once, PVOID arg, PVOID *context) {
    (void)once, (void)arg, (void)context;
    cloth_scratch_key = FlsAlloc(cloth_scratch_free);
    return TRUE;
}
static int cloth_scratch_set(void *p) {
    if (!InitOnceExecuteOnce(&cloth_scratch_once, cloth_scratch_init, NULL, NULL) || cloth_scratch_key == FLS_OUT_OF_INDEXES) return 0;
    return FlsSetValue(cloth_scratch_key, p) != 0;
}
#else
static pthread_key_t cloth_scratch_key;
static pthread_once_t cloth_scratch_once = PTHREAD_ONCE_INIT;
static int cloth_scratch_key_ok;
static void cloth_scratch_init(void) { cloth_scratch_key_ok = pthread_key_create(&cloth_scratch_key, free) == 0; }
static int cloth_scratch_set(void *p) {
    return pthread_once(&cloth_scratch_once, cloth_scratch_init) == 0 && cloth_scratch_key_ok && pthread_setspecific(cloth_scratch_key, p) == 0;
}
#endif
static int cloth_scratch_get(void) {
#ifdef _WIN32
    if (!InitOnceExecuteOnce(&cloth_scratch_once, cloth_scratch_init, NULL, NULL) || cloth_scratch_key == FLS_OUT_OF_INDEXES) return 0;
    cloth_scratch = (uint8_t *)FlsGetValue(cloth_scratch_key);
#endif
    if (cloth_scratch) return 1;
    uint8_t *p = (uint8_t *)malloc((size_t)CLOTH_SPAN);
    if (!p) return 0;
    if (!cloth_scratch_set(p)) { free(p); return 0; }
    cloth_scratch = p;
    return 1;
}

/* A guest range is only reserved, not necessarily readable throughout. Check the spans copied for comparisons. */
static int cloth_readable(const void *p, size_t n) {
    uintptr_t cur = (uintptr_t)p, end = cur + n;
    if (end < cur) return 0;
#ifdef _WIN32
    while (cur < end) {
        MEMORY_BASIC_INFORMATION m;
        if (!VirtualQuery((const void *)cur, &m, sizeof m) || m.State != MEM_COMMIT ||
            (m.Protect & (PAGE_NOACCESS | PAGE_GUARD)) || !(m.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
            PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY))) return 0;
        uintptr_t next = (uintptr_t)m.BaseAddress + m.RegionSize;
        if (next <= cur) return 0;
        cur = next;
    }
    return 1;
#else
    FILE *f = fopen("/proc/self/maps", "r");
    if (!f) return 0;
    char line[512], perm[5];
    uintptr_t lo, hi;
    while (cur < end && fgets(line, sizeof line, f)) {
        if (sscanf(line, "%" SCNxPTR "-%" SCNxPTR " %4s", &lo, &hi, perm) != 3 || hi <= cur) continue;
        if (lo > cur || perm[0] != 'r') break;
        cur = hi;
    }
    fclose(f);
    return cur >= end;
#endif
}

typedef struct ClothCopy {   /* in: the particles' base and how many slots the call reaches; out: the copy */
    uint8_t *real;            /* host pointer of the slots (guest memory) */
    size_t n;                 /* bytes copied (16 a slot, up to the highest index the records name) */
    uint8_t *buf;             /* the thread's scratch, holding the copy */
    const char *what;         /* (a mismatch's words) NULL: particle slots; else what the bytes are */
    uint8_t *real2, *buf2;    /* (step 8) a second region the call writes, its copy after the first's in the scratch */
    size_t n2;
    const char *what2;
    uint8_t slots[0x10000 / 8]; /* particle slots named by the records, for sparse copies */
    int sparse;
} ClothCopy;

/* the named slots through top, or 0 (not compared) when a region the call reads again every record lies in the span: there the
   kernel would read what the game had, not what the call wrote. regions: pairs of (host pointer, bytes) */
static int cloth_copy(ClothCopy *cp, uint8_t *real, uint32_t top, const uint8_t *const *reg, const size_t *reg_n, int nreg,
                      const uint8_t *rec, uint64_t count, size_t size, const int *at, int nat) {
    if (!cloth_scratch_get()) return 0;
    const size_t n = 16 * ((size_t)top + 1);
    for (int i = 0; i < nreg; i++)
        if (reg[i] < real + n && real < reg[i] + reg_n[i]) return 0;
    memset(cloth_scratch, 0, n); /* unnamed slots may appear in a diagnostic capture */
    for (uint64_t i = 0; i < count; i++)
        for (int j = 0; j < nat; j++) {
            uint16_t slot;
            memcpy(&slot, rec + i * size + at[j], 2);
            if (!(cp->slots[slot / 8] & (1u << (slot % 8)))) {
                cp->slots[slot / 8] |= (uint8_t)(1u << (slot % 8));
                memcpy(cloth_scratch + 16 * (size_t)slot, real + 16 * (size_t)slot, 16);
            }
        }
    cp->real = real, cp->n = n, cp->buf = cloth_scratch;
    cp->sparse = 1;
    return 1;
}

static uint32_t top_u16(const uint8_t *rec, uint64_t n, size_t size, const int *at, int nat) {
    uint32_t top = 0;
    for (uint64_t i = 0; i < n; i++)
        for (int j = 0; j < nat; j++) {
            uint16_t v;
            memcpy(&v, rec + i * size + at[j], 2);
            if (v > top) top = v;
        }
    return top;
}

/* the whole-function pieces (bend, anchor, pm): what a caller can see of the registers against the lifted code's: x19-x29,
   the stack pointer, the low halves of v8-v15 (the callee-saved registers), every bit of the v registers in `keep` (a
   mask: the values a piece hands back), FPSR. 0 equal, 2 different (what: the first). The scratch registers and the
   flags are dead at the routine's return and not compared */
static int cloth_seen_diff(const Ctx *want, const Ctx *got, uint32_t keep, char *what, size_t what_n) {
    for (int r = 19; r < 32; r++)
        if (r != 30 && want->x[r] != got->x[r]) {
            snprintf(what, what_n, "x%d host %#" PRIx64 " lifted %#" PRIx64, r, want->x[r], got->x[r]);
            return 2;
        }
    for (int r = 0; r < 32; r++) {
        const int all = (int)(keep >> r & 1), callee = r >= 8 && r < 16;
        if ((all || callee) && (want->vreg[r][0] != got->vreg[r][0] || (all && want->vreg[r][1] != got->vreg[r][1]))) {
            snprintf(what, what_n, "v%d host %016" PRIx64 "%016" PRIx64 " lifted %016" PRIx64 "%016" PRIx64, r, want->vreg[r][1],
                     want->vreg[r][0], got->vreg[r][1], got->vreg[r][0]);
            return 2;
        }
    }
    if (want->fpsr != got->fpsr) {
        snprintf(what, what_n, "FPSR host %#" PRIx64 " lifted %#" PRIx64, want->fpsr, got->fpsr);
        return 2;
    }
    return 0;
}

static int cloth_reported;   /* (the first mismatch's detail is logged; the count says the rest) */

static void cloth_compare_tail(int k, int d, const char *what, const ClothCopy *cp, uint64_t records);

/* check mode, the whole-function pieces: what a caller can see (cloth_seen_diff) and the copy against the lifted code's */
static void cloth_compare_seen(int k, const Ctx *want, const Ctx *got, const ClothCopy *cp, uint32_t keep, uint64_t records) {
    char what[160] = "";
    const int d = cloth_seen_diff(want, got, keep, what, sizeof what);
    cloth_compare_tail(k, d, what, cp, records);
}

/* (the copy compared when the registers were equal, then counted; d: the registers' answer, what: its words) */
static void cloth_compare_tail(int k, int d, const char *what_in, const ClothCopy *cp, uint64_t records) {
    char what[160];
    snprintf(what, sizeof what, "%s", what_in);
    if (d < 2 && cp && cp->sparse) {
        for (size_t slot = 0; slot < cp->n / 16; slot++) {
            if (!(cp->slots[slot / 8] & (1u << (slot % 8)))) continue;
            size_t i = 16 * slot;
            if (!memcmp(cp->buf + i, cp->real + i, 16)) continue;
            while (cp->buf[i] == cp->real[i]) i++;
            snprintf(what, sizeof what, "particle %zu byte %zu: host %02x lifted %02x", i / 16, i % 16, cp->buf[i], cp->real[i]);
            d = 2;
            break;
        }
    }
    if (d < 2 && cp && !cp->sparse && cp->n && memcmp(cp->buf, cp->real, cp->n)) {
        size_t i = 0;
        while (cp->buf[i] == cp->real[i]) i++;
        if (cp->what) snprintf(what, sizeof what, "%s byte %zu: host %02x lifted %02x", cp->what, i, cp->buf[i], cp->real[i]);
        else snprintf(what, sizeof what, "particle %zu byte %zu: host %02x lifted %02x", i / 16, i % 16, cp->buf[i], cp->real[i]);
        d = 2;
    }
    if (d < 2 && cp && cp->n2 && memcmp(cp->buf2, cp->real2, cp->n2)) {
        size_t i = 0;
        while (cp->buf2[i] == cp->real2[i]) i++;
        snprintf(what, sizeof what, "%s byte %zu: host %02x lifted %02x", cp->what2, i, cp->buf2[i], cp->real2[i]);
        d = 2;
    }
    cloth_count(k, S_CHECKED, 1);
    if (d) {
        __atomic_fetch_add(&cloth_total[k][S_MISMATCH], 1, __ATOMIC_RELAXED);
        int no = 0;
        if (__atomic_compare_exchange_n(&cloth_reported, &no, 1, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED))
            hos_log("oot: cloth: MISMATCH (the first) in %s, a call of %" PRIu64 " records: %s", cloth_name[k], records, what);
    }
}

/* check mode, a call the host did not do whole: declined (h 0: the lifted code alone is the game's), partly (h >= 2) or
   one whose copy could not see what the game sees (h -1): those two are not compared */
static void cloth_check_other(int k, int h) {
    if (h == 0) cloth_count(k, S_DECLINED, 1);
    else if (h > 0) cloth_count(k, S_PART, 1), cloth_count(k, S_UNCHECKED, 1);
    else cloth_count(k, S_UNCHECKED, 1);
}

/* check mode, the loop passes: the host's answer waits at the loop's way out (its cloth.*End site) for the lifted loop */
static _Thread_local struct {
    int k;   /* the pass, -1: none */
    uint64_t records;
    ClothCopy cp;
    Ctx want;
} cloth_pending = {.k = -1};

static inline int overlaps(uint64_t a, uint64_t an, uint64_t b, uint64_t bn) { return a < b + bn && b < a + an; }
static inline uint64_t lo64(uint32_t a, uint32_t b) { return (uint64_t)a | (uint64_t)b << 32; }
static inline void setq(Ctx *c, int r, uint32_t a, uint32_t b, uint32_t d, uint32_t e) { c->vreg[r][0] = lo64(a, b), c->vreg[r][1] = lo64(d, e); }

/* the lifted function at a guest address (a64_lookup's, once; NULL: the build has none there) */
static A64Fn cloth_lifted(A64Fn *cache, uint64_t at) {
    A64Fn f = __atomic_load_n(cache, __ATOMIC_ACQUIRE);
    if (!f) {
        f = a64_lookup(at);
        __atomic_store_n(cache, f, __ATOMIC_RELEASE);
    }
    return f;
}

/* The answers return -1 when check mode cannot compare the call (cp given: the copy refused), 0 declined, otherwise
   1 (done) or 2 (done in part), which the site's callback turns into its answer. With cp the kernel writes the copy and
   nothing is marked. */
#define WRITTEN(cp, p, n) do { if (!(cp)) HOS_WRITTEN((p), (n)); } while (0)

/* ---- f_81c7f880: the bending pass --------------------------------------------------------------------------------- */
/* A leaf (game/cloth_bend.h): w0 = count, x1 = particle slots, x2 = particle data, x3 = records (32 bytes), x4 = a
   threshold pair (read once), x5 = the scale (read every record); two pairs of the constants table (read once). What a
   caller can see of it: the slots it writes and the float status (it returns nothing, and its caller reads none of the
   scratch registers or flags it leaves), so the hook writes no register. 1: every record done, 2: the first `done` records
   done, the lifted routine does the rest (w0 = the records left, x3 at the first: nothing but memory is carried from one
   record to the next) */
static _Thread_local int bend_inside;   /* (the lifted routine called from the hook: its site must not answer again) */
static A64Fn bend_fn;                   /* (the lifted routine, cloth_lifted's) */

/* (HOS_CLOTH_DECLINES) the kernel's reason for stopping, as the log names it */
static const char *const bend_why_name[5] = {"every record (no stop)", "an entry value outside what it takes",
                                             "a record value outside the exactness class",
                                             "a reciprocal square root step outside its proven range", "another reason"};

/* HOS_CLOTH_DECLINES: the kernel stopped at record b->done: its reason counted, and the call captured from there */
static void bend_declined(const Ctx *c, const ClothBendPass *b, uint64_t p, const uint8_t *tp, const uint8_t *t, const uint8_t *th) {
    const int known = b->why > CLOTH_BEND_ALL && b->why <= CLOTH_BEND_OTHER;
    if (known) cloth_dn.why.what = bend_why_name[b->why], cloth_dn.why.lane = -1, cloth_dn.why.cls = CW_KERNEL, cloth_dn.why.bits = 0;
    ClothDxKey *e = cloth_dx_record(K_BEND, known, b->n, b->done);
    if (!e) return;
    static const int at[4] = {0x18, 0x1a, 0x1c, 0x1e};
    const uint64_t x1 = c->x[1], x2 = c->x[2], x3 = c->x[3], n = b->n, d = b->done;
    uint32_t lo, hi;
    cloth_u16_span(b->records, d, n, 32, at, 4, &lo, &hi);
    const uint64_t span = 16 * (uint64_t)(hi - lo + 1);
    const ClothCapRegion r[7] = {
        {x1 + 16 * (uint64_t)lo, b->slots + 16 * (size_t)lo, span, 1}, {x2 + 16 * (uint64_t)lo, b->particles + 16 * (size_t)lo, span, 2},
        {x3 + 32 * d, b->records + 32 * d, 32 * (n - d), 3}, {c->x[4], th, 8, 5}, {c->x[5], b->scale, 4, 6}, {CLOTH_TABLE_PTR, tp, 8, 7},
        {p + 0x50, t, 0x18, 8}};
    Ctx s = *c;
    s.x[0] = n - d, s.x[3] = x3 + 32 * d, s.fpsr = c->fpsr;
    const uint64_t meta[4] = {x3 + 32 * d, n - d, 0, 0};
    cloth_cap_write(K_BEND, e, &s, n, (uint32_t)d, meta, r, 7);
}

static int bend_answer(Ctx *c, ClothCopy *cp, uint32_t *done) {
    const uint32_t w0 = (uint32_t)c->x[0];
    if ((int32_t)w0 < 1) return 1;   /* a count below 1: the routine returns at once, nothing read or written */
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_BEND, S_GATE, 1), decline("gate: FPCR");
    const uint64_t x1 = c->x[1], x2 = c->x[2], x3 = c->x[3], x4 = c->x[4], x5 = c->x[5], n = w0;
    const uint8_t *tp = (const uint8_t *)hos_guest_range(CLOTH_TABLE_PTR, 8);
    if (!tp) return decline("memory: the constants' pointer");
    uint64_t p;
    memcpy(&p, tp, 8);
    const uint8_t *t = (const uint8_t *)hos_guest_range(p + 0x50, 0x18), *th = (const uint8_t *)hos_guest_range(x4, 8);
    uint8_t *pos = (uint8_t *)hos_guest_range(x1, CLOTH_SPAN);
    const uint8_t *pd = (const uint8_t *)hos_guest_range(x2, CLOTH_SPAN);
    const uint8_t *rec = (const uint8_t *)hos_guest_range(x3, 32 * n), *kp = (const uint8_t *)hos_guest_range(x5, 4);
    if (!t || !th || !pos || !pd || !rec || !kp)
        return decline(!t ? "memory: the constants" : !th ? "memory: [x4]" : !pos ? "memory: particle slots (1 MB)" :
                       !pd ? "memory: particle data (1 MB)" : !rec ? "memory: records" : "memory: [x5]");
    /* (what the game reads once at entry must not be reachable by a slot store: the lifted rest would read it again) */
    if (overlaps(x1, CLOTH_SPAN, x4, 8) || overlaps(x1, CLOTH_SPAN, CLOTH_TABLE_PTR, 8) || overlaps(x1, CLOTH_SPAN, p + 0x50, 0x18))
        return decline("overlap: the slots reach [x4] or the constants");
    if (cp) {
        static const int at[4] = {0x18, 0x1a, 0x1c, 0x1e};
        const uint32_t top = top_u16(rec, n, 32, at, 4);
        const uint8_t *reg[3] = {rec, pd, kp};
        const size_t reg_n[3] = {(size_t)(32 * n), 16 * ((size_t)top + 1), 4};
        if (!cloth_copy(cp, pos, top, reg, reg_n, 3, rec, n, 32, at, 4)) return -1;
        pos = cp->buf;
    }
    ClothBendPass b;
    memset(&b, 0, sizeof b);
    b.slots = pos, b.particles = pd, b.records = rec, b.scale = kp, b.n = (uint32_t)n;
    memcpy(b.threshold, th, 8), memcpy(b.alt_stiffness, t, 8), memcpy(b.numerator, t + 0x10, 8);
    b.fz = (int)((c->fpcr >> 24) & 1);
    b.fpsr_in = CLOTH_FPSR_IN;
    cloth_bend_pass(&b);
    *done = b.done;
    if (__builtin_expect(b.done < n, 0) && __atomic_load_n(&cloth_dx_now, __ATOMIC_RELAXED) > 0) bend_declined(c, &b, p, tp, t, th);
    if (b.done == 0) return 0;
    if (b.slot_lo <= b.slot_hi) WRITTEN(cp, pos + 16 * (size_t)b.slot_lo, 16 * (size_t)(b.slot_hi - b.slot_lo + 1));
    return b.done < n ? 2 : 1;
}

/* cloth.bend (entry, can_return), first in the routine: non-zero returns from it, the call done (natively, or the host
   ran the lifted routine itself: check mode, the rest of a call done in part); 0 runs the routine from its head. The
   lifted routine called from here reaches this site again, where bend_inside answers 0 */
int hos_site_cloth_bend(Ctx *A64_NOESC c) {
    if (bend_inside) return 0;
    const int mode = cloth_mode();
    if (!mode) return 0;
    const A64Fn lifted = cloth_lifted(&bend_fn, OOT_CLOTH_BEND_FN);
    if (!lifted) return 0;
    const uint64_t n = (uint32_t)c->x[0], rn = (int32_t)n > 0 ? n : 0;
    const int dx = cloth_dx();
    cloth_count(K_BEND, S_CALLS, 1);
    if (mode == CLOTH_CHECK) {
        Ctx want = *c;
        ClothCopy cp = {0};
        uint32_t done = 0;
        const int h = bend_answer(&want, &cp, &done);
        if (__builtin_expect(dx, 0) && h >= 0) cloth_dx_call(K_BEND, rn, h == 1 ? rn : h >= 2 ? done : 0, h != 1);
        bend_inside = 1;
        lifted(c);
        bend_inside = 0;
        if (h == 1) {
            cloth_count(K_BEND, S_ALL, 1), cloth_count(K_BEND, S_REC_NATIVE, (long)rn);
            cloth_compare_seen(K_BEND, &want, c, &cp, 0, n);
        } else {
            cloth_check_other(K_BEND, h);
        }
        cloth_call_done();
        return 1;   /* (the lifted routine ran: its result stays) */
    }
    uint32_t done = 0;
    const int h = bend_answer(c, NULL, &done);
    if (__builtin_expect(dx, 0)) cloth_dx_call(K_BEND, rn, h == 1 ? rn : h == 2 ? done : 0, h != 1);
    if (h == 1) {
        cloth_count(K_BEND, S_ALL, 1), cloth_count(K_BEND, S_REC_NATIVE, (long)rn), cloth_call_done();
        return 1;
    }
    if (h == 0) {
        cloth_count(K_BEND, S_DECLINED, 1), cloth_count(K_BEND, S_REC_LIFTED, (long)rn), cloth_call_done();
        return 0;
    }
    /* the first `done` records were the host's: the lifted routine does the rest from there (w0 = the records left, x3 at
       the first of them: the same routine state; it leaves the same registers), then x0 and x3 are given back */
    cloth_count(K_BEND, S_PART, 1), cloth_count(K_BEND, S_REC_NATIVE, done), cloth_count(K_BEND, S_REC_LIFTED, (long)(n - done));
    const uint64_t s0 = c->x[0], s3 = c->x[3];
    c->x[0] = n - done, c->x[3] = s3 + 32ull * done;
    bend_inside = 1;
    lifted(c);
    bend_inside = 0;
    c->x[0] = s0, c->x[3] = s3;
    cloth_call_done();
    return 1;
}

/* ---- the three loop passes at the end of their loop's set-up: the links, the bend links, the tethers ------------------- */
/* Each loop sits in a Havok timer scope (f_81c86720, f_81c7d2e0, f_81c88940). Its site is the last instruction of the
   loop's set-up, a jump to the loop's way out. At the site: x9 = the count (>= 1), x20 the constraint set (+0x28: its
   records), x23 the particle slots, v0 lane 0 the pass's scale (and for the links and bend links x24 the particle data).
   The answer runs the kernel, which does the records from the first while it can give the game's bits:
     1  every record done. Nothing is handed back: after the loop the routine loads the count's register afresh, closes
        the timer scope and returns, and reads none of what the loop leaves in registers or flags (at the return those
        are scratch registers and flags, no caller's to read); so the site's jump to the loop's way out is all it needs.
     2  the first `done` records done: the count left, the record pointer and the constants the loop's set-up put in
        registers are set for record `done`, and the lifted loop does the rest (nothing but memory is carried from one
        record to the next)
     0  declined: no register written, the lifted loop does every record */

/* HOS_CLOTH_DECLINES, the three loop passes: the kernel stopped at record `d` of `n` for the reason `why` (its name; NULL:
   none the hook knows): counted, and the call captured from there (x9 = the records left) */
static void label_declined(int k, const char *why, const Ctx *c, uint64_t n, uint32_t d, uint64_t recs, size_t size,
                           const uint8_t *rec, const uint8_t *pos, const uint8_t *pd, const uint8_t *op, uint64_t p,
                           const uint8_t *tp, const uint8_t *t) {
    if (why) cloth_dn.why.what = why, cloth_dn.why.lane = -1, cloth_dn.why.cls = CW_KERNEL, cloth_dn.why.bits = 0;
    ClothDxKey *e = cloth_dx_record(k, why != NULL, n, d);
    if (!e) return;
    static const int at[2] = {0, 2};
    uint32_t lo, hi;
    cloth_u16_span(rec, d, n, size, at, 2, &lo, &hi);
    const uint64_t span = 16 * (uint64_t)(hi - lo + 1);
    ClothCapRegion r[7];
    int nr = 0;
    r[nr++] = (ClothCapRegion){c->x[23] + 16 * (uint64_t)lo, pos + 16 * (size_t)lo, span, 1};
    if (pd) r[nr++] = (ClothCapRegion){c->x[24] + 16 * (uint64_t)lo, pd + 16 * (size_t)lo, span, 2};
    r[nr++] = (ClothCapRegion){recs + size * d, rec + size * d, size * (n - d), 3};
    r[nr++] = (ClothCapRegion){c->x[20] + 0x28, op, 8, 4};
    if (tp) r[nr++] = (ClothCapRegion){CLOTH_TABLE_PTR, tp, 8, 7}, r[nr++] = (ClothCapRegion){p + 0x50, t, 8, 8};
    Ctx s = *c;
    s.x[9] = n - d, s.fpsr = c->fpsr;
    const uint64_t meta[4] = {recs + size * d, n - d, 0, 0};
    cloth_cap_write(k, e, &s, n, d, meta, r, nr);
}

/* (HOS_CLOTH_DECLINES) why the link and bend link kernels stopped (CLOTH_LINK_*, CLOTH_BEND_LINK_*: the same numbers), as
   the log names it */
_Static_assert(CLOTH_LINK_ENTRY == 1 && CLOTH_LINK_CLASS == 2 && CLOTH_LINK_STEP == 3 && CLOTH_LINK_OTHER == 4 &&
               CLOTH_BEND_LINK_ENTRY == 1 && CLOTH_BEND_LINK_CLASS == 2 && CLOTH_BEND_LINK_STEP == 3 &&
               CLOTH_BEND_LINK_OTHER == 4, "the link kernels' reasons");
static const char *const label_why_name[5] = {"every link (no stop)", "an entry value outside what it takes",
                                              "a link value outside the exactness class",
                                              "a reciprocal square root step outside its proven range", "another reason"};
static const char *label_why(int why) { return why > 0 && why < 5 ? label_why_name[why] : NULL; }

/* ---- f_81c86720's loop: the standard link constraints ------------------------------------------------------------- */
/* At cloth.links: the links are 12 bytes (u16 a, u16 b, rest length, stiffness) at [x20 + 0x28]; the kernel
   (game/cloth_link.h) writes the slots of the links it does. 2: x9-x11 for the link left */
static int link_answer(Ctx *c, ClothCopy *cp, uint32_t *done) {
    const uint64_t n = c->x[9];
    if (n - 1 >= 0x7fffffffull) return decline("count");   /* (not reachable at the site) */
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_LINK, S_GATE, 1), decline("gate: FPCR");
    const uint8_t *op = (const uint8_t *)hos_guest_range(c->x[20] + 0x28, 8);
    if (!op) return decline("memory: the object's +0x28");
    uint64_t r0;
    memcpy(&r0, op, 8);
    const uint8_t *rec = (const uint8_t *)hos_guest_range(r0, 12 * n);
    uint8_t *pos = (uint8_t *)hos_guest_range(c->x[23], CLOTH_SPAN);
    const uint8_t *pd = (const uint8_t *)hos_guest_range(c->x[24], CLOTH_SPAN);
    if (!rec || !pos || !pd)
        return decline(!rec ? "memory: records" : !pos ? "memory: particle slots (1 MB)" : "memory: particle data (1 MB)");
    if (cp) {
        static const int at[2] = {0, 2};
        const uint32_t top = top_u16(rec, n, 12, at, 2);
        const uint8_t *reg[2] = {rec, pd};
        const size_t reg_n[2] = {(size_t)(12 * n), 16 * ((size_t)top + 1)};
        if (!cloth_copy(cp, pos, top, reg, reg_n, 2, rec, n, 12, at, 2)) return -1;
        pos = cp->buf;
    }
    ClothLinkPass b;
    b.slots = pos, b.particles = pd, b.links = rec, b.n = (uint32_t)n, b.scale = (uint32_t)c->vreg[0][0];
    b.fz = (int)((c->fpcr >> 24) & 1), b.fpsr_in = CLOTH_FPSR_IN;
    cloth_link_pass(&b);
    *done = b.done;
    if (__builtin_expect(b.done < n, 0) && __atomic_load_n(&cloth_dx_now, __ATOMIC_RELAXED) > 0)
        label_declined(K_LINK, label_why(b.why), c, n, b.done, r0, 12, rec, pos, pd, op, 0, NULL, NULL);
    if (b.done == 0) return 0;
    if (b.slot_lo <= b.slot_hi) WRITTEN(cp, pos + 16 * (size_t)b.slot_lo, 16 * (size_t)(b.slot_hi - b.slot_lo + 1));
    if (b.done == n) return 1;
    c->x[9] = n - b.done, c->x[10] = r0 + 4 + 12ull * b.done, c->x[11] = 0x7fffff;
    return 2;
}

/* ---- f_81c7d2e0's loop: the bend link constraints ------------------------------------------------------------------ */
/* At cloth.bendLinks: the links are 20 bytes (u16 p1, u16 p2, lo, hi, k_lo, k_hi) at [x20 + 0x28]; the set-up reads the
   clamp pair at the constants' +0x50 into v16, which the answer reads again itself (no store comes between); the kernel
   (game/cloth_bend_link.h) writes the slots of the links it does. 2: x9-x11 and v16 for the link left */
static int bend_link_answer(Ctx *c, ClothCopy *cp, uint32_t *done) {
    const uint64_t n = c->x[9];
    if (n < 1 || n >= 0x80000000ull) return decline("count");   /* (cannot happen at the site) */
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_BEND_LINK, S_GATE, 1), decline("gate: FPCR");
    const uint64_t x20 = c->x[20], x23 = c->x[23], x24 = c->x[24];
    const uint8_t *tp = (const uint8_t *)hos_guest_range(CLOTH_TABLE_PTR, 8);
    const uint8_t *op = (const uint8_t *)hos_guest_range(x20 + 0x28, 8);
    if (!tp || !op) return decline(!tp ? "memory: the constants' pointer" : "memory: the object's +0x28");
    uint64_t p, x10;
    memcpy(&p, tp, 8), memcpy(&x10, op, 8);
    const uint8_t *t = (const uint8_t *)hos_guest_range(p + 0x50, 8);
    uint8_t *pos = (uint8_t *)hos_guest_range(x23, CLOTH_SPAN);
    const uint8_t *pd = (const uint8_t *)hos_guest_range(x24, CLOTH_SPAN);
    const uint8_t *rec = (const uint8_t *)hos_guest_range(x10, 20 * n);
    if (!t || !pos || !pd || !rec)
        return decline(!t ? "memory: the constants" : !pos ? "memory: particle slots (1 MB)" : !pd ? "memory: particle data (1 MB)" :
                       "memory: records");
    if (cp) {
        static const int at[2] = {0, 2};
        const uint32_t top = top_u16(rec, n, 20, at, 2);
        const uint8_t *reg[2] = {rec, pd};
        const size_t reg_n[2] = {(size_t)(20 * n), 16 * ((size_t)top + 1)};
        if (!cloth_copy(cp, pos, top, reg, reg_n, 2, rec, n, 20, at, 2)) return -1;
        pos = cp->buf;
    }
    uint32_t clamp[2];   /* (as the set-up read it: a slot store may reach the constants) */
    memcpy(clamp, t, 8);
    ClothBendLinkPass b;
    b.slots = pos, b.particles = pd, b.links = rec, b.n = (uint32_t)n, b.clamp[0] = clamp[0], b.clamp[1] = clamp[1];
    b.scale = (uint32_t)c->vreg[0][0], b.fz = (int)((c->fpcr >> 24) & 1), b.fpsr_in = CLOTH_FPSR_IN;
    cloth_bend_link_pass(&b);
    *done = b.done;
    if (__builtin_expect(b.done < n, 0) && __atomic_load_n(&cloth_dx_now, __ATOMIC_RELAXED) > 0)
        label_declined(K_BEND_LINK, label_why(b.why), c, n, b.done, x10, 20, rec, pos, pd, op, p, tp, t);
    if (b.done == 0) return 0;
    if (b.slot_lo <= b.slot_hi) WRITTEN(cp, pos + 16 * (size_t)b.slot_lo, 16 * (size_t)(b.slot_hi - b.slot_lo + 1));
    if (b.done == n) return 1;
    c->x[9] = n - b.done, c->x[10] = x10 + 20ull * b.done, c->x[11] = 0x7fffff;
    c->vreg[16][0] = lo64(clamp[0], clamp[1]), c->vreg[16][1] = 0;
    return 2;
}

/* ---- f_81c88940's loop: the tethers --------------------------------------------------------------------------------- */
/* At cloth.tethers: the records are 12 bytes (u16 i, u16 j, rest, stiffness) at [x20 + 0x28]; the kernel
   (game/cloth_tether.h) writes slot j of each record it does, the only memory written. 2: x9-x11 and the zero vector v7
   for the record left */
static const char *const tether_why_name[3] = {"every record (no stop)", "a value it cannot give the game's bits for",
                                               "another reason"};   /* (CLOTH_TETHER_*, as the log names them) */

static int tether_answer(Ctx *c, ClothCopy *cp, uint32_t *done) {
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_TETHER, S_GATE, 1), decline("gate: FPCR");
    const uint64_t n = c->x[9], x23 = c->x[23];
    if (n == 0 || n >= 0x80000000ull) return decline("count");
    const uint8_t *o = (const uint8_t *)hos_guest_range(c->x[20] + 0x28, 8);
    if (!o) return decline("memory: the object's +0x28");
    uint64_t recs;
    memcpy(&recs, o, 8);
    uint8_t *pos = (uint8_t *)hos_guest_range(x23, CLOTH_SPAN);
    const uint8_t *rec = (const uint8_t *)hos_guest_range(recs, 12 * n);
    if (!pos || !rec) return decline(!pos ? "memory: particle slots (1 MB)" : "memory: records");
    if (cp) {
        static const int at[2] = {0, 2};
        const uint32_t top = top_u16(rec, n, 12, at, 2);
        const uint8_t *reg[1] = {rec};
        const size_t reg_n[1] = {(size_t)(12 * n)};
        if (!cloth_copy(cp, pos, top, reg, reg_n, 1, rec, n, 12, at, 2)) return -1;
        pos = cp->buf;
    }
    ClothTetherCall k;
    k.slots = pos, k.records = rec, k.count = (uint32_t)n, k.scale = (uint32_t)c->vreg[0][0];
    k.fz = (int)((c->fpcr >> 24) & 1), k.fpsr = CLOTH_FPSR_IN;
    ClothTetherResult r;
    cloth_tether_81c88940(&k, &r);
    *done = r.done;
    if (__builtin_expect(r.done < n, 0) && __atomic_load_n(&cloth_dx_now, __ATOMIC_RELAXED) > 0)
        label_declined(K_TETHER, r.why > 0 && r.why < 3 ? tether_why_name[r.why] : NULL, c, n, r.done, recs, 12, rec, pos,
                       NULL, o, 0, NULL, NULL);
    if (r.done == 0) return 0;
    if (r.slot_lo <= r.slot_hi) WRITTEN(cp, pos + 16 * (size_t)r.slot_lo, 16 * (size_t)(r.slot_hi - r.slot_lo + 1));
    if (r.done == n) return 1;
    c->x[9] = n - r.done, c->x[10] = recs + 4 + 12ull * r.done, c->x[11] = 0x7fffff;
    setq(c, 7, 0, 0, 0, 0);
    return 2;
}

/* the three loop hooks: the answer (on), or the answer into copies and the lifted loop for real, compared at the loop's
   way out (check mode). 1: every record done, the site jumps to the way out; 0: the loop runs, from its first record or,
   a call done in part, with the registers the answer set for record `done` */
static int label_hook(Ctx *c, int k, int (*answer)(Ctx *, ClothCopy *, uint32_t *)) {
    const int mode = cloth_mode();
    if (!mode) return 0;
    const uint64_t n = c->x[9], rn = n < 0x80000000ull ? n : 0;
    const int dx = cloth_dx();
    cloth_count(k, S_CALLS, 1);
    uint32_t done = 0;
    if (mode == CLOTH_CHECK) {
        cloth_pending.k = -1;   /* (a pending answer left by a loop that did not reach its exit: dropped) */
        cloth_pending.want = *c;
        ClothCopy cp = {0};
        const int h = answer(&cloth_pending.want, &cp, &done);
        if (__builtin_expect(dx, 0) && h >= 0) cloth_dx_call(k, rn, h == 1 ? rn : h >= 2 ? done : 0, h != 1);
        if (h == 1) {
            cloth_count(k, S_ALL, 1), cloth_count(k, S_REC_NATIVE, (long)n);
            cloth_pending.k = k, cloth_pending.cp = cp, cloth_pending.records = n;
            return 0;   /* (the lifted loop runs; its exit compares) */
        }
        cloth_check_other(k, h);
        cloth_call_done();
        return 0;
    }
    const int h = answer(c, NULL, &done);
    if (__builtin_expect(dx, 0)) cloth_dx_call(k, rn, h == 1 ? rn : h == 2 ? done : 0, h != 1);
    if (h == 1) cloth_count(k, S_ALL, 1), cloth_count(k, S_REC_NATIVE, (long)n);
    else if (h == 2) cloth_count(k, S_PART, 1), cloth_count(k, S_REC_NATIVE, done), cloth_count(k, S_REC_LIFTED, (long)(n - done));
    else cloth_count(k, S_DECLINED, 1), cloth_count(k, S_REC_LIFTED, (long)n);
    cloth_call_done();
    return h == 1;
}

/* (check mode, a loop pass's cloth.*End site: the pending answer against the lifted loop on what the routine can see after
   the loop: the memory written, FPSR and the callee-saved registers; the loop hands back nothing else) */
static void label_exit(Ctx *c, int k) {
    if (cloth_pending.k != k) return;
    cloth_pending.k = -1;
    cloth_compare_seen(k, &cloth_pending.want, c, &cloth_pending.cp, 0, cloth_pending.records);
    cloth_call_done();
}

/* cloth.links (jump to the loop's way out), cloth.linksEnd (at the way out, check mode only: the compare) */
int hos_site_cloth_links(Ctx *A64_NOESC c) { return label_hook(c, K_LINK, link_answer); }
static int cloth_links_end(Ctx *A64_NOESC c) { label_exit(c, K_LINK); return 0; }
/* cloth.bendLinks, cloth.bendLinksEnd: the same for the bend links */
int hos_site_cloth_bend_links(Ctx *A64_NOESC c) { return label_hook(c, K_BEND_LINK, bend_link_answer); }
static int cloth_bend_links_end(Ctx *A64_NOESC c) { label_exit(c, K_BEND_LINK); return 0; }
/* cloth.tethers, cloth.tethersEnd: the same for the tethers */
int hos_site_cloth_tethers(Ctx *A64_NOESC c) { return label_hook(c, K_TETHER, tether_answer); }
static int cloth_tethers_end(Ctx *A64_NOESC c) { label_exit(c, K_TETHER); return 0; }

/* ---- f_81c7bd00: the skinned-anchor distance pass ----------------------------------------------------------------- */
/* A leaf (game/cloth_anchor.h): x0 the anchor constraint, x1 the particle slots (the only memory it writes), x3 the skin,
   x4 the time factor, x5 the scale object or 0. The kernel reads everything else itself through the guest range resolver
   and answers the whole call or declines it whole (writing nothing): the hook is first in the routine and nothing more.
   What a caller can see of the routine: the slots it writes and the float status (it returns nothing, and its caller
   reads none of the scratch registers or flags it leaves), so on "done" the hook marks the slots written and returns; declined, the lifted routine runs from its head on the unchanged memory. */
static _Thread_local int anchor_inside;    /* (check mode: the lifted routine called from the hook) */
static A64Fn anchor_fn;                    /* (the lifted routine, cloth_lifted's) */

static inline uint32_t ld32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static inline uint64_t ld64(const uint8_t *p) { uint64_t v; memcpy(&v, p, 8); return v; }
static inline void setq4(Ctx *c, int r, const uint32_t *v) { setq(c, r, v[0], v[1], v[2], v[3]); }

static void *cloth_range(uint64_t a, uint64_t n) { return hos_guest_range(a, n); }

/* (HOS_CLOTH_DECLINES) the kernel's reasons for declining a call (ClothAnchorResult.why), as the log names them */
static const char *const anchor_why_name[7] = {"kernel: (done)", "kernel: a range not mapped", "kernel: a negative record count",
                                               "kernel: a value it cannot answer with the game's bits",
                                               "kernel: a store could reach what it read", "kernel: its own storage",
                                               "kernel: another reason"};

/* the kernel on the call: 1 done (*r filled), 0 declined (nothing written) */
static int anchor_answer(Ctx *c, ClothAnchorResult *r) {
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_ANCHOR, S_GATE, 1), decline("gate: FPCR");
    ClothAnchorCall k;
    k.constraint = c->x[0], k.slots = c->x[1], k.skin = c->x[3], k.factor = c->x[4], k.scale = c->x[5];
    k.fz = (int)((c->fpcr >> 24) & 1);
    k.fpsr = CLOTH_FPSR_IN;
    k.range = cloth_range;
    memset(r, 0, sizeof *r);
    if (!cloth_anchor_81c7bd00(&k, r))
        return decline(r->why > CLOTH_ANCHOR_DONE && r->why <= CLOTH_ANCHOR_OTHER ? anchor_why_name[r->why] : anchor_why_name[6]);
    return 1;
}

/* check mode: the kernel writes guest memory itself, so the slots the call may write (those its records name at entry)
   are kept in the thread's scratch first; after the kernel the two are swapped (the scratch holds the kernel's result,
   guest memory the entry bytes again) and the lifted routine runs on the memory the game had. -1: not compared (the
   object, the records or the slots not mapped, the records among the slots: a store could change which slots a later
   record names), 0 declined, 1 done. A kernel that changed a slot while declining, or that stored outside the named
   slots' range, is a mismatch */
static int anchor_check(Ctx *c, ClothCopy *cp, ClothAnchorResult *r) {
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_ANCHOR, S_GATE, 1), decline("gate: FPCR");
    const uint8_t *o = (const uint8_t *)hos_guest_range(c->x[0] + 0x28, 0x1c);   /* the two record lists and their counts */
    if (!o) return -1;
    const int32_t n16 = (int32_t)ld32(o + 0x08), n20 = (int32_t)ld32(o + 0x18);   /* (the short list unless it is empty) */
    const int32_t ns = n16 ? n16 : n20;
    const uint64_t size = n16 ? 16 : 20, recs = ld64(o + (n16 ? 0x00 : 0x10)), n = ns > 0 ? (uint64_t)ns : 0;
    uint32_t lo = 0xffffffffu, hi = 0;
    if (n) {
        static const int at_p[1] = {0};
        const uint8_t *rec = (const uint8_t *)hos_guest_range(recs, size * n);
        uint8_t *pos = (uint8_t *)hos_guest_range(c->x[1], CLOTH_SPAN);
        if (!rec || !pos) return -1;
        const uint8_t *reg[1] = {rec};
        const size_t reg_n[1] = {(size_t)(size * n)};
        cloth_u16_span(rec, 0, n, (size_t)size, at_p, 1, &lo, &hi);
        if (!cloth_copy(cp, pos, hi, reg, reg_n, 1, rec, n, (size_t)size, at_p, 1)) return -1;
    }
    const int h = anchor_answer(c, r);
    int bad = 0;
    if (cp->sparse)   /* the swap, and what the kernel left against the entry bytes */
        for (size_t slot = 0; slot < cp->n / 16; slot++) {
            if (!(cp->slots[slot / 8] & (1u << (slot % 8)))) continue;
            uint8_t t[16];
            memcpy(t, cp->real + 16 * slot, 16), memcpy(cp->real + 16 * slot, cp->buf + 16 * slot, 16);
            bad |= !h && memcmp(t, cp->buf + 16 * slot, 16);
            memcpy(cp->buf + 16 * slot, t, 16);
        }
    if (cp->sparse) HOS_WRITTEN(cp->real, cp->n);   /* (the kernel's stores and the entry bytes put back) */
    if (h &&r->slot_lo <= r->slot_hi && (!n || r->slot_lo < lo || r->slot_hi > hi)) bad = 1;
    if (bad) {
        __atomic_fetch_add(&cloth_total[K_ANCHOR][S_MISMATCH], 1, __ATOMIC_RELAXED);
        int no = 0;
        if (__atomic_compare_exchange_n(&cloth_reported, &no, 1, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED))
            hos_log("oot: cloth: MISMATCH (the first) in %s: the kernel %s", cloth_name[K_ANCHOR],
                    h ? "stored outside the slots its records name" : "declined but changed a slot");
    }
    return h;
}

/* cloth.anchor (entry, can_return), first in the routine: non-zero returns from it, the call done (natively, or in check
   mode the host ran the lifted routine itself, anchor_inside answering 0 there); 0 runs the routine from its head */
int hos_site_cloth_anchor(Ctx *A64_NOESC c) {
    if (anchor_inside) return 0;
    const int mode = cloth_mode();
    if (!mode) return 0;
    const A64Fn lifted = cloth_lifted(&anchor_fn, OOT_CLOTH_ANCHOR_FN);
    if (!lifted) return 0;
    const int dx = cloth_dx();
    cloth_count(K_ANCHOR, S_CALLS, 1);
    ClothAnchorResult r;
    if (mode == CLOTH_CHECK) {
        Ctx want = *c;
        ClothCopy cp = {0};
        const int h = anchor_check(&want, &cp, &r);
        if (__builtin_expect(dx, 0) && h >= 0) cloth_dx_call(K_ANCHOR, h ? r.records : 0, h ? r.records : 0, !h);
        anchor_inside = 1;
        lifted(c);
        anchor_inside = 0;
        if (h == 1) {
            cloth_count(K_ANCHOR, S_ALL, 1), cloth_count(K_ANCHOR, S_REC_NATIVE, (long)r.records);
            cloth_compare_seen(K_ANCHOR, &want, c, &cp, 0, r.records);
        } else {
            cloth_check_other(K_ANCHOR, h);
        }
        cloth_call_done();
        return 1;   /* (the lifted routine ran: its result stays) */
    }
    /* (a declined call's records are not counted: the hook does not read the constraint) */
    const int h = anchor_answer(c, &r);
    if (__builtin_expect(dx, 0)) cloth_dx_call(K_ANCHOR, h ? r.records : 0, h ? r.records : 0, !h);
    if (h) {
        if (r.slot_lo <= r.slot_hi)
            HOS_WRITTEN((uint8_t *)hos_guest_range(c->x[1] + 16 * (uint64_t)r.slot_lo, 16 * (uint64_t)(r.slot_hi - r.slot_lo + 1)),
                        16 * (size_t)(r.slot_hi - r.slot_lo + 1));
        cloth_count(K_ANCHOR, S_ALL, 1), cloth_count(K_ANCHOR, S_REC_NATIVE, (long)r.records);
    } else {
        cloth_count(K_ANCHOR, S_DECLINED, 1);
    }
    cloth_call_done();
    return h;
}

/* ---- the loops answered whole or declined whole (the particle boxes, the cull, the Verlet step, bone deform) ----------- */
/* The hook at the loop's entry (a jump site to the loop's way out): the answer (on: 1, the loop done, the lifted code goes
   on at the way out), or in check mode the answer into a copy of the registers (and of what the loop writes), the lifted
   loop for real, and the two compared at the way out (its cloth.*End site). `count`: the particles (records) of the
   call. */
static int count_hook(Ctx *c, int k, int (*answer)(Ctx *, ClothCopy *, uint64_t *)) {
    const int mode = cloth_mode();
    if (!mode) return 0;
    const int dx = cloth_dx();
    cloth_count(k, S_CALLS, 1);
    uint64_t n = 0;
    if (mode == CLOTH_CHECK) {
        cloth_pending.k = -1;   /* (a pending answer left by a loop that did not reach its exit: dropped) */
        cloth_pending.want = *c;
        ClothCopy cp = {0};
        const int h = answer(&cloth_pending.want, &cp, &n);
        if (__builtin_expect(dx, 0) && h >= 0) cloth_dx_call(k, n, h == 1 ? n : 0, h != 1);
        if (h == 1) {
            cloth_count(k, S_ALL, 1), cloth_count(k, S_REC_NATIVE, (long)n);
            cloth_pending.k = k, cloth_pending.cp = cp, cloth_pending.records = n;
            return 0;   /* (the lifted loop runs; its exit compares) */
        }
        if (h == 0) cloth_count(k, S_REC_LIFTED, (long)n);
        cloth_check_other(k, h);
        cloth_call_done();
        return 0;
    }
    const int h = answer(c, NULL, &n);
    if (__builtin_expect(dx, 0)) cloth_dx_call(k, n, h == 1 ? n : 0, h != 1);
    if (h == 1) cloth_count(k, S_ALL, 1), cloth_count(k, S_REC_NATIVE, (long)n);
    else cloth_count(k, S_DECLINED, 1), cloth_count(k, S_REC_LIFTED, (long)n);
    cloth_call_done();
    return h == 1;
}

/* check mode, the loop passes: what the hook hands back against the lifted loop's (the x registers in xs, every bit of the
   v registers in vs; none for a pass that hands nothing back) and FPSR. 0 equal, 2 different (what: the first). The rest
   of the registers and the flags a loop leaves are not read after it, so they are not compared */
static int cloth_kept_diff(const Ctx *want, const Ctx *got, uint32_t xs, uint32_t vs, char *what, size_t what_n) {
    for (int r = 0; r < 32; r++)
        if ((xs >> r & 1) && want->x[r] != got->x[r]) {
            snprintf(what, what_n, "x%d host %#" PRIx64 " lifted %#" PRIx64, r, want->x[r], got->x[r]);
            return 2;
        }
    for (int r = 0; r < 32; r++)
        if ((vs >> r & 1) && (want->vreg[r][0] != got->vreg[r][0] || want->vreg[r][1] != got->vreg[r][1])) {
            snprintf(what, what_n, "v%d host %016" PRIx64 "%016" PRIx64 " lifted %016" PRIx64 "%016" PRIx64, r, want->vreg[r][1],
                     want->vreg[r][0], got->vreg[r][1], got->vreg[r][0]);
            return 2;
        }
    if (want->fpsr != got->fpsr) {
        snprintf(what, what_n, "FPSR host %#" PRIx64 " lifted %#" PRIx64, want->fpsr, got->fpsr);
        return 2;
    }
    return 0;
}

/* (a cloth.*End site's callback for those passes, set only in check mode: the pending answer of pass k, what it hands back
   and the memory it wrote, against the lifted loop's) */
static void cloth_exit_kept(Ctx *c, int k, uint32_t xs, uint32_t vs) {
    if (cloth_pending.k != k) return;
    cloth_pending.k = -1;
    char what[160] = "";
    const int d = cloth_kept_diff(&cloth_pending.want, c, xs, vs, what, sizeof what);
    cloth_compare_tail(k, d, what, &cloth_pending.cp, cloth_pending.records);
    cloth_call_done();
}

/* ---- f_81b38800's loop: hclSimClothInstance::updateAabbs ---------------------------------------------------------------- */
/* At cloth.boxes (the loop's set-up, after the timer scope's begin; x20 the instance): the whole loop (game/cloth_aabb.h).
   It writes no memory. After it the routine reads the three boxes (the positions' box, the marked box and the moving box,
   each a minimum and a maximum corner) and the particle data's pointer, which the set-up loads and the site skips; nothing
   else the loop or its set-up leaves is read again (the last particle's values, the array positions, the two constants
   and the flags are written before any read). So those seven registers are what the hook hands back: v1 / v0 the
   positions' box, v2 / v3 the marked box, v4 / v5 the moving box, x9 the pointer. An empty count hands back the boxes at
   their start value. */
#define AABB_KEPT_X (1u << 9)
#define AABB_KEPT_V 0x3fu
static int aabb_answer(Ctx *c, ClothCopy *cp, uint64_t *count) {
    (void)cp;
    *count = 0;
    const uint64_t x20 = c->x[20];
    const uint8_t *tp = (const uint8_t *)hos_guest_range(CLOTH_TABLE_PTR, 8);
    const uint8_t *o = (const uint8_t *)hos_guest_range(x20 + 0x18, 0x20);   /* +0x18 data, +0x20 cur, +0x28 n, +0x30 prev */
    if (!tp || !o) return decline(!tp ? "memory: the constants' pointer" : "memory: the instance +0x18..+0x38");
    const uint8_t *init = (const uint8_t *)hos_guest_range(ld64(tp) + 0x250, 16);
    if (!init) return decline("memory: the constants");
    const uint64_t data = ld64(o);
    const uint32_t w10 = ld32(o + 0x10);
    uint32_t s[4];
    memcpy(s, init, 16);
    if ((int32_t)w10 < 1) {   /* no particles: the minimum corners at the start value, the maximum ones at its negation */
        setq4(c, 1, s), setq4(c, 2, s), setq4(c, 4, s);
        setq(c, 0, s[0] ^ 0x80000000u, s[1] ^ 0x80000000u, s[2] ^ 0x80000000u, s[3] ^ 0x80000000u);
        c->vreg[3][0] = c->vreg[5][0] = c->vreg[0][0], c->vreg[3][1] = c->vreg[5][1] = c->vreg[0][1];
        c->x[9] = data;
        return 1;
    }
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_AABB, S_GATE, 1), decline("gate: FPCR");
    const uint64_t n = w10;
    *count = n;
    const uint8_t *pd = (const uint8_t *)hos_guest_range(data + 0x40, 0xb0);   /* +0x40 the inverse masses, +0xe8 the masks */
    if (!pd) return decline("memory: the particle data +0x40..+0xf0");
    const uint64_t cur = ld64(o + 8), prev = ld64(o + 0x18), im = ld64(pd) + 4, mk = ld64(pd + 0xa8);
    ClothAabbArgs a;
    memset(&a, 0, sizeof a);
    a.pos = (const uint8_t *)hos_guest_range(cur, 16 * n);
    a.prev = (const uint8_t *)hos_guest_range(prev, 16 * n);
    a.mask = (const uint8_t *)hos_guest_range(mk, 4 * n);
    a.inv_mass = (const uint8_t *)hos_guest_range(im, 16 * (n - 1) + 4);
    if (!a.pos || !a.prev || !a.mask || !a.inv_mass)
        return decline(!a.pos ? "memory: positions" : !a.prev ? "memory: previous positions" : !a.mask ? "memory: masks" :
                       "memory: inverse masses");
    a.n = (uint32_t)n;
    memcpy(a.start, s, 16);
    a.fz = (int)((c->fpcr >> 24) & 1);
    a.fpsr_in = CLOTH_FPSR_IN;
    ClothAabbResult r;
    (void)cloth_aabb_81b38800(&a, &r);   /* (its float status is not put into FPSR: the gate's comment) */
    setq4(c, 0, r.pos_max), setq4(c, 1, r.pos_min), setq4(c, 2, r.marked_min), setq4(c, 3, r.marked_max);
    setq4(c, 4, r.moving_min), setq4(c, 5, r.moving_max);
    c->x[9] = data;
    return 1;
}

/* cloth.boxes (jump to the loop's way out: non-zero, the loop done), cloth.boxesEnd (check mode only: the compare) */
int hos_site_cloth_boxes(Ctx *A64_NOESC c) { return count_hook(c, K_AABB, aabb_answer); }
static int cloth_boxes_end(Ctx *A64_NOESC c) { cloth_exit_kept(c, K_AABB, AABB_KEPT_X, AABB_KEPT_V); return 0; }

/* ---- f_81b5a3c0's cull loop: the capsule collidable's particles inside its box ---------------------------------------- */
/* At cloth.cull (x19 the cloth instance, x20 its particle data, w22 the particles, w24 bit 0 the form, w21 != 0: no box
   test, w23 the collidable's group bits, x0 the list, sp + 0x90 the box): the whole loop (game/cloth_cull.h), the list
   written. After it the routine reads the count taken and the frame word at [x29-8], which the set-up loads and the site
   skips; nothing else the loop or its set-up leaves is read again (the last particle's verdicts, the last-decision word,
   the index and array positions past the loop and the flags are written before any read). So the hook hands back x28 the
   count and x26 the frame word. Declined: the gate, memory, over 0xffff particles (the loop's index is kept as a u16),
   or a list that would reach what the loop reads (positions, masses or masks, the box). */
#define CULL_KEPT_X ((1u << 26) | (1u << 28))
#define CULL_KEPT_V 0u
static int cull_answer(Ctx *c, ClothCopy *cp, uint64_t *count) {
    *count = 0;
    const uint32_t w22 = (uint32_t)c->x[22];
    const uint8_t *fp = (const uint8_t *)hos_guest_range(c->x[29] - 8, 8);
    if (!fp) return decline("memory: [x29-8]");
    const uint64_t frame = ld64(fp);   /* (read before the loop, as the game does: a list store may reach it) */
    if ((int32_t)w22 < 1) {   /* no particles: none taken */
        c->x[26] = frame, c->x[28] = 0;
        return 1;
    }
    if (w22 > 0xffffu) return decline("count over 0xffff");
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_CULL, S_GATE, 1), decline("gate: FPCR");
    const uint64_t n = w22, x0 = c->x[0], sp = c->x[31];
    *count = n;
    const int form_a = (int)(c->x[24] & 1), test = (uint32_t)c->x[21] == 0;
    const uint8_t *pp = (const uint8_t *)hos_guest_range(c->x[19] + 0x20, 8);
    const uint8_t *dp = (const uint8_t *)hos_guest_range(c->x[20] + (form_a ? 0x40 : 0xe8), 8);
    if (!pp || !dp) return decline(!pp ? "memory: the instance +0x20" : "memory: the particle data");
    const uint64_t pos = ld64(pp), arr = ld64(dp) + (form_a ? 4 : 0), arr_n = form_a ? 16 * (n - 1) + 4 : 4 * n;
    ClothCullArgs k;
    memset(&k, 0, sizeof k);
    k.positions = (const uint8_t *)hos_guest_range(pos, 16 * n);
    const uint8_t *arr_p = (const uint8_t *)hos_guest_range(arr, arr_n);
    k.masses = form_a ? arr_p : NULL;
    k.masks = form_a ? NULL : arr_p;
    k.box = test ? (const uint8_t *)hos_guest_range(sp + 0x90, 32) : NULL;
    uint8_t *out = (uint8_t *)hos_guest_range(x0, 2 * n);
    if (!k.positions || !arr_p || (test && !k.box) || !out)
        return decline(!k.positions ? "memory: positions" : !arr_p ? "memory: masses or masks" :
                       !out ? "memory: the list" : "memory: the box [sp+0x90]");
    /* (the loop reads the positions, the masses or masks and the box again for every particle: no list store may reach
       them. What it reads once before the loop, it has read before any store) */
    if (overlaps(x0, 2 * n, pos, 16 * n) || overlaps(x0, 2 * n, arr, arr_n) || (test && overlaps(x0, 2 * n, sp + 0x90, 32)))
        return decline("overlap: the list reaches what the loop reads");
    if (cp) {   /* (check mode: the kernel writes a copy of the list) */
        if (!cloth_scratch_get()) return -1;
        memcpy(cloth_scratch, out, (size_t)(2 * n));
        cp->real = out, cp->n = (size_t)(2 * n), cp->buf = cloth_scratch, cp->what = "the list";
        out = cloth_scratch;
    }
    k.list = out, k.n = (uint32_t)n, k.group = (uint32_t)c->x[23], k.mass_form = form_a, k.box_test = test;
    k.fz = (int)((c->fpcr >> 24) & 1);
    ClothCullResult r;
    cloth_cull_81b5a3c0(&k, &r);   /* (its float status is not put into FPSR: the gate's comment) */
    if (r.count) WRITTEN(cp, out, 2 * (size_t)r.count);
    c->x[26] = frame, c->x[28] = r.count;
    return 1;
}

/* cloth.cull (jump to the loop's way out: non-zero, the loop done), cloth.cullEnd (check mode only: the compare) */
int hos_site_cloth_cull(Ctx *A64_NOESC c) { return count_hook(c, K_CULL, cull_answer); }
static int cloth_cull_end(Ctx *A64_NOESC c) { cloth_exit_kept(c, K_CULL, CULL_KEPT_X, CULL_KEPT_V); return 0; }

/* ---- f_81c66a60's Verlet loop: hclSimulateOperator::_integrateTask (step 8) ------------------------------------------- */
/* At cloth.verlet (x22 the cloth instance, [sp+8] its particle data, w25 the particle count for the groups of four, w28 the
   routine's copy of it for the rest, x21 the forces, x23 the gravity vector, v22 lane 0 the squared time step, v23 the
   damping): the whole loop (game/cloth_verlet.h), the positions and previous positions written. Nothing the loop or its
   set-up leaves in a register is read after it (the rest of the routine, and its caller, write each before reading it),
   so the hook hands back no register: the kernel's values of the last particles are dropped. Declined: the gate, memory,
   or written arrays that meet each other or what the loop reads. */
#define VERLET_KEPT_X 0u
#define VERLET_KEPT_V 0u
static int verlet_answer(Ctx *c, ClothCopy *cp, uint64_t *count) {
    *count = 0;
    const uint64_t x22 = c->x[22], sp = c->x[31];
    const uint8_t *pp = (const uint8_t *)hos_guest_range(sp + 8, 8), *o = (const uint8_t *)hos_guest_range(x22 + 0x20, 0x18);
    if (!pp || !o) return decline(!pp ? "memory: [sp+8]" : "memory: the instance +0x20..+0x38");
    const uint8_t *pdp = (const uint8_t *)hos_guest_range(ld64(pp) + 0x40, 8);
    if (!pdp) return decline("memory: the particle data +0x40");
    const uint64_t cur = ld64(o), prev = ld64(o + 0x10), sab = ld64(pdp), acc = c->x[21], g = c->x[23];
    const uint32_t w25 = (uint32_t)c->x[25], w28 = (uint32_t)c->x[28];
    const uint32_t groups = (int32_t)w25 >= 4 ? w25 >> 2 : 0, singles = w28 & 3u;   /* (no group below four particles) */
    const uint64_t n = 4ull * groups + singles;
    *count = n;
    if (!n) return 1;   /* (no particles: nothing to do, nothing handed back) */
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_VERLET, S_GATE, 1), decline("gate: FPCR");
    uint8_t *hc = (uint8_t *)hos_guest_range(cur, 16 * n), *hp = (uint8_t *)hos_guest_range(prev, 16 * n);
    ClothVerletArgs a;
    memset(&a, 0, sizeof a);
    a.force = (const uint8_t *)hos_guest_range(acc, 16 * n);
    a.scale = (const uint8_t *)hos_guest_range(sab, 16 * (n - 1) + 8);
    a.gravity = (const uint8_t *)hos_guest_range(g, 16);
    if (!hc || !hp || !a.force || !a.scale || !a.gravity)
        return decline(!hc ? "memory: positions" : !hp ? "memory: previous positions" : !a.force ? "memory: forces" :
                       !a.scale ? "memory: the particle data's pairs" : "memory: [x23]");
    /* (every load sees what the game's would whatever the order: the written arrays meet nothing the loop reads) */
    if (overlaps(cur, 16 * n, prev, 16 * n) || overlaps(cur, 16 * n, acc, 16 * n) || overlaps(prev, 16 * n, acc, 16 * n) ||
        overlaps(cur, 16 * n, sab, 16 * (n - 1) + 8) || overlaps(prev, 16 * n, sab, 16 * (n - 1) + 8) ||
        overlaps(cur, 16 * n, g, 16) || overlaps(prev, 16 * n, g, 16))
        return decline("overlap: the positions meet each other or what the loop reads");
    if (cp) {   /* (check mode: the kernel writes copies of both arrays) */
        if (32 * n > CLOTH_SPAN) return -1;
        if (!cloth_scratch_get()) return -1;
        memcpy(cloth_scratch, hc, (size_t)(16 * n)), memcpy(cloth_scratch + 16 * n, hp, (size_t)(16 * n));
        cp->real = hc, cp->n = (size_t)(16 * n), cp->buf = cloth_scratch, cp->what = "the positions";
        cp->real2 = hp, cp->n2 = (size_t)(16 * n), cp->buf2 = cloth_scratch + 16 * n, cp->what2 = "the previous positions";
        hc = cp->buf, hp = cp->buf2;
    }
    ClothVerletLast last;   /* (the kernel's values of the last particles: not read after the loop) */
    a.pos = hc, a.prev = hp, a.groups = groups, a.singles = singles;
    a.dt2 = (uint32_t)c->vreg[22][0];
    memcpy(a.damping, c->vreg[23], 16);
    a.fz = (int)((c->fpcr >> 24) & 1);
    a.fpsr_in = CLOTH_FPSR_IN;
    a.last = &last;
    (void)cloth_verlet_81c66a60(&a);   /* (its float status is not put into FPSR: the gate's comment) */
    WRITTEN(cp, hc, 16 * n);
    WRITTEN(cp, hp, 16 * n);
    return 1;
}

/* cloth.verlet (jump to the loop's way out: non-zero, the loop done), cloth.verletEnd (check mode only: the compare) */
int hos_site_cloth_verlet(Ctx *A64_NOESC c) { return count_hook(c, K_VERLET, verlet_answer); }
static int cloth_verlet_end(Ctx *A64_NOESC c) { cloth_exit_kept(c, K_VERLET, VERLET_KEPT_X, VERLET_KEPT_V); return 0; }

/* ---- f_81cdff40: Prepare Matrices (step 8) ---------------------------------------------------------------------------- */
/* A leaf (game/cloth_matrices.h): x0 M, x1 the bones, x2 the indices, x3 the entries, w4 the count (bits 4..19 groups of
   16, bits 0..3 the rest), x5 the output. What a caller can see of it: the output it writes, the float status, and the
   seven vectors it leaves for the caller (the kernel's `left`, put where the callers read them: v3-v7, v16, v17, all 128
   bits). Those seven are kept because at 20 of the routine's 60 call sites the caller's blend that follows reads them
   when the next vertex has no influences (it skips its own loads then); nothing else of the routine's exit state is read
   by a caller, so no other register, flag or stack word is written. Declined: the gate, memory, an output that meets what
   the routine reads or the stack the routine uses below sp, that stack meeting what it reads. */
static _Thread_local int pm_inside;   /* (check mode: the lifted routine called from the hook) */
static A64Fn pm_fn;                   /* (the lifted routine, cloth_lifted's) */
#define PM_STACK 0xc30ull             /* the bytes below sp the routine uses as scratch */
static const int pm_left_reg[CLOTH_MX_LEFT] = {3, 4, 5, 6, 7, 16, 17};   /* left[k]: where the callers read it */
#define PM_LEFT_MASK ((1u << 3) | (1u << 4) | (1u << 5) | (1u << 6) | (1u << 7) | (1u << 16) | (1u << 17))

static int pm_answer(Ctx *c, ClothCopy *cp, uint64_t *count) {
    const uint32_t w4 = (uint32_t)c->x[4], blocks = (w4 >> 4) & 0xffffu, rest = w4 & 0xfu;
    const uint64_t n = 16ull * blocks + rest, x0 = c->x[0], x1 = c->x[1], x2 = c->x[2], x3 = c->x[3], x5 = c->x[5];
    const uint64_t frame = c->x[31] - PM_STACK, frame_n = PM_STACK;
    *count = n;
    if (!n) return 1;   /* no entry: the routine writes nothing a caller sees */
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_PM, S_GATE, 1), decline("gate: FPCR");
    ClothMatricesArgs a;
    memset(&a, 0, sizeof a);
    a.model = (const uint8_t *)hos_guest_range(x0, 64);
    a.index = (const uint8_t *)hos_guest_range(x2, 2 * n);
    a.in = (const uint8_t *)hos_guest_range(x3, 64 * n);
    uint8_t *out = (uint8_t *)hos_guest_range(x5, 64 * n);
    if (!a.model || !a.index || !a.in || !out)
        return decline(!a.model ? "memory: M [x0]" : !a.index ? "memory: the indices" : !a.in ? "memory: the entries" : "memory: the output");
    uint32_t top = 0;
    for (uint64_t i = 0; i < n; i++) {
        uint16_t k;
        memcpy(&k, a.index + 2 * i, 2);
        top = k > top ? k : top;
    }
    const uint64_t bn = 64ull * (top + 1);
    a.bones = (const uint8_t *)hos_guest_range(x1, bn);
    if (!a.bones) return decline("memory: the bones");
    /* (no store may reach a later load: the output and the routine's stack meet nothing the routine reads, nor each other) */
    if (overlaps(x5, 64 * n, x0, 64) || overlaps(x5, 64 * n, x1, bn) || overlaps(x5, 64 * n, x2, 2 * n) ||
        overlaps(x5, 64 * n, x3, 64 * n) || overlaps(x5, 64 * n, frame, frame_n) || overlaps(frame, frame_n, x0, 64) ||
        overlaps(frame, frame_n, x1, bn) || overlaps(frame, frame_n, x2, 2 * n) || overlaps(frame, frame_n, x3, 64 * n))
        return decline("overlap: the output or the stack meets what the routine reads");
    if (cp) {   /* (check mode: a copy of the output) */
        if (64 * n > CLOTH_SPAN || !cloth_scratch_get()) return -1;
        memcpy(cloth_scratch, out, (size_t)(64 * n));
        cp->real = out, cp->n = (size_t)(64 * n), cp->buf = cloth_scratch, cp->what = "the output";
        out = cp->buf;
    }
    uint8_t left[16 * CLOTH_MX_LEFT];
    a.out = out, a.count_word = w4, a.left = left;
    a.fz = (int)((c->fpcr >> 24) & 1);
    a.fpsr_in = CLOTH_FPSR_IN;
    (void)cloth_matrices_81cdff40(&a);   /* (the flags it reports are not FPSR's: the gate's comment) */
    WRITTEN(cp, out, 64 * n);
    for (int k = 0; k < CLOTH_MX_LEFT; k++) memcpy(c->vreg[pm_left_reg[k]], left + 16 * k, 16);
    return 1;
}

/* cloth.matrices (entry, can_return), first in the routine: non-zero returns from it, the call done (natively, or in
   check mode the host ran the lifted routine itself, pm_inside answering 0 there); 0 runs the routine from its head */
int hos_site_cloth_matrices(Ctx *A64_NOESC c) {
    if (pm_inside) return 0;
    const int mode = cloth_mode();
    if (!mode) return 0;
    const A64Fn lifted = cloth_lifted(&pm_fn, OOT_CLOTH_PM_FN);
    if (!lifted) return 0;
    const int dx = cloth_dx();
    cloth_count(K_PM, S_CALLS, 1);
    uint64_t n = 0;
    if (mode == CLOTH_CHECK) {
        Ctx want = *c;
        ClothCopy cp = {0};
        const int h = pm_answer(&want, &cp, &n);
        if (__builtin_expect(dx, 0) && h >= 0) cloth_dx_call(K_PM, n, h == 1 ? n : 0, h != 1);
        pm_inside = 1;
        lifted(c);
        pm_inside = 0;
        if (h == 1) {
            cloth_count(K_PM, S_ALL, 1), cloth_count(K_PM, S_REC_NATIVE, (long)n);
            cloth_compare_seen(K_PM, &want, c, &cp, PM_LEFT_MASK, n);
        } else {
            if (h == 0) cloth_count(K_PM, S_REC_LIFTED, (long)n);
            cloth_check_other(K_PM, h);
        }
        cloth_call_done();
        return 1;   /* (the lifted routine ran: its result stays) */
    }
    const int h = pm_answer(c, NULL, &n);
    if (__builtin_expect(dx, 0)) cloth_dx_call(K_PM, n, h == 1 ? n : 0, h != 1);
    if (h == 1) cloth_count(K_PM, S_ALL, 1), cloth_count(K_PM, S_REC_NATIVE, (long)n);
    else cloth_count(K_PM, S_DECLINED, 1), cloth_count(K_PM, S_REC_LIFTED, (long)n);
    cloth_call_done();
    return h == 1;
}

/* ---- f_81b8c000 mode 0: the capsule collidable's batch kernel (step 9) --------------------------------------------------
   game/cloth_capsule.h has what the routine computes: the call's particles in batches of 16, then the count & 15 left
   over, one at a time. Four sites in the routine:
     cloth.capsule (a jump to the batch loop's way out), at the branch that enters the batch loop, once a call: every batch
       natively, the last one included. Of everything the batches leave behind, the rest of the routine reads one value:
       the list position, which is the list's start plus 32 bytes a batch done. Every batch done: that position is set
       and the answer is non-zero (the jump). The kernel stopped at batch `done`: the loop's position and offset at that
       batch, and 0 (the lifted loop goes on there). Declined: nothing written, 0.
     cloth.capsuleRecordsEnd (check mode only), the batch loop's way out: check mode's compare of the batches.
     cloth.capsuleTail (a jump to the routine's way out), at the branch into the loop of the particles left over, which
       only a call with count & 15 not 0 reaches (w9 = count & 15 and the loop's constants set by then; x8 the list
       position, x15 the cloth instance): those particles natively. Every one done: non-zero, and nothing handed back,
       because the routine's way out reads none of the values that loop leaves. Stopped at particle `done`: the list
       position and the count left for that particle, and 0 (the lifted loop goes on there; the constants it reads are
       the set-up's, still in place). Declined: nothing written, 0.
     cloth.capsuleEnd (check mode only), the routine's way out: check mode's compare of the particles left over.
   Both decline whole calls for the gate (CLOTH_GATE_OPEN; the kernel is told every float status flag is set already,
   cloth_capsule.h), memory, and slots that would reach what the routine reads (the batches' indices, radii and
   frictions, the frame's capsule and pointers, the collidable, the instance's pointers and particle data, the jump
   tables and the mask constant in the image). Check mode compares what is handed back (the list position after the
   batches), FPSR and the slots written. */

/* the call's constants (the frame from sp+0x70 to sp+0x310 and the collidable's 0x50..0x90) */
static int cap_consts(const Ctx *c, ClothCapsule *k) {
    const uint8_t *f = (const uint8_t *)hos_guest_range(c->x[31] + 0x70, 0x2a0);
    const uint8_t *col = (const uint8_t *)hos_guest_range(c->x[20] + 0x50, 0x40);
    if (!f || !col) return 0;
    memcpy(k->eps, f, 16), memcpy(k->one, f + 0x10, 16);
    memcpy(k->a, f + 0x260, 16), memcpy(k->b, f + 0x270, 16);
    k->dt = ld32(f + 0x230), k->radius = ld32(f + 0x290), k->inv_len2 = ld32(f + 0x294);
    memcpy(k->centre, col, 16), memcpy(k->lin_vel, col + 0x20, 16), memcpy(k->ang_vel, col + 0x30, 16);
    k->fz = (int)((c->fpcr >> 24) & 1);
    return 1;
}
static inline int cap_gate(const Ctx *c) { return CLOTH_GATE_OPEN(c); }

#define COLL_KEPT_X (1u << 8)   /* (check mode: the one register a collision piece hands back, the list position after its batches) */

/* the batches natively (check mode: into copies): 1 every batch done (x8 the list position after them), 2 the first
   `done` done (x8 / x9 at batch `done`), 0 declined (no register written), 4 nothing to do, -1 (check mode: the copy
   refused). count: the batches; *native: those done natively */
#define CAP_FRAME 0x310   /* (the frame the routine reads: sp .. sp+0x310) */
static int caprec_answer(Ctx *c, ClothCopy *cp, uint64_t *count, uint64_t *native) {
    const uint64_t n = c->x[12] >> 6, x8 = c->x[8], sp = c->x[31];
    *count = n, *native = 0;
    if (!n) return 4;
    if (c->x[12] & 63) return decline("the batch loop's bound is not a whole batch");
    if (!cap_gate(c)) return cloth_count(K_CAPREC, S_GATE, 1), decline("gate: FPCR");
    ClothCapsule k;
    const uint8_t *pp = (const uint8_t *)hos_guest_range(sp + 0x228, 0x68);   /* sp+0x228 (x3), sp+0x288 (x2) */
    const uint8_t *fh = (const uint8_t *)hos_guest_range(sp, CAP_FRAME);
    if (!cap_consts(c, &k) || !pp || !fh) return decline("memory: the frame or the collidable");
    const uint64_t fcell = ld64(pp), rcell = ld64(pp + 0x60);
    const uint8_t *fc = (const uint8_t *)hos_guest_range(fcell, 8), *rc = (const uint8_t *)hos_guest_range(rcell, 8);
    if (!fc || !rc) return decline("memory: the radii's or frictions' pointer");
    const uint64_t fric = ld64(fc), rad = ld64(rc), pos = c->x[11], prev = c->x[10];
    const uint8_t *recs = (const uint8_t *)hos_guest_range(x8, 32 * n);
    const uint8_t *hr = (const uint8_t *)hos_guest_range(rad, 64 * n), *hf = (const uint8_t *)hos_guest_range(fric, 64 * n);
    if (!recs || !hr || !hf) return decline(!recs ? "memory: the batches' indices" : "memory: the radii or frictions");
    static const int at[16] = {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30};
    const uint64_t span = 16ull * (top_u16(recs, n, 32, at, 16) + 1);
    uint8_t *hp = (uint8_t *)hos_guest_range(pos, span), *hq = (uint8_t *)hos_guest_range(prev, span);
    if (!hp || !hq) return decline("memory: the particles' slots");
    /* (the slots written meet nothing read: the kernel reads each batch's inputs before its stores, and the routine reads
       the frame's inputs, the pointers, the collidable and the image's tables and mask every batch) */
    if (overlaps(pos, span, prev, span) || overlaps(pos, span, x8, 32 * n) || overlaps(prev, span, x8, 32 * n) ||
        overlaps(pos, span, rad, 64 * n) || overlaps(prev, span, rad, 64 * n) || overlaps(pos, span, fric, 64 * n) ||
        overlaps(prev, span, fric, 64 * n) || overlaps(pos, span, sp, CAP_FRAME) || overlaps(prev, span, sp, CAP_FRAME) ||
        overlaps(pos, span, c->x[20] + 0x50, 0x40) || overlaps(prev, span, c->x[20] + 0x50, 0x40) ||
        overlaps(pos, span, fcell, 8) || overlaps(prev, span, fcell, 8) || overlaps(pos, span, rcell, 8) ||
        overlaps(prev, span, rcell, 8) || overlaps(pos, span, CAP_MASK_CONST, 16) || overlaps(prev, span, CAP_MASK_CONST, 16) ||
        overlaps(pos, span, CAP_TABLES, 0x78) || overlaps(prev, span, CAP_TABLES, 0x78) ||
        overlaps(sp, CAP_FRAME, x8, 32 * n) || overlaps(sp, CAP_FRAME, rad, 64 * n) || overlaps(sp, CAP_FRAME, fric, 64 * n) ||
        overlaps(sp, CAP_FRAME, c->x[20] + 0x50, 0x40) || overlaps(sp, CAP_FRAME, fcell, 8) ||
        overlaps(sp, CAP_FRAME, rcell, 8))
        return decline("overlap: the slots or the frame reach what the routine reads");
    if (cp) {   /* (check mode: the kernel writes copies of both bases' slots) */
        if (2 * span > CLOTH_SPAN || !cloth_readable(hp, (size_t)span) || !cloth_readable(hq, (size_t)span)) return -1;
        if (!cloth_scratch_get()) return -1;
        memcpy(cloth_scratch, hp, (size_t)span), memcpy(cloth_scratch + span, hq, (size_t)span);
        cp->real = hp, cp->n = (size_t)span, cp->buf = cloth_scratch, cp->what = "the positions";
        cp->real2 = hq, cp->n2 = (size_t)span, cp->buf2 = cloth_scratch + span, cp->what2 = "the previous positions";
        hp = cp->buf, hq = cp->buf2;
    }
    ClothCapBatches b;
    b.k = &k, b.indices = recs, b.radii = hr, b.frictions = hf, b.pos = hp, b.prev = hq, b.n = (uint32_t)n;
    b.contacts = NULL;
    cloth_capsule_batches(&b);
    *native = b.done;
    if (b.done) { WRITTEN(cp, hp, span); WRITTEN(cp, hq, span); }
    if (b.done == n) {   /* the list position after the batches: the one value the rest of the routine reads */
        c->x[8] = x8 + 32ull * n;
        return 1;
    }
    c->x[8] = x8 + 32ull * b.done, c->x[9] = 64ull * b.done;   /* (the lifted loop at batch `done`) */
    decline(b.why == CLOTH_CAP_INDEX ? "a batch's indices repeat" : "values: a batch outside the class");
    return b.done ? 2 : 0;
}

/* cloth.capsule: non-zero, every batch done (the jump to the batch loop's way out); 0, the lifted loop runs, from batch
   `done` or from the first. Check mode: the kernel into copies, the lifted loop for real, compared at the way out */
int hos_site_cloth_capsule(Ctx *A64_NOESC c) {
    if (c->x[9] != 0) return 0;   /* (x9, the batch offset, is 0 entering the loop) */
    const int mode = cloth_mode();
    if (!mode) return 0;
    const int dx = cloth_dx();
    cloth_count(K_CAPREC, S_CALLS, 1);
    uint64_t n = 0, native = 0;
    if (mode == CLOTH_CHECK) {
        cloth_pending.k = -1;
        cloth_pending.want = *c;
        ClothCopy cp = {0};
        const int h = caprec_answer(&cloth_pending.want, &cp, &n, &native);
        if (__builtin_expect(dx, 0) && h >= 0 && h != 4) cloth_dx_call(K_CAPREC, n, native, h != 1);
        if (h == 1) {   /* (compared at the loop's way out, cloth.capsuleRecordsEnd, after the lifted loop) */
            cloth_count(K_CAPREC, S_ALL, 1), cloth_count(K_CAPREC, S_REC_NATIVE, (long)n);
            cloth_pending.k = K_CAPREC, cloth_pending.cp = cp, cloth_pending.records = n;
            return 0;
        }
        if (h == 0) cloth_count(K_CAPREC, S_REC_LIFTED, (long)n);
        if (h != 4) cloth_check_other(K_CAPREC, h);
        cloth_call_done();
        return 0;
    }
    const int h = caprec_answer(c, NULL, &n, &native);
    if (h == 4) return cloth_call_done(), 0;
    if (__builtin_expect(dx, 0)) cloth_dx_call(K_CAPREC, n, native, h != 1);
    cloth_count(K_CAPREC, h == 1 ? S_ALL : h == 2 ? S_PART : S_DECLINED, 1);
    cloth_count(K_CAPREC, S_REC_NATIVE, (long)native), cloth_count(K_CAPREC, S_REC_LIFTED, (long)(n - native));
    cloth_call_done();
    return h == 1;
}

/* cloth.capsuleRecordsEnd (check mode only), the batch loop's way out: the list position, FPSR and the slots */
static int cloth_capsule_records_end(Ctx *A64_NOESC c) { cloth_exit_kept(c, K_CAPREC, COLL_KEPT_X, 0); return 0; }

/* the particles left over natively (check mode: into copies): 1 every one done (nothing handed back), 2 the first `done`
   done (x8 / w9 for the next), 0 declined (no register written), -1 (check mode: the copy refused). cnt is never 0:
   the site is reached only when count & 15 is not 0 */
static int captail_answer(Ctx *c, ClothCopy *cp, uint64_t *count, uint64_t *native) {
    const uint32_t cnt = (uint32_t)c->x[9] & 15u;
    const uint64_t x8 = c->x[8], x15 = c->x[15], sp = c->x[31];
    *count = cnt, *native = 0;
    if (!cap_gate(c)) return cloth_count(K_CAPTAIL, S_GATE, 1), decline("gate: FPCR");
    ClothCapsule k;
    const uint8_t *in = (const uint8_t *)hos_guest_range(x15 + 0x18, 0x20);
    if (!cap_consts(c, &k) || !in) return decline("memory: the frame, the collidable or the instance");
    const uint64_t pdc = ld64(in) + 0x40, pos = ld64(in + 8), prev = ld64(in + 0x18);
    const uint8_t *pdp = (const uint8_t *)hos_guest_range(pdc, 8);
    const uint8_t *ix = (const uint8_t *)hos_guest_range(x8, 2ull * cnt);
    if (!pdp || !ix) return decline("memory: the particle data's pointer or the indices");
    const uint64_t pd = ld64(pdp);
    static const int at0[1] = {0};
    const uint64_t span = 16ull * (top_u16(ix, cnt, 2, at0, 1) + 1);
    uint8_t *hp = (uint8_t *)hos_guest_range(pos, span), *hq = (uint8_t *)hos_guest_range(prev, span);
    const uint8_t *hd = (const uint8_t *)hos_guest_range(pd, span);
    if (!hp || !hq || !hd) return decline("memory: the particles' slots or data");
    /* (the routine reads the instance's pointers, the data's pointer, the indices, the data, the frame and the collidable
       again for every particle: the slots written must meet none of them) */
    if (overlaps(pos, span, prev, span) || overlaps(pos, span, x15 + 0x18, 0x20) || overlaps(prev, span, x15 + 0x18, 0x20) ||
        overlaps(pos, span, pdc, 8) || overlaps(prev, span, pdc, 8) || overlaps(pos, span, x8, 2ull * cnt) ||
        overlaps(prev, span, x8, 2ull * cnt) || overlaps(pos, span, pd, span) || overlaps(prev, span, pd, span) ||
        overlaps(pos, span, sp + 0x70, 0x2a0) || overlaps(prev, span, sp + 0x70, 0x2a0) ||
        overlaps(pos, span, c->x[20] + 0x50, 0x40) || overlaps(prev, span, c->x[20] + 0x50, 0x40))
        return decline("overlap: the slots reach what the routine reads");
    if (cp) {
        if (2 * span > CLOTH_SPAN || !cloth_readable(hp, (size_t)span) || !cloth_readable(hq, (size_t)span)) return -1;
        if (!cloth_scratch_get()) return -1;
        memcpy(cloth_scratch, hp, (size_t)span), memcpy(cloth_scratch + span, hq, (size_t)span);
        cp->real = hp, cp->n = (size_t)span, cp->buf = cloth_scratch, cp->what = "the positions";
        cp->real2 = hq, cp->n2 = (size_t)span, cp->buf2 = cloth_scratch + span, cp->what2 = "the previous positions";
        hp = cp->buf, hq = cp->buf2;
    }
    ClothCapSingles s;
    s.k = &k, s.indices = ix, s.pos = hp, s.prev = hq, s.data = hd, s.n = cnt;
    cloth_capsule_singles(&s);
    *native = s.done;
    if (!s.done) return decline("values: the first particle left over outside the class");
    if (s.contacts) { WRITTEN(cp, hp, span); WRITTEN(cp, hq, span); }
    if (s.done == cnt) return 1;
    c->x[8] = x8 + 2ull * s.done, c->x[9] = cnt - s.done;   /* (the lifted loop at particle `done`: the particles left) */
    decline("values: a particle left over outside the class");
    return 2;
}

/* cloth.capsuleTail: non-zero, every particle left over done (the jump to the routine's way out); 0, the lifted loop
   runs, from particle `done` or from the first. A call with none left over does not reach this site, so it is not
   counted as a capsule tail call (K_CAPTAIL's calls are the calls with particles left over) */
int hos_site_cloth_capsule_tail(Ctx *A64_NOESC c) {
    const int mode = cloth_mode();
    if (!mode) return 0;
    const int dx = cloth_dx();
    cloth_count(K_CAPTAIL, S_CALLS, 1);
    uint64_t n = 0, native = 0;
    if (mode == CLOTH_CHECK) {
        cloth_pending.k = -1;
        cloth_pending.want = *c;
        ClothCopy cp = {0};
        const int h = captail_answer(&cloth_pending.want, &cp, &n, &native);
        if (__builtin_expect(dx, 0) && h >= 0) cloth_dx_call(K_CAPTAIL, n, native, h != 1);
        if (h == 1) {   /* (the lifted loop runs; the way out compares, cloth.capsuleEnd) */
            cloth_count(K_CAPTAIL, S_ALL, 1), cloth_count(K_CAPTAIL, S_REC_NATIVE, (long)n);
            cloth_pending.k = K_CAPTAIL, cloth_pending.cp = cp, cloth_pending.records = n;
            return 0;
        }
        if (h == 0) cloth_count(K_CAPTAIL, S_REC_LIFTED, (long)n);
        cloth_check_other(K_CAPTAIL, h);
        cloth_call_done();
        return 0;
    }
    const int h = captail_answer(c, NULL, &n, &native);
    if (__builtin_expect(dx, 0)) cloth_dx_call(K_CAPTAIL, n, native, h != 1);
    cloth_count(K_CAPTAIL, h == 1 ? S_ALL : h == 2 ? S_PART : S_DECLINED, 1);
    cloth_count(K_CAPTAIL, S_REC_NATIVE, (long)native), cloth_count(K_CAPTAIL, S_REC_LIFTED, (long)(n - native));
    cloth_call_done();
    return h == 1;
}
/* cloth.capsuleEnd (check mode only), the routine's way out: FPSR and the slots (nothing is handed back) */
static int cloth_capsule_end(Ctx *A64_NOESC c) { cloth_exit_kept(c, K_CAPTAIL, 0, 0); return 0; }

/* ---- f_81c1a240 mode 0: the sphere collidable's batch kernel ------------------------------------------------------------
   The capsule's routine for a sphere (its centre and radius put into the frame at the routine's start): the same loop of
   batches of 16 particles, then the count & 15 left over one at a time. game/cloth_sphere.h has what the batches
   compute. Two sites in the routine:
     cloth.sphere (a jump to the batch loop's way out), at the branch that enters the batch loop, once a call: every batch
       natively, the last one included. As for the capsule, the rest of the routine reads one value the batches leave:
       the list position, the list's start plus 32 bytes a batch done. Every batch done: that position is set and the
       answer is non-zero (the jump). The kernel stopped at batch `done`: the loop's position and offset at that batch,
       and 0 (the lifted loop goes on there). Declined: nothing written, 0.
     cloth.sphereRecordsEnd (check mode only), the batch loop's way out: check mode's compare (the list position, FPSR
       and the slots written).
   The 1 to 15 particles left over run lifted: there is no kernel for them.
   The kernel takes its inputs from the routine's frame (the sphere, the small constant by place, dt, the radii's and
   frictions' cells) and from the collidable (the point its spin turns about, its velocity and its spin). Its float-mode
   gate is inside it: it is given FPCR and FPSR and does nothing unless FPCR is the game's (its default but for FZ) and
   every cumulative flag is set. The hook asks CLOTH_GATE_OPEN first, as every piece does, and gives it CLOTH_FPSR_IN, so
   when the hook's gate is open the kernel's is too (the bits it asks of FPCR are within CLOTH_FPCR_MASK): it never stops
   for the float mode here. Whole calls are declined for the gate, memory, and slots or a frame that would reach what the
   routine reads (the batches' indices, radii and frictions, their cells, the frame, the collidable, and the image's mask
   constant and jump tables, which the lifted loop reads when it goes on after a call done in part). */
#define SPH_FRAME 0x1e0   /* (the frame the batches read: sp .. sp+0x1e0) */

/* the batches natively (check mode: into copies): 1 every batch done (x8 the list position after them), 2 the first
   `done` done (x8 / x9 at batch `done`), 0 declined (no register written), 4 nothing to do, -1 (check mode: the copy
   refused). count: the batches; *native: those done natively */
static int sphrec_answer(Ctx *c, ClothCopy *cp, uint64_t *count, uint64_t *native) {
    const uint64_t n = c->x[12] >> 6, x8 = c->x[8], sp = c->x[31];
    *count = n, *native = 0;
    if (!n) return 4;
    if (c->x[12] & 63) return decline("the batch loop's bound is not a whole batch");
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_SPHREC, S_GATE, 1), decline("gate: FPCR");
    const uint8_t *fh = (const uint8_t *)hos_guest_range(sp, SPH_FRAME);
    const uint8_t *col = (const uint8_t *)hos_guest_range(c->x[20] + 0x50, 0x40);
    if (!fh || !col) return decline("memory: the frame or the collidable");
    const uint64_t fcell = ld64(fh + 0x148), rcell = ld64(fh + 0x178);
    const uint8_t *fc = (const uint8_t *)hos_guest_range(fcell, 8), *rc = (const uint8_t *)hos_guest_range(rcell, 8);
    if (!fc || !rc) return decline("memory: the radii's or frictions' pointer");
    const uint64_t fric = ld64(fc), rad = ld64(rc), pos = c->x[11], prev = c->x[10];
    const uint8_t *recs = (const uint8_t *)hos_guest_range(x8, 32 * n);
    const uint8_t *hr = (const uint8_t *)hos_guest_range(rad, 64 * n), *hf = (const uint8_t *)hos_guest_range(fric, 64 * n);
    if (!recs || !hr || !hf) return decline(!recs ? "memory: the batches' indices" : "memory: the radii or frictions");
    static const int at[16] = {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30};
    const uint64_t span = 16ull * (top_u16(recs, n, 32, at, 16) + 1);
    uint8_t *hp = (uint8_t *)hos_guest_range(pos, span), *hq = (uint8_t *)hos_guest_range(prev, span);
    if (!hp || !hq) return decline("memory: the particles' slots");
    if (overlaps(pos, span, prev, span) || overlaps(pos, span, x8, 32 * n) || overlaps(prev, span, x8, 32 * n) ||
        overlaps(pos, span, rad, 64 * n) || overlaps(prev, span, rad, 64 * n) || overlaps(pos, span, fric, 64 * n) ||
        overlaps(prev, span, fric, 64 * n) || overlaps(pos, span, sp, SPH_FRAME) || overlaps(prev, span, sp, SPH_FRAME) ||
        overlaps(pos, span, c->x[20] + 0x50, 0x40) || overlaps(prev, span, c->x[20] + 0x50, 0x40) ||
        overlaps(pos, span, fcell, 8) || overlaps(prev, span, fcell, 8) || overlaps(pos, span, rcell, 8) ||
        overlaps(prev, span, rcell, 8) || overlaps(pos, span, CAP_MASK_CONST, 16) || overlaps(prev, span, CAP_MASK_CONST, 16) ||
        overlaps(pos, span, SPH_TABLES, 0x78) || overlaps(prev, span, SPH_TABLES, 0x78) ||
        overlaps(sp, SPH_FRAME, x8, 32 * n) || overlaps(sp, SPH_FRAME, rad, 64 * n) || overlaps(sp, SPH_FRAME, fric, 64 * n) ||
        overlaps(sp, SPH_FRAME, c->x[20] + 0x50, 0x40) || overlaps(sp, SPH_FRAME, fcell, 8) ||
        overlaps(sp, SPH_FRAME, rcell, 8))
        return decline("overlap: the slots or the frame reach what the routine reads");
    if (cp) {   /* (check mode: the kernel writes copies of both bases' slots) */
        if (2 * span > CLOTH_SPAN || !cloth_readable(hp, (size_t)span) || !cloth_readable(hq, (size_t)span)) return -1;
        if (!cloth_scratch_get()) return -1;
        memcpy(cloth_scratch, hp, (size_t)span), memcpy(cloth_scratch + span, hq, (size_t)span);
        cp->real = hp, cp->n = (size_t)span, cp->buf = cloth_scratch, cp->what = "the positions";
        cp->real2 = hq, cp->n2 = (size_t)span, cp->buf2 = cloth_scratch + span, cp->what2 = "the previous positions";
        hp = cp->buf, hq = cp->buf2;
    }
    ClothSphereArgs a;
    a.indices = recs, a.radii = hr, a.frictions = hf, a.positions = hp, a.previous = hq, a.records = (uint32_t)n;
    a.fpcr = (uint32_t)c->fpcr, a.fpsr = CLOTH_FPSR_IN;
    memcpy(a.sphere, fh + 0x1c0, 16), memcpy(a.eps, fh + 0x90, 16), a.dt = ld32(fh + 0x190);
    memcpy(a.body, col, 12), memcpy(a.velocity, col + 0x20, 16), memcpy(a.spin, col + 0x30, 12);
    ClothSphereResult r;
    cloth_sphere_records(&a, &r);
    *native = r.done;
    if (r.done) { WRITTEN(cp, hp, span); WRITTEN(cp, hq, span); }
    if (r.done == n) {   /* the list position after the batches: the one value the rest of the routine reads */
        c->x[8] = x8 + 32ull * n;
        return 1;
    }
    c->x[8] = x8 + 32ull * r.done, c->x[9] = 64ull * r.done;   /* (the lifted loop at batch `done`) */
    decline(r.why == CLOTH_SPHERE_REPEATED ? "a batch's indices repeat" : r.why == CLOTH_SPHERE_FLOAT_MODE ?
            "gate: the kernel's float mode" : "values: a batch outside the class");
    return r.done ? 2 : 0;
}

/* cloth.sphere: non-zero, every batch done (the jump to the batch loop's way out); 0, the lifted loop runs, from batch
   `done` or from the first. Check mode: the kernel into copies, the lifted loop for real, compared at the way out */
int hos_site_cloth_sphere(Ctx *A64_NOESC c) {
    const int mode = cloth_mode();
    if (!mode) return 0;
    const int dx = cloth_dx();
    cloth_count(K_SPHREC, S_CALLS, 1);
    uint64_t n = 0, native = 0;
    if (mode == CLOTH_CHECK) {
        cloth_pending.k = -1;
        cloth_pending.want = *c;
        ClothCopy cp = {0};
        const int h = sphrec_answer(&cloth_pending.want, &cp, &n, &native);
        if (__builtin_expect(dx, 0) && h >= 0 && h != 4) cloth_dx_call(K_SPHREC, n, native, h != 1);
        if (h == 1) {   /* (compared at the loop's way out, cloth.sphereRecordsEnd, after the lifted loop) */
            cloth_count(K_SPHREC, S_ALL, 1), cloth_count(K_SPHREC, S_REC_NATIVE, (long)n);
            cloth_pending.k = K_SPHREC, cloth_pending.cp = cp, cloth_pending.records = n;
            return 0;
        }
        if (h == 0) cloth_count(K_SPHREC, S_REC_LIFTED, (long)n);
        if (h != 4) cloth_check_other(K_SPHREC, h);
        cloth_call_done();
        return 0;
    }
    const int h = sphrec_answer(c, NULL, &n, &native);
    if (h == 4) return cloth_call_done(), 0;
    if (__builtin_expect(dx, 0)) cloth_dx_call(K_SPHREC, n, native, h != 1);
    cloth_count(K_SPHREC, h == 1 ? S_ALL : h == 2 ? S_PART : S_DECLINED, 1);
    cloth_count(K_SPHREC, S_REC_NATIVE, (long)native), cloth_count(K_SPHREC, S_REC_LIFTED, (long)(n - native));
    cloth_call_done();
    return h == 1;
}

/* cloth.sphereRecordsEnd (check mode only), the batch loop's way out: the list position, FPSR and the slots */
static int cloth_sphere_records_end(Ctx *A64_NOESC c) { cloth_exit_kept(c, K_SPHREC, COLL_KEPT_X, 0); return 0; }

/* ---- f_81db9a40's block loop: Object Space Deform (step 12) ------------------------------------------------------------ */
/* At cloth.skin, the loop's head, reached at each block (x1 the block's format byte, w10 the block, w9 the blocks, x27 its
   16 positions, x11..x14 the record streams of the 1-, 2-, 3- and 4-influence formats, x20 + 0x18 the output's base and
   stride, x21 + 0x100 the first output number, x25 the bones, v0..v2 the call's parameters; game/cloth_skin.h). The walk
   takes the blocks in order while each is one the host does. A format byte above 7 is an empty block, skipped; 0..3 are
   blocks of 4, 3, 2 or 1 bone influences with 8-bit weights, done by the kernel (its 16 slots stored); 4..7, the 16-bit
   weights, stop the walk, and so do memory and a block whose stores meet anything it reads.
   What is handed back. Every block done (non-zero answer: the site jumps to the loop's way out): nothing. The rest of the
   routine reads none of the loop's registers, and the values the last vertex leaves are read neither there nor by its
   callers (docs/dev/reference/native-cloth/README.md). A stop after some blocks (answer 0: the lifted loop goes on at the
   first block not done, and the site is asked again at its next block): what the loop carries from one block to the next,
   and nothing else: the format byte's address and the block number one past the blocks done, the positions past theirs,
   and the four 8-bit formats' record streams past their records. Every other value a block reads at its head is set
   before the loop and not changed in it, or is written by the block itself before it is read.
   Declined whole: the gate. In check mode a call is compared only when every block of it is the host's, into a copy of
   its stores (the lifted loop runs and is kept; the copy and FPSR are compared at the way out). */
static _Thread_local int skin_quiet;   /* (check mode: a call not compared; the lifted loop's next blocks are not asked) */

typedef struct SkinRun {
    uint64_t at;            /* the format byte of the next block */
    uint32_t block;         /* its number */
    uint64_t pos;           /* its positions */
    uint64_t stream[5];     /* the record stream of the n-influence format, n = 1..4 */
    long blocks, native;    /* blocks walked (done), of them the kernel's */
    ClothSkinParams p;
    ClothSkinLast last;     /* (the kernel's; not handed back) */
    /* check mode: the bounds of what the call stores and reads */
    uint64_t olo, ohi;      /* stores: [olo, ohi) */
    uint64_t rlo[8], rhi[8];/* reads: 0..3 the streams of 1..4 influences, 4 positions, 5 bones, 6 format bytes, 7 fields */
} SkinRun;

static inline uint32_t skin_ld16(const uint8_t *p) { uint16_t v; memcpy(&v, p, 2); return v; }
static inline void skin_read(SkinRun *r, int i, uint64_t a, uint64_t n) {
    if (a < r->rlo[i]) r->rlo[i] = a;
    if (a + n > r->rhi[i]) r->rhi[i] = a + n;
}

/* the blocks from the walk's state on. mode 0: on (the kernel, stores marked); 1: check's first walk (no kernel: what
   the call would do, its bounds); 2: check's second walk (the kernel into the copy at cb, which is the guest's cbase).
   Stops at the first block not done (*why its reason) or at the loop's end (returns 1) */
static int skin_walk(const Ctx *c, SkinRun *r, int mode, uint8_t *cb, uint64_t cbase, const char **why) {
    const uint32_t blocks = (uint32_t)c->x[9];
    const uint64_t desc = c->x[20] + 0x18, fields = c->x[21] + 0x100, bone0 = c->x[25];
    for (;;) {
        const uint8_t *fp = (const uint8_t *)hos_guest_range(r->at, 1);
        if (!fp) return *why = "memory: the block's format byte", 0;
        const uint32_t f = *fp;
        if (mode == 1) skin_read(r, 6, r->at, 1);   /* (every block's format byte: an empty block's too) */
        if (f >= 4 && f <= 7) return *why = "a block of 16-bit weights", 0;
        if (f < 4) {
            const int n = 4 - (int)f;   /* (the bone influences of a vertex) */
            const uint64_t sg = r->stream[n];
            const uint8_t *rec = (const uint8_t *)hos_guest_range(sg, CLOTH_SKIN_READABLE(n));
            const uint8_t *pos = (const uint8_t *)hos_guest_range(r->pos, 0x80);
            const uint8_t *dp = (const uint8_t *)hos_guest_range(desc, 0x10);
            const uint8_t *fl = (const uint8_t *)hos_guest_range(fields, 4);
            if (!rec || !pos || !dp || !fl)
                return *why = !rec ? "memory: the block's record" : !pos ? "memory: the positions" : "memory: the operator's fields", 0;
            ClothSkinBlock b;
            b.influences = n, b.record = rec, b.positions = pos;
            b.first = ld32(fl), b.stride = dp[0xc];
            const uint64_t obase = ld64(dp);
            uint32_t blo = 0xffff, bhi = 0, olo = 0xffffffffu, ohi = 0;
            for (int i = 0; i < 16 * n; i++) {   /* (the bones the record names) */
                const uint32_t k = skin_ld16(rec + 0x20 + 2 * i);
                blo = k < blo ? k : blo, bhi = k > bhi ? k : bhi;
            }
            for (int i = 0; i < 16; i++) {   /* (the slots it stores, at 32-bit offsets from the output's base) */
                const uint32_t o = (uint32_t)(b.first + skin_ld16(rec + 2 * i)) * b.stride;
                olo = o < olo ? o : olo, ohi = o > ohi ? o : ohi;
            }
            const uint64_t bg = bone0 + 64ull * blo, bn = 64ull * (bhi - blo + 1), og = obase + olo, on = (uint64_t)(ohi - olo) + 16;
            b.bones = (const uint8_t *)hos_guest_range(bg, bn);
            uint8_t *out = (uint8_t *)hos_guest_range(og, on);
            if (!b.bones || !out) return *why = !b.bones ? "memory: the bones" : "memory: the output", 0;
            /* (what the block reads, and what the hook reads for every block: no store of the block may reach it) */
            if (overlaps(og, on, sg, CLOTH_SKIN_READABLE(n)) || overlaps(og, on, r->pos, 0x80) || overlaps(og, on, bg, bn) ||
                overlaps(og, on, desc, 0x10) || overlaps(og, on, fields, 4) || overlaps(og, on, r->at, 1))
                return *why = "overlap: the block's stores reach what it reads", 0;
            b.bone_base = blo, b.out_base = olo;
            if (mode == 1) {
                if (og < r->olo) r->olo = og;
                if (og + on > r->ohi) r->ohi = og + on;
                skin_read(r, n - 1, sg, CLOTH_SKIN_READABLE(n)), skin_read(r, 4, r->pos, 0x80), skin_read(r, 5, bg, bn);
                skin_read(r, 7, desc, 0x10), skin_read(r, 7, fields, 4);
            } else {
                b.out = mode == 2 ? cb + (og - cbase) : out;
                (void)cloth_skin_81db9a40(&r->p, &b, &r->last);   /* (its flags: none are put into FPSR) */
                if (mode == 0) HOS_WRITTEN(out, (size_t)on);
            }
            r->stream[n] = sg + CLOTH_SKIN_RECORD(n), r->pos += 0x80;
            r->native++;
        }
        r->at++, r->block++, r->blocks++;
        if (r->block == blocks) return 1;
    }
}

static void skin_start(const Ctx *c, SkinRun *r) {
    r->at = c->x[1], r->block = (uint32_t)c->x[10], r->pos = c->x[27];
    for (int n = 1; n <= 4; n++) r->stream[n] = c->x[10 + n];
    r->blocks = r->native = 0;
    for (int q = 0; q < 4; q++) r->p.pos_low[q] = (uint16_t)(c->vreg[0][0] >> (16 * q));
    r->p.weight_scale[0] = (uint32_t)c->vreg[1][0], r->p.weight_scale[1] = (uint32_t)(c->vreg[1][0] >> 32);
    r->p.weight_scale[2] = (uint32_t)c->vreg[1][1], r->p.weight_scale[3] = (uint32_t)(c->vreg[1][1] >> 32);
    r->p.fourth_weight = (uint32_t)(c->vreg[2][1] >> 32);
    r->p.fz = (int)((c->fpcr >> 24) & 1);
    r->p.fpsr_in = CLOTH_FPSR_IN;
    r->olo = ~0ull, r->ohi = 0;
    for (int i = 0; i < 8; i++) r->rlo[i] = ~0ull, r->rhi[i] = 0;
}

static _Thread_local SkinRun skin_run;

/* cloth.skin (a jump to the loop's way out, at the loop's head, reached at each block): non-zero, every block done; 0, the
   lifted loop goes on from the first block not done */
int hos_site_cloth_skin(Ctx *A64_NOESC c) {
    const int mode = cloth_mode();
    if (!mode) return 0;
    SkinRun *r = &skin_run;
    const char *why = "";
    if (mode == CLOTH_CHECK) {
        if (cloth_pending.k == K_SKIN || skin_quiet) return 0;   /* (the lifted loop's next blocks of a call being compared) */
        cloth_count(K_SKIN, S_CALLS, 1);
        if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_SKIN, S_GATE, 1), cloth_count(K_SKIN, S_DECLINED, 1), cloth_call_done(), 0;
        skin_start(c, r);
        const int whole = skin_walk(c, r, 1, NULL, 0, &why);
        int ok = whole;
        if (!whole) cloth_count(K_SKIN, r->blocks ? S_PART : S_DECLINED, 1);
        uint8_t *buf = NULL;
        memset(&cloth_pending.cp, 0, sizeof cloth_pending.cp);
        if (ok && r->native) {   /* (the copy of what the call stores: none of it read by the call) */
            for (int i = 0; i < 8; i++)
                if (r->rlo[i] < r->rhi[i] && overlaps(r->olo, r->ohi - r->olo, r->rlo[i], r->rhi[i] - r->rlo[i])) ok = 0;
            uint8_t *real = ok && r->ohi - r->olo <= CLOTH_SPAN ? (uint8_t *)hos_guest_range(r->olo, r->ohi - r->olo) : NULL;
            if (!real || !cloth_readable(real, (size_t)(r->ohi - r->olo)) || !cloth_scratch_get()) ok = 0;
            else {
                buf = cloth_scratch;
                memcpy(buf, real, (size_t)(r->ohi - r->olo));
                cloth_pending.cp.real = real, cloth_pending.cp.n = (size_t)(r->ohi - r->olo), cloth_pending.cp.buf = buf;
                cloth_pending.cp.what = "the vertices";
            }
        }
        if (!ok) {   /* (declined at its first block: the lifted code alone; else a call not compared) */
            if (r->blocks) cloth_count(K_SKIN, S_UNCHECKED, 1);
            skin_quiet = 1;
            cloth_call_done();
            return 0;
        }
        const uint64_t lo = r->olo;
        skin_start(c, r);
        skin_walk(c, r, 2, buf, lo, &why);
        cloth_pending.want = *c;   /* (nothing handed back: the copy and FPSR are compared) */
        cloth_count(K_SKIN, S_ALL, 1), cloth_count(K_SKIN, S_REC_NATIVE, r->native);
        cloth_pending.k = K_SKIN, cloth_pending.records = (uint64_t)r->blocks;
        return 0;
    }
    cloth_count(K_SKIN, S_CALLS, 1);
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_SKIN, S_GATE, 1), cloth_count(K_SKIN, S_DECLINED, 1), cloth_call_done(), 0;
    skin_start(c, r);
    const int whole = skin_walk(c, r, 0, NULL, 0, &why);
    if (!whole && r->blocks) {   /* (the loop's state at the first block not done) */
        c->x[1] = r->at, c->x[10] = r->block, c->x[27] = r->pos;
        for (int n = 1; n <= 4; n++) c->x[10 + n] = r->stream[n];
    }
    cloth_count(K_SKIN, whole ? S_ALL : r->blocks ? S_PART : S_DECLINED, 1);
    cloth_count(K_SKIN, S_REC_NATIVE, r->native);
    if (!whole) cloth_count(K_SKIN, S_REC_LIFTED, 1);
    cloth_dn.has = 0;   /* (HOS_CLOTH_DECLINES does not take this pass) */
    cloth_call_done();
    return whole;
}
/* cloth.skinEnd (check mode only), the loop's way out: the compare, and the next call asked again */
static int cloth_skin_end(Ctx *A64_NOESC c) {
    skin_quiet = 0;
    cloth_exit_kept(c, K_SKIN, 0, 0);
    return 0;
}

/* ---- f_81ce6500's bone loop: Mesh Bone Deform (step 12) ------------------------------------------------------------- */
/* At cloth.bones (after the timer scope's begin; x19, x20, x21 the operator's objects; game/cloth_bones.h has the loop):
   the loop's set-up and every bone. [x19+0x38] zero is the routine's own way out (0x81ce698c, not the loop's): declined.
   No bones ([x21+0x58] below 1): answered, nothing to do. Else every bone's 64 bytes are stored, in order. Nothing is
   handed back: the rest of the routine reads none of the registers the loop leaves (it loads what it needs at the way
   out), and the values the last bone leaves are read neither there nor by its callers
   (docs/dev/reference/native-cloth/README.md). Declined: the gate, memory, stores that meet anything the loop reads (the
   pairs, the triangles they name, the positions those name, the records) or what the hook read once (the operator's
   fields, the constants' pointer and its two constants). */
static int bones_answer(Ctx *c, ClothCopy *cp, uint64_t *count) {
    *count = 0;
    const uint64_t x19 = c->x[19], x20 = c->x[20], x21 = c->x[21];
    const uint8_t *f38 = (const uint8_t *)hos_guest_range(x19 + 0x38, 4), *f58 = (const uint8_t *)hos_guest_range(x21 + 0x58, 4);
    if (!f38 || !f58) return decline("memory: [x19+0x38] / [x21+0x58]");
    if (!ld32(f38)) return decline("[x19+0x38] = 0: the routine's own way out");
    const uint32_t w8 = ld32(f58);
    if ((int32_t)w8 < 1) return 1;   /* (the bone count at +0x58 below 1: no bones) */
    const uint64_t n = w8;
    *count = n;
    if (!CLOTH_GATE_OPEN(c)) return cloth_count(K_BONES, S_GATE, 1), decline("gate: FPCR");
    const uint8_t *f19 = (const uint8_t *)hos_guest_range(x19 + 0x18, 0x20), *f21 = (const uint8_t *)hos_guest_range(x21 + 0x50, 0x18);
    const uint8_t *f20 = (const uint8_t *)hos_guest_range(x20 + 0x18, 8), *tp = (const uint8_t *)hos_guest_range(CLOTH_TABLE_PTR, 8);
    if (!f19 || !f21 || !f20 || !tp) return decline(!tp ? "memory: the constants" : "memory: the operator's fields");
    const uint64_t tab = ld64(tp);
    const uint8_t *q0 = (const uint8_t *)hos_guest_range(tab + 0x160, 16), *q2 = (const uint8_t *)hos_guest_range(tab + 0x60, 16);
    if (!q0 || !q2) return decline("memory: the constants");
    const uint64_t pbase = ld64(f19), tbase = ld64(f19 + 0x18), pairs_at = ld64(f21), rec_at = ld64(f21 + 0x10), obase = ld64(f20);
    const uint8_t *pairs = (const uint8_t *)hos_guest_range(pairs_at, 4 * n), *rec = (const uint8_t *)hos_guest_range(rec_at, 64 * n);
    if (!pairs || !rec) return decline(!pairs ? "memory: the pairs" : "memory: the records");
    uint32_t olo = 0xffff, ohi = 0, tlo = 0xffff, thi = 0, plo = 0xffff, phi = 0;
    for (uint64_t i = 0; i < n; i++) {
        const uint32_t o = skin_ld16(pairs + 4 * i), t = skin_ld16(pairs + 4 * i + 2);
        olo = o < olo ? o : olo, ohi = o > ohi ? o : ohi, tlo = t < tlo ? t : tlo, thi = t > thi ? t : thi;
    }
    const uint64_t tg = tbase + tlo, tn = (uint64_t)(thi - tlo) + 6;
    const uint8_t *tri = (const uint8_t *)hos_guest_range(tg, tn);
    if (!tri) return decline("memory: the triangles");
    for (uint64_t i = 0; i < n; i++) {
        const uint8_t *t = tri + (skin_ld16(pairs + 4 * i + 2) - tlo);
        for (int j = 0; j < 3; j++) {
            const uint32_t p = skin_ld16(t + 2 * j);
            plo = p < plo ? p : plo, phi = p > phi ? p : phi;
        }
    }
    const uint64_t pg = pbase + 16ull * plo, pn = 16ull * (phi - plo + 1), og = obase + olo, on = (uint64_t)(ohi - olo) + 64;
    const uint8_t *pos = (const uint8_t *)hos_guest_range(pg, pn);
    uint8_t *out = (uint8_t *)hos_guest_range(og, on);
    if (!pos || !out) return decline(!pos ? "memory: the positions" : "memory: the output");
    /* (every load sees what the Arm's would, and the stores land as the Arm's in their order: no store meets a read) */
    if (overlaps(og, on, pairs_at, 4 * n) || overlaps(og, on, tg, tn) || overlaps(og, on, pg, pn) || overlaps(og, on, rec_at, 64 * n) ||
        overlaps(og, on, x19 + 0x18, 0x24) || overlaps(og, on, x21 + 0x50, 0x18) || overlaps(og, on, x20 + 0x18, 8) ||
        overlaps(og, on, CLOTH_TABLE_PTR, 8) || overlaps(og, on, tab + 0x160, 16) || overlaps(og, on, tab + 0x60, 16))
        return decline("overlap: the bones' stores reach what the loop reads");
    if (cp) {   /* (check mode: the kernel writes a copy of the output) */
        if (!cloth_scratch_get()) return -1;
        memcpy(cloth_scratch, out, (size_t)on);
        cp->real = out, cp->n = (size_t)on, cp->buf = cloth_scratch, cp->what = "the bones' matrices";
        out = cp->buf;
    }
    ClothBonesLast last;   /* (the kernel's; not handed back) */
    ClothBonesArgs k;
    k.positions = pos, k.pos_first = plo, k.triangles = tri, k.tri_first = tlo, k.pairs = pairs, k.records = rec;
    k.out = out, k.out_first = olo, k.count = (uint32_t)n;
    memcpy(k.centre_weight, q0, 16);
    k.t = ld32(q2);
    k.fz = (int)((c->fpcr >> 24) & 1);
    k.fpsr_in = CLOTH_FPSR_IN;
    k.last = &last;
    (void)cloth_bones_81ce6500(&k);   /* (its flags: none are put into FPSR) */
    WRITTEN(cp, out, (size_t)on);
    return 1;
}

/* cloth.bones (jump to the loop's way out: non-zero, the loop done), cloth.bonesEnd (check mode only: the compare) */
int hos_site_cloth_bones(Ctx *A64_NOESC c) { return count_hook(c, K_BONES, bones_answer); }
static int cloth_bones_end(Ctx *A64_NOESC c) { cloth_exit_kept(c, K_BONES, 0, 0); return 0; }


/* ---- the start (game_init, before the game's threads run) --------------------------------------------------------------- */
/* HOS_NATIVE_CLOTH=check: the sites at the loops' ways out get their compares (cloth.*End: before sites, dormant otherwise,
   so a run that is not checking pays a flag's load there and nothing more). A site the host has no line for (game/sites.def:
   hos_site_set answers 0) is counted and named in the start line (cloth_mode: the log is not open yet here); its pass's
   calls are then left pending and dropped, never compared */
void hos_oot_cloth_init(void) {
    if (cloth_mode_read() != CLOTH_CHECK) return;
    static const struct { const char *name; HosSiteFn fn; } ends[] = {
        {"cloth.linksEnd", cloth_links_end}, {"cloth.bendLinksEnd", cloth_bend_links_end},
        {"cloth.tethersEnd", cloth_tethers_end}, {"cloth.boxesEnd", cloth_boxes_end}, {"cloth.cullEnd", cloth_cull_end},
        {"cloth.verletEnd", cloth_verlet_end}, {"cloth.capsuleRecordsEnd", cloth_capsule_records_end},
        {"cloth.capsuleEnd", cloth_capsule_end}, {"cloth.sphereRecordsEnd", cloth_sphere_records_end},
        {"cloth.skinEnd", cloth_skin_end}, {"cloth.bonesEnd", cloth_bones_end}};
    for (size_t i = 0; i < sizeof ends / sizeof ends[0]; i++)
        if (!hos_site_set(ends[i].name, ends[i].fn)) {
            if (!cloth_end_missing) cloth_end_missing = ends[i].name;
            cloth_end_missing_n++;
        }
}
