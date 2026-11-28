"""HOS_NATIVE_CLOTH's step 8: Prepare Matrices (f_81cdff40, game/cloth_matrices.cpp), _integrateTask's Verlet loop
(f_81c66a60, game/cloth_verlet.cpp), and their sites' callbacks in game/oot_cloth.c (cloth.matrices; cloth.verlet and check
mode's cloth.verletEnd).

- The kernels on random calls (lane 3 plain, the game's (the default NaN, normal and huge values) or garbage; odd values
  anywhere: NaNs quiet and signalling with payloads, infinities, subnormals, the edges of the classes; values near 2^-126;
  FZ on and off; the guest FPSR with and without IOC/OFC/UFC; every split of a call into four-wide passes / groups of 16
  and a rest): the Verlet loop the same bytes written and values of the last particles whether given the guest's FPSR or
  every flag set (what the hook passes); Prepare Matrices' nothing written past its output and its
  seven values, those values by meaning (the stored columns or the model's they are, by both cases of the count). Against
  a plain model written here (IEEE float operations in the routine's operand order) on plain values (Prepare Matrices: on
  its plain calls). By hand: a NaN with a payload propagates the Arm's way in the Verlet loop; the default NaN of lane 3
  under FZ with FPSR 0x9f stays the default NaN; Prepare Matrices' add whose
  operands a group has the other way round (M_3 + M_0 * T_3[0], a group's entries 0..14) gives the other NaN than an entry
  after the groups in the output. Built four ways (this CPU, x86-64-v2, x86-64 = no SSE4.1, -ffp-contract=on): one
  digest.
- The callbacks (game/oot_cloth.c as it is, over a guest window at 64 GB and again just below 2^39: the game's heap and
  its stacks; a call there declined for its address fails the test), a64_lookup answering a stand-in for Prepare
  Matrices' lifted routine:
  - off (0 or off): both answer 0 and write nothing; the lifted routine is not called;
  - on (unset or 1): the Verlet loop answered with the kernel's bytes and no register (nothing it leaves is read after
    it), for each rest, FPSR as it was (whatever it holds: it does not gate), its pages marked; an empty count: nothing
    at all; the arrays over each other or over what the loop reads, another float mode (FPCR),
    memory past the window: declined, untouched. Prepare Matrices answered whole: the output and the seven values it
    leaves for the caller in v3-v7, v16, v17 (all 128 bits), no other register (FPSR neither) and nothing below sp; no
    entries: nothing; the output over an entry or the routine's stack, that stack over M, another float mode, bones past
    the window: declined, untouched; no lifted routine at its address (a64_lookup NULL): answered 0, nothing written;
  - check: the lifted code runs (stood in for by the answer itself, or by a wrong one) and stays; the host's copies are
    compared with it (the Verlet loop at its way out's site, on its two arrays and FPSR; Prepare Matrices after the
    lifted routine, on what a caller can see: the output, FPSR, the callee-saved registers and the seven values): an
    exact one counts as compared, a byte, FPSR, a value left or a callee-saved register off is a MISMATCH, a register the
    Verlet loop leaves, a scratch register or the stack below sp is not looked at; the "oot: cloth" lines name all the
    pieces, each line under the logger's 1023 characters.
The sites' tables and the hooks as the lift lowers them are tested in tests/test_oot_cloth.py.

python -I -B tests/test_cloth_step8.py [--cc cc] [--cxx c++]
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
#include "cloth_matrices.h"
#include "cloth_verlet.h"
#include "cloth_wlane.hpp"
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
/* lane 3: 0 plain, 1 the game's (the default NaN, normal, huge), 2 garbage */
static uint32_t wval(int m) {
    if (m == 0) return fb(rnd() & 1 ? 1.0f : 0.0f);
    if (m == 1) { const uint32_t r = rnd() % 10; return r < 6 ? 0x7fc00000u : r < 8 ? fb(unit() * 50.0f - 25.0f) : 0x5e000000u + (rnd() & 0x1ffffff); }
    return rnd() % 3 ? odd() : rnd();
}
static uint32_t fpsr_in(void) { static const uint32_t F[] = {0x10u, 0x9fu, 0x0800009fu, 0x1du, 0x18u}; return F[rnd() % 5]; }
static void put(uint8_t *p, uint32_t v) { memcpy(p, &v, 4); }
static uint32_t get(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
/* a value somewhere odd, or near 2^-126 */
static void spoil(uint8_t *p, size_t n4) {
    const size_t at = rnd() % n4;
    put(p + 4 * at, rnd() % 3 ? odd() : (rnd() & 0x80000000u) | (0x00700000u + (rnd() & 0x3fffffu)));
}

/* ---- Verlet ---- */
enum { VN = 70 };
static uint8_t vcur[2][16 * VN], vprev[2][16 * VN], vacc[16 * VN], vsab[16 * VN], vg[16];
static long vcalls[2], vparticles;
static void verlet_trial(int plain) {
    const uint32_t n4 = rnd() % 16, rest = rnd() % 4, n = 4 * n4 + rest;
    const int wm = plain ? 0 : (int)(rnd() % 3);
    const float c0 = unit() * 6000.0f - 3000.0f;
    for (uint32_t i = 0; i < n; i++) {
        for (int q = 0; q < 3; q++) {
            const float x = c0 + unit() * 40.0f - 20.0f;
            put(vcur[0] + 16 * i + 4 * q, fb(x)), put(vprev[0] + 16 * i + 4 * q, fb(x - (unit() - 0.5f) * 0.2f));
            put(vacc + 16 * i + 4 * q, fb((unit() - 0.5f) * 30.0f));
        }
        put(vcur[0] + 16 * i + 12, wval(wm)), put(vprev[0] + 16 * i + 12, wval(wm)), put(vacc + 16 * i + 12, wval(wm));
        put(vsab + 16 * i, fb(rnd() % 3 ? 1.0f : unit() * 4.0f)), put(vsab + 16 * i + 4, fb(rnd() % 4 ? unit() : 0.0f));
        put(vsab + 16 * i + 8, rnd()), put(vsab + 16 * i + 12, rnd());
    }
    put(vg, fb(0.0f)), put(vg + 4, fb(-9.8f)), put(vg + 8, fb(unit() - 0.5f)), put(vg + 12, wval(wm));
    ClothVerletArgs a, b;
    memset(&a, 0, sizeof a);
    a.dt2 = fb(1.0f / 3600.0f * (0.5f + unit()));
    for (int q = 0; q < 4; q++) a.damping[q] = fb(0.98f + 0.02f * unit());
    if (!plain) {
        if (rnd() % 8 == 0) a.damping[3] = wval(wm);
        for (int k = rnd() % 4; k > 0 && n; k--) {
            switch (rnd() % 6) {
            case 0: spoil(vcur[0], 4 * n); break;
            case 1: spoil(vprev[0], 4 * n); break;
            case 2: spoil(vacc, 4 * n); break;
            case 3: spoil(vsab, 4 * n); break;
            case 4: spoil(vg, 4); break;
            default: a.dt2 = odd();
            }
        }
    }
    a.fz = (int)(rnd() & 1);
    const uint32_t fin = plain ? 0x10u : fpsr_in();
    if (!n) return;   /* (the hook never calls the kernel with no particle) */
    memcpy(vcur[1], vcur[0], 16 * n), memcpy(vprev[1], vprev[0], 16 * n);
    /* the guest's FPSR, and every flag set (what the hook passes): the same bytes and the same values of the last particles
       (the fields a call does not write keep what they held) */
    ClothVerletLast la, lb;
    memset(&la, 0xa5, sizeof la), memset(&lb, 0xa5, sizeof lb);
    a.force = vacc, a.scale = vsab, a.gravity = vg, a.groups = n4, a.singles = rest;
    b = a;
    a.pos = vcur[0], a.prev = vprev[0], a.fpsr_in = fin, a.last = &la;
    b.pos = vcur[1], b.prev = vprev[1], b.fpsr_in = 0x9fu, b.last = &lb;
    const uint32_t fl = cloth_verlet_81c66a60(&a);
    (void)cloth_verlet_81c66a60(&b);
    CHECK(!memcmp(vcur[0], vcur[1], 16 * n) && !memcmp(vprev[0], vprev[1], 16 * n) && !memcmp(&la, &lb, sizeof la));
    const uint32_t raised = (fin | fl) & ~0x10u;   /* (the flags raised: IXC may be left out) */
    vcalls[n4 ? 1 : 0]++, vparticles += n;
    dig(vcur[0], 16 * n), dig(vprev[0], 16 * n), dig(&la, sizeof la), dig(&raised, 4);
}
/* the plain model, on its own arrays */
static void verlet_model(void) {
    enum { N = 23 };
    static uint8_t cur[16 * N], prev[16 * N], acc[16 * N], sab[16 * N], g[16], cur0[16 * N], prev0[16 * N];
    for (uint32_t i = 0; i < N; i++) {
        for (int q = 0; q < 4; q++)
            put(cur + 16 * i + 4 * q, fb(unit() * 200.0f - 100.0f)), put(prev + 16 * i + 4 * q, fb(unit() * 200.0f - 100.0f)),
            put(acc + 16 * i + 4 * q, fb(unit() * 10.0f - 5.0f));
        put(sab + 16 * i, fb(unit() + 0.5f)), put(sab + 16 * i + 4, fb(unit() + 0.25f));
    }
    for (int q = 0; q < 4; q++) put(g + 4 * q, fb(unit() * 20.0f - 10.0f));
    memcpy(cur0, cur, sizeof cur), memcpy(prev0, prev, sizeof prev);
    ClothVerletArgs a;
    ClothVerletLast last;
    memset(&a, 0, sizeof a);
    a.pos = cur, a.prev = prev, a.force = acc, a.scale = sab, a.gravity = g, a.groups = N / 4, a.singles = N % 4, a.last = &last;
    a.dt2 = fb(1.0f / 3600.0f);
    for (int q = 0; q < 4; q++) a.damping[q] = fb(0.9f + 0.1f * unit());
    a.fz = 1, a.fpsr_in = 0x10;
    const uint32_t fl = cloth_verlet_81c66a60(&a);
    CHECK((fl & ~0x10u) == 0);
    for (uint32_t i = 0; i < N; i++)
        for (int q = 0; q < 4; q++) {
            const float c = bf(get(cur0 + 16 * i + 4 * q)), p = bf(get(prev0 + 16 * i + 4 * q));
            const float gg = bf(get(g + 4 * q)) * bf(get(sab + 16 * i));
            const float t1 = bf(get(acc + 16 * i + 4 * q)) + gg, t2 = t1 * bf(get(sab + 16 * i + 4)), t3 = t2 * bf(a.dt2);
            const float d = c - p, e = bf(a.damping[q]) * d, nn = c + e, nw = nn + t3;
            CHECK(get(cur + 16 * i + 4 * q) == fb(nw) && get(prev + 16 * i + 4 * q) == fb(c));
        }
    /* the last single's values by meaning: its new position, its position before the step */
    CHECK(!memcmp(last.single_new, cur + 16 * (N - 1), 16) && !memcmp(last.single_old, prev + 16 * (N - 1), 16));
}

/* ---- Prepare Matrices ---- */
enum { PN = 70, NB = 24 };
static uint8_t pm_m[64], bones[64 * NB], pidx[2 * PN], pin[64 * PN], pout[64 * PN + 64], pleft[16 * CLOTH_MX_LEFT + 16];
static long pentries, pleft_groups, pleft_tails;
/* the values left for the caller, by meaning (cloth_matrices.h; the routine's spec, section 6): with no entry after the
   groups, the last group's stored column 3 of its entries 1, 12, 0, 11, 10, 9, 8; else the last entry's column 1 first
   and the model's column 2 as read fifth */
static void pm_left_check(uint32_t blocks, uint32_t rest) {
    const uint32_t n = 16 * blocks + rest;
    if (!rest) {
        static const int at[CLOTH_MX_LEFT] = {1, 12, 0, 11, 10, 9, 8};
        for (int k = 0; k < CLOTH_MX_LEFT; k++) CHECK(!memcmp(pleft + 16 * k, pout + 64 * (16 * (blocks - 1) + at[k]) + 48, 16));
        pleft_groups++;
    } else {
        CHECK(!memcmp(pleft, pout + 64 * (n - 1) + 16, 16) && !memcmp(pleft + 64, pm_m + 32, 16));
        pleft_tails++;
    }
}
static void matrix(uint8_t *p, int wm) {
    for (int j = 0; j < 4; j++)
        for (int q = 0; q < 4; q++)
            put(p + 16 * j + 4 * q, fb(j == 3 && q < 3 ? unit() * 4000.0f - 2000.0f : q == 3 ? (j == 3 ? 1.0f : 0.0f) : unit() * 2.0f - 1.0f));
    if (wm)
        for (int j = 0; j < 4; j++) put(p + 16 * j + 12, wval(wm));
}
static void pm_trial(int plain) {
    const uint32_t blocks = rnd() % 4, rest = rnd() % 16, n = 16 * blocks + rest;
    const int wm = plain ? 0 : (int)(rnd() % 3);
    matrix(pm_m, wm);
    for (int k = 0; k < NB; k++) matrix(bones + 64 * k, wm);
    for (uint32_t i = 0; i < n; i++) {
        matrix(pin + 64 * i, wm);
        const uint16_t k = (uint16_t)(rnd() % NB);
        memcpy(pidx + 2 * i, &k, 2);
    }
    if (!plain)
        for (int k = rnd() % 5; k > 0 && n; k--) {
            switch (rnd() % 3) {
            case 0: spoil(pm_m, 16); break;
            case 1: spoil(bones, 16 * NB); break;
            default: spoil(pin, 16 * n);
            }
        }
    if (!n) return;   /* (the hook never calls the kernel with no entry) */
    ClothMatricesArgs a;
    memset(&a, 0, sizeof a);
    a.model = pm_m, a.bones = bones, a.index = pidx, a.in = pin, a.out = pout, a.left = pleft;
    a.count_word = (blocks << 4 | rest) | (rnd() & 0xfff00000u);   /* (bits 20 and up: not the count's) */
    a.fz = (int)(rnd() & 1), a.fpsr_in = plain ? 0x10u : fpsr_in();
    memset(pout, 0x5a, sizeof pout), memset(pleft, 0xa5, sizeof pleft);
    const uint32_t fl = cloth_matrices_81cdff40(&a);
    for (size_t i = 64 * n; i < sizeof pout; i++) CHECK(pout[i] == 0x5a);   /* (nothing past the last entry) */
    for (size_t i = 16 * CLOTH_MX_LEFT; i < sizeof pleft; i++) CHECK(pleft[i] == 0xa5);
    pm_left_check(blocks, rest);
    pentries += n;
    if (plain)   /* the plain model: IEEE in the routine's operand order (the rest loop's) */
        for (uint32_t i = 0; i < n; i++) {
            uint16_t k;
            memcpy(&k, pidx + 2 * i, 2);
            float B[4][4], C[4][4], M[4][4], T[4][4], O[4][4];
            for (int j = 0; j < 4; j++)
                for (int q = 0; q < 4; q++)
                    B[j][q] = bf(get(bones + 64 * k + 16 * j + 4 * q)), C[j][q] = bf(get(pin + 64 * i + 16 * j + 4 * q)),
                    M[j][q] = bf(get(pm_m + 16 * j + 4 * q));
            for (int j = 0; j < 4; j++)
                for (int q = 0; q < 4; q++) {
                    if (j < 3) T[j][q] = (B[0][q] * C[j][0] + B[1][q] * C[j][1]) + B[2][q] * C[j][2];
                    else T[j][q] = B[2][q] * C[3][2] + (B[1][q] * C[3][1] + (B[3][q] + B[0][q] * C[3][0]));
                }
            for (int j = 0; j < 4; j++)
                for (int q = 0; q < 4; q++) {
                    if (j < 3) O[j][q] = (M[0][q] * T[j][0] + M[1][q] * T[j][1]) + M[2][q] * T[j][2];
                    else O[j][q] = M[2][q] * T[3][2] + (M[1][q] * T[3][1] + (M[3][q] + M[0][q] * T[3][0]));
                    CHECK(get(pout + 64 * i + 16 * j + 4 * q) == fb(O[j][q]));
                }
        }
    const uint32_t raised = a.fpsr_in | fl;   /* (bits already set may be reported or not) */
    dig(pout, 64 * n), dig(pleft, 16 * CLOTH_MX_LEFT), dig(&raised, 4);
}

static void by_hand(void) {
    trial = -1;
    /* Verlet: a quiet NaN with a payload in cur.x (FPSR without the fast way's flags): the Arm's NaN, the payload kept */
    {
        uint8_t cur[16], prev[16], acc[16], sab[16], g[16];
        memset(cur, 0, 16), memset(prev, 0, 16), memset(acc, 0, 16), memset(sab, 0, 16), memset(g, 0, 16);
        put(cur, 0x7fc12345u), put(sab, fb(1.0f)), put(sab + 4, fb(1.0f));
        ClothVerletArgs a;
        ClothVerletLast l;
        memset(&a, 0, sizeof a);
        a.pos = cur, a.prev = prev, a.force = acc, a.scale = sab, a.gravity = g, a.singles = 1, a.dt2 = fb(1.0f), a.fpsr_in = 0x10;
        a.last = &l;
        for (int q = 0; q < 4; q++) a.damping[q] = fb(1.0f);
        uint32_t fl = cloth_verlet_81c66a60(&a);
        CHECK(get(cur) == 0x7fc12345u && get(prev) == 0x7fc12345u && (fl & ~0x10u) == 0 && l.single_new[0] == 0x7fc12345u);
        /* lane 3 the default NaN, FZ, FPSR 0x9f: the default NaN kept */
        memset(cur, 0, 16), memset(prev, 0, 16);
        put(cur, fb(1.5f)), put(cur + 12, 0x7fc00000u), put(prev + 12, 0x7fc00000u);
        a.singles = 1, a.fz = 1, a.fpsr_in = 0x9f;
        fl = cloth_verlet_81c66a60(&a);
        CHECK(get(cur + 12) == 0x7fc00000u && get(cur) == fb(3.0f) && get(prev) == fb(1.5f) && (fl & ~0x9fu) == 0);
    }
    /* Prepare Matrices: M3 + M0 * T3[0] with a NaN in each (B0 lane 0 a NaN B, M3 lane 2 a NaN A): a group's entries
       0..14 add them the other way round (O3 lane 2 is NaN B), its entry 15 and an entry after the groups as written
       (NaN A); the seven values left: the last entry's (one after the group) */
    {
        static uint8_t m[64], bn[64], in[64 * 17], out[64 * 17], left[16 * CLOTH_MX_LEFT];
        for (int j = 0; j < 4; j++)
            for (int q = 0; q < 4; q++) put(m + 16 * j + 4 * q, fb(j == q ? 1.0f : 0.0f)), put(bn + 16 * j + 4 * q, fb(j == q ? 1.0f : 0.0f));
        put(m + 48 + 8, 0x7fc0000au);   /* M3 lane 2: NaN A */
        put(bn + 0, 0x7fc0000bu);       /* B0 lane 0: NaN B */
        for (int i = 0; i < 17; i++)
            for (int j = 0; j < 4; j++)
                for (int q = 0; q < 4; q++) put(in + 64 * i + 16 * j + 4 * q, fb(j == q ? 1.0f : 0.5f));
        uint8_t idx[34];
        memset(idx, 0, sizeof idx);
        ClothMatricesArgs a;
        memset(&a, 0, sizeof a);
        a.model = m, a.bones = bn, a.index = idx, a.in = in, a.out = out, a.count_word = 0x11, a.fpsr_in = 0x10, a.left = left;
        cloth_matrices_81cdff40(&a);
        for (int i = 0; i < 17; i++) CHECK(get(out + 64 * i + 48 + 8) == (i < 15 ? 0x7fc0000bu : 0x7fc0000au));
        CHECK(!memcmp(left, out + 64 * 16 + 16, 16) && !memcmp(left + 64, m + 32, 16));
    }
}

int main(int argc, char **argv) {
    const int trials = argc > 1 ? atoi(argv[1]) : 2000;
    by_hand();
    verlet_model();
    for (trial = 0; trial < trials; trial++) verlet_trial(trial % 4 == 0), pm_trial(trial % 4 == 1);
    printf("digest %016llx\nsimd %d\nverlet %ld %ld %ld\npm %ld %ld %ld\n", (unsigned long long)digest,
#if defined(__SSE4_1__)
           1,
#else
           0,
#endif
           vcalls[0], vcalls[1], vparticles, pentries, pleft_groups, pleft_tails);
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
extern uint8_t marks[WINDOW >> 12];
static inline void *hos_guest_range(uint64_t a, uint64_t n) {
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
/* a64_lookup: Prepare Matrices' lifted routine (the glue keeps the first it gets, so it routes to the stand-in the case
   chose); none: a build with no lifted routine there */
static int lookup_none;
static void (*pm_now)(Ctx *);
static void pm_route(Ctx *c) { pm_now(c); }
A64Fn a64_lookup(uint64_t a) { return !lookup_none && a == OOT_CLOTH_PM_FN ? pm_route : NULL; }
uint8_t *window;
uint8_t marks[WINDOW >> 12];
static int64_t now_ns = 1;
static int logged_first, logged_lines, long_line;
static char all_lines[16384];
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
    long_line |= strlen(line) > 1023;
    if (strstr(line, "oot: cloth (HOS_NATIVE_CLOTH=")) {
        logged_lines++;
        if (strlen(all_lines) + strlen(line) + 2 < sizeof all_lines) strcat(all_lines, line), strcat(all_lines, "\n");
    }
    printf("  log: %.300s\n", line);
}
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); exit(1); } } while (0)
#define G(a) (WBASE + (uint64_t)(a))
static void put32(uint64_t a, uint32_t v) { memcpy(window + a, &v, 4); }
static void put64(uint64_t a, uint64_t v) { memcpy(window + a, &v, 8); }
static void putf(uint64_t a, float f) { memcpy(window + a, &f, 4); }
static uint32_t get32(uint64_t a) { uint32_t v; memcpy(&v, window + a, 4); return v; }
static uint64_t get64(uint64_t a) { uint64_t v; memcpy(&v, window + a, 8); return v; }
static uint32_t fbits_(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
enum { VINST = 0x1000, VPD = 0x1100, VSP = 0x2000, VCUR = 0x100000, VPREV = 0x140000, VACC = 0x180000, VSAB = 0x1c0000,
       VG = 0x1100 + 0x80, PM_M = 0x3000, PM_B = 0x200000, PM_I = 0x240000, PM_IN = 0x280000, PM_OUT = 0x2c0000,
       PSP = 0x380000, VN = 23, PBLK = 2, PREST = 5, PN = 16 * PBLK + PREST, NB = 12 };
static uint8_t *before, *copy;
static uint32_t rs = 7;
static uint32_t rnd(void) { return rs = rs * 1664525u + 1013904223u; }

static void setup(void) {
    memset(window, 0, WINDOW), memset(marks, 0, sizeof marks);
    put64(VINST + 0x20, G(VCUR)), put64(VINST + 0x30, G(VPREV)), put64(VSP + 8, G(VPD)), put64(VPD + 0x40, G(VSAB));
    for (int i = 0; i < VN + 4; i++)
        for (int q = 0; q < 4; q++) {
            putf(VCUR + 16 * i + 4 * q, q == 3 ? 0.0f : (float)i * 0.25f + (float)q), putf(VPREV + 16 * i + 4 * q, q == 3 ? 0.0f : (float)i * 0.25f + (float)q - 0.01f);
            putf(VACC + 16 * i + 4 * q, (float)(q + 1) * 0.5f), put32(VSAB + 16 * i + 4 * q, q == 0 ? fbits_(1.0f) : q == 1 ? fbits_(0.5f) : rnd());
            if (q == 3 && i % 2) put32(VCUR + 16 * i + 12, 0x7fc00000u), put32(VPREV + 16 * i + 12, 0x7fc00000u);
        }
    putf(VG, 0.0f), putf(VG + 4, -9.8f), putf(VG + 8, 0.0f), put32(VG + 12, 0x7fc00000u);
    for (int j = 0; j < 4; j++)
        for (int q = 0; q < 4; q++) putf(PM_M + 16 * j + 4 * q, j == q ? 1.0f : 0.125f * (float)(j + q));
    for (int k = 0; k < NB; k++)
        for (int j = 0; j < 16; j++) putf(PM_B + 64 * k + 4 * j, (float)((k + j) % 7) * 0.25f - 0.5f);
    for (int i = 0; i < PN; i++) {
        const uint16_t k = (uint16_t)((i * 5) % NB);
        memcpy(window + PM_I + 2 * i, &k, 2);
        for (int j = 0; j < 16; j++) putf(PM_IN + 64 * i + 4 * j, (float)((i + 3 * j) % 9) * 0.5f - 2.0f);
    }
    for (int b = 0; b < 0xd00; b++) window[PSP - 0xd00 + b] = (uint8_t)rnd();
    memset(window + PM_OUT, 0x5a, 64 * (PN + 1));
}
static void verlet_ctx(Ctx *c, uint32_t n) {
    memset(c, 0, sizeof *c);
    for (int r = 0; r < 31; r++) c->x[r] = (uint64_t)rnd() << 32 | rnd();
    for (int r = 0; r < 32; r++) c->vreg[r][0] = (uint64_t)rnd() << 32 | rnd(), c->vreg[r][1] = (uint64_t)rnd() << 32 | rnd();
    c->x[21] = G(VACC), c->x[22] = G(VINST), c->x[23] = G(VG), c->x[25] = n, c->x[28] = n, c->x[31] = G(VSP);
    c->vreg[22][0] = (c->vreg[22][0] & ~0xffffffffull) | fbits_(1.0f / 3600.0f);
    c->vreg[23][0] = (uint64_t)fbits_(0.99f) << 32 | fbits_(0.99f), c->vreg[23][1] = c->vreg[23][0];
    c->fpcr = 1u << 24, c->fpsr = 0x9f;
}
static void pm_ctx(Ctx *c, uint32_t w4) {
    memset(c, 0, sizeof *c);
    for (int r = 0; r < 31; r++) c->x[r] = (uint64_t)rnd() << 32 | rnd();
    for (int r = 0; r < 32; r++) c->vreg[r][0] = (uint64_t)rnd() << 32 | rnd(), c->vreg[r][1] = (uint64_t)rnd() << 32 | rnd();
    c->x[0] = G(PM_M), c->x[1] = G(PM_B), c->x[2] = G(PM_I), c->x[3] = G(PM_IN), c->x[4] = w4 | 0xabc00000ull << 8, c->x[5] = G(PM_OUT);
    c->x[31] = G(PSP);
    c->fpcr = 1u << 24, c->fpsr = 0x9f;
}
static int lifted_calls;
static void lifted_none(Ctx *c) { (void)c; lifted_calls++; }

/* what the Verlet hook must leave: the kernel's bytes on a copy of the window, and every register as it was (nothing the
   loop leaves in one is read after it; FPSR as it was) */
static void verlet_want(const Ctx *c0, uint32_t n, Ctx *w) {
    *w = *c0;
    memcpy(copy, window, WINDOW);
    const uint32_t n4 = n >= 4 ? n >> 2 : 0, rest = n & 3;
    if (!n4 && !rest) return;
    ClothVerletArgs a;
    ClothVerletLast last;
    memset(&a, 0, sizeof a);
    a.pos = copy + VCUR, a.prev = copy + VPREV, a.force = copy + VACC, a.scale = copy + VSAB, a.gravity = copy + VG;
    a.groups = n4, a.singles = rest, a.last = &last;
    a.dt2 = (uint32_t)c0->vreg[22][0], memcpy(a.damping, c0->vreg[23], 16), a.fz = 1, a.fpsr_in = CLOTH_FPSR_IN;
    (void)cloth_verlet_81c66a60(&a);
}

static void run_off(void) {
    setup(), memcpy(before, window, WINDOW);
    Ctx c, k;
    verlet_ctx(&c, VN), k = c;
    CHECK(hos_site_cloth_verlet(&c) == 0 && !memcmp(&c, &k, sizeof c) && !memcmp(before, window, WINDOW));
    at_end("cloth.verletEnd", &c);
    pm_ctx(&c, PN), k = c;
    CHECK(hos_site_cloth_matrices(&c) == 0 && !memcmp(&c, &k, sizeof c) && !memcmp(before, window, WINDOW) && !lifted_calls);
    puts("PASS: off: the Verlet and Prepare Matrices callbacks answer 0 and write nothing");
}

static void run_on(void) {
    {   /* no lifted routine at Prepare Matrices' address (a64_lookup NULL): answered 0, nothing written */
        setup(), memcpy(before, window, WINDOW), lookup_none = 1;
        Ctx c, k;
        pm_ctx(&c, PN), k = c;
        CHECK(hos_site_cloth_matrices(&c) == 0 && !memcmp(&c, &k, sizeof c) && !memcmp(before, window, WINDOW) && !lifted_calls);
        CHECK(cloth_local[K_PM][S_CALLS] == 0);
        lookup_none = 0;
        puts("PASS: on: no lifted routine at Prepare Matrices' address: the callback answers 0, nothing written");
    }
    for (uint32_t n = VN - 3; n <= VN; n++) {   /* rests 0..3 */
        setup();
        Ctx c, w;
        verlet_ctx(&c, n);
        verlet_want(&c, n, &w);
        CHECK(hos_site_cloth_verlet(&c) == 1);
        CHECK(!memcmp(&c, &w, sizeof c) && !memcmp(copy, window, WINDOW) && marks[VCUR >> 12] && marks[VPREV >> 12]);
    }
    {   /* empty: nothing at all */
        setup(), memcpy(before, window, WINDOW);
        Ctx c, w;
        verlet_ctx(&c, 0);
        verlet_want(&c, 0, &w);
        CHECK(hos_site_cloth_verlet(&c) == 1 && !memcmp(&c, &w, sizeof c) && !memcmp(before, window, WINDOW));
    }
    {   /* FPSR does not gate (the lifted code raises no cumulative flag): IXC clear is answered, FPSR as it was */
        setup();
        Ctx c, w;
        verlet_ctx(&c, VN), c.fpsr = 0x8f;
        verlet_want(&c, VN, &w);
        CHECK(hos_site_cloth_verlet(&c) == 1 && !memcmp(&c, &w, sizeof c) && c.fpsr == 0x8f && !memcmp(copy, window, WINDOW));
    }
    puts("PASS: on: the Verlet loop answered (positions and previous positions for rests 0-3, no register; FPSR as it was, IXC "
         "clear too), its pages marked, at a 64 GB-or-higher guest address; an empty count: nothing at all");
    static const struct { const char *what; int k; } no[] = {{"previous = positions", 0}, {"forces in the positions", 1},
        {"G in the previous positions", 2}, {"another float mode (DN)", 3}, {"positions past the window", 4}};
    for (unsigned i = 0; i < sizeof no / sizeof no[0]; i++) {
        setup();
        Ctx c;
        verlet_ctx(&c, VN);
        if (no[i].k == 0) put64(VINST + 0x30, G(VCUR));
        if (no[i].k == 1) c.x[21] = G(VCUR + 32);
        if (no[i].k == 2) c.x[23] = G(VPREV + 48);
        if (no[i].k == 3) c.fpcr |= 1u << 25;
        if (no[i].k == 4) put64(VINST + 0x20, G(WINDOW - 0x100));
        memcpy(before, window, WINDOW);
        const Ctx k = c;
        if (hos_site_cloth_verlet(&c) != 0 || memcmp(&c, &k, sizeof c) || memcmp(before, window, WINDOW)) {
            fprintf(stderr, "FAIL: the Verlet loop answered with %s\n", no[i].what);
            exit(1);
        }
    }
    puts("PASS: on: the Verlet loop: arrays over each other or over what it reads, another float mode, memory past the window: "
         "declined, untouched");
    /* Prepare Matrices: 2 groups and 5 after them; 2 groups and none (the other case of the values left); no entries */
    for (int pass = 0; pass < 3; pass++) {
        setup();
        Ctx c, w;
        const uint32_t w4 = pass == 0 ? PN : pass == 1 ? 16 * PBLK : 0;
        pm_ctx(&c, w4);
        w = c;
        memcpy(copy, window, WINDOW);
        if (w4) {   /* the kernel on a copy: the output and its seven values in v3-v7, v16, v17 (all 128 bits); FPSR as it was */
            uint8_t left[16 * CLOTH_MX_LEFT];
            ClothMatricesArgs a;
            memset(&a, 0, sizeof a);
            a.model = copy + PM_M, a.bones = copy + PM_B, a.index = copy + PM_I, a.in = copy + PM_IN, a.out = copy + PM_OUT;
            a.count_word = (uint32_t)c.x[4], a.fz = 1, a.fpsr_in = 0x9f, a.left = left;
            (void)cloth_matrices_81cdff40(&a);
            static const int at[CLOTH_MX_LEFT] = {3, 4, 5, 6, 7, 16, 17};
            for (int k = 0; k < CLOTH_MX_LEFT; k++) memcpy(w.vreg[at[k]], left + 16 * k, 16);
        }
        CHECK(hos_site_cloth_matrices(&c) == 1 && !lifted_calls);
        CHECK(!memcmp(&c, &w, sizeof c) && c.fpsr == 0x9f);
        CHECK(!memcmp(copy, window, WINDOW));   /* (the output alone: nothing below sp) */
        if (w4) CHECK(marks[PM_OUT >> 12] && !marks[(PSP - 0x100) >> 12]);
        /* the values left, by meaning: the model's column 2 in v7 (entries after the groups); the last group's entry 1's
           column 3 in v3 (none after them) */
        if (pass == 0) CHECK(!memcmp(c.vreg[7], window + PM_M + 32, 16));
        if (pass == 1) CHECK(!memcmp(c.vreg[3], window + PM_OUT + 64 * (16 * (PBLK - 1) + 1) + 48, 16));
    }
    puts("PASS: on: Prepare Matrices answered whole (the output and the seven values it leaves in v3-v7, v16, v17, by both "
         "cases of the count; no other register, FPSR neither, nothing below sp), its pages marked; no entries: nothing at all");
    static const struct { const char *what; int k; } pno[] = {{"the output over an entry", 0}, {"the routine's stack over M", 1},
        {"another float mode (AH)", 2}, {"bones past the window", 3}, {"the output over the routine's stack", 4}};
    for (unsigned i = 0; i < sizeof pno / sizeof pno[0]; i++) {
        setup();
        Ctx c;
        pm_ctx(&c, PN);
        if (pno[i].k == 0) c.x[5] = G(PM_IN + 64 * 3);
        if (pno[i].k == 1) c.x[0] = G(PSP - 0x200);
        if (pno[i].k == 2) c.fpcr |= 2u;
        if (pno[i].k == 3) c.x[1] = G(WINDOW - 0x100);
        if (pno[i].k == 4) c.x[5] = G(PSP - 0x800);
        memcpy(before, window, WINDOW);
        const Ctx k = c;
        if (hos_site_cloth_matrices(&c) != 0 || memcmp(&c, &k, sizeof c) || memcmp(before, window, WINDOW)) {
            fprintf(stderr, "FAIL: Prepare Matrices answered with %s\n", pno[i].what);
            exit(1);
        }
    }
    puts("PASS: on: Prepare Matrices: the output over an entry or the routine's stack, that stack over M, another float mode, "
         "bones past the window: declined, untouched");
}

/* check mode's stand-ins for the lifted code: the answer itself (an exact lift), or with 1 a byte of the output off, 2 a
   value left for the caller off (v16's upper half), 3 a callee-saved register off (d8), 4 a scratch register off (v20),
   5 a byte below sp off (the routine's scratch): 1-3 are MISMATCHES, 4 and 5 are not looked at */
static int pm_bad;
static void pm_lifted(Ctx *c) {
    uint64_t n;
    CHECK(pm_answer(c, NULL, &n) == 1);
    if (pm_bad == 1) window[PM_OUT + 64 * 3 + 5] ^= 1;
    if (pm_bad == 2) c->vreg[16][1] ^= 1;
    if (pm_bad == 3) c->vreg[8][0] ^= 1;
    if (pm_bad == 4) c->vreg[20][0] ^= 1;
    if (pm_bad == 5) window[PSP - 0x50 - 0x20] ^= 1;
    lifted_calls++;
}
static void run_check(void) {
    /* the Verlet loop: the lifted loop stood in for by the answer itself, or with 1 a byte of the previous positions off,
       2 registers the loop leaves off (x8, v0: none is handed back), 3 FPSR off: 1 and 3 are MISMATCHES */
    for (int bad = 0; bad <= 3; bad++) {
        setup(), memcpy(before, window, WINDOW);
        Ctx c;
        verlet_ctx(&c, VN);
        CHECK(hos_site_cloth_verlet(&c) == 0 && !memcmp(before, window, WINDOW));
        uint64_t n;
        CHECK(verlet_answer(&c, NULL, &n) == 1);   /* (the lifted loop, exact) */
        if (bad == 1) window[VPREV + 16 * 7 + 2] ^= 1;
        if (bad == 2) c.x[8] ^= 1, c.vreg[0][0] ^= 1;
        if (bad == 3) c.fpsr ^= 0x10;
        at_end("cloth.verletEnd", &c);
        CHECK(cloth_local[K_VERLET][S_CHECKED] == 1 && logged_first == (bad > 0));
        cloth_flush();
        CHECK(cloth_total[K_VERLET][S_CHECKED] == 1 + bad && cloth_total[K_VERLET][S_MISMATCH] == (bad == 3 ? 2 : bad ? 1 : 0));
    }
    for (int bad = 0; bad <= 5; bad++) {
        setup();
        Ctx c;
        pm_ctx(&c, bad == 2 ? 16 * PBLK : PN);   /* (both cases of the values left) */
        pm_bad = bad, lifted_calls = 0;
        pm_now = pm_lifted;
        CHECK(hos_site_cloth_matrices(&c) == 1 && lifted_calls == 1);
        cloth_flush();
        CHECK(cloth_total[K_PM][S_CHECKED] == 1 + bad && cloth_total[K_PM][S_MISMATCH] == (bad > 3 ? 3 : bad));
    }
    pm_bad = 0;
    CHECK(logged_first == 1);
    puts("PASS: check: the Verlet loop's copies (two regions) and FPSR, and Prepare Matrices' (the output, FPSR, the "
         "callee-saved registers and the seven values it leaves) compared with the lifted code: an exact one passes, a byte, "
         "FPSR, a value left or a callee-saved register off is a MISMATCH; a register the Verlet loop leaves, a scratch "
         "register or the stack below sp is not looked at");
    now_ns += CLOTH_LOG_NS * 2;
    cloth_flush();
    for (int k = 0; k < K_COUNT; k++) CHECK(strstr(all_lines, cloth_name[k]));
    CHECK(logged_lines >= 2 && !long_line && strstr(all_lines, "verlet f_81c66a60: calls 4") && strstr(all_lines, "pm f_81cdff40: calls 6"));
    puts("PASS: check: the oot: cloth lines name every piece with its counts, each under 1023 characters");
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "off";
    window = (uint8_t *)calloc(1, WINDOW), before = (uint8_t *)calloc(1, WINDOW), copy = (uint8_t *)calloc(1, WINDOW);
    CHECK(window && before && copy);
    pm_now = lifted_none;
    hos_oot_cloth_init();   /* (check mode: the ways out's sites) */
    if (!strcmp(mode, "off")) run_off();
    else if (!strcmp(mode, "on")) run_on();
    else run_check();
    return 0;
}
'''


def kernels(cxx, tmp, trials):
    for k in ("cloth_matrices", "cloth_verlet"):   # (the guard against fusing, by text)
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
        srcs = [str(ROOT / "game" / "cloth_matrices.cpp"), str(ROOT / "game" / "cloth_verlet.cpp"), str(tmp / "kfix.cpp")]
        subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "game"), "-I", str(tmp)] + flags + srcs + ["-o", str(exe)], check=True)
        r = subprocess.run([str(exe), str(trials)], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("FAIL (%s):\n%s%s" % (name, r.stdout, r.stderr))
        out = dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)
        digests[name] = out["digest"]
        v, p = out["verlet"].split(), out["pm"].split()
        print("PASS: %s (%s): the Verlet loop's kernel the same with the guest's FPSR and with every flag set on %d trials "
              "(calls of singles only / with groups %s / %s, %s particles); Prepare Matrices' kernel on the same trials (%s "
              "entries; the seven values left by meaning: %s calls ending in a group, %s with entries after the groups; nothing "
              "written past them); the plain models the same; the cases by hand" % (
                  name, "SSE4.1" if out["simd"] == "1" else "no SSE4.1", trials, v[0], v[1], v[2], p[0], p[1], p[2]))
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
                        "-Werror"] + native + ["-c", str(tmp / (k + ".cpp")), "-o", str(o)], check=True)
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
    with tempfile.TemporaryDirectory(prefix="cloth-step8-") as tmp:
        kernels(cxx, Path(tmp), a.trials)
        hooks(cc, cxx, Path(tmp))


if __name__ == "__main__":
    main()
