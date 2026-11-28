"""The cloth kernels (game/cloth_*.cpp, HOS_NATIVE_CLOTH): what can be held to them without the game.

- FRSQRTE: the kernels' reciprocal square root estimate (cloth_common.hpp's) against the Arm pseudocode's
  RecipSqrtEstimate (written here in Python) on every table entry (the exponent's parity and the fraction's top 8 bits)
  at the lowest, middle and highest exponents, with random low fraction bits.
- The bending and anchor passes (whole functions, kernels written from a spec: no scalar twin, no why function): on the
  same random cloths, their interfaces: the bending pass's records done and its reason, every changed slot within the
  range it reports, and the call split in two (the second part on what the first left) the same bytes and flags as
  whole; the anchor pass in a guest memory of its own read through its range resolver, done whole (every changed slot
  among the records' and within the range reported) or declined (a negative count, vertices not mapped: the reason
  given, nothing written); a third of the anchor's plain trials under FZ with small normals in lane 3 of the particles,
  the skin's columns and eps (2^-126 .. 2^-100, some a few units apart: their tiny products, sums and differences are
  zeros on the Arm; the plain x86-64 build, which has no vector path, does each record the Arm's way). Their bytes go
  into the builds' digest. Four made-up anchor records with a result that is tiny before rounding must store the bits
  the Arm's rules give (written in). Their exactness against the game's routine is held offline in an Arm emulator (the
  kits' check.py), outside this repository.
- The link, tether and bend link passes (loops, kernels written from a spec: no scalar twin, no why function), on random
  cloth (grids, folded and stretched; records sharing particles so each sees what the one before wrote; FZ on and off;
  a third of the trials with garbage in every particle's lane 3: the default NaN as in the game, other NaN payloads
  quiet and signalling, infinities, huge, tiny and subnormal values, zeros; the guest FPSR drawn with and without
  IOC/OFC/UFC set): their interfaces: every record done on plain cloth, whatever lane 3 holds; edge values (NaN,
  infinity, subnormal, huge) in a record or a particle: the records done from the first, a reason when the kernel stops
  (the call's own only with none done), every changed slot within the range it reports; the end values of a call done
  whole (the last record's indices and stiffness as read, the slot it stored last); and the call split in two (the
  second part on what the first left) the same bytes and flags as whole, as the hook's lifted rest relies on. A second
  round of trials ("harsh") puts values at and around every bound into records, particles, particle data and the entry
  values and scales whole cloths by 2^-90 .. 2^90; each kernel must have stopped somewhere. Their bytes go into the
  builds' digest.
- Two kernels against a plain model of the game's instructions written here (the link constraints and the tethers: each
  operation one IEEE rounding, FRSQRTS as one rounding of 3 - L*e then halved, Arm's FMIN; lane 3 through
  cloth_wlane.hpp's Arm operations in the model's own order), on the records each did.
- Built four ways: for this CPU, for x86-64-v2 (SSE4.1 without FMA: FRSQRTS in binary64), for baseline x86-64 (the
  kernels' SSE2 forms) and with -ffp-contract=on, the compilers' default (the files' pragmas must keep products from
  being fused into sums; -ffp-contract=fast disregards pragmas by design, so the build itself passes -ffp-contract=off).
  Every build must print the same digest of all of the above.
The game's own routines are not run here: that is the job of an exactness run against the game's code in an Arm emulator,
outside this repository (it needs the game).

python -I -B tests/test_cloth_kernels.py [--cxx c++]
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
from cloth_glue import supports_x86_64_v2  # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]
KERNELS = ("cloth_bend", "cloth_link", "cloth_bend_link", "cloth_anchor", "cloth_tether")

FIXTURE = r'''
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cloth_anchor.h"
#include "cloth_bend.h"
#include "cloth_bend_link.h"
#include "cloth_link.h"
#include "cloth_tether.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s (trial %d)\n", __LINE__, #x, trial); exit(1); } } while (0)
static int trial;
static uint64_t rs = 0x9e3779b97f4a7c15ull;
static uint32_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return (uint32_t)(rs >> 16); }
static float unit(void) { return (float)(rnd() & 0xffffff) / 16777216.0f; }   /* [0, 1) */
static float sym(void) { return 2.0f * unit() - 1.0f; }
static uint32_t fb(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
static float bf(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
static uint64_t digest = 1469598103934665603ull;
static void dig(const void *p, size_t n) { for (size_t i = 0; i < n; i++) digest = (digest ^ ((const uint8_t *)p)[i]) * 1099511628211ull; }

#define SLOTS (0x10000u * 16u)
static uint8_t *pos0, *posA, *posB, *pdata, *verts;
static uint8_t rec[64 * 1024];
static const uint32_t EDGE[] = {0x7fc00000u, 0xff812345u, 0x7f800000u, 0xff800000u, 0x00000001u, 0x807fffffu, 0x7f7fffffu, 0x5f800000u, 0x1f000000u};

/* lane 3 garbage (a third of the trials): the game's default NaN, other NaNs quiet and signalling, infinities, huge,
   tiny and subnormal values, zeros, the edges of the class */
static const uint32_t WGARB[] = {0x7fc00000u, 0x7fc00000u, 0x7fc00000u, 0xffc00000u, 0x7fc12345u, 0xffd00001u, 0x7f800001u,
                                 0xff812345u, 0x7fbfffffu, 0x7f800000u, 0xff800000u, 0x5f000000u, 0x6a123456u, 0xfe000001u,
                                 0x7f7fffffu, 0x1f000000u, 0x0c000001u, 0x00800000u, 0x80800000u, 0x00000001u, 0x807fffffu,
                                 0x00400000u, 0x00000000u, 0x80000000u, 0x3f800000u, 0x20000000u, 0x5effffffu};
static int wgarb;
static uint32_t wgarb_value(void) {
    const uint32_t k = rnd() % 8;
    if (k < 5) return WGARB[rnd() % (sizeof WGARB / sizeof WGARB[0])];
    if (k == 5) return (rnd() & 0x80000000u) | ((0x3f000000u + (rnd() % 0x3f800000u)) & 0x7fffffffu);   /* (2^-1 .. 2^127, NaN) */
    if (k == 6) return (rnd() & 0x80000000u) | (rnd() % 0x01800000u);                                      /* (subnormal .. 2^-125) */
    return fb(0.5f + unit());
}
static uint32_t fpsr_in(void) { static const uint32_t F[] = {0x10u, 0x9fu, 0x8800009fu, 0x1du, 0x18u, 0x11u, 0x15u}; return F[rnd() % 7]; }

/* a side x side sheet, folded and jittered, w lanes random */
static void sheet(int side, float spacing) {
    memset(pos0, 0, SLOTS);
    const float fold = sym() * 0.8f;
    wgarb = rnd() % 3 == 0 ? 1 + (int)(rnd() % 3) : 0;   /* (1: every slot's w garbage; 2: the default NaN everywhere; 3: some) */
    for (int i = 0; i < side * side; i++) {
        const float x = (float)(i % side) * spacing, y = (float)(i / side) * spacing;
        float p[4] = {x + sym() * 0.1f * spacing, y * cosf(fold) + sym() * 0.1f * spacing, y * sinf(fold) + 3.0f, 0.5f + unit()};
        if (wgarb == 1 || (wgarb == 3 && rnd() % 3 == 0)) p[3] = bf(wgarb_value());
        else if (wgarb == 2) p[3] = bf(cw::DEFAULT_NAN);
        memcpy(pos0 + 16 * i, p, 16);
        const float d[4] = {0.0f, (i < side && (rnd() & 1)) ? 0.0f : 0.5f + unit(), 0.0f, 0.0f};
        memcpy(pdata + 16 * i, d, 16);
    }
}
static void edge_into(uint8_t *p, int floats) { const uint32_t e = EDGE[rnd() % 9]; memcpy(p + 4 * (rnd() % floats), &e, 4); }
static void ab(void) { memcpy(posA, pos0, SLOTS), memcpy(posB, pos0, SLOTS); }

/* ---- the second round ("harsh"): values at and around every bound, scaled cloth ---- */
static int harsh;
static const uint32_t HARSH[] = {0x1a000000u, 0x17800000u, 0x1fffffffu, 0x20000000u, 0x5effffffu, 0x5f000000u, 0x00800000u,
                                 0x00400000u, 0x80000001u, 0x7f800000u, 0xff800000u, 0x7fc00000u, 0x0f000000u, 0x01000000u,
                                 0x00ffffffu, 0x62000000u};
static uint32_t harsh_value(void) { return HARSH[rnd() % 16]; }
static void harsh_into(uint8_t *p, size_t bytes, int k) {
    for (int i = 0; i < k; i++) { const uint32_t e = harsh_value(); memcpy(p + 4 * (rnd() % (bytes / 4)), &e, 4); }
}
static void harsh_data(uint8_t *r, size_t rb, int np) {
    if (rnd() % 3 == 0) {   /* the whole cloth scaled by 2^-90 .. 2^90 */
        const float s = ldexpf(1.0f, (int)(rnd() % 181) - 90);
        for (int i = 0; i < 4 * np; i++) { float v; memcpy(&v, pos0 + 4 * i, 4), v *= s, memcpy(pos0 + 4 * i, &v, 4); }
    }
    if (rnd() & 1) harsh_into(r, rb, 1 + (int)(rnd() % 3));
    if (rnd() & 1) harsh_into(pos0, 16 * (size_t)np, 1 + (int)(rnd() % 2));
    if (rnd() % 4 == 0) harsh_into(pdata, 16 * (size_t)np, 1);
}

/* ---- the plain models (the game's instructions, one IEEE rounding each) ---- */
static uint32_t rsqrte(uint32_t bits) { return kc::frsqrte_bits(bits); }   /* (checked against the pseudocode) */
static float rsq(float L) {   /* FRSQRTE, x*x, FRSQRTS (one rounding of 3 - L*e, halved), x*step */
    if (!(L > 0.0f)) return 0.0f;
    const float v = bf(rsqrte(fb(L))), e = v * v;
    const float s = (float)(3.0 - (double)L * (double)e) * 0.5f;
    return v * s;
}
static float fmin_arm(float a, float b) { return a < b ? a : b < a ? b : bf(fb(a) | fb(b)); }   /* (no NaN here) */
/* (lane 3: the Arm's FADD / FSUB / FMUL, cloth_wlane.hpp; the flags are not modelled) */
static float wadd(float a, float b, int fz) { uint32_t f = 0; return bf(cw::arm_add(fb(a), fb(b), (uint32_t)fz, f)); }
static float wsub(float a, float b, int fz) { uint32_t f = 0; return bf(cw::arm_sub(fb(a), fb(b), (uint32_t)fz, f)); }
static float wmul(float a, float b, int fz) { uint32_t f = 0; return bf(cw::arm_mul(fb(a), fb(b), (uint32_t)fz, f)); }
static void model_link(uint8_t *pos, const uint8_t *pd, const uint8_t *r, float s0, int fz) {
    uint16_t ia, ib;
    float A[4], B[4], D[4], SQ[4], rl, k, ima, imb;
    memcpy(&ia, r, 2), memcpy(&ib, r + 2, 2), memcpy(&rl, r + 4, 4), memcpy(&k, r + 8, 4);
    memcpy(B, pos + 16 * ib, 16), memcpy(A, pos + 16 * ia, 16);
    for (int q = 0; q < 3; q++) D[q] = B[q] - A[q], SQ[q] = D[q] * D[q];
    D[3] = wsub(B[3], A[3], fz);
    const float L = (SQ[0] + SQ[1]) + SQ[2], rr = rsq(L), len = L * rr;
    memcpy(&ima, pd + 16 * ia + 4, 4), memcpy(&imb, pd + 16 * ib + 4, 4);
    const float cc = ((len - rl) * k) * s0;
    float A2[4], B2[4];
    for (int q = 0; q < 3; q++) A2[q] = A[q] + (D[q] * rr * cc) * ima;
    A2[3] = wadd(A[3], wmul(wmul(wmul(D[3], rr, fz), cc, fz), ima, fz), fz);
    memcpy(pos + 16 * ia, A2, 16);
    memcpy(B, pos + 16 * ib, 16);   /* (the reload) */
    for (int q = 0; q < 3; q++) B2[q] = B[q] - (D[q] * rr * cc) * imb;
    B2[3] = wsub(B[3], wmul(wmul(wmul(D[3], rr, fz), cc, fz), imb, fz), fz);
    memcpy(pos + 16 * ib, B2, 16);
}
static void model_tether(uint8_t *pos, const uint8_t *r, float s0, int fz) {
    uint16_t i, j;
    float Pi[4], Pj[4], D[4], rest, stiff;
    memcpy(&i, r, 2), memcpy(&j, r + 2, 2), memcpy(&rest, r + 4, 4), memcpy(&stiff, r + 8, 4);
    memcpy(Pj, pos + 16 * j, 16), memcpy(Pi, pos + 16 * i, 16);
    for (int q = 0; q < 3; q++) D[q] = Pj[q] - Pi[q];
    D[3] = wsub(Pj[3], Pi[3], fz);
    const float L = (D[0] * D[0] + D[1] * D[1]) + D[2] * D[2], rr = rsq(L), len = L * rr;
    const float g = (stiff * fmin_arm(rest - len, 0.0f)) * s0;
    for (int q = 0; q < 3; q++) Pj[q] = Pj[q] + (D[q] * rr) * g;
    Pj[3] = wadd(Pj[3], wmul(wmul(D[3], rr, fz), g, fz), fz);
    memcpy(pos + 16 * j, Pj, 16);
}

/* ---- the link, bend link and tether kernels (loops, written from a spec: no scalar twin, no why function) ---- */
static int side_of(void) { return 3 + (int)(rnd() % 30); }
enum { L_LINK, L_TETHER, L_BEND_LINK };
static long loop_calls[3], loop_whole[3], loop_split[3], loop_records[3], loop_done[3];
/* the interface: records done from the first (every one on plain cloth, lane 3 garbage or not), a reason exactly when it
   stopped (the call's own, ENTRY, only with none done), every changed slot within the range reported */
static void loop_check(int k, uint32_t n, uint32_t done, int why, int all, int entry, uint32_t lo, uint32_t hi, int plain,
                       int side) {
    CHECK(done <= n && (done == n) == (why == all));
    if (why == entry) CHECK(done == 0);
    if (plain) CHECK(done == n);
    for (int s = 0; s < side * side; s++)
        if (memcmp(posA + 16 * s, pos0 + 16 * s, 16)) CHECK(done && (uint32_t)s >= lo && (uint32_t)s <= hi);
    loop_calls[k]++, loop_whole[k] += done == n, loop_records[k] += n, loop_done[k] += done;
}
static void loop_dig(uint32_t done, uint32_t fin, uint32_t fpsr, int why, int side) {
    const uint32_t raised = done ? fin | fpsr : fin;   /* (a flag already set may be reported or not) */
    dig(&done, 4), dig(&raised, 4), dig(&why, 4), dig(posA, 16 * (size_t)side * (size_t)side);
}

static void t_link(void) {
    const int side = side_of(), edge = rnd() % 4 == 0;
    sheet(side, 0.05f + unit() * 2.0f);
    uint32_t n = 0;
    for (int i = 0; i < side * side && n < 4000; i++) {
        const int nb[2] = {i % side + 1 < side ? i + 1 : -1, i + side < side * side ? i + side : -1};
        for (int k = 0; k < 2; k++) if (nb[k] >= 0) {
            uint8_t *r = rec + 12 * n++;
            const uint16_t a = (uint16_t)i, b = (uint16_t)nb[k];
            const float rl = (rnd() & 7) ? 0.9f + 0.2f * unit() : 0.0f, kk = unit();
            memcpy(r, &a, 2), memcpy(r + 2, &b, 2), memcpy(r + 4, &rl, 4), memcpy(r + 8, &kk, 4);
        }
    }
    if (edge) { if (rnd() & 1) edge_into(rec + 12 * (rnd() % n) + 4, 2); else edge_into(pos0 + 16 * (rnd() % (side * side)), 4); }
    if (harsh) harsh_data(rec, 12 * (size_t)n, side * side);
    ab();
    const float s0 = harsh && rnd() % 8 == 0 ? bf(harsh_value()) : 0.5f + unit();
    ClothLinkPass a;
    memset(&a, 0, sizeof a);
    a.slots = posA, a.particles = pdata, a.links = rec, a.n = n, a.scale = fb(s0), a.fz = (int)(rnd() & 1), a.fpsr_in = fpsr_in();
    ClothLinkPass b = a;
    b.slots = posB;
    cloth_link_pass(&a);
    loop_check(L_LINK, n, a.done, a.why, CLOTH_LINK_ALL, CLOTH_LINK_ENTRY, a.slot_lo, a.slot_hi, !edge && !harsh, side);
    if (a.done == n) {   /* (SPEC.md 5: the last link's indices and stiffness as read, slot b as it stored it) */
        uint16_t ia, ib;
        uint32_t k;
        memcpy(&ia, rec + 12 * (size_t)(n - 1), 2), memcpy(&ib, rec + 12 * (size_t)(n - 1) + 2, 2);
        memcpy(&k, rec + 12 * (size_t)(n - 1) + 8, 4);
        CHECK(a.end.a == ia && a.end.b == ib && a.end.stiffness == k && !memcmp(a.end.pos_b_new, posA + 16 * ib, 16));
    }
    /* the call in two parts (links [0, h), then [h, done) on what the first left): the same bytes and flags as whole, as
       the hook's lifted rest relies on (nothing but memory carried from one link to the next) */
    if (a.done >= 2) {
        const uint32_t h = 1 + rnd() % (a.done - 1);
        b.n = h;
        cloth_link_pass(&b);
        CHECK(b.done == h);
        ClothLinkPass c2 = b;
        c2.links = rec + 12 * (size_t)h, c2.n = a.done - h, c2.done = 0, c2.fpsr = 0;
        cloth_link_pass(&c2);
        CHECK(c2.done == a.done - h && !memcmp(posA, posB, SLOTS) && (a.fpsr_in | a.fpsr) == (a.fpsr_in | b.fpsr | c2.fpsr));
        loop_split[L_LINK]++;
    }
    memcpy(posB, pos0, SLOTS);
    for (uint32_t i = 0; i < a.done; i++) model_link(posB, pdata, rec + 12 * i, s0, a.fz);
    CHECK(!memcmp(posA, posB, SLOTS));
    loop_dig(a.done, a.fpsr_in, a.fpsr, a.why, side);
}

static void t_tether(void) {
    const int side = side_of(), edge = rnd() % 4 == 0;
    sheet(side, 0.05f + unit() * 2.0f);
    uint32_t n = 0;
    for (int i = 1; i < side * side; i++) {
        uint8_t *r = rec + 12 * n++;
        const uint16_t a = (uint16_t)(rnd() % 3), b = (uint16_t)i;
        float d[4], p[4];
        memcpy(d, pos0 + 16 * a, 16), memcpy(p, pos0 + 16 * b, 16);
        const float dist = sqrtf((p[0] - d[0]) * (p[0] - d[0]) + (p[1] - d[1]) * (p[1] - d[1]) + (p[2] - d[2]) * (p[2] - d[2]));
        const float rest = (rnd() & 7) ? dist * (0.4f + unit()) : -0.0f, st = unit();
        memcpy(r, &a, 2), memcpy(r + 2, &b, 2), memcpy(r + 4, &rest, 4), memcpy(r + 8, &st, 4);
    }
    if (edge) edge_into(rec + 12 * (rnd() % n) + 4, 2);
    if (harsh) harsh_data(rec, 12 * (size_t)n, side * side);
    ab();
    const float s0 = harsh && rnd() % 8 == 0 ? bf(harsh_value()) : 0.5f + unit();
    const ClothTetherCall k = {posA, rec, n, fb(s0), (int)(rnd() & 1), fpsr_in()};
    ClothTetherResult a;
    memset(&a, 0, sizeof a);
    cloth_tether_81c88940(&k, &a);
    loop_check(L_TETHER, n, a.done, a.why, CLOTH_TETHER_DONE, -1, a.slot_lo, a.slot_hi, !edge && !harsh, side);
    if (a.done == n) {   /* (SPEC.md 5: the last record's indices and stiffness as read, slot j as it stored it) */
        uint16_t i, j;
        uint32_t st;
        memcpy(&i, rec + 12 * (size_t)(n - 1), 2), memcpy(&j, rec + 12 * (size_t)(n - 1) + 2, 2);
        memcpy(&st, rec + 12 * (size_t)(n - 1) + 8, 4);
        CHECK(a.last.i == i && a.last.j == j && a.last.stiffness == st && !memcmp(a.last.moved, posA + 16 * j, 16));
    }
    if (a.done >= 2) {   /* (in two parts: the same bytes and flags as whole) */
        const uint32_t h = 1 + rnd() % (a.done - 1);
        ClothTetherCall k1 = k, k2 = k;
        ClothTetherResult b, c2;
        k1.slots = posB, k1.count = h;
        cloth_tether_81c88940(&k1, &b);
        CHECK(b.done == h);
        k2.slots = posB, k2.records = rec + 12 * (size_t)h, k2.count = a.done - h;
        cloth_tether_81c88940(&k2, &c2);
        CHECK(c2.done == a.done - h && !memcmp(posA, posB, SLOTS) && (k.fpsr | a.fpsr) == (k.fpsr | b.fpsr | c2.fpsr));
        loop_split[L_TETHER]++;
    }
    memcpy(posB, pos0, SLOTS);
    for (uint32_t i = 0; i < a.done; i++) model_tether(posB, rec + 12 * i, s0, k.fz);
    CHECK(!memcmp(posA, posB, SLOTS));
    loop_dig(a.done, k.fpsr, a.fpsr, a.why, side);
}

static void t_bend_link(void) {
    const int side = side_of(), edge = rnd() % 4 == 0;
    sheet(side, 0.05f + unit() * 2.0f);
    uint32_t n = 0;
    for (int i = 0; i < side * side; i++) {
        const int nb[2] = {i % side + 2 < side ? i + 2 : -1, i + 2 * side < side * side ? i + 2 * side : -1};
        for (int k = 0; k < 2; k++) if (nb[k] >= 0) {
            uint8_t *r = rec + 20 * n++;
            const uint16_t a = (uint16_t)i, b = (uint16_t)nb[k];
            const float lo = 0.5f + unit(), hi = lo + unit(), klo = unit(), khi = unit();
            memcpy(r, &a, 2), memcpy(r + 2, &b, 2), memcpy(r + 4, &lo, 4), memcpy(r + 8, &hi, 4), memcpy(r + 12, &klo, 4), memcpy(r + 16, &khi, 4);
        }
    }
    if (!n) return;
    if (edge) edge_into(rec + 20 * (rnd() % n) + 4, 4);
    if (harsh) harsh_data(rec, 20 * (size_t)n, side * side);
    ab();
    ClothBendLinkPass a;
    memset(&a, 0, sizeof a);   /* (the clamp pair +0, +0, as the game has it) */
    a.slots = posA, a.particles = pdata, a.links = rec, a.n = n, a.scale = fb(0.5f + unit()), a.fz = (int)(rnd() & 1);
    a.fpsr_in = fpsr_in();
    if (harsh && rnd() % 8 == 0) a.scale = harsh_value();
    if (harsh && rnd() % 8 == 0) a.clamp[rnd() & 1] = harsh_value();
    ClothBendLinkPass b = a;
    b.slots = posB;
    cloth_bend_link_pass(&a);
    loop_check(L_BEND_LINK, n, a.done, a.why, CLOTH_BEND_LINK_ALL, CLOTH_BEND_LINK_ENTRY, a.slot_lo, a.slot_hi, !edge && !harsh,
               side);
    if (a.done == n) {   /* (SPEC.md 5: the last link's indices and stiffnesses as read, slot p2 as it stored it) */
        const uint8_t *r = rec + 20 * (size_t)(n - 1);
        uint16_t p1, p2;
        uint32_t klo, khi;
        memcpy(&p1, r, 2), memcpy(&p2, r + 2, 2), memcpy(&klo, r + 12, 4), memcpy(&khi, r + 16, 4);
        CHECK(a.end.p1 == p1 && a.end.p2 == p2 && a.end.k_lo == klo && a.end.k_hi == khi && !memcmp(a.end.p2_new, posA + 16 * p2, 16));
    }
    if (a.done >= 2) {   /* (in two parts: the same bytes and flags as whole) */
        const uint32_t h = 1 + rnd() % (a.done - 1);
        b.n = h;
        cloth_bend_link_pass(&b);
        CHECK(b.done == h);
        ClothBendLinkPass c2 = b;
        c2.links = rec + 20 * (size_t)h, c2.n = a.done - h, c2.done = 0, c2.fpsr = 0;
        cloth_bend_link_pass(&c2);
        CHECK(c2.done == a.done - h && !memcmp(posA, posB, SLOTS) && (a.fpsr_in | a.fpsr) == (a.fpsr_in | b.fpsr | c2.fpsr));
        loop_split[L_BEND_LINK]++;
    }
    loop_dig(a.done, a.fpsr_in, a.fpsr, a.why, side);
}

static long bend_records, bend_done, bend_split, anchor_calls, anchor_declined, anchor_records;
static void t_bend(void) {
    const int side = side_of(), edge = rnd() % 4 == 0;
    sheet(side, 0.05f + unit() * 2.0f);
    uint32_t n = 0;
    for (int y = 0; y + 1 < side; y++)
        for (int x = 0; x + 1 < side; x++) {
            uint8_t *r = rec + 32 * n++;
            const uint16_t s[4] = {(uint16_t)(y * side + x), (uint16_t)((y + 1) * side + x + 1), (uint16_t)(y * side + x + 1),
                                   (uint16_t)((y + 1) * side + x)};   /* (a, d, b, c) */
            float w[6];
            for (int q = 0; q < 6; q++) w[q] = sym();
            memcpy(r, w, 24), memcpy(r + 24, s, 8);
        }
    if (edge) edge_into(rec + 32 * (rnd() % n), 6);
    if (harsh) harsh_data(rec, 32 * (size_t)n, side * side);
    ab();
    float k = harsh && rnd() % 8 == 0 ? bf(harsh_value()) : unit();
    ClothBendPass a, b;
    memset(&a, 0, sizeof a);
    a.slots = posA, a.particles = pdata, a.records = rec, a.scale = (const uint8_t *)&k, a.n = n, a.fz = (int)(rnd() & 1);
    a.fpsr_in = fpsr_in();
    a.threshold[0] = a.threshold[1] = fb(unit() * 0.1f), a.numerator[0] = a.numerator[1] = fb(1.0f);
    if (harsh && rnd() % 8 == 0) (rnd() & 1 ? a.threshold : rnd() & 1 ? a.numerator : a.alt_stiffness)[rnd() & 1] = harsh_value();
    b = a, b.slots = posB;
    cloth_bend_pass(&a);
    /* (the interface: a count of records done, a reason when it stopped, every changed slot in the range reported) */
    CHECK(a.done <= n && (a.done == n) == (a.why == CLOTH_BEND_ALL));
    CHECK(a.why == CLOTH_BEND_ALL || a.why == CLOTH_BEND_CLASS || a.why == CLOTH_BEND_STEP || (a.why == CLOTH_BEND_ENTRY && !a.done));
    for (int s = 0; s < side * side; s++)
        if (memcmp(posA + 16 * s, pos0 + 16 * s, 16)) CHECK(a.done && (uint32_t)s >= a.slot_lo && (uint32_t)s <= a.slot_hi);
    /* the same call in two parts (records [0, h), then [h, done) on what the first left): the same bytes and flags, as
       the hook's lifted rest relies on (nothing but memory carried from one record to the next) */
    if (a.done >= 2) {
        const uint32_t h = 1 + rnd() % (a.done - 1);
        b.n = h;
        cloth_bend_pass(&b);
        CHECK(b.done == h);
        ClothBendPass c2 = b;
        c2.records = rec + 32 * (size_t)h, c2.n = a.done - h, c2.done = 0, c2.fpsr = 0;
        cloth_bend_pass(&c2);
        CHECK(c2.done == a.done - h && !memcmp(posA, posB, SLOTS) && (a.fpsr_in | a.fpsr) == (a.fpsr_in | b.fpsr | c2.fpsr));
        bend_split++;
    }
    const uint32_t raised = a.done ? a.fpsr_in | a.fpsr : a.fpsr_in;
    dig(&a.done, 4), dig(&raised, 4), dig(&a.why, 4), dig(posA, 16 * side * side);
    bend_records += n, bend_done += a.done;
}

/* the anchor kernel's guest memory: an arena at GB (the constraint, the factor, the scale, the skin, the records, the
   constants, the slots' whole 1 MB reach, the vertices' whole reach) and the page of the constants' cell */
#define GB 0x10000000ull
enum { AR_OBJ = 0x0, AR_X4 = 0x100, AR_SCALE = 0x200, AR_SKIN = 0x300, AR_REC = 0x1000, AR_CONST = 0x8000, AR_SLOTS = 0x100000,
       AR_VERTS = 0x200000, AR_TOP = 0x400000 };
static uint8_t *arena, cpage[0x1000];
static void *grange(uint64_t a, uint64_t n) {
    const uint64_t page = CLOTH_ANCHOR_CONSTANTS_CELL & ~0xfffull;
    if (a >= page && a - page < sizeof cpage) return n <= sizeof cpage - (a - page) ? cpage + (a - page) : NULL;
    return a >= GB && a - GB < AR_TOP && n <= AR_TOP - (a - GB) ? arena + (a - GB) : NULL;
}
/* under FZ, lane 3 of the particles, of the skin's columns and of eps as small normals (2^-126 .. 2^-100, powers of two,
   just above 2^-126; in half the trials most of them within a few units of one of two values, so that their differences
   are tiny too): a product, sum or difference that is tiny is a zero on the Arm, and the kernel must store that */
static int snw;
static uint32_t snb[2];
static uint32_t sn_value(void) {
    const uint32_t s = rnd() & 0x80000000u, k = rnd() % 16;
    if (snb[0] && k < 8) { const uint32_t b = snb[rnd() & 1]; return (b & 0x80000000u) | ((b & 0x7fffffffu) + rnd() % 7 - 3); }
    if (k < 11) return s | ((1u + rnd() % 27u) << 23) | (rnd() & 0x7fffffu);
    if (k < 13) return s | ((1u + rnd() % 27u) << 23);
    if (k < 15) return s | (0x00800001u + rnd() % 0x10000u);
    return rnd() % 2 ? cw::DEFAULT_NAN : 0u;
}
static void t_anchor(void) {
    const int side = side_of(), edge = rnd() % 4 == 0, s20 = (int)(rnd() & 1), scaled = (int)(rnd() & 1);
    sheet(side, 0.05f + unit() * 2.0f);
    snw = !harsh && rnd() % 3 == 0;   /* (the values up to 2^-106: one unit apart is tiny) */
    snb[0] = snw && rnd() % 2 ? (rnd() & 0x80000000u) | (0x00800010u + rnd() % 0x0a800000u) : 0u;
    snb[1] = snb[0] ? (rnd() & 0x80000000u) | (0x00800010u + rnd() % 0x0a800000u) : 0u;
    if (snw)
        for (int i = 0; i < side * side; i++) { const uint32_t w = sn_value(); memcpy(pos0 + 16 * i + 12, &w, 4); }
    const uint32_t stride = 16 + 4 * (rnd() % 4), n = (uint32_t)(side * side);
    const float ang = sym() * 3.0f, sc = 0.5f + unit(), ca = cosf(ang) * sc, sa = sinf(ang) * sc;
    const float m[16] = {ca, sa, 0.0f, 0.0f, -sa, ca, 0.0f, 0.0f, 0.0f, 0.0f, sc, 0.0f, sym() * 10.0f, sym() * 10.0f, sym(), 1.0f};
    for (uint32_t i = 0; i < n; i++) {   /* (vertices the skin maps near the particles: the sheet's, inverse-transformed, moved) */
        float p[4], v[4];
        memcpy(p, pos0 + 16 * i, 16);
        const float x = p[0] - m[12], y = p[1] - m[13], z = p[2] - m[14];
        v[0] = (ca * x + sa * y) / (sc * sc) + sym() * 0.3f, v[1] = (-sa * x + ca * y) / (sc * sc) + sym() * 0.3f, v[2] = z / sc, v[3] = 1.0f;
        memcpy(verts + stride * i, v, 16);
        uint8_t *r = rec + (s20 ? 20 : 16) * i;
        const uint16_t pi = (uint16_t)i, vi = (uint16_t)i;
        const float rad = unit() * 0.4f, st = unit();
        memset(r, 0, s20 ? 20 : 16);
        memcpy(r, &pi, 2), memcpy(r + 2, &vi, 2), memcpy(r + 4, &rad, 4);
        if (s20) memcpy(r + 16, &st, 4);   /* (the 20-byte records' stiffness) */
    }
    if (edge) edge_into(rec + (s20 ? 20 : 16) * (rnd() % n) + 4, 1);
    if (harsh) harsh_data(rec, (s20 ? 20 : 16) * (size_t)n, side * side);
    if (harsh && rnd() % 4 == 0) harsh_into(verts, stride * (size_t)n, 1);
    /* the call in a guest memory of its own (the kernel reads everything through the range resolver) */
    const uint32_t size = s20 ? 20 : 16;
    memset(arena, 0, AR_TOP), memset(cpage, 0, sizeof cpage);
    memcpy(arena + AR_SLOTS, pos0, 16 * (size_t)n), memcpy(arena + AR_REC, rec, size * (size_t)n);
    memcpy(arena + AR_VERTS, verts, stride * (size_t)n);
    const uint64_t cst = GB + AR_CONST;
    memcpy(cpage + (CLOTH_ANCHOR_CONSTANTS_CELL & 0xfff), &cst, 8);
    uint32_t mm[16], eps[4] = {0x34800000u, 0x34800000u, 0x34800000u, 0x34800000u};   /* (a small value, read from memory) */
    for (int q = 0; q < 16; q++) mm[q] = fb(m[q]);
    uint32_t s0 = fb(0.25f + unit()), sx = fb(0.5f + unit());
    if (wgarb && rnd() % 2) {   /* (lane 3 of the skin's matrix and of eps: not held to the class either) */
        for (int q = 3; q < 16; q += 4) if (rnd() % 2) mm[q] = wgarb_value();
        if (rnd() % 2) eps[3] = wgarb_value();
    }
    if (harsh && rnd() % 8 == 0) {
        const uint32_t v = harsh_value();
        switch (rnd() % 4) { case 0: mm[rnd() % 16] = v; break; case 1: eps[rnd() % 4] = v; break; case 2: s0 = v; break; default: sx = v; }
    }
    if (snw) {   /* (in most of them the columns' lane 3 too: lane 3 off the class for the whole call) */
        if (rnd() % 10 < 7)
            for (int q = 3; q < 16; q += 4) mm[q] = sn_value();
        eps[3] = rnd() % 2 ? sn_value() : 0u;
    }
    const uint64_t recs = GB + AR_REC, nn = n;
    memcpy(arena + AR_OBJ + (s20 ? 0x38 : 0x28), &recs, 8), memcpy(arena + AR_OBJ + (s20 ? 0x40 : 0x30), &nn, 4);
    const uint32_t one = fb(1.0f);
    memcpy(arena + AR_OBJ + 0x6c, &one, 4), memcpy(arena + AR_X4, &s0, 4);
    const uint64_t vb = GB + AR_VERTS;
    memcpy(arena + AR_SKIN + 0x18, &vb, 8), arena[AR_SKIN + 0x24] = (uint8_t)stride, memcpy(arena + AR_SKIN + 0x90, mm, 64);
    memcpy(arena + AR_CONST + 0x270, eps, 16);
    memcpy(arena + AR_SCALE + 0x18, &sx, 4), memcpy(arena + AR_SCALE + 0x1c, &one, 4), memcpy(arena + AR_SCALE + 0x20, &one, 4);
    const int fz = snw ? 1 : (int)(rnd() & 1);
    const uint32_t fpsr = snw && rnd() % 4 ? 0x9fu : fpsr_in();
    ClothAnchorCall call = {GB + AR_OBJ, GB + AR_SLOTS, GB + AR_SKIN, GB + AR_X4, scaled ? GB + AR_SCALE : 0, fz, fpsr, grange};
    const int bad = rnd() % 16 == 0 ? 1 + (int)(rnd() % 2) : 0;   /* (1: a negative count; 2: the vertices not mapped) */
    if (bad == 1) { const int32_t neg = -1 - (int32_t)(rnd() % 100); memcpy(arena + AR_OBJ + (s20 ? 0x40 : 0x30), &neg, 4); }
    if (bad == 2) { const uint64_t far = GB + AR_TOP - 8; memcpy(arena + AR_SKIN + 0x18, &far, 8); }
    memcpy(posB, arena + AR_SLOTS, SLOTS);
    static uint8_t named[0x10000];   /* (the slots the records name: the only ones the routine writes) */
    memset(named, 0, sizeof named);
    for (uint32_t i = 0; i < n; i++) { uint16_t p; memcpy(&p, arena + AR_REC + size * i, 2); named[p] = 1; }
    ClothAnchorResult r;
    memset(&r, 0, sizeof r);
    const int done = cloth_anchor_81c7bd00(&call, &r);
    anchor_calls++;
    if (!done) {   /* declined: nothing written, a reason */
        anchor_declined++;
        CHECK(!memcmp(posB, arena + AR_SLOTS, SLOTS) && r.why > CLOTH_ANCHOR_DONE && r.why <= CLOTH_ANCHOR_OTHER);
        CHECK(bad || harsh);
        if (bad == 1) CHECK(r.why == CLOTH_ANCHOR_COUNT);
        if (bad == 2) CHECK(r.why == CLOTH_ANCHOR_RANGE);
        dig(&r.why, 4);
        return;
    }
    CHECK(!bad && r.why == CLOTH_ANCHOR_DONE && r.records == n);
    for (uint32_t s = 0; s < 0x10000u; s++)
        if (memcmp(posB + 16 * s, arena + AR_SLOTS + 16 * s, 16)) CHECK(named[s] && s >= r.slot_lo && s <= r.slot_hi);
    anchor_records += n;
    const uint32_t raised = call.fpsr | r.fpsr;
    dig(&raised, 4), dig(&r.records, 4);
    for (uint32_t s = 0; s < 0x10000u; s++)
        if (named[s]) dig(arena + AR_SLOTS + 16 * s, 16);
}

/* one record (particle 0 at vertex 0, a 16-byte record, strength and factor 1, no scale) under FZ: the slot the kernel
   leaves */
static void anchor_one(const uint32_t P[4], const uint32_t V[4], const uint32_t cols[16], const uint32_t eps[4], uint32_t radius,
                       const uint32_t lims[2], uint32_t fpsr, uint32_t out[4]) {
    memset(arena, 0, AR_TOP), memset(cpage, 0, sizeof cpage);
    memcpy(arena + AR_SLOTS, P, 16), memcpy(arena + AR_VERTS, V, 16), memcpy(arena + AR_REC + 4, &radius, 4);
    const uint64_t cst = GB + AR_CONST, recs = GB + AR_REC, vb = GB + AR_VERTS;
    const uint32_t n1 = 1, one = fb(1.0f);
    memcpy(cpage + (CLOTH_ANCHOR_CONSTANTS_CELL & 0xfff), &cst, 8);
    memcpy(arena + AR_CONST + 0x270, eps, 16), memcpy(arena + AR_CONST + 0x50, lims, 8);
    memcpy(arena + AR_OBJ + 0x28, &recs, 8), memcpy(arena + AR_OBJ + 0x30, &n1, 4), memcpy(arena + AR_OBJ + 0x6c, &one, 4);
    memcpy(arena + AR_X4, &one, 4);
    memcpy(arena + AR_SKIN + 0x18, &vb, 8), arena[AR_SKIN + 0x24] = 16, memcpy(arena + AR_SKIN + 0x90, cols, 64);
    ClothAnchorCall call = {GB + AR_OBJ, GB + AR_SLOTS, GB + AR_SKIN, GB + AR_X4, 0, 1, fpsr, grange};
    ClothAnchorResult r;
    memset(&r, 0, sizeof r);
    CHECK(cloth_anchor_81c7bd00(&call, &r) == 1 && r.records == 1);
    memcpy(out, arena + AR_SLOTS, 16);
}

/* Made-up records under FZ with a result that is tiny before rounding, and the bits the Arm's rules give them (worked out
   in exact arithmetic, a tiny result a zero; the game's routine in an Arm emulator stores the same): A, a column's lane 3
   2^-120 times the vertex's x 2^-7 (2^-127); C, c0.w * V.x + c1.w * V.y = 2^-128 with every column lane in the class and
   the particle's lane 3 2^-120; D, D.w = 2^-124 times r (about 0.2), then times the coefficient 5; B, lane x's
   c0.x * V.x + c1.x * V.y = 2^-128 with every lane in the class (FPSR without UFC; the stored bytes are held here) */
static void anchor_fixed(void) {
    static const uint32_t EPS[4] = {0x34000000u, 0x34000000u, 0x34000000u, 0u}, HALF[2] = {0x3f000000u, 0x3f000000u},
                          EIGHT[2] = {0x41000000u, 0x41000000u}, R02 = 0x3e4ccccdu;
    static const struct { const char *name; uint32_t P[4], V[4], cols[16], radius; const uint32_t *lims; uint32_t fpsr, want[4]; } F[] = {
        {"A", {0x3e9d999au, 0x3e800000u, 0x3f000000u, 0x08800000u}, {0x3c000000u, 0x3e800000u, 0x3f000000u, 0x3f800000u},
         {0x3f800000u, 0, 0, 0x03800000u, 0, 0x3f800000u, 0, 0, 0, 0, 0x3f800000u, 0, 0, 0, 0, 0x03c00000u}, R02, HALF, 0x9fu,
         {0x3e54cd28u, 0x3e7ffffdu, 0x3effffffu, 0x082acaf8u}},
        {"C", {0x3e99999au, 0x3e800000u, 0x3f000000u, 0x03800000u}, {0x20400000u, 0xa0200000u, 0x3f000000u, 0x3f800000u},
         {0x3f800000u, 0, 0, 0x20000000u, 0, 0x3f800000u, 0, 0x20000000u, 0, 0, 0x3f800000u, 0, 0, 0, 0, 0}, R02, HALF, 0x9fu,
         {0x3e1d55deu, 0x3e031ce3u, 0x3efffffeu, 0x03031ce6u}},
        {"D", {0x40a04000u, 0x3e800000u, 0x3f000000u, 0x01800000u}, {0x3c000000u, 0x3e800000u, 0x3f000000u, 0x3f800000u},
         {0x3f800000u, 0, 0, 0, 0, 0x3f800000u, 0, 0, 0, 0, 0x3f800000u, 0, 0, 0, 0, 0}, 0x41200000u, EIGHT, 0x9fu,
         {0x41202000u, 0x3e800004u, 0x3f000002u, 0x01800000u}},
        {"B", {0x3f800000u, 0x3f000000u, 0x3f000000u, 0x3f800000u}, {0x20400000u, 0xa0200000u, 0x3f000000u, 0x3f800000u},
         {0x20000000u, 0, 0, 0, 0x20000000u, 0x3f800000u, 0, 0, 0, 0, 0x3f800000u, 0, 0, 0, 0, 0x3f800000u}, R02, HALF, 0x10u,
         {0x3e3730c4u, 0x3db730bcu, 0x3efffffdu, 0x3f800000u}},
    };
    for (unsigned i = 0; i < sizeof F / sizeof F[0]; i++) {
        uint32_t out[4];
        anchor_one(F[i].P, F[i].V, F[i].cols, EPS, F[i].radius, F[i].lims, F[i].fpsr, out);
        if (memcmp(out, F[i].want, 16)) {
            fprintf(stderr, "FAIL: anchor record %s: stored %08x %08x %08x %08x, the Arm's %08x %08x %08x %08x\n", F[i].name,
                    out[0], out[1], out[2], out[3], F[i].want[0], F[i].want[1], F[i].want[2], F[i].want[3]);
            exit(1);
        }
    }
}

int main(int argc, char **argv) {
    pos0 = (uint8_t *)calloc(1, SLOTS), posA = (uint8_t *)calloc(1, SLOTS), posB = (uint8_t *)calloc(1, SLOTS);
    pdata = (uint8_t *)calloc(1, SLOTS), verts = (uint8_t *)calloc(1, 64 * SLOTS / 16), arena = (uint8_t *)calloc(1, AR_TOP);
    if (!pos0 || !posA || !posB || !pdata || !verts || !arena) return 2;
    if (argc > 1 && !strcmp(argv[1], "rsqrte")) {   /* every (parity, top 8 bits) at a few exponents, random low bits */
        static const uint32_t ex[] = {1, 2, 3, 63, 64, 126, 127, 128, 200, 253, 254};
        for (unsigned e = 0; e < sizeof ex / sizeof ex[0]; e++)
            for (uint32_t t = 0; t < 256; t++) {
                const uint32_t u = (ex[e] << 23) | (t << 15) | (rnd() & 0x7fff);
                printf("%08x %08x\n", u, kc::frsqrte_bits(u));
            }
        return 0;
    }
    const int trials = argc > 1 ? atoi(argv[1]) : 300;
    anchor_fixed();
    for (trial = 0; trial < trials; trial++) t_link(), t_tether(), t_bend_link(), t_bend(), t_anchor();
    harsh = 1;
    for (trial = 0; trial < trials; trial++) t_link(), t_tether(), t_bend_link(), t_bend(), t_anchor();
    /* (each loop kernel stopped part way or declined somewhere, and was split in two somewhere) */
    for (int k = 0; k < 3; k++)
        if (loop_whole[k] == loop_calls[k] || !loop_split[k]) { fprintf(stderr, "FAIL: loop kernel %d never stopped or split\n", k); return 1; }
    printf("loops");
    for (int k = 0; k < 3; k++)
        printf(" %ld %ld %ld %ld %ld", loop_calls[k], loop_whole[k], loop_split[k], loop_records[k], loop_done[k]);
    printf("\n");
    printf("whole %ld %ld %ld %ld %ld %ld\n", bend_records, bend_done, bend_split, anchor_calls, anchor_declined, anchor_records);
    printf("digest %016llx\n", (unsigned long long)digest);
    return 0;
}
'''


def recip_sqrt_estimate(a):
    """The Arm pseudocode's RecipSqrtEstimate (no FEAT_RPRES): 128 <= a < 512 -> 256 <= r < 512"""
    if a < 256:
        a = a * 2 + 1
    else:
        a = (a >> 1) << 1
        a = (a + 1) * 2
    b = 512
    while a * (b + 1) * (b + 1) < (1 << 28):
        b += 1
    return (b + 1) // 2


