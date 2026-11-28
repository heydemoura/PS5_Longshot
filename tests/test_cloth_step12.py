"""HOS_NATIVE_CLOTH's step 12: Object Space Deform's block loop (f_81db9a40, game/cloth_skin.cpp) and Mesh Bone Deform's bone
loop (f_81ce6500, game/cloth_bones.cpp), and their sites' callbacks in game/oot_cloth.c (cloth.skin, cloth.bones and check
mode's cloth.skinEnd, cloth.bonesEnd). Nothing here is taken from the game: the values are made up, the test needs no
image.

- The kernels on random calls (the four formats of Object Space Deform; lane 3 plain, the game's (the default NaN, normal
  and huge values) or garbage; odd values anywhere: NaNs quiet and signalling with payloads, infinities, subnormals, the
  edges of the classes; FZ on and off; the status flags already set with and without IOC/OFC/UFC): no flag outside the
  status bits, no store outside the slots (Mesh Bone Deform: lane 3 of each bone's first three columns zero). Object
  Space Deform against a plain model written here (IEEE float operations in the routine's operand order) where every
  value is plain. Built four ways (this CPU, x86-64-v2, x86-64 = the plain path only, -ffp-contract=on): one digest of
  the bytes stored and the flags as the interface defines them, so the vector paths and the plain path agree.
- The callbacks (game/oot_cloth.c as it is, over a guest window at 64 GB and again just below 2^39: a call there declined
  for its address fails the test):
  - off: both answer 0 and write nothing;
  - on: Object Space Deform's blocks (4, 3, 2 and 1 bones, an empty block) answered with the kernel's stores and no
    register written, FPSR as it was, the pages marked; a block of 16-bit weights first: declined, untouched; one later:
    the blocks before it done and, of the registers, only the loop's state at it (the format byte's address, the block,
    the positions, the four 8-bit formats' record streams); a store over the positions, another float mode, bones past
    the window: declined. Mesh Bone Deform answered with the kernel's stores and no register written, FPSR as it was; no
    bones: answered, nothing written; [x19+0x38] zero, a store over a triangle: declined, untouched;
  - check: the lifted code (stood in for by the answer itself, or by a wrong one) runs and stays; the host's copy and
    FPSR are compared with it at the loop's way out (its site's callback, set by hos_oot_cloth_init), not the registers
    the lifted loop leaves: an exact copy is compared, a byte off or another FPSR is a MISMATCH.
The sites' tables and the hooks as the lift lowers them are tested in tests/test_oot_cloth.py.

python -I -B tests/test_cloth_step12.py [--cc cc] [--cxx c++]
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
#include "cloth_skin.h"
#include "cloth_bones.h"
#include "cloth_common.hpp"
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
static const uint32_t ODD[] = {0x7fc00000u, 0xffc00000u, 0x7fc12345u, 0x7f800001u, 0xff812345u, 0x7f800000u, 0xff800000u,
                               0x00000001u, 0x807fffffu, 0x00800000u, 0x80000000u, 0x00000000u, 0x7f7fffffu, 0x5f800000u,
                               0x1f000000u, 0x0d000000u};
static uint32_t odd(void) { return ODD[rnd() % 16]; }
static uint32_t wval(int m) {   /* lane 3: 0 plain, 1 the game's kind, 2 garbage */
    if (m == 0) return fb(rnd() & 1 ? 1.0f : 0.0f);
    if (m == 1) { const uint32_t r = rnd() % 10; return r < 6 ? 0x7fc00000u : r < 8 ? fb(unit() * 50.0f - 25.0f) : 0x5e000000u + (rnd() & 0x1ffffff); }
    return rnd() % 3 ? odd() : rnd();
}
static uint32_t fpsr_in(void) { static const uint32_t F[] = {0x10u, 0x9fu, 0x0800009fu, 0x1du, 0x18u}; return F[rnd() % 5]; }
static void put(uint8_t *p, uint32_t v) { memcpy(p, &v, 4); }
static void put16(uint8_t *p, uint32_t v) { const uint16_t h = (uint16_t)v; memcpy(p, &h, 2); }
static uint32_t get16(const uint8_t *p) { uint16_t v; memcpy(&v, p, 2); return v; }
static uint32_t get32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }

/* ---- Object Space Deform ---- */
enum { NBONE = 24, NOUT = 40 };
static uint8_t bones[64 * NBONE], pos[16 * 8], st[0x100], out[4096];
static long skin_plain, skin_blocks;
/* the routine's vertex in plain IEEE floats, operand order as written */
static void skin_model(int nb, const ClothSkinParams *k, int i, uint32_t r[4]) {
    float p[4], w[4] = {0, 0, 0, 0}, M[4][4];
    for (int q = 0; q < 4; q++) {
        const int32_t z = (int32_t)((uint32_t)k->pos_low[q] | get16(pos + 8 * i + 2 * q) << 16);
        p[q] = (float)z * bf((uint32_t)k->pos_low[3] | get16(pos + 8 * i + 6) << 16);
    }
    if (nb == 4) for (int q = 0; q < 4; q++) w[q] = (float)st[0xa0 + 4 * i + q] * bf(k->weight_scale[q]);
    if (nb == 3) {
        for (int q = 0; q < 3; q++) w[q] = (float)st[0x80 + 3 * i + q] * bf(k->weight_scale[q]);
        w[3] = bf(k->fourth_weight) * bf(k->weight_scale[3]);
    }
    if (nb == 2) for (int q = 0; q < 4; q++) w[q] = (float)st[0x60 + 2 * i + q] * bf(k->weight_scale[q]);
    for (int c = 0; c < 4; c++)
        for (int q = 0; q < 4; q++) {
            float acc = 0;
            for (int j = 0; j < nb; j++) {
                float col;
                memcpy(&col, bones + 64 * get16(st + 0x20 + 2 * (nb * i + j)) + 16 * c + 4 * q, 4);
                acc = nb == 1 ? col : j == 0 ? col * w[0] : acc + col * w[j];
            }
            M[c][q] = acc;
        }
    for (int q = 0; q < 4; q++) {
        const float s1 = nb >= 3 ? M[0][q] * p[0] + M[3][q] : M[3][q] + M[0][q] * p[0];
        const float s2 = M[1][q] * p[1] + s1;
        r[q] = fb(M[2][q] * p[2] + s2);
    }
}
static void skin_trial(int plain) {
    const int nb = 1 + (int)(rnd() % 4), wm = plain ? 0 : (int)(rnd() % 3);
    for (int b = 0; b < NBONE; b++) {
        for (int c = 0; c < 4; c++)
            for (int q = 0; q < 3; q++) put(bones + 64 * b + 16 * c + 4 * q, fb(c == 3 ? unit() * 4000.0f - 2000.0f : unit() * 2.0f - 1.0f));
        for (int c = 0; c < 4; c++) put(bones + 64 * b + 16 * c + 12, c == 3 && wm == 0 ? fb(1.0f) : wval(wm));
    }
    for (int i = 0; i < 16; i++) {
        for (int q = 0; q < 3; q++) put16(pos + 8 * i + 2 * q, rnd());
        put16(pos + 8 * i + 6, fb(unit() * 0.001f + 0.00001f) >> 16);
    }
    for (int i = 0; i < 0x100; i++) st[i] = (uint8_t)rnd();
    for (int i = 0; i < 16; i++) put16(st + 2 * i, rnd() % NOUT);
    for (int i = 0; i < 16 * nb; i++) put16(st + 0x20 + 2 * i, rnd() % NBONE);
    ClothSkinParams k;
    memset(&k, 0, sizeof k);
    for (int q = 0; q < 4; q++) k.pos_low[q] = 0, k.weight_scale[q] = 0x3b800000u;
    k.fourth_weight = 0x3f800000u, k.fz = (int)(rnd() & 1), k.fpsr_in = plain ? 0x10u : fpsr_in();
    if (!plain) {
        if (rnd() % 6 == 0) for (int q = 0; q < 4; q++) k.pos_low[q] = (uint16_t)rnd();
        if (rnd() % 8 == 0) k.weight_scale[rnd() % 4] = rnd() % 2 ? odd() : fb(unit() * 3.0f);
        if (rnd() % 8 == 0) k.fourth_weight = wval(2);
        for (int n = rnd() % 4; n > 0; n--) {
            if (rnd() % 2) put(bones + 4 * (rnd() % (16 * NBONE)), rnd() % 3 ? odd() : (rnd() & 0x80000000u) | (0x00700000u + (rnd() & 0x3fffffu)));
            else put16(pos + 8 * (rnd() % 16) + 6, odd() >> 16);
        }
    }
    ClothSkinBlock b;
    memset(&b, 0, sizeof b);
    b.influences = nb, b.positions = pos, b.record = st, b.bones = bones, b.bone_base = 0, b.out_base = 0;
    b.first = rnd() % 8, b.stride = rnd() % 5 ? 16 : rnd() % 40;
    ClothSkinLast last;
    memset(&last, 0, sizeof last), memset(out, 0x5a, sizeof out);
    b.out = out;
    const uint32_t f = cloth_skin_81db9a40(&k, &b, &last);
    CHECK(!(f & ~0x9fu));
    if (plain) {   /* (every value plain: the IEEE model's bytes, stored in order, and no flag but IXC) */
        uint8_t want[sizeof out];
        memset(want, 0x5a, sizeof want);
        for (int i = 0; i < 16; i++) {
            uint32_t r[4];
            skin_model(nb, &k, i, r);
            memcpy(want + (uint32_t)(b.first + get16(st + 2 * i)) * b.stride, r, 16);
        }
        CHECK(!memcmp(want, out, sizeof want) && (f | 0x10u) == 0x10u);
        skin_plain++;
    }
    const uint32_t seen = k.fpsr_in | f | 0x10u;   /* (the flags as the interface defines them: IXC and those set may be left out) */
    dig(out, sizeof out), dig(&seen, 4);
    skin_blocks++;
}

/* ---- Mesh Bone Deform ---- */
enum { BN = 20, BP = 40 };
static uint8_t bpos[16 * BP], btri[6 * BN + 8], bpairs[4 * BN], brec[64 * BN], bout[64 * BN + 64];
static void bones_trial(int plain) {
    const uint32_t n = 1 + rnd() % BN;
    const int wm = plain ? 0 : (int)(rnd() % 3);
    for (int i = 0; i < BP; i++) {
        for (int q = 0; q < 3; q++) put(bpos + 16 * i + 4 * q, fb(unit() * 20.0f - 10.0f));
        put(bpos + 16 * i + 12, wval(wm));
    }
    for (uint32_t i = 0; i < n; i++) {
        for (int j = 0; j < 3; j++) put16(btri + 6 * i + 2 * j, rnd() % BP);
        put16(bpairs + 4 * i, 64 * i), put16(bpairs + 4 * i + 2, 6 * (rnd() % n));
        for (int q = 0; q < 16; q++) put(brec + 64 * i + 4 * q, fb(unit() * 2.0f - 1.0f));
    }
    if (!plain && rnd() % 4 == 0) put16(btri + 6 * (rnd() % n) + 2, get16(btri + 6 * (rnd() % n)));   /* (two corners alike) */
    if (!plain)
        for (int k = rnd() % 4; k > 0; k--) {
            if (rnd() % 2) put(bpos + 4 * (rnd() % (4 * BP)), rnd() % 3 ? odd() : (rnd() & 0x80000000u) | (0x00700000u + (rnd() & 0x3fffffu)));
            else put(brec + 4 * (rnd() % (16 * n)), odd());
        }
    ClothBonesLast last;
    ClothBonesArgs a;
    memset(&a, 0, sizeof a);
    a.positions = bpos, a.pos_first = 0, a.triangles = btri, a.tri_first = 0, a.pairs = bpairs, a.records = brec;
    a.out_first = 0, a.count = n, a.last = &last;
    for (int q = 0; q < 4; q++) a.centre_weight[q] = fb(0.3f);
    a.t = fb(1.0f), a.fz = (int)(rnd() & 1), a.fpsr_in = plain ? 0x10u : fpsr_in();
    if (!plain && rnd() % 8 == 0) a.centre_weight[3] = wval(2);
    memset(bout, 0x5a, sizeof bout);
    a.out = bout;
    const uint32_t f = cloth_bones_81ce6500(&a);
    CHECK(!(f & ~0x9fu));
    for (uint32_t i = 0; i < n; i++)   /* (each bone's 64 bytes: lane 3 of its first three columns zero) */
        CHECK(get32(bout + 64 * i + 0xc) == 0 && get32(bout + 64 * i + 0x1c) == 0 && get32(bout + 64 * i + 0x2c) == 0);
    for (size_t i = 64 * n; i < sizeof bout; i++) CHECK(bout[i] == 0x5a);   /* (nothing past the last bone) */
    const uint32_t seen = a.fpsr_in | f | 0x10u;
    dig(bout, sizeof bout), dig(&seen, 4);
}

int main(int argc, char **argv) {
    const int trials = argc > 1 ? atoi(argv[1]) : 2000;
    for (trial = 0; trial < trials; trial++) skin_trial(trial % 4 == 0);
    for (trial = 0; trial < trials; trial++) bones_trial(trial % 4 == 0);
    printf("simd %d\nskin %ld %ld\ndigest %016llx\n", KC_SIMD, skin_plain, skin_blocks, (unsigned long long)digest);
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
#define WINDOW 0x400000ull   /* (4 MB of guest memory at WBASE: 64 GB, or just below 2^39) */
#define WBASE @WBASE@
extern uint8_t *window;
extern uint8_t table_page[0x1000];   /* (the page of the constants' pointer, 0x84ca4000) */
extern uint8_t marks[WINDOW >> 12];
static inline void *hos_guest_range(uint64_t a, uint64_t n) {
    if (a >= 0x84ca4000ull && a - 0x84ca4000ull < 0x1000 && n <= 0x1000 - (a - 0x84ca4000ull)) return table_page + (a - 0x84ca4000ull);
    return a >= WBASE && a - WBASE < WINDOW && n <= WINDOW - (a - WBASE) ? (void *)(window + (a - WBASE)) : (void *)0;
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
uint8_t *window;
uint8_t table_page[0x1000];
uint8_t marks[WINDOW >> 12];
static int64_t now_ns = 1;
static int logged_first;
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
    printf("  log: %.300s\n", line);
}
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); exit(1); } } while (0)
#define G(a) (WBASE + (uint64_t)(a))
static void put16(uint64_t a, uint32_t v) { const uint16_t h = (uint16_t)v; memcpy(window + a, &h, 2); }
static void put32(uint64_t a, uint32_t v) { memcpy(window + a, &v, 4); }
static void put64(uint64_t a, uint64_t v) { memcpy(window + a, &v, 8); }
static void putf(uint64_t a, float f) { memcpy(window + a, &f, 4); }
static uint32_t fbits_(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
static uint8_t *before;
static uint32_t get16_(const uint8_t *p) { uint16_t v; memcpy(&v, p, 2); return v; }
static int ctx_eq(const Ctx *a, const Ctx *b) {
    return !memcmp(a->x, b->x, sizeof a->x) && !memcmp(a->vreg, b->vreg, sizeof a->vreg) && a->n == b->n && a->z == b->z &&
           a->c == b->c && a->v == b->v && a->fpcr == b->fpcr && a->fpsr == b->fpsr;
}
static uint32_t rs = 7;
static uint32_t rnd(void) { return rs = rs * 1664525u + 1013904223u; }

/* Object Space Deform: six blocks (4, 3, 2, 1 bones, an empty block, 4 bones again) */
#undef OUT   /* (windows.h, which game/oot_cloth.c includes on Windows, defines a macro of this name) */
enum { TYPES = 0x1000, X20 = 0x1200, X21 = 0x1300, POS = 0x10000, BONES = 0x20000, OUT = 0x40000,
       S4 = 0x80000, S3 = 0x90000, S2 = 0xa0000, S1 = 0xb0000, NBLK = 6, NBN = 12 };
static const uint8_t TYPE[NBLK] = {0, 1, 2, 3, 0x9c, 0};
static void skin_setup(void) {
    memset(window, 0, WINDOW), memset(marks, 0, sizeof marks);
    for (int t = 0; t < NBLK; t++) window[TYPES + t] = TYPE[t];
    put64(X20 + 0x18, G(OUT)), window[X20 + 0x24] = 16, put32(X21 + 0x100, 3);
    for (int b = 0; b < NBN; b++)
        for (int c = 0; c < 4; c++)
            for (int q = 0; q < 4; q++) putf(BONES + 64 * b + 16 * c + 4 * q, q == 3 ? (c == 3 ? 1.0f : 0.0f) : (float)((b + c + q) % 5) * 0.25f - 0.5f);
    put32(BONES + 64 * 2 + 12, 0x7fc00000u);   /* (a column's lane 3 the default NaN) */
    for (int i = 0; i < 16 * 5; i++) {
        for (int q = 0; q < 3; q++) put16(POS + 8 * i + 2 * q, rnd());
        put16(POS + 8 * i + 6, 0x3880);   /* (the scale's upper half: 2^-14) */
    }
    const uint64_t S[4] = {S4, S3, S2, S1};
    for (int k = 0; k < 4; k++)
        for (int blk = 0; blk < 2; blk++) {
            const int nb = 4 - k, step = (int)CLOTH_SKIN_RECORD(nb);
            const uint64_t s = S[k] + (uint64_t)step * blk;
            for (int i = 0; i < 16; i++) put16(s + 2 * i, (uint32_t)(blk * 16 + i + 16 * k * 2));
            for (int i = 0; i < 16 * nb; i++) put16(s + 0x20 + 2 * i, rnd() % NBN);
            for (int i = 0x20 + 32 * nb; i < step + 8; i++) window[s + i] = (uint8_t)rnd();
        }
}
static void skin_ctx(Ctx *c) {
    memset(c, 0, sizeof *c);
    for (int r = 0; r < 31; r++) c->x[r] = 0x1111111111111111ull * (uint64_t)(r % 15);
    for (int r = 0; r < 32; r++) c->vreg[r][0] = c->vreg[r][1] = 0x2222222222222222ull * (uint64_t)(r % 7);
    c->x[0] = 0, c->x[1] = G(TYPES), c->x[10] = 0, c->x[9] = NBLK, c->x[27] = G(POS), c->x[20] = G(X20), c->x[21] = G(X21);
    c->x[25] = G(BONES), c->x[14] = G(S4), c->x[13] = G(S3), c->x[12] = G(S2), c->x[11] = G(S1);
    c->vreg[0][0] = c->vreg[0][1] = 0;
    c->vreg[1][0] = c->vreg[1][1] = 0x3b8000003b800000ull;
    c->vreg[2][0] = c->vreg[2][1] = 0x3f8000003f800000ull;
    c->fpcr = 1u << 24, c->fpsr = 0x9f;
}
/* the stores the blocks should leave, by the kernel over a copy of the window */
static void skin_want(uint8_t *mem) {
    ClothSkinParams k;
    memset(&k, 0, sizeof k);
    for (int q = 0; q < 4; q++) k.weight_scale[q] = 0x3b800000u;
    k.fourth_weight = 0x3f800000u, k.fz = 1, k.fpsr_in = 0x9f;
    uint64_t S[4] = {S4, S3, S2, S1}, pos = POS;
    for (int t = 0; t < NBLK; t++) {
        if (TYPE[t] > 7) continue;
        const int p = TYPE[t], nb = 4 - p;
        ClothSkinBlock b;
        ClothSkinLast last;
        b.influences = nb, b.positions = mem + pos, b.record = mem + S[p], b.bones = mem + BONES, b.bone_base = 0;
        b.out = mem + OUT, b.out_base = 0, b.first = 3, b.stride = 16;
        cloth_skin_81db9a40(&k, &b, &last);
        S[p] += CLOTH_SKIN_RECORD(nb), pos += 0x80;
    }
}

/* Mesh Bone Deform: five bones */
enum { B19 = 0x2000, B20 = 0x2100, B21 = 0x2200, BPOS = 0x110000, BTRI = 0x120000, BPAIR = 0x130000, BREC = 0x140000,
       BOUT = 0x150000, BTAB = 0x160000, BNN = 5 };
static void bones_setup(void) {
    memset(window, 0, WINDOW), memset(marks, 0, sizeof marks), memset(table_page, 0, sizeof table_page);
    const uint64_t t = G(BTAB);
    memcpy(table_page + 0x680, &t, 8);
    for (int q = 0; q < 4; q++) putf(BTAB + 0x160 + 4 * q, 0.3f), putf(BTAB + 0x60 + 4 * q, 1.0f);
    put64(B19 + 0x18, G(BPOS)), put64(B19 + 0x30, G(BTRI)), put32(B19 + 0x38, 1);
    put64(B20 + 0x18, G(BOUT));
    put64(B21 + 0x50, G(BPAIR)), put32(B21 + 0x58, BNN), put64(B21 + 0x60, G(BREC));
    for (int i = 0; i < 16; i++)
        for (int q = 0; q < 4; q++) putf(BPOS + 16 * i + 4 * q, q == 3 ? 0.0f : (float)((i * 7 + q * 3) % 11) * 0.5f - 2.0f);
    put32(BPOS + 16 * 3 + 12, 0x7fc00000u);
    for (int i = 0; i < BNN; i++) {
        for (int j = 0; j < 3; j++) put16(BTRI + 6 * i + 2 * j, (uint32_t)(i + 3 * j) % 16);
        put16(BPAIR + 4 * i, 64u * (uint32_t)i), put16(BPAIR + 4 * i + 2, 6u * (uint32_t)i);
        for (int q = 0; q < 16; q++) putf(BREC + 64 * i + 4 * q, (float)((i + q) % 7) * 0.25f - 0.75f);
    }
}
static void bones_ctx(Ctx *c) {
    memset(c, 0, sizeof *c);
    for (int r = 0; r < 31; r++) c->x[r] = 0x3333333333333333ull * (uint64_t)(r % 5);
    for (int r = 0; r < 32; r++) c->vreg[r][0] = c->vreg[r][1] = 0x4444444444444444ull * (uint64_t)(r % 3);
    c->x[19] = G(B19), c->x[20] = G(B20), c->x[21] = G(B21);
    c->fpcr = 1u << 24, c->fpsr = 0x9f;
}
static void run_off(void) {
    skin_setup(), memcpy(before, window, WINDOW);
    Ctx c, k;
    skin_ctx(&c), k = c;
    CHECK(hos_site_cloth_skin(&c) == 0 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    bones_setup(), memcpy(before, window, WINDOW);
    bones_ctx(&c), k = c;
    CHECK(hos_site_cloth_bones(&c) == 0 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    at_end("cloth.skinEnd", &c), at_end("cloth.bonesEnd", &c);
    puts("PASS: off: both callbacks answer 0, nothing written");
}

static void run_on(void) {
    Ctx c, w, k;
    skin_setup();
    uint8_t *mem = (uint8_t *)malloc(WINDOW);
    memcpy(mem, window, WINDOW);
    skin_want(mem);
    skin_ctx(&c), k = c;
    CHECK(hos_site_cloth_skin(&c) == 1 && !memcmp(mem, window, WINDOW) && ctx_eq(&c, &k) && marks[OUT >> 12]);
    puts("PASS: on: Object Space Deform's six blocks (each format, an empty block) with the kernel's stores and no register "
         "written, FPSR as it was, the pages marked");
    skin_setup(), window[TYPES] = 5, memcpy(before, window, WINDOW);
    skin_ctx(&c), k = c;
    CHECK(hos_site_cloth_skin(&c) == 0 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    skin_setup(), window[TYPES + 2] = 6;
    skin_ctx(&c), w = c;
    w.x[1] = G(TYPES) + 2, w.x[10] = 2, w.x[27] = G(POS) + 0x100, w.x[14] = G(S4) + 0xe0, w.x[13] = G(S3) + 0xb0;
    CHECK(hos_site_cloth_skin(&c) == 0 && ctx_eq(&c, &w));
    skin_setup(), put64(X20 + 0x18, G(POS) + 0x40), memcpy(before, window, WINDOW);
    skin_ctx(&c), k = c;
    CHECK(hos_site_cloth_skin(&c) == 0 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    skin_setup(), memcpy(before, window, WINDOW);
    skin_ctx(&c), c.fpcr |= 1u << 25, k = c;
    CHECK(hos_site_cloth_skin(&c) == 0 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    skin_ctx(&c), c.x[25] = G(WINDOW - 64), k = c;
    CHECK(hos_site_cloth_skin(&c) == 0 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    puts("PASS: on: Object Space Deform declines a first block of 16-bit weights, the stores over the positions, another "
         "float mode and bones past the window (untouched), and stops at a later block of 16-bit weights with only the loop's "
         "state at it written");
    bones_setup(), memcpy(before, window, WINDOW), memcpy(mem, window, WINDOW);
    {   /* (the kernel over a copy: what the callback should store) */
        ClothBonesLast last;
        ClothBonesArgs a;
        a.positions = mem + BPOS, a.pos_first = 0, a.triangles = mem + BTRI, a.tri_first = 0, a.pairs = mem + BPAIR;
        a.records = mem + BREC, a.out = mem + BOUT, a.out_first = 0, a.count = BNN, a.last = &last;
        for (int q = 0; q < 4; q++) a.centre_weight[q] = fbits_(0.3f);
        a.t = fbits_(1.0f), a.fz = 1, a.fpsr_in = 0x9f;
        cloth_bones_81ce6500(&a);
    }
    bones_ctx(&c), k = c;
    CHECK(hos_site_cloth_bones(&c) == 1 && memcmp(before, window, WINDOW) && !memcmp(mem, window, WINDOW) && marks[BOUT >> 12]);
    CHECK(ctx_eq(&c, &k));
    for (int i = 0; i < BNN; i++) CHECK(*(uint32_t *)(window + BOUT + 64 * i + 0xc) == 0 && *(uint32_t *)(window + BOUT + 64 * i + 0x2c) == 0);
    bones_setup(), put32(B21 + 0x58, 0), memcpy(before, window, WINDOW);
    bones_ctx(&c), k = c;
    CHECK(hos_site_cloth_bones(&c) == 1 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    bones_setup(), put32(B19 + 0x38, 0), memcpy(before, window, WINDOW);
    bones_ctx(&c), k = c;
    CHECK(hos_site_cloth_bones(&c) == 0 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    bones_setup(), put64(B20 + 0x18, G(BTRI) - 128), memcpy(before, window, WINDOW);
    bones_ctx(&c), k = c;
    CHECK(hos_site_cloth_bones(&c) == 0 && ctx_eq(&c, &k) && !memcmp(before, window, WINDOW));
    puts("PASS: on: Mesh Bone Deform answered (the kernel's 64 bytes a bone, the zeroed words, no register written, FPSR as "
         "it was); no bones: answered, nothing written; [x19+0x38] zero and a store over a triangle declined, untouched");
    free(mem);
}

/* (the lifted loop's registers at its way out: other than the hook's, which hands none back) */
static void scramble(Ctx *c) {
    for (int r = 3; r < 8; r++) c->x[r] ^= 0x5555;
    for (int r = 4; r < 28; r++) c->vreg[r][0] ^= 0x1234, c->vreg[r][1] ^= 0x5678;
    c->n ^= 1, c->z ^= 1;
}

static void run_check(void) {
    for (int bad = 0; bad < 3; bad++) {   /* (0 exact, 1 a byte off, 2 another FPSR) */
        skin_setup();
        Ctx c, w;
        skin_ctx(&c), w = c;
        uint8_t *mem = (uint8_t *)malloc(WINDOW);
        memcpy(mem, window, WINDOW);
        skin_want(mem);
        CHECK(hos_site_cloth_skin(&c) == 0);
        CHECK(hos_site_cloth_skin(&c) == 0);   /* (the lifted loop's next block: not asked again) */
        memcpy(window, mem, WINDOW);          /* (the lifted loop, exact: the kernel's) */
        scramble(&w);
        if (bad == 1) window[OUT + 16 * 20 + 3] ^= 1;
        if (bad == 2) w.fpsr |= 1u << 27;
        at_end("cloth.skinEnd", &w);
        cloth_flush();
        CHECK(cloth_total[K_SKIN][S_CHECKED] == 1 + bad && cloth_total[K_SKIN][S_MISMATCH] == bad);
        free(mem);
    }
    for (int bad = 0; bad < 3; bad++) {
        bones_setup();
        Ctx c, w;
        bones_ctx(&c), w = c;
        CHECK(hos_site_cloth_bones(&c) == 0);
        uint64_t n;
        CHECK(bones_answer(&w, NULL, &n) == 1);   /* (the lifted loop, exact) */
        scramble(&w);
        if (bad == 1) window[BOUT + 64 * 2 + 5] ^= 1;
        if (bad == 2) w.fpsr |= 1u << 27;
        at_end("cloth.bonesEnd", &w);
        cloth_flush();
        CHECK(cloth_total[K_BONES][S_CHECKED] == 1 + bad && cloth_total[K_BONES][S_MISMATCH] == bad);
    }
    CHECK(logged_first == 1);
    puts("PASS: check: both pieces' copies and FPSR compared with the lifted code at the loops' ways out, the registers the "
         "lifted loop leaves not: an exact copy passes, a byte off or another FPSR is a MISMATCH");
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
    for k in ("cloth_skin", "cloth_bones"):   # (the guard against fusing, by text)
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
        srcs = [str(ROOT / "game" / "cloth_skin.cpp"), str(ROOT / "game" / "cloth_bones.cpp"), str(tmp / "kfix.cpp")]
        subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "game"), "-I", str(tmp)] + flags + srcs + ["-o", str(exe)], check=True)
        r = subprocess.run([str(exe), str(trials)], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("FAIL (%s):\n%s%s" % (name, r.stdout, r.stderr))
        out = dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)
        digests[name] = out["digest"]
        s = out["skin"].split()
        print("PASS: %s (%s): Object Space Deform's and Mesh Bone Deform's kernels on %d trials each (no flag outside the "
              "status bits, no store outside their slots); Object Space Deform the same as the plain model on %s of its %s "
              "blocks" % (name, "vector paths" if out["simd"] == "1" else "the plain path only", trials, s[0], s[1]))
    if len(set(digests.values())) != 1:
        raise SystemExit("FAIL: the builds disagree: %s" % digests)
    print("PASS: every build gives the same bytes (%s)" % ", ".join(digests))


def hooks(cc, cxx, tmp):
    copy_glue(tmp)
    (tmp / "hfix.c").write_text(HFIX, encoding="utf-8")
    native = ["-march=native"] if subprocess.run(cc + ["-march=native", "-x", "c", "-c", "-o", os.devnull, "-"], input="int x;\n",
                                                 capture_output=True, text=True).returncode == 0 else []
    objs = []
    for k in KERNELS:
        o = tmp / (k + ".o")
        subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-ffp-contract=off", "-Wall", "-Wextra",
                        "-Werror"] + native + ["-I", str(tmp), "-c", str(tmp / (k + ".cpp")), "-o", str(o)], check=True)
        objs.append(str(o))
    # (the game's heap from 64 GB, its thread stacks near the top of the 39-bit window: rewrite-sessions.md)
    for base in (0x1000000000, (1 << 39) - 0x400000 - 0x1000):
        (tmp / "hos.h").write_text(HOS_H.replace("@WBASE@", "0x%xull" % base), encoding="utf-8")
        fo = tmp / "hfix.o"
        subprocess.run(cc + ["-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                        "-Wno-unused-parameter", "-ffp-contract=off",
                        "-include", str(ROOT / "gpu" / "vk" / "compat" / "longshot_env.h"), "-I", str(tmp)] + native +
                       ["-c", str(tmp / "hfix.c"), "-o", str(fo)], check=True)
        exe = tmp / ("hfix.exe" if os.name == "nt" else "hfix")
        subprocess.run(cxx + [str(fo)] + objs + ([] if os.name == "nt" else ["-pthread"]) + ["-o", str(exe)], check=True)
        env = {k: v for k, v in os.environ.items() if k != "HOS_NATIVE_CLOTH" and not k.startswith("HOS_CLOTH_")}
        for mode, extra in (("on", {}), ("off", {"HOS_NATIVE_CLOTH": "0"}), ("off", {"HOS_NATIVE_CLOTH": "off"}), ("on", {"HOS_NATIVE_CLOTH": "1"}),
                            ("check", {"HOS_NATIVE_CLOTH": "check"})):
            r = subprocess.run([str(exe), mode], env=dict(env, **extra), capture_output=True, text=True)
            print("\n".join(line for line in r.stdout.splitlines() if line.startswith("PASS")))
            if r.returncode:
                raise SystemExit("FAIL (guest window at %#x, %s %s):\n%s%s" % (base, mode, extra, r.stdout, r.stderr))
        print("PASS: the callbacks as above with the guest window at %#x" % base)


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
    with tempfile.TemporaryDirectory(prefix="cloth-step12-") as tmp:
        kernels(cxx, Path(tmp), a.trials)
        hooks(cc, cxx, Path(tmp))


if __name__ == "__main__":
    main()
