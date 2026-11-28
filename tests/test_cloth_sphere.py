"""HOS_NATIVE_CLOTH: the sphere collidable's batch kernel f_81c1a240, mode 0 (game/cloth_sphere.cpp), and its sites' callbacks
in game/oot_cloth.c (cloth.sphere at the branch into the batch loop, check mode's cloth.sphereRecordsEnd at its way out).

- The kernel (cloth_sphere_records) on random calls (a cloth around a moving sphere; lane 3 plain, the game's (the default
  NaN, normal and huge values), garbage or, under FZ with frictions up to 3, small normals (2^-126 .. 2^-120); odd values
  anywhere; FZ on and off): built three ways (this CPU, x86-64-v2, x86-64 = plain SSE2; -ffp-contract=off, as the build
  compiles it), the same bytes stored, the same records done and the same reasons in every build. (The kernel has one
  path, with no lane-by-lane one to compare against: a record with a value it cannot take is left to the game's code.) Against a plain model written here (IEEE float operations in SPEC.md's order, the first
  two particles in contact of a group sharing one previous-position store) on the plain trials. By hand: the pair's
  store (the second particle's previous position left as it was), a NaN with a payload in a particle in contact and
  repeated particle numbers each stopping their record before anything of it is stored, another float mode (FPCR) or a
  float status without every cumulative flag stopping the whole call with nothing stored.
- The callbacks (game/oot_cloth.c as it is, over a guest window at 64 GB and again just below 2^39):
  - off: the callback answers 0 and writes nothing;
  - on: every record natively, the last included (the kernel's bytes, its pages marked), the list position past them
    (x8, the one value the rest of the routine reads) set and 1 answered, nothing else written; a record the kernel
    stops at: the records before it done, x8 / x9 at it, 0 answered; another float mode (FPCR: the hook's gate, before
    the kernel's own), the slots over each other, over the frame or past the window: declined, untouched; FPSR does not
    gate and is left as it was (the kernel is told every cumulative flag is set);
  - check: the callback writes nothing and answers 0; the records are compared at the batch loop's way out (its site's
    callback, set by hos_oot_cloth_init) with what stands in for the lifted loop: the list position, FPSR and the slots
    (exact: compared; a byte off, the list position off or FPSR off: a MISMATCH).
  The particles left over (count & 15) run lifted: no site.

python -I -B tests/test_cloth_sphere.py [--cc cc] [--cxx c++]
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
from sitecheck import site  # noqa: E402

RUN_ALL_ARGS = ["--cc", "{cc}", "--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]

KFIX = r'''
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cloth_sphere.h"
#include "cloth_common.hpp"
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s (trial %d)\n", __LINE__, #x, trial); exit(1); } } while (0)
static int trial;
static uint64_t rs = 0x9e3779b97f4a7c15ull;
static uint32_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return (uint32_t)(rs >> 16); }
static float unit(void) { return (float)(rnd() & 0xffffff) / 16777216.0f; }
static uint32_t fb(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
static float bf(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
static uint64_t digest = 1469598103934665603ull;
static void dig(const void *p, size_t n) { for (size_t i = 0; i < n; i++) digest = (digest ^ ((const uint8_t *)p)[i]) * 1099511628211ull; }
static const uint32_t ODD[] = {0x7fc00000u, 0xffc00000u, 0x7fc12345u, 0x7f800001u, 0x7f800000u, 0xff800000u, 0x00000001u,
                               0x807fffffu, 0x00800000u, 0x80000000u, 0x00000000u, 0x7f7fffffu, 0x5f800000u, 0x1f000000u};
static uint32_t wval(int m) {   /* lane 3: 0 plain, 1 the game's, 2 garbage, 3 small normals (or the game's NaN, a zero) */
    if (m == 0) return fb(1.0f);
    if (m == 3) return rnd() % 5 ? (rnd() & 0x80000000u) | (0x00800000u + rnd() % 0x03000000u) : rnd() % 2 ? 0x7fc00000u : 0u;
    if (m == 1) { const uint32_t r = rnd() % 10; return r < 6 ? 0x7fc00000u : r < 8 ? fb(unit() * 50.0f - 25.0f) : 0x5e000000u + (rnd() & 0x1ffffff); }
    return rnd() % 3 ? ODD[rnd() % 14] : rnd();
}
enum { NP = 96, NREC = 5 };
static uint8_t pos[16 * NP], prev[16 * NP], recs[32 * NREC], rad[64 * NREC], fric[64 * NREC];
static ClothSphereArgs K;
static int wm, plain;

/* ---- the plain model (IEEE float, SPEC.md's order; FZ off, no NaN) ------------------------------------------------------ */
static float frsqrte(float x) { return bf(kc::frsqrte_bits(fb(x))); }
static float step(float a, float b) { const double p = (double)a * (double)b; return (float)(3.0 - p) * 0.5f; }
static float invroot(float L) {
    if (L <= bf(0x007fffffu)) return 0.0f;
    const float e0 = frsqrte(L), y1 = e0 * step(L, e0 * e0);
    return y1 * step(L, y1 * y1);
}
static void model(uint8_t *ps, uint8_t *qs) {
    const float cx = bf(K.sphere[0]), cy = bf(K.sphere[1]), cz = bf(K.sphere[2]), rho = bf(K.sphere[3]), dt = bf(K.dt);
    for (int r = 0; r < NREC; r++) {
        uint16_t ix[16];
        memcpy(ix, recs + 32 * r, 32);
        float Pn[16][4], Qn[16][4], G[16][4];
        int hit[16] = {0};
        for (int k = 0; k < 16; k++) {
            float P[4], Q[4], R, F;
            memcpy(P, ps + 16 * ix[k], 16), memcpy(Q, qs + 16 * ix[k], 16);
            memcpy(&R, rad + 64 * r + 4 * k, 4), memcpy(&F, fric + 64 * r + 4 * k, 4);
            const float dx = P[0] - cx, dy = P[1] - cy, dz = P[2] - cz;
            const float sxy = dx * dx + dy * dy, sq = dz * dz + sxy, L = bf(K.eps[k % 4]) + sq;
            const float rr = invroot(L), len = L * rr, pen = (len - rho) - R;
            if (!(pen < 0.0f)) continue;
            hit[k] = 1;
            const float dw = P[3] - rho;
            const float n[4] = {dx * rr, dy * rr, dz * rr, dw * rr};
            const float S[3] = {cx + n[0] * rho, cy + n[1] * rho, cz + n[2] * rho};
            const float e[3] = {P[0] - S[0], P[1] - S[1], P[2] - S[2]};
            const float depth = R - (n[2] * e[2] + (n[0] * e[0] + n[1] * e[1]));
            for (int i = 0; i < 4; i++) Pn[k][i] = P[i] + n[i] * depth;
            const float ax = S[0] - bf(K.body[0]), ay = S[1] - bf(K.body[1]), az = S[2] - bf(K.body[2]);
            const float wx = bf(K.spin[0]), wy = bf(K.spin[1]), wz = bf(K.spin[2]);
            const float V[4] = {bf(K.velocity[0]) + (wy * az - ay * wz), bf(K.velocity[1]) + (wz * ax - az * wx),
                                bf(K.velocity[2]) + (wx * ay - ax * wy), bf(K.velocity[3]) + 0.0f};
            float rel[4];
            for (int i = 0; i < 4; i++) rel[i] = (Pn[k][i] - Q[i]) - V[i] * dt;
            const float dn = (n[0] * rel[0] + n[1] * rel[1]) + n[2] * rel[2];
            for (int i = 0; i < 4; i++) G[k][i] = (rel[i] - n[i] * dn) * F, Qn[k][i] = Q[i] + G[k][i];
        }
        int stq[16] = {0};
        float Qs[16][4];
        for (int g = 0; g < 4; g++) {
            int seen = 0, a = -1;
            for (int l = 0; l < 4; l++) {
                const int k = 4 * g + l;
                if (!hit[k]) continue;
                if (seen == 0) { a = k, stq[k] = 1; memcpy(Qs[k], Qn[k], 16); }
                else if (seen == 1) { for (int i = 0; i < 4; i++) Qs[a][i] = G[k][i] + Qn[a][i]; }
                else { stq[k] = 1; memcpy(Qs[k], Qn[k], 16); }
                seen++;
            }
        }
        for (int k = 0; k < 16; k++) if (hit[k]) memcpy(ps + 16 * ix[k], Pn[k], 16);
        for (int k = 0; k < 16; k++) if (stq[k]) memcpy(qs + 16 * ix[k], Qs[k], 16);
    }
}

/* ---- a random call ----------------------------------------------------------------------------------------------------- */
static float C[3], RHO;
static void make(int odd) {
    wm = plain ? 0 : (int)(rnd() % 4);
    for (int i = 0; i < 3; i++) C[i] = unit() * 4.0f - 2.0f;
    RHO = 0.1f + unit() * 0.4f;
    memset(&K, 0, sizeof K);
    K.sphere[0] = fb(C[0]), K.sphere[1] = fb(C[1]), K.sphere[2] = fb(C[2]), K.sphere[3] = fb(RHO);
    for (int l = 0; l < 4; l++) K.eps[l] = 0x34000000u;   /* (2^-23, the game's) */
    K.dt = fb(1.0f / 60.0f);
    for (int i = 0; i < 3; i++) K.body[i] = fb(C[i] + unit() * 0.2f - 0.1f), K.spin[i] = fb(unit() * 4.0f - 2.0f);
    for (int i = 0; i < 3; i++) K.velocity[i] = fb(unit() - 0.5f);
    K.velocity[3] = plain ? 0 : wval(wm);
    K.fpcr = plain ? 0 : wm == 3 ? 1u << 24 : (rnd() & 1) << 24, K.fpsr = 0x9fu;
    for (int p = 0; p < NP; p++) {
        const float dist = (RHO + 0.03f) * (0.5f + unit());
        float dir[3] = {unit() - 0.5f, unit() - 0.5f, unit() - 0.5f}, nn = 0;
        for (int i = 0; i < 3; i++) nn += dir[i] * dir[i];
        nn = sqrtf(nn) + 1e-6f;
        uint32_t P[4], Q[4];
        for (int i = 0; i < 3; i++) {
            const float x = C[i] + dir[i] / nn * dist;
            P[i] = fb(x), Q[i] = fb(x - (unit() - 0.5f) * 0.02f);
        }
        P[3] = wval(wm), Q[3] = wval(wm);
        memcpy(pos + 16 * p, P, 16), memcpy(prev + 16 * p, Q, 16);
    }
    for (int i = 0; i < NREC; i++) {
        uint16_t ix[16];
        for (int j = 0; j < 16; j++) {
            ix[j] = (uint16_t)((i * 16 + j) % NP);
            const float R = 0.03f, f = wm == 3 ? unit() * 3.0f : unit();
            memcpy(rad + 64 * i + 4 * j, &R, 4), memcpy(fric + 64 * i + 4 * j, &f, 4);
        }
        for (int j = 15; j > 0; j--) { const int s = (int)(rnd() % (unsigned)(j + 1)); const uint16_t x = ix[j]; ix[j] = ix[s], ix[s] = x; }
        memcpy(recs + 32 * i, ix, 32);
    }
    if (odd)
        for (int k = 0, n = 1 + (int)(rnd() % 4); k < n; k++) {
            const uint32_t v = ODD[rnd() % 14];
            uint8_t *where = rnd() % 2 ? pos : prev;
            memcpy(where + 4 * (rnd() % (4 * NP)), &v, 4);
        }
}

static ClothSphereResult call(int n) {
    K.indices = recs, K.radii = rad, K.frictions = fric, K.positions = pos, K.previous = prev, K.records = (uint32_t)n;
    ClothSphereResult r = {0xffffffffu, 0xffffffffu};
    cloth_sphere_records(&K, &r);
    CHECK(r.done <= (uint32_t)n && (r.done == (uint32_t)n) == (r.why == CLOTH_SPHERE_ALL) && r.why <= CLOTH_SPHERE_REPEATED);
    return r;
}

int main(int argc, char **argv) {
    const int trials = argc > 1 ? atoi(argv[1]) : 1000;
    long rec_done = 0, plain_checked = 0, stopped = 0;
    for (trial = 0; trial < trials; trial++) {
        plain = trial % 3 == 0;
        make(!plain && trial % 4 == 1);
        static uint8_t mp[16 * NP], mq[16 * NP];
        memcpy(mp, pos, sizeof mp), memcpy(mq, prev, sizeof mq);
        const ClothSphereResult r = call(NREC);
        const uint32_t o[2] = {r.done, r.why};
        dig(o, sizeof o), dig(pos, sizeof pos), dig(prev, sizeof prev);
        stopped += r.done < NREC;
        if (plain) {   /* the plain model from the same inputs */
            CHECK(r.done == NREC);
            model(mp, mq);
            CHECK(!memcmp(mp, pos, sizeof mp) && !memcmp(mq, prev, sizeof mq));
            rec_done += r.done, plain_checked++;
        }
    }
    /* by hand */
    trial = -1;
    plain = 1, make(0);
    {
        /* a record whose group 0 has particles 0 and 1 in contact, the rest far */
        uint16_t ix[16];
        for (int j = 0; j < 16; j++) ix[j] = (uint16_t)j;
        memcpy(recs, ix, 32);
        for (int j = 0; j < 16; j++) {
            const float P[4] = {C[0] + (j < 2 ? 0.0f : 5.0f), C[1] + 0.01f * (float)(j + 1), C[2], 1.0f};
            memcpy(pos + 16 * j, P, 16);
        }
        static uint8_t q0[16 * NP];
        memcpy(q0, prev, sizeof q0);
        CHECK(call(1).done == 1);
        CHECK(!memcmp(prev + 16, q0 + 16, 16) && memcmp(prev, q0, 16));   /* particle 1's previous position untouched, 0's written */
    }
    {
        plain = 1, make(0), plain = 0;   /* (lane 3 plain: the stops below are the payload's and the repeat's) */
        K.fpcr = 1u << 24;
        static uint8_t p0[16 * NP], q0[16 * NP];
        memcpy(p0, pos, sizeof p0), memcpy(q0, prev, sizeof q0);
        /* a NaN with a payload in lane 3 of a particle in contact in record 2: records 0 and 1 done, nothing of 2 or after */
        uint16_t ix0;
        memcpy(&ix0, recs + 32 * 2, 2);
        const float P[4] = {C[0], C[1] + 0.01f, C[2], 0.0f};
        memcpy(pos + 16 * ix0, P, 16);
        const uint32_t pay = 0x7fc12345u;
        memcpy(pos + 16 * ix0 + 12, &pay, 4);
        static uint8_t keep[16 * NP], kq[16 * NP];
        memcpy(keep, pos, sizeof keep), memcpy(kq, prev, sizeof kq);
        ClothSphereResult r = call(NREC);
        CHECK(r.done == 2 && r.why == CLOTH_SPHERE_VALUES);
        for (int k = 2; k < NREC; k++) {
            uint16_t ix2[16];
            memcpy(ix2, recs + 32 * k, 32);
            int earlier = 0;   /* (a particle of records 0 or 1 too was stored by them) */
            for (int j = 0; j < 16; j++) {
                for (int q = 0; q < 32; q++) { uint16_t e; memcpy(&e, recs + 2 * q, 2); earlier |= e == ix2[j]; }
                if (!earlier) CHECK(!memcmp(pos + 16 * ix2[j], keep + 16 * ix2[j], 16) && !memcmp(prev + 16 * ix2[j], kq + 16 * ix2[j], 16));
                earlier = 0;
            }
        }
        /* repeated particle numbers in record 1 */
        memcpy(pos, p0, sizeof p0), memcpy(prev, q0, sizeof q0);
        memcpy(recs + 32 * 1 + 2, recs + 32 * 1, 2);
        r = call(NREC);
        CHECK(r.done == 1 && r.why == CLOTH_SPHERE_REPEATED);
        /* another float mode, or a float status without every cumulative flag: the whole call left, nothing stored */
        memcpy(pos, p0, sizeof p0), memcpy(prev, q0, sizeof q0);
        const uint32_t fpcr[3] = {(1u << 24) | (1u << 25), 1u << 22, 1u << 24}, fpsr[3] = {0x9fu, 0x9fu, 0x8fu};
        for (int m = 0; m < 3; m++) {
            K.fpcr = fpcr[m], K.fpsr = fpsr[m];
            r = call(NREC);
            CHECK(r.done == 0 && r.why == CLOTH_SPHERE_FLOAT_MODE && !memcmp(pos, p0, sizeof p0) && !memcmp(prev, q0, sizeof q0));
        }
    }
    printf("simd %d\nplain %ld %ld\nstopped %ld\ndigest %016llx\n", KC_SIMD, plain_checked, rec_done, stopped,
           (unsigned long long)digest);
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
#define WINDOW 0x400000ull
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
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include "hos.h"
#include "oot_cloth.c"
''' + SITE_SET + r'''
A64Fn a64_lookup(uint64_t a) { (void)a; return NULL; }   /* (no whole routine's callback is called here) */
uint8_t *window;
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
static void put32(uint64_t a, uint32_t v) { memcpy(window + a, &v, 4); }
static void put64(uint64_t a, uint64_t v) { memcpy(window + a, &v, 8); }
static void putf(uint64_t a, float f) { memcpy(window + a, &f, 4); }
static uint32_t rs = 11;
static uint32_t rnd(void) { return rs = rs * 1664525u + 1013904223u; }
static float unit(void) { return (float)(rnd() >> 8) / 16777216.0f; }
enum { SP = 0x10000, FCELL = 0x2000, RCELL = 0x2100, COLLA = 0x3000, REC = 0x20000, RADS = 0x22000, FRICS = 0x23000,
       POS = 0x100000, PREV = 0x180000, NPART = 96, NREC = 4 };
static uint8_t *before;
static const float CEN[3] = {0.5f, 1.0f, -0.25f}, RHO = 0.3f;

/* the routine's frame as the batches read it (sp+0x90 the small constant by place, +0x148 / +0x178 the frictions' and
   radii's cells, +0x190 dt, +0x1c0 the sphere), the collidable (+0x50 the point its spin turns about, +0x70 its velocity,
   +0x80 its spin), the cloth */
static void setup(void) {
    memset(window, 0, WINDOW), memset(marks, 0, sizeof marks);
    for (int l = 0; l < 4; l++) put32(SP + 0x90 + 4 * l, 0x34000000u);
    put64(SP + 0x148, G(FCELL)), put64(SP + 0x178, G(RCELL)), put64(FCELL, G(FRICS)), put64(RCELL, G(RADS));
    putf(SP + 0x190, 1.0f / 60.0f);
    for (int l = 0; l < 3; l++) putf(SP + 0x1c0 + 4 * l, CEN[l]);
    putf(SP + 0x1cc, RHO);
    for (int l = 0; l < 3; l++) putf(COLLA + 0x50 + 4 * l, CEN[l] + 0.05f), putf(COLLA + 0x70 + 4 * l, unit() - 0.5f), putf(COLLA + 0x80 + 4 * l, unit() * 2.0f - 1.0f);
    put32(COLLA + 0x7c, 0x7fc00000u);
    for (int p = 0; p < NPART; p++) {
        float dir[3] = {unit() - 0.5f, unit() - 0.5f, unit() - 0.5f};
        const float nn = sqrtf(dir[0] * dir[0] + dir[1] * dir[1] + dir[2] * dir[2]) + 1e-6f, d = (RHO + 0.03f) * (0.5f + unit());
        for (int l = 0; l < 3; l++) {
            const float x = CEN[l] + dir[l] / nn * d;
            putf(POS + 16 * p + 4 * l, x), putf(PREV + 16 * p + 4 * l, x - 0.005f);
        }
        put32(POS + 16 * p + 12, p % 2 ? 0x7fc00000u : 0x3f800000u), put32(PREV + 16 * p + 12, 0x7fc00000u);
    }
    for (int i = 0; i < NREC; i++)
        for (int j = 0; j < 16; j++) {
            const uint16_t ix = (uint16_t)((i * 16 + j * 5) % 64);
            memcpy(window + REC + 32 * i + 2 * j, &ix, 2);
            putf(RADS + 64 * i + 4 * j, 0.03f), putf(FRICS + 64 * i + 4 * j, unit());
        }
}
static void rec_ctx(Ctx *c, int n) {
    memset(c, 0, sizeof *c);
    for (int r = 0; r < 31; r++) c->x[r] = (uint64_t)rnd() << 32 | rnd();
    for (int r = 0; r < 32; r++) c->vreg[r][0] = (uint64_t)rnd() << 32 | rnd(), c->vreg[r][1] = (uint64_t)rnd() << 32 | rnd();
    c->x[20] = G(COLLA), c->x[31] = G(SP);
    c->n = 1, c->z = 0, c->c = 1, c->v = 1;
    c->fpcr = 1u << 24, c->fpsr = 0;
    c->x[8] = G(REC), c->x[9] = 0, c->x[10] = G(PREV), c->x[11] = G(POS), c->x[12] = 64ull * (uint64_t)n;
}
/* what the callback must leave: the kernel on the window's copy */
static void want(uint8_t *w, int n, uint32_t done) {
    ClothSphereArgs a;
    a.indices = w + REC, a.radii = w + RADS, a.frictions = w + FRICS, a.positions = w + POS, a.previous = w + PREV;
    a.records = (uint32_t)n, a.fpcr = 1u << 24, a.fpsr = 0x9fu;
    memcpy(a.sphere, w + SP + 0x1c0, 16), memcpy(a.eps, w + SP + 0x90, 16), memcpy(&a.dt, w + SP + 0x190, 4);
    memcpy(a.body, w + COLLA + 0x50, 12), memcpy(a.velocity, w + COLLA + 0x70, 16), memcpy(a.spin, w + COLLA + 0x80, 12);
    ClothSphereResult r;
    cloth_sphere_records(&a, &r);
    CHECK(r.done == done);
}

static void run_off(void) {
    setup(), memcpy(before, window, WINDOW);
    Ctx c, c0;
    rec_ctx(&c, NREC), c0 = c;
    CHECK(hos_site_cloth_sphere(&c) == 0 && !memcmp(&c, &c0, sizeof c) && !memcmp(before, window, WINDOW));
    at_end("cloth.sphereRecordsEnd", &c);
    puts("PASS: off: nothing written, the callback answers 0");
}

static void run_on(void) {
    Ctx c, c0;
    for (int n = 1; n <= NREC; n += NREC - 1) {
        setup(), memcpy(before, window, WINDOW), want(before, n, (uint32_t)n);
        CHECK(memcmp(before, window, WINDOW));   /* (some particles in contact) */
        rec_ctx(&c, n), c0 = c;
        CHECK(hos_site_cloth_sphere(&c) == 1);
        CHECK(!memcmp(window, before, WINDOW));
        CHECK(c.x[8] == G(REC) + 32ull * (uint64_t)n);
        c.x[8] = c0.x[8];
        CHECK(!memcmp(&c, &c0, sizeof c));
        CHECK(marks[POS >> 12] && marks[PREV >> 12]);
    }
    puts("PASS: on: every record natively (one, and four), the last included: the list position past them, the callback "
         "answers 1, nothing else changed, pages marked");
    /* record 2's particle numbers repeat: records 0 and 1 done, the lifted loop at record 2 */
    setup();
    memcpy(window + REC + 64 + 2, window + REC + 64, 2);
    memcpy(before, window, WINDOW), want(before, NREC, 2);
    rec_ctx(&c, NREC), c0 = c;
    CHECK(hos_site_cloth_sphere(&c) == 0 && !memcmp(window, before, WINDOW));
    CHECK(c.x[8] == G(REC) + 64 && c.x[9] == 128);
    c.x[8] = c0.x[8], c.x[9] = c0.x[9];
    CHECK(!memcmp(&c, &c0, sizeof c));
    /* a NaN with a payload in a particle in contact in record 0: nothing done, nothing written */
    setup();
    uint16_t i0;
    memcpy(&i0, window + REC, 2);
    putf(POS + 16 * i0, CEN[0]), putf(POS + 16 * i0 + 4, CEN[1] + 0.01f), putf(POS + 16 * i0 + 8, CEN[2]);
    put32(POS + 16 * i0 + 12, 0x7fc12345u);
    memcpy(before, window, WINDOW);
    rec_ctx(&c, NREC), c0 = c;
    CHECK(hos_site_cloth_sphere(&c) == 0 && !memcmp(window, before, WINDOW) && !memcmp(&c, &c0, sizeof c));
    puts("PASS: on: a record the kernel stops at: the records before it done, x8 / x9 at it (or nothing at the first), the "
         "callback answers 0");
    /* declines */
    struct { const char *what; uint64_t fpcr; uint64_t prev; } dec[] = {{"default NaN", (1u << 24) | (1u << 25), G(PREV)},
                                                                        {"another rounding", 1u << 22, G(PREV)},
                                                                        {"the slots over each other", 1u << 24, G(POS) + 0x40},
                                                                        {"past the window", 1u << 24, WBASE + WINDOW - 0x100},
                                                                        {"the slots over the frame", 1u << 24, G(SP) - 0x100}};
    for (int k = 0; k < 5; k++) {
        setup(), memcpy(before, window, WINDOW);
        rec_ctx(&c, NREC), c.fpcr = dec[k].fpcr, c.x[10] = dec[k].prev, c0 = c;
        const long g0 = cloth_local[K_SPHREC][S_GATE];
        CHECK(hos_site_cloth_sphere(&c) == 0 && !memcmp(&c, &c0, sizeof c) && !memcmp(window, before, WINDOW));
        CHECK((cloth_local[K_SPHREC][S_GATE] > g0) == (k < 2));   /* (the hook's gate: the kernel is not called) */
    }
    setup(), memcpy(before, window, WINDOW);
    rec_ctx(&c, NREC), c0 = c;
    put64(SP + 0x178, G(RCELL) + WINDOW);   /* (the radii's cell past the window) */
    memcpy(before, window, WINDOW);
    CHECK(hos_site_cloth_sphere(&c) == 0 && !memcmp(&c, &c0, sizeof c) && !memcmp(window, before, WINDOW));
    puts("PASS: on: another float mode (the hook's gate), the slots over each other or over the frame, memory past the "
         "window: declined, untouched");
    /* FPSR does not gate (the lifted code raises no cumulative flag): any FPSR is answered and left as it was */
    setup(), memcpy(before, window, WINDOW), want(before, NREC, NREC);
    rec_ctx(&c, NREC), c.fpsr = 0x10;
    CHECK(hos_site_cloth_sphere(&c) == 1 && c.fpsr == 0x10 && !memcmp(window, before, WINDOW));
    puts("PASS: on: FPSR does not gate: the records answered whatever it holds, and it is left as it was");
}

static void run_check(void) {
    Ctx c, c0;
    static uint8_t orig[WINDOW];
    for (int bad = 0; bad < 4; bad++) {   /* exact; a byte off; the list position off; FPSR off */
        setup(), memcpy(orig, window, WINDOW), memcpy(before, window, WINDOW), want(before, NREC, NREC);
        rec_ctx(&c, NREC), c0 = c;
        CHECK(hos_site_cloth_sphere(&c) == 0 && !memcmp(&c, &c0, sizeof c) && cloth_pending.k == K_SPHREC);
        CHECK(!memcmp(window, orig, WINDOW));   /* (nothing written for real) */
        /* the lifted loop, stood in for: the bytes and the list position it leaves (its scratch registers as they may be) */
        memcpy(window + POS, before + POS, 0x80000), memcpy(window + PREV, before + PREV, 0x80000);
        c.x[8] = G(REC) + 32ull * NREC, c.x[9] = 64ull * NREC, c.vreg[3][0] ^= 1, c.z = 1;
        if (bad == 1) window[POS + 16 * 7 + 1] ^= 1;
        if (bad == 2) c.x[8] -= 32;
        if (bad == 3) c.fpsr ^= 0x10;
        const long m0 = cloth_total[K_SPHREC][S_MISMATCH], k0 = cloth_total[K_SPHREC][S_CHECKED] + cloth_local[K_SPHREC][S_CHECKED];
        at_end("cloth.sphereRecordsEnd", &c);
        CHECK(cloth_total[K_SPHREC][S_CHECKED] + cloth_local[K_SPHREC][S_CHECKED] == k0 + 1);
        CHECK(cloth_total[K_SPHREC][S_MISMATCH] == m0 + (bad ? 1 : 0) && cloth_pending.k == -1);
    }
    CHECK(logged_first == 1);
    puts("PASS: check: the records into copies, compared at the batch loop's way out with the lifted loop's: exact compared, a "
         "byte off, the list position off or FPSR off a MISMATCH");
}

int main(int argc, char **argv) {
    window = (uint8_t *)calloc(1, WINDOW), before = (uint8_t *)malloc(WINDOW);
    const char *mode = argc > 1 ? argv[1] : "on";
    hos_oot_cloth_init();   /* (check mode: the ways out's sites) */
    if (!strcmp(mode, "off")) run_off();
    else if (!strcmp(mode, "on")) run_on();
    else run_check();
    return 0;
}
'''


def kernels(cxx, tmp, trials):
    (tmp / "kfix.cpp").write_text(KFIX, encoding="utf-8")
    builds = [("this CPU", ["-march=native"]), ("x86-64-v2", ["-march=x86-64-v2"]), ("x86-64", ["-march=x86-64"])]
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
        srcs = [str(ROOT / "game" / "cloth_sphere.cpp"), str(tmp / "kfix.cpp")]
        subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-ffp-contract=off", "-Wall", "-Wextra",
                        "-Werror", "-I", str(ROOT / "game"), "-I", str(tmp)] + flags + srcs + ["-o", str(exe)], check=True)
        r = subprocess.run([str(exe), str(trials)], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("FAIL (%s):\n%s%s" % (name, r.stdout, r.stderr))
        out = dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)
        digests[name] = out["digest"]
        p = out["plain"].split()
        print("PASS: %s (%s): the sphere kernel on %d trials (%s stopped part way or whole); the plain model the same on %s "
              "plain trials (%s records); the cases by hand" %
              (name, "vector paths" if out["simd"] == "1" else "plain SSE2", trials, out["stopped"], p[0], p[1]))
    if len(set(digests.values())) != 1:
        raise SystemExit("FAIL: the builds disagree: %s" % digests)
    print("PASS: every build gives the same bytes, records done and reasons (%s)" % ", ".join(digests))


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
    for base in (0x1000000000, (1 << 39) - 0x400000 - 0x1000):   # (the game's heap from 64 GB, its stacks near 2^39)
        (tmp / "hos.h").write_text(HOS_H.replace("@WBASE@", "0x%xull" % base), encoding="utf-8")
        fo = tmp / "hfix.o"
        subprocess.run(cc + ["-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                        "-Wno-unused-parameter", "-ffp-contract=off",
                        "-include", str(ROOT / "gpu" / "vk" / "compat" / "longshot_env.h"), "-I", str(tmp)] + native +
                       ["-c", str(tmp / "hfix.c"), "-o", str(fo)], check=True)
        exe = tmp / ("hfix.exe" if os.name == "nt" else "hfix")
        subprocess.run(cxx + [str(fo)] + objs + ([] if os.name == "nt" else ["-pthread", "-lm"]) + ["-o", str(exe)], check=True)
        env = {k: v for k, v in os.environ.items() if k != "HOS_NATIVE_CLOTH" and not k.startswith("HOS_CLOTH_")}
        for mode, extra in (("on", {}), ("off", {"HOS_NATIVE_CLOTH": "0"}), ("off", {"HOS_NATIVE_CLOTH": "off"}), ("on", {"HOS_NATIVE_CLOTH": "1"}),
                            ("on", {"HOS_CLOTH_DECLINES": "1"}), ("check", {"HOS_NATIVE_CLOTH": "check"})):
            r = subprocess.run([str(exe), mode], env=dict(env, **extra), capture_output=True, text=True)
            print("\n".join(line for line in r.stdout.splitlines() if line.startswith("PASS")))
            if r.returncode:
                raise SystemExit("FAIL (guest window at %#x, %s %s):\n%s%s" % (base, mode, extra, r.stdout, r.stderr))
        print("PASS: the callbacks as above with the guest window at %#x (also with HOS_CLOTH_DECLINES=1)" % base)


def sites():
    """cloth.sphere at the branch into the batch loop, a jump to its way out, where cloth.sphereRecordsEnd is"""
    x = site("cloth.sphere", "f_81c1a240", "jump", offset=0xe8)
    e = site("cloth.sphereRecordsEnd", "f_81c1a240", "before", offset=0x560c)
    assert x.to == e.offset, (x.to, e.offset)
    print("PASS: cloth.sphere is a jump at f_81c1a240+0xe8 to the batch loop's way out, where cloth.sphereRecordsEnd is")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cc", default=os.environ.get("CC", "gcc"))
    p.add_argument("--cxx", default=os.environ.get("CXX", "c++"))
    p.add_argument("--trials", type=int, default=2000)
    a = p.parse_args()
    sites()
    cc = parse_compiler(a.cc)
    cxx = parse_compiler(a.cxx)
    if not cc or not shutil.which(cc[0]) or not cxx or not shutil.which(cxx[0]):
        print("SKIP: no C or C++ compiler")
        return
    with tempfile.TemporaryDirectory(prefix="cloth-sphere-") as tmp:
        kernels(cxx, Path(tmp), a.trials)
        hooks(cc, cxx, Path(tmp))


if __name__ == "__main__":
    main()
