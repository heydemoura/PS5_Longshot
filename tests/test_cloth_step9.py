"""HOS_NATIVE_CLOTH's step 9: the capsule collidable's batch kernel f_81b8c000, mode 0 (game/cloth_capsule.cpp), and its sites'
callbacks in game/oot_cloth.c (cloth.capsule at the branch into the batch loop, cloth.capsuleTail at the branch into the
loop of the particles left over, and check mode's cloth.capsuleRecordsEnd and cloth.capsuleEnd at their ways out).

- The kernel (cloth_capsule_batches, cloth_capsule_singles), its vector paths and its lane-by-lane path
  (CAP_FORCE_SLOW), on random batches and particles left over (the game's cloth around a capsule; lane 3 plain, the
  game's (the default NaN, normal and huge values), garbage or, under FZ with frictions up to 3, small normals (the fast
  paths must leave a particle whose lane-3 values become tiny to the lane-by-lane path); and, under FZ, particles on the
  surface of an axis-aligned capsule with a coordinate of exactly zero and their push on that axis tiny (the batch path
  must leave such a lane in contact to the lane-by-lane path); odd values anywhere; FZ on and
  off): the same bytes stored, the same batches and particles done, the same contacts. Against a plain model written
  here (IEEE float operations in the routine's operand order, the first two contacting lanes of a group sharing one
  previous-position store) on the plain trials. By hand: the pair's store (Q of the second lane left as it was, Q' of
  the first taking both), the left-over particles' clamp keeping -0, the default NaN in lane 3 stored as the default
  NaN, a NaN with a payload in lane 3 of a particle in contact stored with its payload (as the Arm does), repeated
  indices stopping a batch before anything of it is stored. Built five ways (this CPU, x86-64-v2, x86-64 = plain SSE2,
  the lane-by-lane path, -ffp-contract=on): one digest.
- The callbacks (game/oot_cloth.c as it is, over a guest window at 64 GB and again just below 2^39):
  - off: both answer 0 and write nothing;
  - on: the batches' callback does every batch, the last included (the kernel's bytes, its pages marked), sets the list
    position past them (x8, the one value the rest of the routine reads) and answers 1, nothing else written; a batch
    the kernel stops at (repeated indices): the batches before it done, x8 / x9 at it, answer 0; the left-over particles'
    callback answers 1 with the kernel's bytes and no register written (the routine's way out reads none), or, the
    kernel stopping at a particle (stood in for: this kernel does every value made here), 0 with x8 / w9 at it; another float mode (FPCR), the slots over each other, over the frame or past the window:
    declined, untouched; FPSR does not gate and is left as it was;
  - check: both callbacks write nothing and answer 0; the batches are compared at the batch loop's way out (its site's
    callback, set by hos_oot_cloth_init) with what stands in for the lifted loop: the list position, FPSR and the slots
    (exact: compared; a byte off or the list position off: a MISMATCH), the left-over particles at the routine's way out
    (FPSR and the slots: a byte off or FPSR off: a MISMATCH).
  A call with no particles left over does not reach the tail's site: its guard in the tables is the ands / b.eq that
  skips the tail loop. The sites' tables and the hooks as the lift lowers them are tested in tests/test_oot_cloth.py.

python -I -B tests/test_cloth_step9.py [--cc cc] [--cxx c++]
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
#include "cloth_capsule.h"
#include "cloth_common.hpp"
#ifndef CAP_FORCE_SLOW   /* (the lane-by-lane build passes it to both files) */
#define CAP_FORCE_SLOW 0
#endif
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
static const uint32_t ODD[] = {0x7fc00000u, 0xffc00000u, 0x7fc12345u, 0x7f800001u, 0x7f800000u, 0xff800000u, 0x00000001u,
                               0x807fffffu, 0x00800000u, 0x80000000u, 0x00000000u, 0x7f7fffffu, 0x5f800000u, 0x1f000000u};
/* lane 3: 0 plain, 1 the game's, 2 garbage, 3 small normals (2^-126 .. 2^-120, either sign: under FZ a product or a
   difference of them is tiny, which the Arm flushes) or now and then the game's */
static uint32_t wval(int m) {
    if (m == 0) return fb(1.0f);
    if (m == 3) return rnd() % 5 ? (rnd() & 0x80000000u) | (0x00800000u + rnd() % 0x03000000u) : rnd() % 2 ? 0x7fc00000u : 0u;
    if (m == 1) { const uint32_t r = rnd() % 10; return r < 6 ? 0x7fc00000u : r < 8 ? fb(unit() * 50.0f - 25.0f) : 0x5e000000u + (rnd() & 0x1ffffff); }
    return rnd() % 3 ? ODD[rnd() % 14] : rnd();
}
enum { NP = 96, NREC = 4 };
static uint8_t pos[16 * NP], prev[16 * NP], pd[16 * NP], recs[32 * NREC], rad[64 * NREC], fric[64 * NREC], tidx[32];
static ClothCapsule K;
static float A[3], B[3];
static int wm, plain, zcls;

/* ---- the plain model (IEEE float, the routine's operand order) --------------------------------------------------------- */
static float frsqrte(float x) { return bf(kc::frsqrte_bits(fb(x))); }
static float rsqrts(float a, float b) { const double p = (double)a * (double)b; return (float)(3.0 - p) * 0.5f; }
static float root(float L) {
    const float r0 = frsqrte(L), r1 = r0 * rsqrts(L, r0 * r0);
    return r1 * rsqrts(L, r1 * r1);
}
static void vget(const uint8_t *p, float v[4]) { memcpy(v, p, 16); }
static void vput(uint8_t *p, const float v[4]) { memcpy(p, v, 16); }
static const float *kf(const uint32_t *u) { return (const float *)(const void *)u; }
/* P' and T f of one contacting particle (C, d: closest point and P - C; r the root) */
static void respond(const float P[4], const float Q[4], const float C[4], const float d[4], float r, float R, float f,
                    float Pn[4], float TF[4], int tail) {
    const float *c = kf(K.centre), *v = kf(K.lin_vel), *w = kf(K.ang_vel), rad = bf(K.radius), dt = bf(K.dt);
    float n[4], S[3], m[3];
    for (int i = 0; i < 4; i++) n[i] = d[i] * r;
    for (int i = 0; i < 3; i++) S[i] = C[i] + n[i] * rad, m[i] = n[i] * (P[i] - S[i]);
    const float dS = tail ? m[2] + (m[0] + m[1]) : (m[0] + m[1]) + m[2];
    const float s = R - dS;
    for (int i = 0; i < 4; i++) Pn[i] = P[i] + n[i] * s;
    const float rx = S[0] - c[0], ry = S[1] - c[1], rz = S[2] - c[2];
    const float V[4] = {v[0] + (w[1] * rz - ry * w[2]), v[1] + (w[2] * rx - rz * w[0]), v[2] + (w[0] * ry - rx * w[1]), v[3] + 0.0f};
    float rel[4];
    for (int i = 0; i < 4; i++) rel[i] = (Pn[i] - Q[i]) - V[i] * dt;
    const float dn = (n[0] * rel[0] + n[1] * rel[1]) + n[2] * rel[2];
    for (int i = 0; i < 4; i++) TF[i] = (rel[i] - n[i] * dn) * f;
}
static void model_batches(uint8_t *ps, uint8_t *qs) {
    const float *a = kf(K.a), *b = kf(K.b), k = bf(K.inv_len2), crad = bf(K.radius);
    const float c7f = bf(K.fz ? 0u : 0x007fffffu);
    for (int i = 0; i < NREC; i++) {
        uint16_t ix[16];
        memcpy(ix, recs + 32 * i, 32);
        float Pn[16][4], Qn[16][4];
        int stp[16] = {0}, stq[16] = {0};
        for (int g = 0; g < 4; g++) {
            int first = -1, seen = 0;
            for (int l = 0; l < 4; l++) {
                const int j = 4 * g + l;
                float P[4], Q[4], C[4], d[4];
                vget(ps + 16 * ix[j], P), vget(qs + 16 * ix[j], Q);
                const float R = kf((const uint32_t *)(const void *)(rad + 64 * i))[j];
                const float f = kf((const uint32_t *)(const void *)(fric + 64 * i))[j];
                const float ab[4] = {b[0] - a[0], b[1] - a[1], b[2] - a[2], b[3] - a[3]};
                const float dot = ab[2] * (P[2] - a[2]) + (ab[0] * (P[0] - a[0]) + ab[1] * (P[1] - a[1]));
                float t = dot * k;
                t = t > 0.0f ? t : 0.0f;
                const float one = kf(K.one)[l];
                t = t < one ? t : one;
                for (int q = 0; q < 4; q++) C[q] = a[q] + ab[q] * t, d[q] = P[q] - C[q];
                const float L = kf(K.eps)[l] + (d[2] * d[2] + (d[0] * d[0] + d[1] * d[1]));
                const float r = c7f >= L ? 0.0f : root(L);
                if (!(((L * r) - crad) - R < 0.0f)) continue;
                float TF[4];
                respond(P, Q, C, d, r, R, f, Pn[j], TF, 0);
                stp[j] = 1;
                if (seen == 0) { first = j, stq[j] = 1; for (int q = 0; q < 4; q++) Qn[j][q] = Q[q] + TF[q]; }
                else if (seen == 1) { for (int q = 0; q < 4; q++) Qn[first][q] = TF[q] + Qn[first][q]; }
                else { stq[j] = 1; for (int q = 0; q < 4; q++) Qn[j][q] = Q[q] + TF[q]; }
                seen++;
            }
        }
        for (int j = 0; j < 16; j++) if (stp[j]) vput(ps + 16 * ix[j], Pn[j]);
        for (int j = 0; j < 16; j++) if (stq[j]) vput(qs + 16 * ix[j], Qn[j]);
    }
}
static void model_singles(uint8_t *ps, uint8_t *qs, int n) {
    const float *a = kf(K.a), *b = kf(K.b), k = bf(K.inv_len2), crad = bf(K.radius);
    const float c7f = bf(K.fz ? 0u : 0x007fffffu);
    for (int i = 0; i < n; i++) {
        uint16_t ix;
        memcpy(&ix, tidx + 2 * i, 2);
        float P[4], Q[4], C[4], d[4];
        vget(ps + 16 * ix, P), vget(qs + 16 * ix, Q);
        float R, f;
        memcpy(&R, pd + 16 * ix + 8, 4), memcpy(&f, pd + 16 * ix + 12, 4);
        const float ab[4] = {b[0] - a[0], b[1] - a[1], b[2] - a[2], b[3] - a[3]};
        const float t0 = ((ab[0] * (P[0] - a[0]) + ab[1] * (P[1] - a[1])) + ab[2] * (P[2] - a[2])) * k;
        const float t = t0 < 0.0f ? 0.0f : t0 > 1.0f ? 1.0f : t0;
        for (int q = 0; q < 4; q++) C[q] = a[q] + ab[q] * t, d[q] = P[q] - C[q];
        const float L = (d[0] * d[0] + d[1] * d[1]) + d[2] * d[2];
        const float r = L <= c7f ? 0.0f : root(L);
        if (!(((L * r) - crad) - R < 0.0f)) continue;
        float Pn[4], TF[4], Qn[4];
        respond(P, Q, C, d, r, R, f, Pn, TF, 1);
        for (int q = 0; q < 4; q++) Qn[q] = Q[q] + TF[q];
        vput(ps + 16 * ix, Pn), vput(qs + 16 * ix, Qn);
    }
}

/* ---- a random call ----------------------------------------------------------------------------------------------------- */
static void vset(uint32_t *u, float x, float y, float z, uint32_t w) { u[0] = fb(x), u[1] = fb(y), u[2] = fb(z), u[3] = w; }
static void make(int odd) {
    wm = plain ? 0 : (int)(rnd() % 4);
    for (int i = 0; i < 3; i++) A[i] = unit() * 4.0f - 2.0f, B[i] = A[i] + unit() * 2.0f - 1.0f;
    const float radius = 0.05f + unit() * 0.3f;
    vset(K.a, A[0], A[1], A[2], plain ? 0 : wval(wm)), vset(K.b, B[0], B[1], B[2], plain ? 0 : wval(wm));
    float len2 = 0;
    for (int i = 0; i < 3; i++) len2 += (B[i] - A[i]) * (B[i] - A[i]);
    K.radius = fb(radius), K.inv_len2 = fb(1.0f / len2), K.dt = fb(1.0f / 60.0f);
    for (int l = 0; l < 4; l++) K.eps[l] = fb(1e-12f), K.one[l] = fb(1.0f);
    vset(K.centre, unit(), unit(), unit(), 0), vset(K.lin_vel, unit() - 0.5f, unit() - 0.5f, unit() - 0.5f, plain ? 0 : wval(wm));
    vset(K.ang_vel, unit() * 4.0f - 2.0f, unit() * 4.0f - 2.0f, unit() * 4.0f - 2.0f, 0);
    K.fz = plain ? 0 : wm == 3 ? 1 : (int)(rnd() & 1);
    for (int p = 0; p < NP; p++) {
        const float t = unit() * 1.4f - 0.2f, dist = (radius + 0.03f) * (0.5f + unit());
        float dir[3] = {unit() - 0.5f, unit() - 0.5f, unit() - 0.5f}, nn = 0;
        for (int i = 0; i < 3; i++) nn += dir[i] * dir[i];
        nn = sqrtf(nn) + 1e-6f;
        uint32_t P[4], Q[4];
        for (int i = 0; i < 3; i++) {
            const float x = A[i] + (B[i] - A[i]) * (t < 0 ? 0 : t > 1 ? 1 : t) + dir[i] / nn * dist;
            P[i] = fb(x), Q[i] = fb(x - (unit() - 0.5f) * 0.02f);
        }
        P[3] = wval(wm), Q[3] = wval(wm);
        memcpy(pos + 16 * p, P, 16), memcpy(prev + 16 * p, Q, 16);
        const float pdv[4] = {0.0f, 1.0f, 0.03f, wm == 3 ? unit() * 3.0f : unit()};   /* (frictions above 1 too) */
        memcpy(pd + 16 * p, pdv, 16);
    }
    for (int i = 0; i < NREC; i++) {
        uint16_t ix[16];
        for (int j = 0; j < 16; j++) {
            ix[j] = (uint16_t)(i * 16 + j);
            const float R = 0.03f, f = wm == 3 ? unit() * 3.0f : unit();
            memcpy(rad + 64 * i + 4 * j, &R, 4), memcpy(fric + 64 * i + 4 * j, &f, 4);
        }
        for (int j = 15; j > 0; j--) { const int s = (int)(rnd() % (unsigned)(j + 1)); const uint16_t x = ix[j]; ix[j] = ix[s], ix[s] = x; }
        memcpy(recs + 32 * i, ix, 32);
    }
    for (int i = 0; i < 16; i++) { const uint16_t x = (uint16_t)(64 + (rnd() % 32)); memcpy(tidx + 2 * i, &x, 2); }
    if (zcls) {
        /* x, y, z values tiny under FZ: the capsule along axis a, its ends' coordinate on axis i a small value T (2^-63 ..
           2^-30); half the particles exactly on its surface (offset from the axis along b alone, by D = 2^k, the radius |D|)
           with coordinate i exactly zero, the particles' radius R = 2^-63 .. 2^-20: the push on axis i, n_i R, is tiny */
        const int ax = (int)(rnd() % 3), bx = (ax + 1 + (int)(rnd() % 2)) % 3, ix = 3 - ax - bx;
        const float T = (rnd() % 2 ? -1.0f : 1.0f) * ldexpf(1.0f, -30 - (int)(rnd() % 34));
        const float D = (rnd() % 2 ? -1.0f : 1.0f) * ldexpf(1.0f, 1 + (int)(rnd() % 20)), R = ldexpf(1.0f, -20 - (int)(rnd() % 44));
        A[ax] = (float)((int)(rnd() % 16) - 8) * 0.25f, B[ax] = A[ax] + (rnd() % 2 ? -1.0f : 1.0f) * ldexpf(1.0f, (int)(rnd() % 4) - 1);
        A[bx] = B[bx] = (float)((int)(rnd() % 16) - 8) * 0.25f, A[ix] = B[ix] = T;
        vset(K.a, A[0], A[1], A[2], 0), vset(K.b, B[0], B[1], B[2], 0);
        K.radius = fb(fabsf(D)), K.inv_len2 = fb(1.0f / ((B[ax] - A[ax]) * (B[ax] - A[ax]))), K.fz = 1, K.lin_vel[3] = 0;
        for (int p = 0; p < NP; p++) {
            uint32_t P[4], Q[4];
            memcpy(P, pos + 16 * p, 16), memcpy(Q, prev + 16 * p, 16);
            if (rnd() % 2) {
                float y[3];
                y[ax] = A[ax] + (B[ax] - A[ax]) * 0.5f, y[bx] = A[bx] + D, y[ix] = 0.0f;
                for (int i = 0; i < 3; i++) P[i] = fb(y[i]), Q[i] = fb(y[i] + (rnd() % 3 ? (unit() - 0.5f) * 0.02f : 0.0f));
                if (rnd() % 4 == 0) Q[ix] = 0;
            } else if (rnd() % 2) {
                P[ix] = 0;
            }
            P[3] = Q[3] = fb(1.0f);
            memcpy(pos + 16 * p, P, 16), memcpy(prev + 16 * p, Q, 16);
            memcpy(pd + 16 * p + 8, &R, 4);
        }
        for (int i = 0; i < 16 * NREC; i++) memcpy(rad + 4 * i, &R, 4);
    }
    if (odd)
        for (int k = 0, n = 1 + (int)(rnd() % 4); k < n; k++) {
            const uint32_t v = ODD[rnd() % 14];
            uint8_t *where = rnd() % 2 ? pos : prev;
            memcpy(where + 4 * (rnd() % (4 * NP)), &v, 4);
        }
}

static ClothCapBatches batches(int n, uint16_t *contacts) {
    ClothCapBatches b;
    memset(&b, 0xa5, sizeof b);
    b.k = &K, b.indices = recs, b.radii = rad, b.frictions = fric, b.pos = pos, b.prev = prev, b.n = (uint32_t)n;
    b.contacts = contacts;
    cloth_capsule_batches(&b);
    CHECK(b.done <= (uint32_t)n && (b.done == (uint32_t)n) == (b.why == CLOTH_CAP_ALL));
    return b;
}
static ClothCapSingles singles(int n) {
    ClothCapSingles s;
    memset(&s, 0xa5, sizeof s);
    s.k = &K, s.indices = tidx, s.pos = pos, s.prev = prev, s.data = pd, s.n = (uint32_t)n;
    cloth_capsule_singles(&s);
    CHECK(s.done <= (uint32_t)n && (s.done == (uint32_t)n) == (s.why == CLOTH_CAP_ALL) && s.contacts <= s.done);
    return s;
}

int main(int argc, char **argv) {
    const int trials = argc > 1 ? atoi(argv[1]) : 1000;
    long rec_done = 0, tail_done = 0, contacts = 0, plain_checked = 0;
    for (trial = 0; trial < trials; trial++) {
        plain = trial % 3 == 0, zcls = !plain && trial % 5 == 2;
        make(!plain && trial % 4 == 1);
        static uint8_t mp[16 * NP], mq[16 * NP];
        memcpy(mp, pos, sizeof mp), memcpy(mq, prev, sizeof mq);
        uint16_t cm[NREC];
        const ClothCapBatches b = batches(NREC, cm);
        const uint32_t ob[2] = {b.done, b.why};
        dig(ob, sizeof ob), dig(cm, 2 * (size_t)b.done);
        const int n = 1 + (int)(rnd() % 15);
        const ClothCapSingles s = singles(n);
        const uint32_t os[3] = {s.done, s.why, s.contacts};
        dig(os, sizeof os);
        dig(pos, sizeof pos), dig(prev, sizeof prev);
        if (plain) {   /* the plain model from the same inputs */
            CHECK(b.done == NREC && s.done == (uint32_t)n);
            model_batches(mp, mq), model_singles(mp, mq, n);
            CHECK(!memcmp(mp, pos, sizeof mp) && !memcmp(mq, prev, sizeof mq));
            for (int i = 0; i < NREC; i++) contacts += __builtin_popcount(cm[i]);
            rec_done += b.done, tail_done += s.done, contacts += s.contacts, plain_checked++;
        }
    }
    /* by hand: the pair's one store; the left-over particles' -0; NaNs in lane 3; repeated indices stop a batch before any store */
    trial = -1, zcls = 0;
    plain = 1, make(0);
    {
        /* a batch whose group 0 has lanes 0 and 1 in contact, the rest far: particles on the axis's middle */
        uint16_t ix[16];
        for (int j = 0; j < 16; j++) ix[j] = (uint16_t)j;
        memcpy(recs, ix, 32);
        for (int j = 0; j < 16; j++) {
            uint32_t P[4];
            const float far = j < 2 ? 0.0f : 5.0f;
            vset(P, (A[0] + B[0]) * 0.5f + far, (A[1] + B[1]) * 0.5f + 0.01f * (float)(j + 1), (A[2] + B[2]) * 0.5f, fb(1.0f));
            memcpy(pos + 16 * j, P, 16);
        }
        static uint8_t q0[16 * NP];
        memcpy(q0, prev, sizeof q0);
        uint16_t cm[1];
        const ClothCapBatches b = batches(1, cm);
        CHECK(b.done == 1 && cm[0] == 3);
        CHECK(!memcmp(prev + 16, q0 + 16, 16) && memcmp(prev, q0, 16));   /* lane 1's Q untouched, lane 0's written */
    }
    {
        /* a particle left over at A (AB . e = -0): the clamp keeps t = -0; the model gives the same bytes */
        plain = 1, make(0);
        vset(K.a, 1.0f, 0.0f, 0.0f, 0), vset(K.b, 2.0f, 0.0f, 0.0f, 0), K.inv_len2 = fb(1.0f), K.radius = fb(10.0f);
        const uint16_t ix = 3;
        memcpy(tidx, &ix, 2);
        uint32_t P[4];
        vset(P, 1.0f, -0.5f, 0.25f, fb(1.0f));
        memcpy(pos + 48, P, 16);
        static uint8_t mp[16 * NP], mq[16 * NP];
        memcpy(mp, pos, sizeof mp), memcpy(mq, prev, sizeof mq);
        const ClothCapSingles s = singles(1);
        CHECK(s.done == 1 && s.contacts == 1);   /* (inside the radius) */
        model_singles(mp, mq, 1);
        CHECK(!memcmp(mp, pos, sizeof mp) && !memcmp(mq, prev, sizeof mq));
    }
    {
        /* lane 3 the default NaN under FZ: stored as the default NaN. A NaN with a payload in lane 3 of a particle in
           contact: stored as the Arm leaves it, the payload kept (a batch or particle the kernel could not give the Arm's
           bits for would be left to the game's code, SPEC.md 6: this kernel does these exactly) */
        plain = 0, make(0);
        wm = 1, K.fz = 1, K.a[3] = K.b[3] = K.lin_vel[3] = 0x7fc00000u;
        for (int p = 0; p < NP; p++) { const uint32_t w = 0x7fc00000u; memcpy(pos + 16 * p + 12, &w, 4), memcpy(prev + 16 * p + 12, &w, 4); }
        static uint8_t p0[16 * NP], q0[16 * NP];
        memcpy(p0, pos, sizeof p0), memcpy(q0, prev, sizeof q0);
        CHECK(batches(NREC, NULL).done == NREC);
        for (int p = 0; p < 64; p++) { uint32_t w; memcpy(&w, pos + 16 * p + 12, 4); CHECK(w == 0x7fc00000u); }
        memcpy(pos, p0, sizeof p0), memcpy(prev, q0, sizeof q0);
        uint16_t ix0;
        memcpy(&ix0, recs + 32 * 2, 2);
        const uint32_t pay = 0x7fc12345u;
        uint32_t P0[4];
        vset(P0, (A[0] + B[0]) * 0.5f, (A[1] + B[1]) * 0.5f + 0.01f, (A[2] + B[2]) * 0.5f, pay);   /* (in contact) */
        memcpy(pos + 16 * ix0, P0, 16);
        uint16_t cm[NREC];
        ClothCapBatches b = batches(NREC, cm);
        uint32_t w;
        memcpy(&w, pos + 16 * ix0 + 12, 4);
        CHECK(b.done == NREC && (cm[2] & 1) && w == pay);
        /* repeated indices: the batch left, nothing of it or after it stored */
        memcpy(pos, p0, sizeof p0), memcpy(prev, q0, sizeof q0);
        memcpy(recs + 32 * 1 + 2, recs + 32 * 1, 2);
        b = batches(NREC, NULL);
        CHECK(b.done == 1 && b.why == CLOTH_CAP_INDEX);
        for (int r = 1; r < NREC; r++) {
            uint16_t ix2[16];
            memcpy(ix2, recs + 32 * r, 32);
            for (int j = 0; j < 16; j++)
                CHECK(!memcmp(pos + 16 * ix2[j], p0 + 16 * ix2[j], 16) && !memcmp(prev + 16 * ix2[j], q0 + 16 * ix2[j], 16));
        }
        /* the same NaN in a particle left over (repeated indices are taken there: each sees the one before) */
        memcpy(pos, p0, sizeof p0), memcpy(prev, q0, sizeof q0);
        uint16_t t3;
        memcpy(&t3, tidx + 2 * 3, 2);
        memcpy(pos + 16 * t3, P0, 16);
        const ClothCapSingles s = singles(8);
        memcpy(&w, pos + 16 * t3 + 12, 4);
        CHECK(s.done == 8 && s.contacts >= 1 && w == pay);
    }
    printf("simd %d\nslow %d\nplain %ld %ld %ld %ld\ndigest %016llx\n", KC_SIMD, CAP_FORCE_SLOW, plain_checked, rec_done,
           tail_done, contacts, (unsigned long long)digest);
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
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include "hos.h"
#define cloth_capsule_singles test_capsule_singles   /* (the glue calls the stand-in below) */
#include "oot_cloth.c"
#undef cloth_capsule_singles
''' + SITE_SET + r'''
/* the left-over particles' kernel as the glue calls it: the kernel itself, or, with singles_stop set, the kernel stopping
   at that particle as it does for a value it cannot give the game's bits for (this kernel does every value the tests
   here make: a stop is stood in for) */
void cloth_capsule_singles(ClothCapSingles *s);
static int singles_stop = -1;
void test_capsule_singles(ClothCapSingles *s) {
    const uint32_t n = s->n;
    if (singles_stop < 0 || (uint32_t)singles_stop >= n) { cloth_capsule_singles(s); return; }
    s->n = (uint32_t)singles_stop;
    if (singles_stop) cloth_capsule_singles(s);
    else s->contacts = 0;
    s->n = n, s->done = (uint32_t)singles_stop, s->why = CLOTH_CAP_VALUES;
}
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
enum { SP = 0x10000, FCELL = 0x2000, RCELL = 0x2100, COLLA = 0x3000, INSTA = 0x4000, PDOBJ = 0x4100, REC = 0x20000,
       TAIL = 0x21000, RADS = 0x22000, FRICS = 0x23000, POS = 0x100000, PREV = 0x180000, PD = 0x200000, NPART = 96,
       NREC = 4 };
static uint8_t *before;

static void setup(void) {
    memset(window, 0, WINDOW), memset(marks, 0, sizeof marks);
    const float A[4] = {0.5f, 1.0f, -0.25f, 0.0f}, B[4] = {1.25f, 1.5f, 0.25f, 0.0f};
    for (int l = 0; l < 4; l++) putf(SP + 0x70 + 4 * l, 1e-12f), putf(SP + 0x80 + 4 * l, 1.0f);
    for (int l = 0; l < 4; l++) putf(SP + 0x2d0 + 4 * l, A[l]), putf(SP + 0x2e0 + 4 * l, B[l]);
    put32(SP + 0x2dc, 0x7fc00000u);
    float len2 = 0;
    for (int l = 0; l < 3; l++) len2 += (B[l] - A[l]) * (B[l] - A[l]);
    putf(SP + 0x300, 0.2f), putf(SP + 0x304, 1.0f / len2), putf(SP + 0x2a0, 1.0f / 60.0f);
    put64(SP + 0x228, G(FCELL)), put64(SP + 0x288, G(RCELL)), put64(FCELL, G(FRICS)), put64(RCELL, G(RADS));
    for (int l = 0; l < 3; l++) putf(COLLA + 0x50 + 4 * l, A[l]), putf(COLLA + 0x70 + 4 * l, unit() - 0.5f), putf(COLLA + 0x80 + 4 * l, unit() * 2.0f - 1.0f);
    put64(INSTA + 0x18, G(PDOBJ)), put64(INSTA + 0x20, G(POS)), put64(INSTA + 0x30, G(PREV)), put64(PDOBJ + 0x40, G(PD));
    for (int p = 0; p < NPART; p++) {
        const float t = unit() * 1.2f - 0.1f, off = (0.2f + 0.03f) * (0.5f + unit());
        for (int l = 0; l < 3; l++) {
            const float x = A[l] + (B[l] - A[l]) * t + (l == 1 ? off : 0.0f);
            putf(POS + 16 * p + 4 * l, x), putf(PREV + 16 * p + 4 * l, x - 0.005f);
        }
        put32(POS + 16 * p + 12, p % 2 ? 0x7fc00000u : 0x3f800000u), put32(PREV + 16 * p + 12, 0x7fc00000u);
        putf(PD + 16 * p + 8, 0.03f), putf(PD + 16 * p + 12, unit());
    }
    for (int i = 0; i < NREC; i++)
        for (int j = 0; j < 16; j++) {
            const uint16_t ix = (uint16_t)((i * 16 + j * 5) % 64);
            memcpy(window + REC + 32 * i + 2 * j, &ix, 2);
            putf(RADS + 64 * i + 4 * j, 0.03f), putf(FRICS + 64 * i + 4 * j, unit());
        }
    for (int j = 0; j < 15; j++) { const uint16_t ix = (uint16_t)(64 + (j * 7) % 32); memcpy(window + TAIL + 2 * j, &ix, 2); }
}
static void base_ctx(Ctx *c) {
    memset(c, 0, sizeof *c);
    for (int r = 0; r < 31; r++) c->x[r] = (uint64_t)rnd() << 32 | rnd();
    for (int r = 0; r < 32; r++) c->vreg[r][0] = (uint64_t)rnd() << 32 | rnd(), c->vreg[r][1] = (uint64_t)rnd() << 32 | rnd();
    c->x[20] = G(COLLA), c->x[31] = G(SP);
    c->n = 1, c->z = 0, c->c = 1, c->v = 1;
    c->fpcr = 1u << 24, c->fpsr = 0x9f;
}
static void rec_ctx(Ctx *c, int n) {
    base_ctx(c);
    c->x[8] = G(REC), c->x[9] = 0, c->x[10] = G(PREV), c->x[11] = G(POS), c->x[12] = 64ull * (uint64_t)n;
}
static void tail_ctx(Ctx *c, int n) {
    base_ctx(c);
    c->x[8] = G(TAIL), c->x[9] = (uint32_t)n, c->x[15] = G(INSTA);
}
static ClothCapsule consts(void) {
    Ctx c;
    base_ctx(&c);
    ClothCapsule k;
    CHECK(cap_consts(&c, &k));
    return k;
}
/* what the batches' callback must leave: the kernel on the window's copy */
static void want_batches(uint8_t *w, int n, uint32_t done) {
    ClothCapsule k = consts();
    ClothCapBatches b;
    b.k = &k, b.indices = w + REC, b.radii = w + RADS, b.frictions = w + FRICS, b.pos = w + POS, b.prev = w + PREV;
    b.n = (uint32_t)n, b.contacts = NULL;
    cloth_capsule_batches(&b);
    CHECK(b.done == done);
}
static uint32_t want_singles(uint8_t *w, int n, uint32_t done) {
    ClothCapsule k = consts();
    ClothCapSingles s;
    s.k = &k, s.indices = w + TAIL, s.pos = w + POS, s.prev = w + PREV, s.data = w + PD, s.n = (uint32_t)n;
    cloth_capsule_singles(&s);
    CHECK(s.done == done);
    return s.contacts;
}

static void run_off(void) {
    setup(), memcpy(before, window, WINDOW);
    Ctx c, c0;
    rec_ctx(&c, NREC), c0 = c;
    CHECK(hos_site_cloth_capsule(&c) == 0 && !memcmp(&c, &c0, sizeof c));
    tail_ctx(&c, 7), c0 = c;
    CHECK(hos_site_cloth_capsule_tail(&c) == 0 && !memcmp(&c, &c0, sizeof c) && !memcmp(before, window, WINDOW));
    at_end("cloth.capsuleRecordsEnd", &c), at_end("cloth.capsuleEnd", &c);
    puts("PASS: off: nothing written, both callbacks answer 0");
}

static void run_on(void) {
    Ctx c, c0;
    for (int n = 1; n <= NREC; n += NREC - 1) {
        setup(), memcpy(before, window, WINDOW), want_batches(before, n, (uint32_t)n);
        rec_ctx(&c, n), c0 = c;
        CHECK(hos_site_cloth_capsule(&c) == 1);
        CHECK(!memcmp(window, before, WINDOW));
        CHECK(c.x[8] == G(REC) + 32ull * (uint64_t)n);
        c.x[8] = c0.x[8];
        CHECK(!memcmp(&c, &c0, sizeof c));
        CHECK(marks[POS >> 12] && marks[PREV >> 12]);
    }
    puts("PASS: on: every batch natively (one, and four), the last included: the list position past them, the callback "
         "answers 1, nothing else changed, pages marked");
    /* batch 2's indices repeat: batches 0 and 1 done, the lifted loop at batch 2 */
    setup();
    memcpy(window + REC + 64 + 2, window + REC + 64, 2);   /* (batch 2's indices repeat) */
    memcpy(before, window, WINDOW), want_batches(before, NREC, 2);
    rec_ctx(&c, NREC), c0 = c;
    CHECK(hos_site_cloth_capsule(&c) == 0 && !memcmp(window, before, WINDOW));
    CHECK(c.x[8] == G(REC) + 64 && c.x[9] == 128);
    c.x[8] = c0.x[8], c.x[9] = c0.x[9];
    CHECK(!memcmp(&c, &c0, sizeof c));
    puts("PASS: on: a batch the kernel stops at: the batches before it done, x8 / x9 at it, the callback answers 0");
    uint32_t touched = 0;
    for (int n = 1; n <= 15; n++) {
        setup(), memcpy(before, window, WINDOW), touched = want_singles(before, n, (uint32_t)n);
        tail_ctx(&c, n), c0 = c;
        CHECK(hos_site_cloth_capsule_tail(&c) == 1);
        CHECK(!memcmp(window, before, WINDOW) && !memcmp(&c, &c0, sizeof c));
    }
    CHECK(touched > 0);   /* (the 15: some in contact) */
    puts("PASS: on: the particles left over, 1..15: the kernel's bytes, no register written, the callback answers 1");
    setup(), memcpy(before, window, WINDOW), want_singles(before, 4, 4);
    singles_stop = 4;
    tail_ctx(&c, 9), c0 = c;
    CHECK(hos_site_cloth_capsule_tail(&c) == 0 && !memcmp(window, before, WINDOW));
    singles_stop = -1;
    CHECK(c.x[8] == G(TAIL) + 8 && c.x[9] == 5);
    c.x[8] = c0.x[8], c.x[9] = c0.x[9];
    CHECK(!memcmp(&c, &c0, sizeof c));
    puts("PASS: on: a particle left over the kernel stops at: the ones before it done, x8 / w9 at it, the callback answers 0");
    /* declines */
    struct { const char *what; uint64_t fpcr; uint64_t prev; } dec[] = {{"default NaN", (1u << 24) | (1u << 25), G(PREV)},
                                                                        {"the slots over each other", 1u << 24, G(POS) + 0x40},
                                                                        {"past the window", 1u << 24, WBASE + WINDOW - 0x100}};
    for (int k = 0; k < 3; k++) {
        setup();
        if (k) put64(INSTA + 0x30, dec[k].prev);
        memcpy(before, window, WINDOW);
        rec_ctx(&c, NREC), c.fpcr = dec[k].fpcr, c.x[10] = dec[k].prev, c0 = c;
        CHECK(hos_site_cloth_capsule(&c) == 0 && !memcmp(&c, &c0, sizeof c) && !memcmp(window, before, WINDOW));
        tail_ctx(&c, 9), c.fpcr = dec[k].fpcr, c0 = c;
        CHECK(hos_site_cloth_capsule_tail(&c) == 0 && !memcmp(&c, &c0, sizeof c) && !memcmp(window, before, WINDOW));
    }
    setup(), memcpy(before, window, WINDOW);
    rec_ctx(&c, NREC), c.x[11] = G(SP) - 0x100, c0 = c;   /* (the slots over the frame) */
    CHECK(hos_site_cloth_capsule(&c) == 0 && !memcmp(&c, &c0, sizeof c) && !memcmp(window, before, WINDOW));
    puts("PASS: on: another float mode, the slots over each other or over the frame, memory past the window: declined, untouched");
    /* FPSR does not gate (the lifted code raises no cumulative flag): any FPSR is answered and left as it was */
    setup(), memcpy(before, window, WINDOW), want_batches(before, NREC, NREC);
    rec_ctx(&c, NREC), c.fpsr = 0x10;
    CHECK(hos_site_cloth_capsule(&c) == 1 && c.fpsr == 0x10 && !memcmp(window, before, WINDOW));
    setup(), memcpy(before, window, WINDOW), want_singles(before, 9, 9);
    tail_ctx(&c, 9), c.fpsr = 0;
    CHECK(hos_site_cloth_capsule_tail(&c) == 1 && c.fpsr == 0 && !memcmp(window, before, WINDOW));
    puts("PASS: on: FPSR does not gate: the batches and the particles left over answered whatever it holds, and it is left as it was");
}

static void run_check(void) {
    Ctx c, c0;
    static uint8_t orig[WINDOW];
    for (int bad = 0; bad < 3; bad++) {   /* exact; a byte off; the list position off */
        setup(), memcpy(orig, window, WINDOW), memcpy(before, window, WINDOW), want_batches(before, NREC, NREC);
        rec_ctx(&c, NREC), c0 = c;
        CHECK(hos_site_cloth_capsule(&c) == 0 && !memcmp(&c, &c0, sizeof c) && cloth_pending.k == K_CAPREC);
        CHECK(!memcmp(window, orig, WINDOW));   /* (nothing written for real) */
        /* the lifted loop, stood in for: the bytes and the list position it leaves */
        memcpy(window + POS, before + POS, 0x80000), memcpy(window + PREV, before + PREV, 0x80000);
        c.x[8] = G(REC) + 32ull * NREC, c.x[9] = 64ull * NREC;
        if (bad == 1) window[POS + 16 * 3 + 2] ^= 1;
        if (bad == 2) c.x[8] += 32;
        const long m0 = cloth_total[K_CAPREC][S_MISMATCH], k0 = cloth_total[K_CAPREC][S_CHECKED] + cloth_local[K_CAPREC][S_CHECKED];
        at_end("cloth.capsuleRecordsEnd", &c);
        CHECK(cloth_total[K_CAPREC][S_CHECKED] + cloth_local[K_CAPREC][S_CHECKED] == k0 + 1);
        CHECK(cloth_total[K_CAPREC][S_MISMATCH] == m0 + (bad ? 1 : 0) && cloth_pending.k == -1);
    }
    CHECK(logged_first == 1);
    puts("PASS: check: the batches into copies, compared at the batch loop's way out with the lifted loop's: exact compared, a "
         "byte off or the list position off a MISMATCH");
    for (int bad = 0; bad < 3; bad++) {   /* exact; a byte off; FPSR off */
        static uint8_t want[WINDOW];
        setup(), memcpy(before, window, WINDOW), memcpy(want, window, WINDOW);
        CHECK(want_singles(want, 9, 9) > 0);
        tail_ctx(&c, 9), c0 = c;
        CHECK(hos_site_cloth_capsule_tail(&c) == 0);
        CHECK(!memcmp(&c, &c0, sizeof c));
        CHECK(!memcmp(window, before, WINDOW));
        /* the lifted loop, stood in for: the bytes it leaves (its registers are not compared: none is read after it) */
        memcpy(window + POS, want + POS, 0x80000), memcpy(window + PREV, want + PREV, 0x80000);
        c.x[8] = G(TAIL) + 18, c.x[9] = 0, c.vreg[5][1] ^= 1;
        if (bad == 1) window[PREV + 16 * 70 + 5] ^= 1;
        if (bad == 2) c.fpsr ^= 0x10;
        const long m0 = cloth_total[K_CAPTAIL][S_MISMATCH];
        at_end("cloth.capsuleEnd", &c);
        CHECK(cloth_total[K_CAPTAIL][S_MISMATCH] == m0 + (bad ? 1 : 0));
    }
    puts("PASS: check: the particles left over compared at the way out: exact (scratch registers as they may be) compared, a "
         "byte off or FPSR off a MISMATCH");
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
    t = (ROOT / "game" / "cloth_capsule.cpp").read_text(encoding="utf-8")
    assert "#pragma clang fp contract(off)" in t and "fp-contract=off" in t, "cloth_capsule.cpp: the fp contract pragmas are gone"
    (tmp / "kfix.cpp").write_text(KFIX, encoding="utf-8")
    builds = [("this CPU", ["-march=native"]), ("x86-64-v2", ["-march=x86-64-v2"]), ("x86-64", ["-march=x86-64"]),
              ("lane by lane", ["-march=native", "-DCAP_FORCE_SLOW=1"]), ("contract on", ["-march=native", "-ffp-contract=on"])]
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
        srcs = [str(ROOT / "game" / "cloth_capsule.cpp"), str(tmp / "kfix.cpp")]
        subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "game"), "-I", str(tmp)] + flags + srcs + ["-o", str(exe)], check=True)
        r = subprocess.run([str(exe), str(trials)], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("FAIL (%s):\n%s%s" % (name, r.stdout, r.stderr))
        out = dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)
        digests[name] = out["digest"]
        p = out["plain"].split()
        print("PASS: %s (%s): the capsule kernel's batches and particles left over on %d trials; the plain model the same on %s "
              "plain trials (%s batches, %s particles left over, %s contacts); the cases by hand" %
              (name, "lane by lane" if out["slow"] == "1" else "vector paths" if out["simd"] == "1" else "plain SSE2",
               trials, p[0], p[1], p[2], p[3]))
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
    for base in (0x1000000000, (1 << 39) - 0x400000 - 0x1000):   # (the game's heap from 64 GB, its stacks near 2^39)
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
                            ("on", {"HOS_CLOTH_DECLINES": "1"}), ("check", {"HOS_NATIVE_CLOTH": "check"})):
            r = subprocess.run([str(exe), mode], env=dict(env, **extra), capture_output=True, text=True)
            print("\n".join(line for line in r.stdout.splitlines() if line.startswith("PASS")))
            if r.returncode:
                raise SystemExit("FAIL (guest window at %#x, %s %s):\n%s%s" % (base, mode, extra, r.stdout, r.stderr))
        print("PASS: the callbacks as above with the guest window at %#x (also with HOS_CLOTH_DECLINES=1)" % base)


def tail_guard():
    """the tail's site is after the branch that skips a call with no particles left over (its guard in the tables)"""
    x = site("cloth.capsuleTail", "f_81b8c000", "jump")
    words = dict(x.guards).get(0x58bc)
    assert x.offset == 0x58d0 and words and words[:2] == ((0x72000d29, 0xffffffff), (0x54000000, 0xff00001f)), x.guards
    print("PASS: a call with no particles left over does not reach cloth.capsuleTail: its guard is the ands / b.eq that skips "
          "the tail loop")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cc", default=os.environ.get("CC", "gcc"))
    p.add_argument("--cxx", default=os.environ.get("CXX", "c++"))
    p.add_argument("--trials", type=int, default=2000)
    a = p.parse_args()
    tail_guard()
    cc = parse_compiler(a.cc)
    cxx = parse_compiler(a.cxx)
    if not cc or not shutil.which(cc[0]) or not cxx or not shutil.which(cxx[0]):
        print("SKIP: no C or C++ compiler")
        return
    with tempfile.TemporaryDirectory(prefix="cloth-step9-") as tmp:
        kernels(cxx, Path(tmp), a.trials)
        hooks(cc, cxx, Path(tmp))


if __name__ == "__main__":
    main()
