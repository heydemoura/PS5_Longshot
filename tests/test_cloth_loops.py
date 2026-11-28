"""HOS_NATIVE_CLOTH's two particle loops: updateAabbs' (f_81b38800, game/cloth_aabb.cpp) and the capsule collidable's cull
(f_81b5a3c0, game/cloth_cull.cpp), their kernels and their sites' callbacks in game/oot_cloth.c (cloth.boxes, cloth.cull and
check mode's cloth.boxesEnd, cloth.cullEnd).

- The kernels on random particles (the game's lane 3: the default NaN, normal and huge values; garbage lane 3: NaNs quiet
  and signalling, infinities, subnormals; positions out of the class; inverse masses of every kind; the boxes' start odd;
  FZ on and off; the guest FPSR with and without IOC/OFC/UFC): updateAabbs' the same result whether given the guest's
  FPSR or every flag set (what the hook passes); the cull nothing written past its list. Against a plain model of the
  game's routine written here (IEEE float operations, the Arm's minimum and maximum with their signed zeros, its
  compare as >=) on plain particles. And by hand: the minimum of +0 and -0, a NaN box kept, -0 and FZ-subnormal inverse
  masses skip (IDC), the compare of a NaN false with IOC, a signalling inverse mass raises IOC, FZ flushes a subnormal
  position before the box test, the cull's last verdicts and last-decision word. Built four ways (this CPU, x86-64-v2,
  x86-64 = no SSE4.1, -ffp-contract=on): one digest of every value the specs define.
- The callbacks (game/oot_cloth.c as it is, over a guest window of its own and the constants' page):
  - off (0 or off): both answer 0 and write nothing;
  - on (unset or 1): updateAabbs' loop answered with the kernel's six box corners (v0-v5) and the particle data's pointer
    (x9), no other register, nothing written, FPSR as it was (whatever it holds: it does not gate); the empty count (0,
    negative): the boxes at their start value and the pointer; the cull: the list written, the count (x28) and the frame
    word (x26), no other register, its page marked, FPSR as it was (the lift raises no flag: none for the NaN lane 3's
    compare either); a list over the positions, over 0xffff particles, another float mode (FPCR), memory past the
    window: declined, untouched;
  - check: the lifted loop runs (the callback answers 0 and writes only its copy), the way out's site (its callback set by
    hos_oot_cloth_init) compares what is handed back, the list and FPSR: an exact lift passes, a handed-back register, a
    list byte or FPSR off is a MISMATCH, NZCV or a register not handed back is not looked at; the "oot: cloth" lines name
    every pass, each line under the logger's 1023 characters, the total in the last.
The sites' tables and the hooks as the lift lowers them are tested in tests/test_oot_cloth.py.

python -I -B tests/test_cloth_loops.py [--cc cc] [--cxx c++]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compiler_command import parse_compiler  # noqa: E402
from cloth_glue import KERNELS, SITE_SET, copy_glue, supports_x86_64_v2  # noqa: E402

RUN_ALL_ARGS = ["--cc", "{cc}", "--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]

KFIX = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cloth_aabb.h"
#include "cloth_cull.h"
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s (trial %d)\n", __LINE__, #x, trial); exit(1); } } while (0)
static int trial;
static uint64_t rs = 0x2545f4914f6cdd1dull;
static uint32_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return (uint32_t)(rs >> 16); }
static float unit(void) { return (float)(rnd() & 0xffffff) / 16777216.0f; }
static uint32_t fb(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
static float bf(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
static uint64_t digest = 1469598103934665603ull;
static void dig(const void *p, size_t n) { for (size_t i = 0; i < n; i++) digest = (digest ^ ((const uint8_t *)p)[i]) * 1099511628211ull; }
enum { NMAX = 700 };
static uint8_t cur[16 * NMAX], prev[16 * NMAX], mask[4 * NMAX], imass[16 * NMAX], box[32], out1[2 * NMAX];
static const uint32_t ODD[] = {0x7fc00000u, 0xffc00000u, 0x7fc12345u, 0x7f800001u, 0xff812345u, 0x7f800000u, 0xff800000u,
                               0x00000001u, 0x807fffffu, 0x00800000u, 0x80000000u, 0x00000000u, 0x7f7fffffu, 0x5f800000u,
                               0x1f000000u, 0x0d000000u};
static uint32_t odd(void) { return ODD[rnd() % 16]; }
/* lane 3: 0 plain, 1 the game's (the default NaN, normal, huge), 2 garbage */
static uint32_t wval(int m) {
    if (m == 0) return fb(1.0f);
    if (m == 1) { const uint32_t r = rnd() % 10; return r < 6 ? 0x7fc00000u : r < 8 ? fb(unit() * 50.0f - 25.0f) : 0x5e000000u + (rnd() & 0x1ffffff); }
    return rnd() % 3 ? odd() : rnd();
}
static uint32_t fpsr_in(void) { static const uint32_t F[] = {0x10u, 0x9fu, 0x0800009fu, 0x1du, 0x18u}; return F[rnd() % 5]; }

static void particles(int n, int wm, int plain) {
    const float cx = unit() * 6000.0f - 3000.0f, sp = 0.01f + unit() * 3.0f;
    for (int i = 0; i < n; i++) {
        float c[4], p[4];
        for (int q = 0; q < 3; q++) c[q] = cx + sp * (unit() * 20.0f - 10.0f), p[q] = c[q] - (rnd() % 4 ? (unit() - 0.5f) * 0.1f : 0.0f);
        memcpy(cur + 16 * i, c, 12), memcpy(prev + 16 * i, p, 12);
        const uint32_t w1 = wval(wm), w2 = wval(wm);
        memcpy(cur + 16 * i + 12, &w1, 4), memcpy(prev + 16 * i + 12, &w2, 4);
        const uint32_t m = rnd() % 3 ? 0u : (rnd() | (rnd() & 1u) << 31);
        memcpy(mask + 4 * i, &m, 4);
        const uint32_t r = rnd() % 10, im = r < 4 ? 0u : r < 5 ? 0x80000000u : fb(0.5f + unit());
        memcpy(imass + 16 * i + 4, &im, 4);
        if (!plain && rnd() % 40 == 0) {   /* an odd value anywhere */
            const uint32_t e = odd();
            memcpy((rnd() % 3 == 0 ? imass + 16 * i + 4 : (rnd() & 1 ? cur : prev) + 16 * i + 4 * (rnd() % 4)), &e, 4);
        }
    }
}

/* the plain model: one IEEE operation a step, the minimum and maximum with the signed zeros' rule (no NaN here); bx as the
   registers the hook hands back: 0 the positions' maximum, 1 their minimum, 2 / 3 the marked box, 4 / 5 the moving box */
static float mmin(float a, float b) { return a < b ? a : b < a ? b : bf(fb(a) | fb(b)); }
static float mmax(float a, float b) { return a > b ? a : b > a ? b : bf(fb(a) & fb(b)); }
static void model_aabb(int n, const uint32_t init[4], float bx[6][4]) {
    for (int q = 0; q < 4; q++) bx[1][q] = bx[2][q] = bx[4][q] = bf(init[q]), bx[0][q] = bx[3][q] = bx[5][q] = -bf(init[q]);
    for (int i = 0; i < n; i++) {
        float c[4], p[4], im;
        uint32_t m;
        memcpy(c, cur + 16 * i, 16), memcpy(p, prev + 16 * i, 16), memcpy(&m, mask + 4 * i, 4), memcpy(&im, imass + 16 * i + 4, 4);
        if (m || im != 0.0f)
            for (int q = 0; q < 4; q++) {
                const float d = c[q] - p[q], h = d * 0.5f, t = d * 1.5f, qv = p[q] + h, rv = p[q] + t;
                bx[4][q] = mmin(mmin(bx[4][q], qv), rv), bx[5][q] = mmax(mmax(bx[5][q], qv), rv);
                if (m >> 31) bx[2][q] = mmin(mmin(bx[2][q], qv), rv), bx[3][q] = mmax(mmax(bx[3][q], qv), rv);
            }
        for (int q = 0; q < 4; q++) bx[1][q] = mmin(bx[1][q], c[q]), bx[0][q] = mmax(bx[0][q], c[q]);
    }
}
static int model_cull(int n, int form_a, int test, uint32_t bit, uint8_t *out) {
    float lo[4], hi[4];
    memcpy(lo, box, 16), memcpy(hi, box + 16, 16);
    int k = 0;
    for (int i = 0; i < n; i++) {
        float p[4], im;
        uint32_t m;
        memcpy(p, cur + 16 * i, 16), memcpy(&im, imass + 16 * i + 4, 4), memcpy(&m, mask + 4 * i, 4);
        if (test && !(p[0] >= lo[0] && hi[0] >= p[0] && p[1] >= lo[1] && hi[1] >= p[1] && p[2] >= lo[2] && hi[2] >= p[2])) continue;
        if (form_a ? im == 0.0f : !(m & bit)) continue;
        const uint16_t v = (uint16_t)i;
        memcpy(out + 2 * k++, &v, 2);
    }
    return k;
}

static void aabb_in(ClothAabbArgs *a, int n, const uint32_t init[4], int fz, uint32_t fin) {
    memset(a, 0, sizeof *a);
    a->pos = cur, a->prev = prev, a->mask = mask, a->inv_mass = imass + 4, a->n = (uint32_t)n, a->fz = fz, a->fpsr_in = fin;
    memcpy(a->start, init, 16);
}
/* the fields the spec defines (ahead and ahead_step only when a particle moved) */
static int aabb_same(const ClothAabbResult *x, const ClothAabbResult *y) {
    return !memcmp(x, y, offsetof(ClothAabbResult, last_mid) + sizeof x->last_mid) && x->moved == y->moved &&
           (!x->moved || (!memcmp(x->ahead, y->ahead, 16) && !memcmp(x->ahead_step, y->ahead_step, 16)));
}
static void aabb_dig(const ClothAabbResult *r, uint32_t flags) {
    dig(r, offsetof(ClothAabbResult, last_mid) + sizeof r->last_mid), dig(&r->moved, 4), dig(&flags, 4);
    if (r->moved) dig(r->ahead, 16), dig(r->ahead_step, 16);
}
static void cull_in(ClothCullArgs *k, uint8_t *out, int n, int form_a, int test, uint32_t bit, int fz) {
    memset(k, 0, sizeof *k);
    k->positions = cur, k->masses = form_a ? imass + 4 : NULL, k->masks = form_a ? NULL : mask, k->box = test ? box : NULL;
    k->list = out, k->n = (uint32_t)n, k->group = bit, k->mass_form = form_a, k->box_test = test, k->fz = fz;
}
/* the fields the spec defines, as their conditions say */
static void cull_dig(const ClothCullArgs *k, const ClothCullResult *r) {
    dig(k->list, 2 * (size_t)r->count), dig(&r->count, 4), dig(&r->status, 4), dig(&r->mass_examined, 4), dig(&r->word_set, 4);
    if (k->box_test) dig(r->inside_yz, 16), dig(r->inside_z, 16);
    if (k->box_test && !r->mass_examined) dig(r->inside_all, 16);
    if (r->mass_examined) dig(&r->last_mass, 4);
    if (r->word_set) dig(&r->word, 4);
}

int main(int argc, char **argv) {
    const int trials = argc > 1 ? atoi(argv[1]) : 2000;
    long plain_aabb = 0, plain_cull = 0, moved = 0;
    for (trial = 0; trial < trials; trial++) {
        const int n = 1 + (int)(rnd() % (rnd() % 4 ? 64 : NMAX)), wm = (int)(rnd() % 3), plain = rnd() % 4 == 0, fz = (int)(rnd() & 1);
        particles(n, plain ? 0 : wm, plain);
        uint32_t init[4] = {0x7f7f0000u, 0x7f7f0000u, 0x7f7f0000u, 0x7f7f0000u};   /* (a start value: the hook reads it) */
        if (!plain && rnd() % 10 == 0) init[rnd() % 4] = odd();
        const uint32_t fin = fpsr_in();
        /* the guest's FPSR, and every flag set (what the hook passes): the same result; the flags it reports with the
           guest's are those raised, but for IXC, which it may leave out */
        ClothAabbArgs a;
        ClothAabbResult r, g;
        memset(&r, 0xa5, sizeof r), memset(&g, 0x5a, sizeof g);
        aabb_in(&a, n, init, fz, fin);
        const uint32_t fl = cloth_aabb_81b38800(&a, &r);
        a.fpsr_in = 0x9fu;
        (void)cloth_aabb_81b38800(&a, &g);
        CHECK(aabb_same(&r, &g) && (r.moved == 0 || r.moved == 1));
        aabb_dig(&r, (fin | fl) & ~0x10u);
        moved += r.moved;
        if (plain) {   /* (plain particles: the plain model's boxes, no flag but IXC) */
            float bx[6][4];
            model_aabb(n, init, bx);
            CHECK(!memcmp(bx[0], r.pos_max, 16) && !memcmp(bx[1], r.pos_min, 16) && !memcmp(bx[2], r.marked_min, 16));
            CHECK(!memcmp(bx[3], r.marked_max, 16) && !memcmp(bx[4], r.moving_min, 16) && !memcmp(bx[5], r.moving_max, 16));
            CHECK(((fin | fl) & ~0x10u) == (fin & ~0x10u));
            plain_aabb++;
        }
        /* the cull over the same particles and a box among them */
        float lo[4], hi[4];
        for (int q = 0; q < 4; q++) {
            float c0, c1;
            memcpy(&c0, cur + 16 * (rnd() % n) + 4 * q, 4), memcpy(&c1, cur + 16 * (rnd() % n) + 4 * q, 4);
            lo[q] = c0 < c1 ? c0 : c1, hi[q] = c0 < c1 ? c1 : c0;
        }
        memcpy(box, lo, 16), memcpy(box + 16, hi, 16);
        if (!plain && rnd() % 8 == 0) { const uint32_t e = odd(); memcpy(box + 4 * (rnd() % 8), &e, 4); }
        const int form_a = (int)(rnd() & 1), test = rnd() % 5 != 0;
        const uint32_t bit = rnd() % 4 ? 1u << (rnd() % 32) : rnd();
        memset(out1, 0xa5, sizeof out1);
        ClothCullArgs k;
        ClothCullResult kr;
        memset(&kr, 0xa5, sizeof kr);
        cull_in(&k, out1, n, form_a, test, bit, fz);
        cloth_cull_81b5a3c0(&k, &kr);
        CHECK(kr.count <= (uint32_t)n && !(kr.status & ~0x81u));
        for (size_t i = 2 * (size_t)kr.count; i < sizeof out1; i++) CHECK(out1[i] == 0xa5);   /* (nothing past the list) */
        cull_dig(&k, &kr);
        if (plain) {
            uint8_t m[2 * NMAX];
            memset(m, 0xa5, sizeof m);
            CHECK(model_cull(n, form_a, test, bit, m) == (int)kr.count && !memcmp(m, out1, sizeof m));
            plain_cull++;
        }
    }
    /* by hand */
    {
        trial = -1;
        const uint32_t init[4] = {0x7f7f0000u, 0x7f7f0000u, 0x7f7f0000u, 0x7f7f0000u};   /* (a start value: the hook reads it) */
        const float c0[4] = {0.0f, -0.0f, 1.0f, 2.0f}, c1[4] = {-0.0f, 0.0f, 1.0f, 2.0f};
        memcpy(cur, c0, 16), memcpy(cur + 16, c1, 16), memcpy(prev, c0, 16), memcpy(prev + 16, c1, 16);
        uint32_t w = 0x7fc00000u, z = 0, sub = 0x00000005u;
        memcpy(cur + 12, &w, 4), memcpy(prev + 12, &w, 4);   /* particle 0's w: the default NaN */
        memcpy(mask, &z, 4), memcpy(mask + 4, &z, 4), memcpy(imass + 4, &sub, 4);   /* 0: subnormal mass, 1: +1 */
        const float one = 1.0f;
        memcpy(imass + 20, &one, 4);
        for (int fz = 0; fz < 2; fz++) {
            ClothAabbArgs a;
            ClothAabbResult r;
            aabb_in(&a, 2, init, fz, 0x10u);   /* (IDC not in the guest FPSR yet: reported) */
            const uint32_t fl = cloth_aabb_81b38800(&a, &r);
            CHECK(r.pos_min[0] == 0x80000000u && r.pos_min[1] == 0x80000000u);   /* the minimum of +0 and -0: -0, both ways */
            CHECK(r.pos_max[0] == 0u && r.pos_max[1] == 0u);                     /* their maximum: +0 */
            CHECK(r.pos_min[3] == 0x7fc00000u && r.pos_max[3] == 0x7fc00000u);   /* the NaN taken, then kept */
            /* particle 0: FZ reads its subnormal mass as 0 (skipped: the moving box has particle 1 alone), IDC */
            /* (its midpoint = prev + (cur - prev) * 0.5: -0 + +0 = +0) */
            CHECK(fz ? (r.moving_min[0] == 0u && r.moving_min[3] == fb(2.0f) && (fl & 0x80u)) :
                       (r.moving_min[0] == 0u && r.moving_min[3] == 0x7fc00000u && !(fl & 0x80u)));
            CHECK(r.last_mid[0] == 0u && r.last_mid[3] == fb(2.0f) && r.last_pos[0] == 0x80000000u && r.last_mask == 0u && r.moved == 1);
        }
        uint32_t snan = 0x7f800001u;   /* a signalling NaN mass: IOC, not skipped */
        memcpy(imass + 4, &snan, 4);
        ClothAabbArgs a;
        ClothAabbResult r;
        aabb_in(&a, 2, init, 1, 0x10u);
        const uint32_t fl = cloth_aabb_81b38800(&a, &r);
        CHECK((fl & 1u) && r.moving_min[3] == 0x7fc00000u);
        /* the cull: a NaN lane 3 raises IOC on every test; FZ flushes a subnormal x before the test (in, IDC) */
        const float lo[4] = {0.0f, -1.0f, -1.0f, 0.0f}, hi[4] = {1.0f, 1.0f, 1.0f, 0.0f};
        memcpy(box, lo, 16), memcpy(box + 16, hi, 16);
        const uint32_t p0[4] = {0x80000001u, 0u, 0u, 0x7fc00000u};
        memcpy(cur, p0, 16), memcpy(imass + 4, &one, 4);
        for (int fz = 0; fz < 2; fz++) {
            ClothCullArgs k;
            ClothCullResult kr;
            memset(out1, 0, sizeof out1);
            cull_in(&k, out1, 1, 1, 1, 0, fz);
            cloth_cull_81b5a3c0(&k, &kr);
            CHECK(kr.count == (uint32_t)fz && (kr.status & 1u) && !!(kr.status & 0x80u) == fz && kr.word_set);
            /* taken: its mass examined last, the word the count; outside: the box's verdicts, the word 0 */
            CHECK(fz ? (kr.mass_examined && kr.last_mass == fb(1.0f) && kr.word == 1u) :
                       (!kr.mass_examined && kr.inside_all[0] == 0u && kr.word == 0u && kr.inside_z[0] == 0xffffffffu));
        }
    }
#if defined(__SSE4_1__)
    printf("simd 1\n");
#else
    printf("simd 0\n");
#endif
    printf("plain %ld %ld moved %ld\n", plain_aabb, plain_cull, moved);
    printf("digest %016llx\n", (unsigned long long)digest);
    return 0;
}
'''

HOS_H = r'''
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>
typedef struct { uint64_t x[32]; uint8_t n, z, c, v; uint64_t vreg[32][2]; uint64_t fpcr, fpsr; } Ctx;
#define A64_NOESC
typedef void (*A64Fn)(Ctx *);
A64Fn a64_lookup(uint64_t addr);
#define WINDOW 0x400000ull   /* (4 MB of guest memory, and the constants' page at 0x84ca4000) */
#define TABLE_PAGE 0x84ca4000ull
extern uint8_t *window, table_page[0x1000];
extern uint8_t marks[WINDOW >> 12];
static inline void *hos_guest_range(uint64_t a, uint64_t n) {
    if (a >= TABLE_PAGE && a < TABLE_PAGE + 0x1000) return n <= TABLE_PAGE + 0x1000 - a ? table_page + (a - TABLE_PAGE) : (void *)0;
    return a < WINDOW && n <= WINDOW - a ? (void *)(window + a) : (void *)0;
}
void test_written(const void *p, size_t n);
#define HOS_WRITTEN(p, n) test_written((p), (n))
void hos_log(const char *fmt, ...);
int64_t hos_now_ns(void);
'''

HFIX = r'''
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include "hos.h"
#include "oot_cloth.c"
''' + SITE_SET + r'''
A64Fn a64_lookup(uint64_t a) { (void)a; return NULL; }   /* (no whole routine's callback is called here) */
uint8_t *window, table_page[0x1000];
uint8_t marks[WINDOW >> 12];
static int64_t now_ns = 1;
static int logged_first, logged_lines, long_line;
static char all_lines[8192], last_line[2048];
int64_t hos_now_ns(void) { return now_ns; }
void test_written(const void *p, size_t n) {
    const size_t a = (size_t)((const uint8_t *)p - window);
    if ((const uint8_t *)p < window || a + n > WINDOW) { fprintf(stderr, "FAIL: a mark outside the window\n"); exit(1); }
    for (size_t q = a >> 12; q <= (a + n - 1) >> 12; q++) marks[q] = 1;
}
void hos_log(const char *fmt, ...) {
    char line[4096];
    va_list ap;
    va_start(ap, fmt), vsnprintf(line, sizeof line, fmt, ap), va_end(ap);
    if (strstr(line, "MISMATCH (the first)")) logged_first++;
    if (strstr(line, "oot: cloth (HOS_NATIVE_CLOTH=")) {
        logged_lines++, strcpy(last_line, line), long_line |= strlen(line) > 1023;
        if (strlen(all_lines) + strlen(line) + 2 < sizeof all_lines) strcat(all_lines, line), strcat(all_lines, "\n");
    }
    printf("  log: %.300s\n", line);
}
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); exit(1); } } while (0)
static void put32(uint64_t a, uint32_t v) { memcpy(window + a, &v, 4); }
static void put64(uint64_t a, uint64_t v) { memcpy(window + a, &v, 8); }
static void putf(uint64_t a, float f) { memcpy(window + a, &f, 4); }
static uint32_t get32(uint64_t a) { uint32_t v; memcpy(&v, window + a, 4); return v; }
static uint16_t get16(uint64_t a) { uint16_t v; memcpy(&v, window + a, 2); return v; }
static uint32_t fbits_(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
#undef CONST   /* (windows.h, which game/oot_cloth.c includes on Windows, defines a macro of this name) */
enum { CONST = 0x3000, AINST = 0x7000, APD = 0x7100, ACUR = 0x100000, APREV = 0x140000, AMASK = 0x180000, AIM = 0x1c0000,
       CINST = 0x7400, CPD = 0x7500, CPOS = 0x200000, CIM = 0x240000, CMASK = 0x280000, CLIST = 0x2c0000, CSP = 0x300000,
       N = 60 };
static uint8_t *before;
static uint32_t rs = 7;
static uint32_t rnd(void) { return rs = rs * 1664525u + 1013904223u; }

static void setup(void) {
    memset(window, 0, WINDOW), memset(table_page, 0, sizeof table_page), memset(marks, 0, sizeof marks);
    const uint64_t cst = CONST;
    memcpy(table_page + 0x680, &cst, 8);
    for (int q = 0; q < 4; q++) put32(CONST + 0x250 + 4 * q, 0x7f7f0000u);   /* (the start value: any) */
    /* updateAabbs: the instance and its particle data; positions on a line, lane 3 the default NaN for half of them */
    put64(AINST + 0x18, APD), put64(AINST + 0x20, ACUR), put32(AINST + 0x28, N), put64(AINST + 0x30, APREV);
    put64(APD + 0x40, AIM), put64(APD + 0xe8, AMASK);
    put64(CINST + 0x20, CPOS), put64(CPD + 0x40, CIM), put64(CPD + 0xe8, CMASK);
    for (int i = 0; i < N; i++) {
        const float x = (float)i * 0.25f, y = 1.0f - (float)(i % 7) * 0.3f;
        putf(ACUR + 16 * i, x), putf(ACUR + 16 * i + 4, y), putf(ACUR + 16 * i + 8, -x), put32(ACUR + 16 * i + 12, i & 1 ? 0x7fc00000u : fbits_(1.0f));
        putf(APREV + 16 * i, x - 0.01f), putf(APREV + 16 * i + 4, y), putf(APREV + 16 * i + 8, 0.5f - x), put32(APREV + 16 * i + 12, i & 1 ? 0x7fc00000u : 0u);
        put32(AMASK + 4 * i, i % 3 == 0 ? 0u : i % 3 == 1 ? 0x80000002u : 4u), putf(AIM + 16 * i + 4, i % 2 ? 0.0f : 2.0f);
        putf(CPOS + 16 * i, x), putf(CPOS + 16 * i + 4, y * 0.5f), putf(CPOS + 16 * i + 8, 0.0f), put32(CPOS + 16 * i + 12, 0x7fc00000u);
        putf(CIM + 16 * i + 4, i % 3 ? 1.0f : 0.0f), put32(CMASK + 4 * i, i % 4 == 1 ? 0x10u : 0x3u);
    }
    putf(CSP + 0x90, 2.5f), putf(CSP + 0x94, -1.0f), putf(CSP + 0x98, -1.0f), putf(CSP + 0x9c, 0.0f);
    putf(CSP + 0xa0, 10.5f), putf(CSP + 0xa4, 1.0f), putf(CSP + 0xa8, 1.0f), putf(CSP + 0xac, 0.0f);
    put64(CSP + 0x2f8, 0x1122334455667788ull);   /* [x29-8] */
    memset(window + CLIST, 0x5a, 4 * N);
}
static Ctx ctx(void) {
    Ctx c;
    memset(&c, 0, sizeof c);
    for (int r = 0; r < 31; r++) c.x[r] = (uint64_t)rnd() << 32 | rnd();
    for (int r = 0; r < 32; r++) c.vreg[r][0] = (uint64_t)rnd() << 32 | rnd(), c.vreg[r][1] = (uint64_t)rnd() << 32 | rnd();
    c.fpcr = 1u << 24, c.fpsr = 0x9f, c.n = 1, c.z = 0, c.c = 1, c.v = 1;
    c.x[20] = AINST;
    return c;
}
static Ctx cctx(int form_a, int test) {
    Ctx c = ctx();
    c.x[0] = CLIST, c.x[19] = CINST, c.x[20] = CPD, c.x[22] = N, c.x[24] = (c.x[24] & ~1ull) | (uint64_t)form_a;
    c.x[21] = test ? 0xabcd00000000ull : 7, c.x[23] = 0x10, c.x[31] = CSP, c.x[29] = CSP + 0x300;
    return c;
}
/* every register but those named equal */
static int same_but(const Ctx *a, const Ctx *b, uint64_t xs, uint64_t vs) {
    for (int r = 0; r < 32; r++) if (!(xs >> r & 1) && a->x[r] != b->x[r]) return 0;
    for (int r = 0; r < 32; r++) if (!(vs >> r & 1) && (a->vreg[r][0] != b->vreg[r][0] || a->vreg[r][1] != b->vreg[r][1])) return 0;
    return a->n == b->n && a->z == b->z && a->c == b->c && a->v == b->v && a->fpcr == b->fpcr && a->fpsr == b->fpsr;
}
static int vq(const Ctx *c, int r, const uint32_t *v) { return c->vreg[r][0] == lo64(v[0], v[1]) && c->vreg[r][1] == lo64(v[2], v[3]); }

static void run_off(void) {
    setup(), memcpy(before, window, WINDOW);
    Ctx c = ctx(), k = c;
    CHECK(hos_site_cloth_boxes(&c) == 0 && !memcmp(&c, &k, sizeof c));
    c = cctx(1, 1), k = c;
    CHECK(hos_site_cloth_cull(&c) == 0 && !memcmp(&c, &k, sizeof c) && !memcmp(before, window, WINDOW));
    at_end("cloth.boxesEnd", &c), at_end("cloth.cullEnd", &c);
    CHECK(!memcmp(&c, &k, sizeof c));
    puts("PASS: off: both loops' callbacks answer 0 and write nothing");
}

static void run_on(void) {
    /* updateAabbs: the kernel's six box corners and the particle data's pointer, no other register, nothing written */
    setup(), memcpy(before, window, WINDOW);
    Ctx c = ctx(), k = c;
    ClothAabbArgs a;
    ClothAabbResult r;
    memset(&a, 0, sizeof a);
    a.pos = window + ACUR, a.prev = window + APREV, a.mask = window + AMASK, a.inv_mass = window + AIM + 4, a.n = N, a.fz = 1, a.fpsr_in = 0x9f;
    for (int q = 0; q < 4; q++) a.start[q] = 0x7f7f0000u;
    (void)cloth_aabb_81b38800(&a, &r);
    CHECK(hos_site_cloth_boxes(&c) == 1 && !memcmp(before, window, WINDOW));
    CHECK(vq(&c, 0, r.pos_max) && vq(&c, 1, r.pos_min) && vq(&c, 2, r.marked_min) && vq(&c, 3, r.marked_max));
    CHECK(vq(&c, 4, r.moving_min) && vq(&c, 5, r.moving_max) && c.x[9] == APD);
    CHECK(same_but(&c, &k, 1ull << 9, 0x3full));   /* (NZCV, FPCR and FPSR as they were too) */
    /* the boxes by hand for this sheet: x of the positions' box from 0 to 14.75, z from -14.75 to 0 */
    CHECK(c.vreg[1][0] == lo64(0u, fbits_(1.0f - 6 * 0.3f)) && c.vreg[0][0] == lo64(fbits_(14.75f), fbits_(1.0f)));
    CHECK((uint32_t)c.vreg[1][1] == fbits_(-14.75f) && (uint32_t)(c.vreg[1][1] >> 32) == 0x7fc00000u);
    at_end("cloth.boxesEnd", &c);
    puts("PASS: on: updateAabbs' loop answered with the kernel's six box corners (v0-v5) and the particle data's pointer (x9), "
         "no other register, nothing written");
    /* the empty counts: the boxes at their start value, the pointer */
    put32(AINST + 0x28, 0), c = ctx(), k = c;
    CHECK(hos_site_cloth_boxes(&c) == 1 && c.x[9] == APD && same_but(&c, &k, 1ull << 9, 0x3full));
    CHECK(c.vreg[2][0] == lo64(0x7f7f0000u, 0x7f7f0000u) && c.vreg[5][1] == lo64(0xff7f0000u, 0xff7f0000u));
    put32(AINST + 0x28, 0xfffffffdu), c = ctx(), k = c;
    CHECK(hos_site_cloth_boxes(&c) == 1 && c.x[9] == APD && same_but(&c, &k, 1ull << 9, 0x3full));
    CHECK(c.vreg[1][1] == lo64(0x7f7f0000u, 0x7f7f0000u) && c.vreg[3][0] == lo64(0xff7f0000u, 0xff7f0000u));
    puts("PASS: on: updateAabbs with no particles (0, negative): the boxes at their start value and the pointer, nothing else");
    struct { const char *why; uint64_t fpcr, fpsr, cur; } no[] = {
        {"default NaN", 1u << 25, 0x9f, ACUR}, {"round toward zero", 3u << 22, 0x9f, ACUR},
        {"positions past the window", 1u << 24, 0x9f, WINDOW - 0x100}};
    for (unsigned i = 0; i < sizeof no / sizeof no[0]; i++) {
        setup(), put64(AINST + 0x20, no[i].cur), memcpy(before, window, WINDOW);
        c = ctx(), c.fpcr = no[i].fpcr, c.fpsr = no[i].fpsr, k = c;
        if (hos_site_cloth_boxes(&c) != 0 || memcmp(&c, &k, sizeof c) || memcmp(before, window, WINDOW)) {
            fprintf(stderr, "FAIL: updateAabbs answered for %s\n", no[i].why);
            exit(1);
        }
    }
    setup(), c = ctx(), c.fpsr = 0x8f;
    CHECK(hos_site_cloth_boxes(&c) == 1 && c.fpsr == 0x8f);   /* (FPSR does not gate, and is left as it was) */
    puts("PASS: on: updateAabbs: another float mode and memory past the window are left to the lifted loop; FPSR.IXC clear is "
         "answered, FPSR as it was");

    /* the cull, both forms, with and without the box test */
    for (int form_a = 0; form_a < 2; form_a++)
        for (int test = 0; test < 2; test++) {
            setup(), memcpy(before, window, WINDOW);
            c = cctx(form_a, test), k = c;
            CHECK(hos_site_cloth_cull(&c) == 1);
            uint16_t want[N];
            int n = 0;
            for (int i = 0; i < N; i++) {
                const float x = (float)i * 0.25f, y = (1.0f - (float)(i % 7) * 0.3f) * 0.5f;
                if (test && !(x >= 2.5f && x <= 10.5f && y >= -1.0f && y <= 1.0f)) continue;
                if (form_a ? i % 3 == 0 : !((i % 4 == 1 ? 0x10u : 0x3u) & 0x10u)) continue;
                want[n++] = (uint16_t)i;
            }
            CHECK(n > 3 && c.x[28] == (uint64_t)n && !memcmp(window + CLIST, want, 2 * (size_t)n) && get16(CLIST + 2 * n) == 0x5a5a);
            memcpy(window + CLIST, before + CLIST, 2 * (size_t)n);
            CHECK(!memcmp(before, window, WINDOW) && marks[CLIST >> 12]);
            CHECK(c.x[26] == 0x1122334455667788ull && same_but(&c, &k, 1ull << 26 | 1ull << 28, 0));   /* (NZCV, FPSR as they were) */
            c = cctx(form_a, test), c.fpsr = 0x10;
            CHECK(hos_site_cloth_cull(&c) == 1 && c.fpsr == 0x10u);   /* (a NaN lane 3's compare: no flag put in) */
        }
    puts("PASS: on: the cull, both forms with and without the box test: the list, the count (x28) and the frame word (x26), no "
         "other register, FPSR as it was, its page marked");
    setup(), c = cctx(1, 1), c.x[22] = 0xffffffff00000000ull, k = c;
    CHECK(hos_site_cloth_cull(&c) == 1 && c.x[28] == 0 && c.x[26] == 0x1122334455667788ull);
    CHECK(same_but(&c, &k, 1ull << 26 | 1ull << 28, 0) && get32(CLIST) == 0x5a5a5a5au);
    puts("PASS: on: the cull with no particles: x26 the frame word, x28 = 0, nothing else");
    struct { const char *why; uint64_t fpcr, x0, x22; } cno[] = {
        {"default NaN", (1u << 25) | (1u << 24), CLIST, N}, {"round toward zero", 3u << 22, CLIST, N},
        {"the list over the positions", 1u << 24, CPOS + 16 * 7, N}, {"the list over the masses", 1u << 24, CIM + 16 * 3, N},
        {"the list over the box", 1u << 24, CSP + 0x80, N}, {"over 0xffff particles", 1u << 24, CLIST, 0x10000},
        {"the list past the window", 1u << 24, WINDOW - 0x10, N}};
    for (unsigned i = 0; i < sizeof cno / sizeof cno[0]; i++) {
        setup(), memcpy(before, window, WINDOW);
        c = cctx(1, 1), c.fpcr = cno[i].fpcr, c.x[0] = cno[i].x0, c.x[22] = cno[i].x22, k = c;
        if (hos_site_cloth_cull(&c) != 0 || memcmp(&c, &k, sizeof c) || memcmp(before, window, WINDOW)) {
            fprintf(stderr, "FAIL: the cull answered for %s\n", cno[i].why);
            exit(1);
        }
    }
    setup(), c = cctx(1, 1), c.fpsr = 0x9f & ~0x10u;
    CHECK(hos_site_cloth_cull(&c) == 1);   /* (compares do not round: FPSR.IXC is not asked for) */
    puts("PASS: on: the cull: another float mode, a list over what the loop reads, over 0xffff particles, memory past the window: "
         "left to the lifted loop, untouched; FPSR.IXC clear is answered");
}

static void run_check(void) {
    /* updateAabbs: the lifted loop runs (stood in for here by the answer itself: an exact lift), its exit compares what is
       handed back and FPSR: 1 a box corner off and 4 the pointer off are MISMATCHES; 2 NZCV off, 3 a register not handed
       back off (the last particle's position) are not looked at */
    for (int bad = 0; bad <= 4; bad++) {
        setup();
        Ctx c = ctx(), k = c;
        CHECK(hos_site_cloth_boxes(&c) == 0 && !memcmp(&c, &k, sizeof c));
        uint64_t n;
        CHECK(aabb_answer(&c, NULL, &n) == 1 && n == N);
        if (bad == 1) c.vreg[4][1] ^= 1;
        if (bad == 2) c.c ^= 1;
        if (bad == 3) c.vreg[16][0] ^= 1;
        if (bad == 4) c.x[9] += 16;
        at_end("cloth.boxesEnd", &c);
        CHECK(cloth_pending.k == -1);
    }
    /* the cull: the hook writes only its copy; the lifted loop's list and what is handed back compared at the exit: 1 a list
       byte off, 2 the count off, 4 FPSR off are MISMATCHES; 3 a register not handed back off (x11) is not looked at */
    for (int bad = 0; bad <= 4; bad++) {
        setup(), memcpy(before, window, WINDOW);
        Ctx c = cctx(1, 1), k = c;
        CHECK(hos_site_cloth_cull(&c) == 0 && !memcmp(&c, &k, sizeof c) && !memcmp(before, window, WINDOW));
        uint64_t n;
        CHECK(cull_answer(&c, NULL, &n) == 1 && n == N && c.x[28] > 3);
        if (bad == 1) window[CLIST + 3] ^= 1;
        if (bad == 2) c.x[28]++;
        if (bad == 3) c.x[11] ^= 1;
        if (bad == 4) c.fpsr ^= 0x10;
        at_end("cloth.cullEnd", &c);
    }
    cloth_flush();
    CHECK(cloth_total[K_AABB][S_CHECKED] == 5 && cloth_total[K_AABB][S_MISMATCH] == 2);
    CHECK(cloth_total[K_CULL][S_CHECKED] == 5 && cloth_total[K_CULL][S_MISMATCH] == 3 && logged_first == 1);
    puts("PASS: check: both loops run lifted and their ways out's sites compare what is handed back, the list and FPSR: an exact "
         "lift passes, a handed-back register, a list byte or FPSR off is a MISMATCH, NZCV or a register not handed back is not "
         "looked at");
    now_ns += CLOTH_LOG_NS * 2;
    cloth_flush();
    for (int k = 0; k < K_COUNT; k++) CHECK(strstr(all_lines, cloth_name[k]));
    CHECK(logged_lines >= 2 && !long_line && strstr(last_line, "MISMATCHES in all 5") && strstr(all_lines, "aabb f_81b38800: calls 5 (native 5"));
    puts("PASS: check: the oot: cloth lines name every piece with its counts, each under 1023 characters, the total last");
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "off";
    window = (uint8_t *)calloc(1, WINDOW), before = (uint8_t *)calloc(1, WINDOW);
    CHECK(window && before);
    hos_oot_cloth_init();   /* (check mode: the ways out's sites) */
    if (!strcmp(mode, "off")) run_off();
    else if (!strcmp(mode, "on")) run_on();
    else run_check();
    return 0;
}
'''


def kernels(cxx, tmp, trials):
    for k in ("cloth_aabb",):   # (the guard against fusing, by text; the cull only compares: nothing to fuse)
        t = (ROOT / "game" / (k + ".cpp")).read_text(encoding="utf-8") + (ROOT / "game" / "cloth_common.hpp").read_text(encoding="utf-8")
        assert "#pragma clang fp contract(off)" in t and "fp-contract=off" in t, k + ": the fp contract pragmas are gone"
    (tmp / "kfix.cpp").write_text(KFIX, encoding="utf-8")
    builds = [("this CPU", ["-march=native"]), ("x86-64-v2", ["-march=x86-64-v2"]), ("x86-64", ["-march=x86-64"]),
              ("contract on", ["-march=native", "-ffp-contract=on"])]
    digests = {}
    for name, flags in builds:
        if name == "x86-64-v2" and not supports_x86_64_v2(cxx, tmp):
            print("SKIP: x86-64-v2 (CPU support not established)")
            continue
        if subprocess.run(cxx + flags + ["-x", "c++", "-c", "-o", os.devnull, "-"], input="int x;\n", capture_output=True,
                          text=True).returncode:
            print("SKIP: %s (the compiler does not take %s)" % (name, " ".join(flags)))
            continue
        exe = tmp / ("k_%d%s" % (len(digests), ".exe" if os.name == "nt" else ""))
        srcs = [str(ROOT / "game" / "cloth_aabb.cpp"), str(ROOT / "game" / "cloth_cull.cpp"), str(tmp / "kfix.cpp")]
        subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "game"), "-I", str(tmp)] + flags + srcs + ["-o", str(exe)], check=True)
        r = subprocess.run([str(exe), str(trials)], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("FAIL (%s):\n%s%s" % (name, r.stdout, r.stderr))
        out = dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)
        digests[name] = out["digest"]
        pa, pc, _, moved = out["plain"].split()
        print("PASS: %s (%s): updateAabbs' kernel the same with the guest's FPSR and with every flag set on %d trials (%s with a "
              "particle tracked), the cull's nothing past its list; the plain model the same on %s and %s; the cases by hand"
              % (name, "SSE4.1" if out["simd"] == "1" else "no SSE4.1", trials, moved, pa, pc))
    if len(set(digests.values())) != 1:
        raise SystemExit("FAIL: the builds disagree: %s" % digests)
    print("PASS: every build gives the same bytes (%s)" % ", ".join(digests))


def hooks(cc, cxx, tmp):
    (tmp / "hos.h").write_text(HOS_H, encoding="utf-8")
    (tmp / "hfix.c").write_text(HFIX, encoding="utf-8")
    copy_glue(tmp)
    native = ["-march=native"] if subprocess.run(cc + ["-march=native", "-x", "c", "-c", "-o", os.devnull, "-"], input="int x;\n",
                                                 capture_output=True, text=True).returncode == 0 else []
    objs = []
    for k in KERNELS:
        o = tmp / (k + ".o")
        subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-ffp-contract=off", "-Wall", "-Wextra",
                        "-Werror"] + native + ["-c", str(tmp / (k + ".cpp")), "-o", str(o)], check=True)
        objs.append(str(o))
    fo = tmp / "hfix.o"
    subprocess.run(cc + ["-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                    "-Wno-unused-parameter", "-ffp-contract=off",
                    "-include", str(ROOT / "gpu" / "vk" / "compat" / "longshot_env.h"), "-I", str(tmp)] + native +
                   ["-c", str(tmp / "hfix.c"), "-o", str(fo)], check=True)
    exe = tmp / ("hfix.exe" if os.name == "nt" else "hfix")
    subprocess.run(cxx + [str(fo)] + objs + ([] if os.name == "nt" else ["-pthread"]) + ["-o", str(exe)], check=True)
    env = {k: v for k, v in os.environ.items() if k != "HOS_NATIVE_CLOTH" and not k.startswith("HOS_CLOTH_")}
    for mode, value in (("on", None), ("off", "0"), ("off", "off"), ("on", "1"), ("check", "check")):
        run_env = dict(env) if value is None else dict(env, HOS_NATIVE_CLOTH=value)
        r = subprocess.run([str(exe), mode], env=run_env, capture_output=True, text=True)
        print("\n".join(line for line in r.stdout.splitlines() if line.startswith("PASS")))
        if r.returncode:
            raise SystemExit("FAIL (HOS_NATIVE_CLOTH=%s, %s):\n%s%s" % (value, mode, r.stdout, r.stderr))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cc", default=os.environ.get("CC", "gcc"))
    p.add_argument("--cxx", default=os.environ.get("CXX", "c++"))
    p.add_argument("--trials", type=int, default=2000)
    a = p.parse_args()
    cc = parse_compiler(a.cc)
    cxx = parse_compiler(a.cxx)
    if not cc or not shutil.which(cc[0]) or not cxx or not shutil.which(cxx[0]):
        print("SKIP: no C or C++ compiler")
        return
    with tempfile.TemporaryDirectory(prefix="cloth-loops-") as tmp:
        kernels(cxx, Path(tmp), a.trials)
        hooks(cc, cxx, Path(tmp))


if __name__ == "__main__":
    main()