def frsqrte_single(u):
    """FRSQRTE of a positive normal single, as the Arm pseudocode's FPRSqrtEstimate computes it"""
    exp, frac = u >> 23, u & 0x7fffff
    scaled = (256 | (frac >> 15)) if exp % 2 == 0 else (128 | (frac >> 16))
    est = recip_sqrt_estimate(scaled)
    return (((380 - exp) // 2) << 23) | ((est & 0xff) << 15)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default=os.environ.get("CXX", "c++"))
    p.add_argument("--trials", type=int, default=300)
    a = p.parse_args()
    cxx = parse_compiler(a.cxx)
    if not cxx or not shutil.which(cxx[0]):
        print("SKIP: no C++ compiler")
        return
    for k in KERNELS:   # (the kernels' guard against fusing, by text: a fused multiply-add rounds once where the game rounds twice)
        t = (ROOT / "game" / (k + ".cpp")).read_text(encoding="utf-8") + (ROOT / "game" / "cloth_common.hpp").read_text(encoding="utf-8")
        assert "#pragma clang fp contract(off)" in t and "fp-contract=off" in t, k + ": the fp contract pragmas are gone"
    with tempfile.TemporaryDirectory(prefix="cloth-kernels-") as tmp:
        tmp = Path(tmp)
        (tmp / "fixture.cpp").write_text(FIXTURE, encoding="utf-8")
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
            srcs = [str(ROOT / "game" / (k + ".cpp")) for k in KERNELS] + [str(tmp / "fixture.cpp")]
            subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "game")] + flags + srcs + ["-o", str(exe)], check=True)
            if not digests:
                out = subprocess.run([str(exe), "rsqrte"], capture_output=True, text=True, check=True).stdout.split("\n")
                rows = [line.split() for line in out if line.strip()]
                for u, e1 in rows:
                    want = frsqrte_single(int(u, 16))
                    if int(e1, 16) != want:
                        raise SystemExit("FAIL: FRSQRTE of %s: the kernels' %s, the Arm pseudocode %08x" % (u, e1, want))
                print("PASS: FRSQRTE: the kernels' estimate (cloth_common.hpp's) gives the Arm pseudocode's on %d inputs (every "
                      "table entry at 11 exponents)" % len(rows))
            r = subprocess.run([str(exe), str(a.trials)], capture_output=True, text=True)
            if r.returncode:
                raise SystemExit("FAIL (%s):\n%s%s" % (name, r.stdout, r.stderr))
            loops = [line for line in r.stdout.splitlines() if line.startswith("loops ")][0].split()[1:]
            digests[name] = [line for line in r.stdout.splitlines() if line.startswith("digest")][0]
            whole = [line for line in r.stdout.splitlines() if line.startswith("whole ")][0].split()[1:]
            print("PASS: %s: the link, tether and bend link kernels keep their interfaces on %d plain and %d harsh trials of "
                  "each (every record done on plain cloth; the slots changed within the range reported; a reason when they "
                  "stop; the end values of the last record; split in two the same as whole), the link and tether kernels as "
                  "their models: %s" % (name, a.trials, a.trials, "; ".join(
                      "%s %s calls, %s whole, %s split, records %s of %s done" % (k, *loops[5 * i:5 * i + 3], loops[5 * i + 4],
                                                                                  loops[5 * i + 3])
                      for i, k in enumerate(("link", "tether", "bend link")))))
            print("PASS: %s: the bending pass (%s records, %s done; %s calls split in two the same as whole) and the anchor "
                  "pass (%s calls, %s declined: a negative count or vertices not mapped, nothing written; %s records) keep "
                  "their interfaces: the slots changed within the range reported, the reasons given" % ((name,) + tuple(whole)))
        if len(set(digests.values())) != 1:
            raise SystemExit("FAIL: the builds disagree: %s" % digests)
        print("PASS: every build gives the same bytes (%s)" % ", ".join(digests))


if __name__ == "__main__":
    main()
