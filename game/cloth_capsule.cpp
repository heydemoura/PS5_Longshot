/* Capsule collider particle pass. Batches: 4-wide SSE (one lane per particle in a group).
   Values the host cannot vouch for (NaN payloads, flush-to-zero tinies, a bad reciprocal estimate)
   take a scalar Arm soft-float path. Stores follow SPEC.md 4 and 5. */
#include "cloth_capsule.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"
#include <cstring>
#include <xmmintrin.h>
#include <emmintrin.h>
#if defined(__FMA__)
#include <immintrin.h>
#endif
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif

using kc::bf;
using kc::fb;
using kc::frsqrte_bits;

#ifndef CAP_FORCE_SLOW
#define CAP_FORCE_SLOW 0
#endif

struct Mx {
    unsigned v;
    Mx() : v(_mm_getcsr()) {}
    ~Mx() { _mm_setcsr(v); }
};

static inline uint32_t flush_u(uint32_t u, int fz) {
    if (fz && (u & 0x7fffffffu) - 1u < 0x007fffffu) return u & 0x80000000u;
    return u;
}
/* xyz that the fast path must not consume: any NaN, or (FZ) a nonzero magnitude <= 2^-126 */
static inline int bad_xyz_u(uint32_t u, int fz) {
    uint32_t a = u & 0x7fffffffu;
    if (a > 0x7f800000u) return 1;
    if (fz && a - 1u < 0x00800000u) return 1;
    return 0;
}
/* w input the fast way cannot take: a non-default NaN, or (FZ) the same tiny band */
static inline int bad_w_in(uint32_t u, int fz) {
    uint32_t a = u & 0x7fffffffu;
    if (a > 0x7f800000u && u != 0x7fc00000u) return 1;
    if (fz && a - 1u < 0x00800000u) return 1;
    return 0;
}
static inline int bad_w_mid(uint32_t u, int fz) {
    if (!fz) return 0;
    uint32_t a = u & 0x7fffffffu;
    return a - 1u < 0x00800000u;
}
static inline uint32_t canon_u(uint32_t u) {
    return (u & 0x7fffffffu) > 0x7f800000u ? 0x7fc00000u : u;
}

static inline __m128 spl(uint32_t u) {
    __m128i x = _mm_cvtsi32_si128((int)u);
    x = _mm_unpacklo_epi32(x, x);
    x = _mm_unpacklo_epi64(x, x);
    return _mm_castsi128_ps(x);
}
template <int FZ>
static inline __m128 flush_ps(__m128 v) {
    if constexpr (!FZ) return v;
    __m128i u = _mm_castps_si128(v);
    __m128i a = _mm_and_si128(u, _mm_set1_epi32(0x7fffffff));
    __m128i sub = _mm_and_si128(_mm_cmpgt_epi32(a, _mm_setzero_si128()),
                               _mm_cmplt_epi32(a, _mm_set1_epi32(0x00800000)));
    __m128i sign = _mm_and_si128(u, _mm_set1_epi32((int)0x80000000u));
    return _mm_castsi128_ps(_mm_or_si128(_mm_andnot_si128(sub, u), _mm_and_si128(sub, sign)));
}
[[maybe_unused]] static inline __m128i tiny_mask(__m128 v) {   /* (the SSE4.1 builds test it a cheaper way) */
    __m128i a = _mm_and_si128(_mm_castps_si128(v), _mm_set1_epi32(0x7fffffff));
    return _mm_and_si128(_mm_cmpgt_epi32(a, _mm_setzero_si128()),
                         _mm_cmplt_epi32(a, _mm_set1_epi32(0x00800001)));
}
#if !defined(__SSE4_1__)
/* nonzero and below 2^-63 in magnitude (in_r_u's lower bound) */
static inline __m128i small_mask(__m128 v) {
    __m128i a = _mm_and_si128(_mm_castps_si128(v), _mm_set1_epi32(0x7fffffff));
    return _mm_and_si128(_mm_cmpgt_epi32(a, _mm_setzero_si128()),
                         _mm_cmplt_epi32(a, _mm_set1_epi32(0x20000000)));
}
#endif
template <int FZ>
static inline __m128i xyz_bad_mask(__m128 v) {
    __m128i nan = _mm_castps_si128(_mm_cmpunord_ps(v, v));
    if constexpr (!FZ) return nan;
    return _mm_or_si128(nan, tiny_mask(v));
}
static inline __m128 amin4(__m128 a, __m128 b) {
    __m128 eq = _mm_cmpeq_ps(a, b);
    __m128 mn = _mm_min_ps(a, b);
    __m128 both = _mm_or_ps(a, b);
#if defined(__SSE4_1__)
    return _mm_blendv_ps(mn, both, eq);
#else
    return _mm_or_ps(_mm_and_ps(eq, both), _mm_andnot_ps(eq, mn));
#endif
}
static inline __m128 ld_ps(const uint8_t *p) {
    return _mm_loadu_ps((const float *)(const void *)p);
}
/* Zero, or magnitude in [2^-63, 2^63). Products and sums of these are never tiny. */
static inline int in_r_u(uint32_t u) {
    uint32_t a = u & 0x7fffffffu;
    return a == 0u || a - 0x20000000u < 0x3f000000u;
}
static inline int all_in_r(__m128 v) {
    __m128i a = _mm_and_si128(_mm_castps_si128(v), _mm_set1_epi32(0x7fffffff));
    __m128i z = _mm_cmpeq_epi32(a, _mm_setzero_si128());
    __m128i span = _mm_and_si128(_mm_cmpgt_epi32(a, _mm_set1_epi32(0x1fffffff)),
                                _mm_cmplt_epi32(a, _mm_set1_epi32(0x5f000000)));
    return _mm_movemask_epi8(_mm_or_si128(z, span)) == 0xffff;
}
/* Hit lanes must be in range; other lanes are ignored. */
static inline int hits_in_r(__m128 v, __m128 hit) {
    __m128 one = _mm_set1_ps(1.0f);
    return all_in_r(_mm_or_ps(_mm_and_ps(hit, v), _mm_andnot_ps(hit, one)));
}

/* rsqrt_step. Certified: NaN, any zero (the fused 3-0 is 1.5, and inf with zero is defined as 1.5),
   or a finite product in [1/8, 8]. Anything else is refused. */
static int step_f(float a, float b, float &s) {
    uint32_t ua = fb(a), ub = fb(b);
    uint32_t aa = ua & 0x7fffffffu, bb = ub & 0x7fffffffu;
    if (aa > 0x7f800000u || bb > 0x7f800000u) {
        s = bf(0x7fc00000u);
        return 1;
    }
    if (aa == 0u || bb == 0u) {
        s = 1.5f;
        return 1;
    }
    if (aa == 0x7f800000u || bb == 0x7f800000u) return 0;
    double p = (double)a * (double)b;
    if (!(p >= 0.125 && p <= 8.0)) return 0;
#if KC_FRSQRTS_FMA
    s = __builtin_fmaf(-a, b, 3.0f) * 0.5f;
#else
    s = (float)(3.0 - p) * 0.5f;
#endif
    return 1;
}
static int step_u(uint32_t a, uint32_t b, uint32_t &s) {
    float sf;
    if (!step_f(bf(a), bf(b), sf)) return 0;
    s = fb(sf);
    return 1;
}

/* Two Newton steps. Fast path refuses a result it cannot vouch for (caller then uses the Arm path). */
[[maybe_unused]] static int refine_fast(float L, int fz, float &r) {
    uint32_t u = fb(L);
    float r0;
    if (u == 0x7f800000u) r0 = 0.0f;
    else if (u >= 0x00800000u && u < 0x7f800000u) r0 = bf(frsqrte_bits(u));
    else return 0;
    float e0 = r0 * r0;
    float s0;
    if (!step_f(L, e0, s0)) return 0;
    float r1 = r0 * s0;
    float e1 = r1 * r1;
    float s1;
    if (!step_f(L, e1, s1)) return 0;
    r = r1 * s1;
    if (bad_xyz_u(fb(r0), fz) || bad_xyz_u(fb(e0), fz) || bad_xyz_u(fb(s0), fz) || bad_xyz_u(fb(r1), fz) ||
        bad_xyz_u(fb(e1), fz) || bad_xyz_u(fb(s1), fz) || bad_xyz_u(fb(r), fz))
        return 0;
    return 1;
}
static int refine_slow(uint32_t L, int fz, uint32_t &r) {
    uint32_t fl = 0, r0;
    if ((L & 0x7fffffffu) > 0x7f800000u) {
        r = 0x7fc00000u;
        return 1;
    }
    if (L == 0x7f800000u) r0 = 0;
    else if (L >= 0x00800000u && L < 0x7f800000u) r0 = frsqrte_bits(L);
    else return 0;
    uint32_t e0 = cw::arm_mul(r0, r0, (uint32_t)fz, fl);
    uint32_t s0;
    if (!step_u(L, e0, s0)) return 0;
    uint32_t r1 = cw::arm_mul(r0, s0, (uint32_t)fz, fl);
    uint32_t e1 = cw::arm_mul(r1, r1, (uint32_t)fz, fl);
    uint32_t s1;
    if (!step_u(L, e1, s1)) return 0;
    r = cw::arm_mul(r1, s1, (uint32_t)fz, fl);
    return 1;
}

struct Prep {
    uint32_t A[4], AB[4], rad, k, dt, c[3], v[4], w[3], eps[4], one[4];
    float c0;
    int fz, base_r, batch_r, wconst_bad;
};
static void make_prep(const ClothCapsule *k, Prep &p) {
    uint32_t fl = 0;
    p.fz = k->fz ? 1 : 0;
    for (int i = 0; i < 4; i++) {
        p.A[i] = flush_u(k->a[i], p.fz);
        p.AB[i] = cw::arm_sub(k->b[i], k->a[i], (uint32_t)p.fz, fl);
    }
    p.rad = flush_u(k->radius, p.fz);
    p.k = flush_u(k->inv_len2, p.fz);
    p.dt = flush_u(k->dt, p.fz);
    for (int i = 0; i < 3; i++) {
        p.c[i] = flush_u(k->centre[i], p.fz);
        p.w[i] = flush_u(k->ang_vel[i], p.fz);
    }
    for (int i = 0; i < 4; i++) p.v[i] = flush_u(k->lin_vel[i], p.fz);
    for (int i = 0; i < 4; i++) {
        p.eps[i] = flush_u(k->eps[i], p.fz);
        p.one[i] = flush_u(k->one[i], p.fz);
    }
    p.c0 = p.fz ? 0.0f : bf(0x007fffffu);
    int ok = 1;
    for (int i = 0; i < 3; i++) {
        ok &= in_r_u(p.A[i]) && in_r_u(p.AB[i]);
        ok &= in_r_u(p.c[i]) && in_r_u(p.v[i]) && in_r_u(p.w[i]);
    }
    ok &= in_r_u(p.rad) && in_r_u(p.k) && in_r_u(p.dt);
    p.base_r = ok;
    int bok = ok;
    for (int i = 0; i < 4; i++) bok &= in_r_u(p.eps[i]) && in_r_u(p.one[i]);
    p.batch_r = bok;
    p.wconst_bad = bad_w_in(p.A[3], p.fz) || bad_w_in(p.AB[3], p.fz) || bad_w_in(p.v[3], p.fz);
}

struct Slot {
    uint32_t p[4], q[4], tf[4], prev[4];
    int hit, wp;
};

static void load4(const uint8_t *p, uint32_t o[4], int fz) {
    memcpy(o, p, 16);
    for (int i = 0; i < 4; i++) o[i] = flush_u(o[i], fz);
}

/* One particle, Arm ops. is_batch selects the clamp and eps. Returns 0 if the reciprocal step is uncertified. */
static int slow_particle(const Prep &pr, const uint32_t P[4], uint32_t R, const uint32_t Q[4], uint32_t fr, int is_batch,
                         uint32_t eps, uint32_t onev, int &hit, uint32_t Pp[4], uint32_t TF[4]) {
    uint32_t fl = 0;
    uint32_t fz = (uint32_t)pr.fz;
    uint32_t ex = cw::arm_sub(P[0], pr.A[0], fz, fl);
    uint32_t ey = cw::arm_sub(P[1], pr.A[1], fz, fl);
    uint32_t ez = cw::arm_sub(P[2], pr.A[2], fz, fl);
    uint32_t dot = cw::arm_add(cw::arm_add(cw::arm_mul(pr.AB[0], ex, fz, fl), cw::arm_mul(pr.AB[1], ey, fz, fl), fz, fl),
                              cw::arm_mul(pr.AB[2], ez, fz, fl), fz, fl);
    uint32_t t0 = cw::arm_mul(dot, pr.k, fz, fl);
    uint32_t t;
    if (is_batch) {
        t = cw::arm_fmax(t0, 0u, fz, fl);
        t = cw::arm_fmin(t, onev, fz, fl);
    } else {
        float tf0 = bf(t0);
        if (tf0 < 0.0f) t = 0u;
        else if (tf0 > 1.0f) t = 0x3f800000u;
        else t = t0;
    }
    uint32_t C[4], d[4];
    for (int i = 0; i < 4; i++) {
        C[i] = cw::arm_add(pr.A[i], cw::arm_mul(pr.AB[i], t, fz, fl), fz, fl);
        d[i] = cw::arm_sub(P[i], C[i], fz, fl);
    }
    uint32_t L = cw::arm_add(cw::arm_add(cw::arm_mul(d[0], d[0], fz, fl), cw::arm_mul(d[1], d[1], fz, fl), fz, fl),
                            cw::arm_mul(d[2], d[2], fz, fl), fz, fl);
    if (is_batch) L = cw::arm_add(eps, L, fz, fl);
    uint32_t r;
    if (bf(L) <= pr.c0) r = 0u;
    else if (!refine_slow(L, pr.fz, r)) return 0;
    uint32_t sc = cw::arm_sub(cw::arm_sub(cw::arm_mul(L, r, fz, fl), pr.rad, fz, fl), R, fz, fl);
    hit = bf(sc) < 0.0f;
    if (!hit) return 1;
    uint32_t n[4];
    for (int i = 0; i < 4; i++) n[i] = cw::arm_mul(d[i], r, fz, fl);
    uint32_t S0 = cw::arm_add(C[0], cw::arm_mul(n[0], pr.rad, fz, fl), fz, fl);
    uint32_t S1 = cw::arm_add(C[1], cw::arm_mul(n[1], pr.rad, fz, fl), fz, fl);
    uint32_t S2 = cw::arm_add(C[2], cw::arm_mul(n[2], pr.rad, fz, fl), fz, fl);
    uint32_t m0 = cw::arm_mul(n[0], cw::arm_sub(P[0], S0, fz, fl), fz, fl);
    uint32_t m1 = cw::arm_mul(n[1], cw::arm_sub(P[1], S1, fz, fl), fz, fl);
    uint32_t m2 = cw::arm_mul(n[2], cw::arm_sub(P[2], S2, fz, fl), fz, fl);
    uint32_t dS = cw::arm_add(cw::arm_add(m0, m1, fz, fl), m2, fz, fl);
    uint32_t s = cw::arm_sub(R, dS, fz, fl);
    for (int i = 0; i < 4; i++) Pp[i] = cw::arm_add(P[i], cw::arm_mul(n[i], s, fz, fl), fz, fl);
    uint32_t ux = cw::arm_sub(S0, pr.c[0], fz, fl);
    uint32_t uy = cw::arm_sub(S1, pr.c[1], fz, fl);
    uint32_t uz = cw::arm_sub(S2, pr.c[2], fz, fl);
    uint32_t X = cw::arm_sub(cw::arm_mul(pr.w[1], uz, fz, fl), cw::arm_mul(uy, pr.w[2], fz, fl), fz, fl);
    uint32_t Y = cw::arm_sub(cw::arm_mul(pr.w[2], ux, fz, fl), cw::arm_mul(uz, pr.w[0], fz, fl), fz, fl);
    uint32_t Z = cw::arm_sub(cw::arm_mul(pr.w[0], uy, fz, fl), cw::arm_mul(ux, pr.w[1], fz, fl), fz, fl);
    uint32_t V0 = cw::arm_add(pr.v[0], X, fz, fl);
    uint32_t V1 = cw::arm_add(pr.v[1], Y, fz, fl);
    uint32_t V2 = cw::arm_add(pr.v[2], Z, fz, fl);
    uint32_t V3 = cw::arm_add(pr.v[3], 0u, fz, fl);
    uint32_t V[4] = {V0, V1, V2, V3};
    uint32_t rel[4], T[4];
    for (int i = 0; i < 4; i++)
        rel[i] = cw::arm_sub(cw::arm_sub(Pp[i], Q[i], fz, fl), cw::arm_mul(V[i], pr.dt, fz, fl), fz, fl);
    uint32_t m2x = cw::arm_mul(n[0], rel[0], fz, fl);
    uint32_t m2y = cw::arm_mul(n[1], rel[1], fz, fl);
    uint32_t m2z = cw::arm_mul(n[2], rel[2], fz, fl);
    uint32_t dn = cw::arm_add(cw::arm_add(m2x, m2y, fz, fl), m2z, fz, fl);
    for (int i = 0; i < 4; i++) {
        T[i] = cw::arm_sub(rel[i], cw::arm_mul(n[i], dn, fz, fl), fz, fl);
        TF[i] = cw::arm_mul(T[i], fr, fz, fl);
    }
    return 1;
}

static void combine_slow(Slot *o, int fz) {
    int id[4], n = 0;
    uint32_t fl = 0;
    for (int i = 0; i < 4; i++) {
        o[i].wp = 0;
        if (o[i].hit) id[n++] = i;
    }
    if (!n) return;
    int a = id[0];
    uint32_t s[4];
    for (int k = 0; k < 4; k++) s[k] = cw::arm_add(o[a].q[k], o[a].tf[k], (uint32_t)fz, fl);
    if (n >= 2) {
        int b = id[1];
        for (int k = 0; k < 4; k++) s[k] = cw::arm_add(o[b].tf[k], s[k], (uint32_t)fz, fl);
    }
    memcpy(o[a].prev, s, 16);
    o[a].wp = 1;
    for (int t = 2; t < n; t++) {
        int c = id[t];
        for (int k = 0; k < 4; k++) o[c].prev[k] = cw::arm_add(o[c].q[k], o[c].tf[k], (uint32_t)fz, fl);
        o[c].wp = 1;
    }
}
static int combine_fast(Slot *o, int fz) {
    int id[4], n = 0;
    for (int i = 0; i < 4; i++) {
        o[i].wp = 0;
        if (o[i].hit) id[n++] = i;
    }
    if (!n) return 1;
    auto add = [&](const uint32_t *a, const uint32_t *b, uint32_t *d) -> int {
        for (int k = 0; k < 4; k++) {
            float r = bf(a[k]) + bf(b[k]);
            d[k] = fb(r);
            if (k < 3) {
                if (bad_xyz_u(d[k], fz)) return 0;
            } else if (bad_w_mid(d[k], fz)) return 0;
        }
        return 1;
    };
    uint32_t s[4];
    if (!add(o[id[0]].q, o[id[0]].tf, s)) return 0;
    if (n >= 2) {
        uint32_t s2[4];
        if (!add(o[id[1]].tf, s, s2)) return 0;
        memcpy(s, s2, sizeof s);
    }
    memcpy(o[id[0]].prev, s, sizeof s);
    o[id[0]].wp = 1;
    for (int t = 2; t < n; t++) {
        int c = id[t];
        if (!add(o[c].q, o[c].tf, o[c].prev)) return 0;
        o[c].wp = 1;
    }
    return 1;
}

static void store_slots(uint8_t *pos, uint8_t *prv, const uint16_t *ix, Slot *s, int do_canon) {
    for (int j = 0; j < 16; j++) {
        if (!s[j].hit) continue;
        uint32_t w[4];
        for (int k = 0; k < 4; k++) w[k] = do_canon ? canon_u(s[j].p[k]) : s[j].p[k];
        memcpy(pos + 16u * ix[j], w, 16);
    }
    for (int j = 0; j < 16; j++) {
        if (!s[j].wp) continue;
        uint32_t w[4];
        for (int k = 0; k < 4; k++) w[k] = do_canon ? canon_u(s[j].prev[k]) : s[j].prev[k];
        memcpy(prv + 16u * ix[j], w, 16);
    }
}

static int slow_batch(const Prep &pr, const ClothCapBatches *b, uint32_t bi, const uint16_t *ix, Slot *s, uint16_t *mask) {
    memset(s, 0, 16 * sizeof(Slot));
    const uint8_t *radp = b->radii + 64u * bi;
    const uint8_t *frp = b->frictions + 64u * bi;
    for (int j = 0; j < 16; j++) {
        uint32_t P[4], Q[4], R, f;
        load4(b->pos + 16u * ix[j], P, pr.fz);
        memcpy(&R, radp + 4u * j, 4);
        R = flush_u(R, pr.fz);
        memcpy(&f, frp + 4u * j, 4);
        f = flush_u(f, pr.fz);
        load4(b->prev + 16u * ix[j], Q, pr.fz);
        int hit = 0;
        uint32_t Pp[4], TF[4];
        if (!slow_particle(pr, P, R, Q, f, 1, pr.eps[j & 3], pr.one[j & 3], hit, Pp, TF)) return 0;
        s[j].hit = hit;
        if (hit) {
            memcpy(s[j].p, Pp, 16);
            memcpy(s[j].q, Q, 16);
            memcpy(s[j].tf, TF, 16);
        }
    }
    for (int g = 0; g < 4; g++) combine_slow(s + 4 * g, pr.fz);
    uint16_t m = 0;
    for (int j = 0; j < 16; j++)
        if (s[j].hit) m = (uint16_t)(m | (1u << j));
    *mask = m;
    return 1;
}

/* Positive normals only; every other bit pattern (zero, negative, subnormal, inf, NaN) yields +0.
   The table read is unconditional so four lanes stay independent. */
static inline uint32_t est_bits(uint32_t u) {
    uint32_t e = u >> 23, f = u & 0x7fffffu;
    uint32_t r = (((380u - e) >> 1) << 23) | ((uint32_t)kc::RSQ.t[((e & 1u) << 8) | (f >> 15)] << 15);
    return (u - 0x00800000u < 0x7f000000u) ? r : 0u;
}
static inline __m128 refine_vec(__m128 L) {
    __m128i b = _mm_castps_si128(L);
    uint32_t i0 = est_bits((uint32_t)_mm_cvtsi128_si32(b));
    uint32_t i1 = est_bits((uint32_t)_mm_cvtsi128_si32(_mm_shuffle_epi32(b, 0x55)));
    uint32_t i2 = est_bits((uint32_t)_mm_cvtsi128_si32(_mm_shuffle_epi32(b, 0xAA)));
    uint32_t i3 = est_bits((uint32_t)_mm_cvtsi128_si32(_mm_shuffle_epi32(b, 0xFF)));
    __m128 r0 = _mm_castsi128_ps(_mm_set_epi32((int)i3, (int)i2, (int)i1, (int)i0));
#if KC_FRSQRTS_FMA
    const __m128 three = _mm_set1_ps(3.0f), half = _mm_set1_ps(0.5f);
    __m128 s0 = _mm_mul_ps(_mm_fnmadd_ps(L, _mm_mul_ps(r0, r0), three), half);
    __m128 r1 = _mm_mul_ps(r0, s0);
    __m128 s1 = _mm_mul_ps(_mm_fnmadd_ps(L, _mm_mul_ps(r1, r1), three), half);
    return _mm_mul_ps(r1, s1);
#else
    alignas(16) float Ls[4], r0s[4], rs[4];
    _mm_store_ps(Ls, L);
    _mm_store_ps(r0s, r0);
    for (int i = 0; i < 4; i++) {
        float a = Ls[i], r0f = r0s[i];
        if (r0f == 0.0f) { rs[i] = 0.0f; continue; }
        float e0 = r0f * r0f;
        float s0 = (float)(3.0 - (double)a * (double)e0) * 0.5f;
        float r1 = r0f * s0;
        float e1 = r1 * r1;
        float s1 = (float)(3.0 - (double)a * (double)e1) * 0.5f;
        rs[i] = r1 * s1;
    }
    return _mm_load_ps(rs);
#endif
}

template <int FZ>
static int fast_batch(const Prep &pr, const ClothCapBatches *b, uint32_t bi, const uint16_t *ix, Slot *s, uint16_t *mask) {
    if (pr.fz != FZ || pr.wconst_bad) return 0;
    memset(s, 0, 16 * sizeof(Slot));
    __m128 Ax = spl(pr.A[0]), Ay = spl(pr.A[1]), Az = spl(pr.A[2]), Aw = spl(pr.A[3]);
    __m128 ABx = spl(pr.AB[0]), ABy = spl(pr.AB[1]), ABz = spl(pr.AB[2]), ABw = spl(pr.AB[3]);
    __m128 rad = spl(pr.rad), kk = spl(pr.k), dt = spl(pr.dt);
    __m128 cx = spl(pr.c[0]), cy = spl(pr.c[1]), cz = spl(pr.c[2]);
    __m128 vx = spl(pr.v[0]), vy = spl(pr.v[1]), vz = spl(pr.v[2]), vw = spl(pr.v[3]);
    __m128 wx = spl(pr.w[0]), wy = spl(pr.w[1]), wz = spl(pr.w[2]);
    __m128 epsv = _mm_castsi128_ps(_mm_set_epi32((int)pr.eps[3], (int)pr.eps[2], (int)pr.eps[1], (int)pr.eps[0]));
    __m128 onev = _mm_castsi128_ps(_mm_set_epi32((int)pr.one[3], (int)pr.one[2], (int)pr.one[1], (int)pr.one[0]));
    __m128 zero = _mm_setzero_ps();
    const uint8_t *radp = b->radii + 64u * bi;
    const uint8_t *frp = b->frictions + 64u * bi;
    uint16_t bits = 0;
    for (int g = 0; g < 4; g++) {
        int base = g * 4;
        __m128 p0 = ld_ps(b->pos + 16u * ix[base + 0]);
        __m128 p1 = ld_ps(b->pos + 16u * ix[base + 1]);
        __m128 p2 = ld_ps(b->pos + 16u * ix[base + 2]);
        __m128 p3 = ld_ps(b->pos + 16u * ix[base + 3]);
        _MM_TRANSPOSE4_PS(p0, p1, p2, p3);
        __m128 px = p0, py = p1, pz = p2, pw = p3;
        __m128 Rv = ld_ps(radp + 16u * g);
        alignas(16) uint32_t wb[4];
        _mm_store_si128((__m128i *)wb, _mm_castps_si128(pw));
        int wbad = 0;
        for (int i = 0; i < 4; i++) wbad |= bad_w_in(wb[i], FZ);
        __m128 ex = _mm_sub_ps(px, Ax), ey = _mm_sub_ps(py, Ay), ez = _mm_sub_ps(pz, Az);
        __m128 dxp = _mm_mul_ps(ABx, ex), dyp = _mm_mul_ps(ABy, ey), dzp = _mm_mul_ps(ABz, ez);
        __m128 sxy = _mm_add_ps(dxp, dyp);
        __m128 dot = _mm_add_ps(sxy, dzp);
        __m128 t = amin4(_mm_max_ps(_mm_mul_ps(dot, kk), zero), onev);
        __m128 tx = _mm_mul_ps(ABx, t), ty = _mm_mul_ps(ABy, t), tz = _mm_mul_ps(ABz, t), tw = _mm_mul_ps(ABw, t);
        __m128 Cx = _mm_add_ps(Ax, tx), Cy = _mm_add_ps(Ay, ty), Cz = _mm_add_ps(Az, tz), Cw = _mm_add_ps(Aw, tw);
        __m128 dx = _mm_sub_ps(px, Cx), dy = _mm_sub_ps(py, Cy), dz = _mm_sub_ps(pz, Cz), dw = _mm_sub_ps(pw, Cw);
        __m128 sq = _mm_add_ps(_mm_add_ps(_mm_mul_ps(dx, dx), _mm_mul_ps(dy, dy)), _mm_mul_ps(dz, dz));
        __m128 Lv = _mm_add_ps(epsv, sq);
        __m128 r = refine_vec(Lv);
        __m128 sc = _mm_sub_ps(_mm_sub_ps(_mm_mul_ps(Lv, r), rad), Rv);
        if (wbad || !all_in_r(px) || !all_in_r(py) || !all_in_r(pz) || !all_in_r(Rv) || !all_in_r(ex) ||
            !all_in_r(ey) || !all_in_r(ez) || !all_in_r(sxy) || !all_in_r(dot) || !all_in_r(t) || !all_in_r(dx) ||
            !all_in_r(dy) || !all_in_r(dz) || !all_in_r(Lv) || !all_in_r(sc))
            return 0;
        __m128 hit = _mm_cmplt_ps(sc, zero);
        int hm = _mm_movemask_ps(hit);
#if defined(__SSE4_1__)
        __m128i wkey = _mm_set1_epi32(-1);   /* (tiny_mask's test, cheaper: the least of (bits doubled less 2), unsigned: a zero
                                                wraps to the top, a tiny value is at most 0xfffffe) */
        auto kw = [&](__m128 v) {
            if constexpr (FZ)
                wkey = _mm_min_epu32(wkey, _mm_sub_epi32(_mm_add_epi32(_mm_castps_si128(v), _mm_castps_si128(v)), _mm_set1_epi32(2)));
        };
#else
        __m128i wtiny = _mm_setzero_si128();
        auto kw = [&](__m128 v) {
            if constexpr (FZ) wtiny = _mm_or_si128(wtiny, tiny_mask(v));
        };
#endif
        kw(tw); kw(Cw); kw(dw);
        /* (FZ: x, y, z from the contact on. A value that can be tiny is a product with a factor below 2^-63 or a sum of
           two values below 2^-102; each such value's first use is a sum checked here to be zero or at least 2^-63 (S, the
           two partial sums of dS, P', X, rd, the two of dn, T), which either catches it (it, or the sum, is below) or
           leaves it no effect (a tiny addend is under half an ulp of the other, which the Arm adds a zero to)) */
#if defined(__SSE4_1__)
        __m128i xkey = _mm_set1_epi32(-1);   /* (the least of (bits doubled less 2): a zero wraps to the top) */
        auto kx = [&](__m128 a, __m128 b, __m128 c) {
            if constexpr (FZ) {
                const __m128i two = _mm_set1_epi32(2);
                auto key = [&](__m128 v) { return _mm_sub_epi32(_mm_add_epi32(_mm_castps_si128(v), _mm_castps_si128(v)), two); };
                xkey = _mm_min_epu32(xkey, _mm_min_epu32(_mm_min_epu32(key(a), key(b)), key(c)));
            }
        };
#else
        __m128i xsmall = _mm_setzero_si128();
        auto kx = [&](__m128 a, __m128 b, __m128 c) {
            if constexpr (FZ) xsmall = _mm_or_si128(xsmall, _mm_or_si128(_mm_or_si128(small_mask(a), small_mask(b)), small_mask(c)));
        };
#endif
        if (!hm) continue;
        __m128 q0 = ld_ps(b->prev + 16u * ix[base + 0]);
        __m128 q1 = ld_ps(b->prev + 16u * ix[base + 1]);
        __m128 q2 = ld_ps(b->prev + 16u * ix[base + 2]);
        __m128 q3 = ld_ps(b->prev + 16u * ix[base + 3]);
        _MM_TRANSPOSE4_PS(q0, q1, q2, q3);
        __m128 qx = q0, qy = q1, qz = q2, qw = q3;
        __m128 fv = ld_ps(frp + 16u * g);
        if (!hits_in_r(qx, hit) || !hits_in_r(qy, hit) || !hits_in_r(qz, hit) || !hits_in_r(fv, hit)) return 0;
        _mm_store_si128((__m128i *)wb, _mm_castps_si128(qw));
        for (int i = 0; i < 4; i++)
            if ((hm >> i) & 1 && bad_w_in(wb[i], FZ)) return 0;
        __m128 nx = _mm_mul_ps(dx, r), ny = _mm_mul_ps(dy, r), nz = _mm_mul_ps(dz, r), nw = _mm_mul_ps(dw, r);
        kw(nw);
        __m128 Sx = _mm_add_ps(Cx, _mm_mul_ps(nx, rad));
        __m128 Sy = _mm_add_ps(Cy, _mm_mul_ps(ny, rad));
        __m128 Sz = _mm_add_ps(Cz, _mm_mul_ps(nz, rad));
        __m128 pSx = _mm_sub_ps(px, Sx), pSy = _mm_sub_ps(py, Sy), pSz = _mm_sub_ps(pz, Sz);
        __m128 mx = _mm_mul_ps(nx, pSx), my = _mm_mul_ps(ny, pSy), mz = _mm_mul_ps(nz, pSz);
        __m128 mxy = _mm_add_ps(mx, my);
        __m128 dS = _mm_add_ps(mxy, mz);
        kx(Sx, Sy, Sz); kx(mxy, dS, dS);
        __m128 spv = _mm_sub_ps(Rv, dS);
        __m128 nsx = _mm_mul_ps(nx, spv), nsy = _mm_mul_ps(ny, spv), nsz = _mm_mul_ps(nz, spv), nsw = _mm_mul_ps(nw, spv);
        kw(nsw);
        __m128 Ppx = _mm_add_ps(px, nsx), Ppy = _mm_add_ps(py, nsy), Ppz = _mm_add_ps(pz, nsz), Ppw = _mm_add_ps(pw, nsw);
        kw(Ppw);
        kx(Ppx, Ppy, Ppz);
        __m128 ux = _mm_sub_ps(Sx, cx), uy = _mm_sub_ps(Sy, cy), uz = _mm_sub_ps(Sz, cz);
        __m128 Xa = _mm_mul_ps(wy, uz), Xb = _mm_mul_ps(uy, wz);
        __m128 Ya = _mm_mul_ps(wz, ux), Yb = _mm_mul_ps(uz, wx);
        __m128 Za = _mm_mul_ps(wx, uy), Zb = _mm_mul_ps(ux, wy);
        __m128 X = _mm_sub_ps(Xa, Xb), Y = _mm_sub_ps(Ya, Yb), Z = _mm_sub_ps(Za, Zb);
        kx(X, Y, Z);
        __m128 Vx = _mm_add_ps(vx, X), Vy = _mm_add_ps(vy, Y), Vz = _mm_add_ps(vz, Z), Vw = _mm_add_ps(vw, zero);
        kw(Vw);
        __m128 pqx = _mm_sub_ps(Ppx, qx), pqy = _mm_sub_ps(Ppy, qy), pqz = _mm_sub_ps(Ppz, qz), pqw = _mm_sub_ps(Ppw, qw);
        __m128 vdx = _mm_mul_ps(Vx, dt), vdy = _mm_mul_ps(Vy, dt), vdz = _mm_mul_ps(Vz, dt), vdw = _mm_mul_ps(Vw, dt);
        kw(pqw); kw(vdw);
        __m128 rdx = _mm_sub_ps(pqx, vdx), rdy = _mm_sub_ps(pqy, vdy), rdz = _mm_sub_ps(pqz, vdz), rdw = _mm_sub_ps(pqw, vdw);
        kw(rdw);
        kx(rdx, rdy, rdz);
        __m128 m2x = _mm_mul_ps(nx, rdx), m2y = _mm_mul_ps(ny, rdy), m2z = _mm_mul_ps(nz, rdz);
        __m128 m2xy = _mm_add_ps(m2x, m2y);
        __m128 dn = _mm_add_ps(m2xy, m2z);
        kx(m2xy, dn, dn);
        __m128 ndx = _mm_mul_ps(nx, dn), ndy = _mm_mul_ps(ny, dn), ndz = _mm_mul_ps(nz, dn), ndw = _mm_mul_ps(nw, dn);
        kw(ndw);
        __m128 Tx = _mm_sub_ps(rdx, ndx), Ty = _mm_sub_ps(rdy, ndy), Tz = _mm_sub_ps(rdz, ndz), Tw = _mm_sub_ps(rdw, ndw);
        kw(Tw);
        kx(Tx, Ty, Tz);
        __m128 tfx = _mm_mul_ps(Tx, fv), tfy = _mm_mul_ps(Ty, fv), tfz = _mm_mul_ps(Tz, fv), tfw = _mm_mul_ps(Tw, fv);
        kw(tfw);
        if constexpr (FZ) {
#if defined(__SSE4_1__)
            const __m128i xsmall = _mm_cmpeq_epi32(_mm_min_epu32(xkey, _mm_set1_epi32(0x3ffffffc)), xkey);
            const __m128i wtiny = _mm_cmpeq_epi32(_mm_min_epu32(wkey, _mm_set1_epi32(0x00fffffe)), wkey);
#endif
            if (_mm_movemask_epi8(_mm_and_si128(_mm_or_si128(wtiny, xsmall), _mm_castps_si128(hit)))) return 0;
        }
        alignas(16) float xs[4], ys[4], zs[4], ws[4];
        alignas(16) float qxs[4], qys[4], qzs[4], qws[4];
        alignas(16) float txs[4], tys[4], tzs[4], tws[4];
        _mm_store_ps(xs, Ppx); _mm_store_ps(ys, Ppy); _mm_store_ps(zs, Ppz); _mm_store_ps(ws, Ppw);
        _mm_store_ps(qxs, qx); _mm_store_ps(qys, qy); _mm_store_ps(qzs, qz); _mm_store_ps(qws, qw);
        _mm_store_ps(txs, tfx); _mm_store_ps(tys, tfy); _mm_store_ps(tzs, tfz); _mm_store_ps(tws, tfw);
        for (int i = 0; i < 4; i++) {
            if (((hm >> i) & 1) == 0) continue;
            int j = base + i;
            s[j].hit = 1;
            memcpy(&s[j].p[0], &xs[i], 4); memcpy(&s[j].p[1], &ys[i], 4);
            memcpy(&s[j].p[2], &zs[i], 4); memcpy(&s[j].p[3], &ws[i], 4);
            memcpy(&s[j].q[0], &qxs[i], 4); memcpy(&s[j].q[1], &qys[i], 4);
            memcpy(&s[j].q[2], &qzs[i], 4); memcpy(&s[j].q[3], &qws[i], 4);
            memcpy(&s[j].tf[0], &txs[i], 4); memcpy(&s[j].tf[1], &tys[i], 4);
            memcpy(&s[j].tf[2], &tzs[i], 4); memcpy(&s[j].tf[3], &tws[i], 4);
        }
        bits = (uint16_t)(bits | (hm << base));
    }
    for (int g = 0; g < 4; g++)
        if (!combine_fast(s + 4 * g, FZ)) return 0;
    *mask = bits;
    return 1;
}

static int dup16(const uint16_t *ix) {
    __m128i a = _mm_loadu_si128((const __m128i *)ix);
    __m128i b = _mm_loadu_si128((const __m128i *)ix + 1);
    for (int i = 0; i < 16; i++) {
        __m128i s = _mm_set1_epi16((short)ix[i]);
        unsigned m = (unsigned)_mm_movemask_epi8(_mm_cmpeq_epi16(a, s)) |
                     ((unsigned)_mm_movemask_epi8(_mm_cmpeq_epi16(b, s)) << 16);
        if (m != (3u << (2 * (i & 7) + 16 * (i >> 3)))) {
            /* more than the one copy of this value, or the bit layout differs: count bits */
            unsigned n = 0;
            while (m) { n += m & 1u; m >>= 1; }
            if (n != 2) return 1;
        }
    }
    return 0;
}

static int newton_r(float L, float &r) {
    uint32_t u = fb(L);
    if (u == 0u || L < 0.0f) { r = 0.0f; return 1; }
    if (u >= 0x7f800000u) return 0;
    float r0 = bf(frsqrte_bits(u));
    float e0 = r0 * r0;
#if KC_FRSQRTS_FMA
    float s0 = __builtin_fmaf(-L, e0, 3.0f) * 0.5f;
    float r1 = r0 * s0;
    float s1 = __builtin_fmaf(-L, r1 * r1, 3.0f) * 0.5f;
    r = r1 * s1;
#else
    float s0 = (float)(3.0 - (double)L * (double)e0) * 0.5f;
    float r1 = r0 * s0;
    float e1 = r1 * r1;
    float s1 = (float)(3.0 - (double)L * (double)e1) * 0.5f;
    r = r1 * s1;
#endif
    return 1;
}

/* Scalar fast path. Caller has already checked P and R are in range. 0 = use the Arm path. */
static int fast_single(const Prep &pr, const uint32_t P[4], uint32_t R, uint32_t f, const uint32_t Q[4], int &hit,
                       uint32_t Pp[4], uint32_t Prev[4]) {
    if (pr.wconst_bad) return 0;
    int fz = pr.fz;
    float px = bf(P[0]), py = bf(P[1]), pz = bf(P[2]), pw = bf(P[3]);
    float ex = px - bf(pr.A[0]), ey = py - bf(pr.A[1]), ez = pz - bf(pr.A[2]);
    if (!in_r_u(fb(ex)) || !in_r_u(fb(ey)) || !in_r_u(fb(ez))) return 0;
    float abx = bf(pr.AB[0]), aby = bf(pr.AB[1]), abz = bf(pr.AB[2]), abw = bf(pr.AB[3]);
    float sxy = abx * ex + aby * ey;
    float dot = sxy + abz * ez;
    if (!in_r_u(fb(sxy)) || !in_r_u(fb(dot))) return 0;
    float t0 = dot * bf(pr.k);
    float t = t0 < 0.0f ? 0.0f : t0 > 1.0f ? 1.0f : t0;
    if (!in_r_u(fb(t))) return 0;
    float Cx = bf(pr.A[0]) + abx * t, Cy = bf(pr.A[1]) + aby * t, Cz = bf(pr.A[2]) + abz * t;
    float Cw = bf(pr.A[3]) + abw * t;
    float dx = px - Cx, dy = py - Cy, dz = pz - Cz, dw = pw - Cw;
    if (!in_r_u(fb(dx)) || !in_r_u(fb(dy)) || !in_r_u(fb(dz))) return 0;
    float L = (dx * dx + dy * dy) + dz * dz;
    if (!in_r_u(fb(L))) return 0;
    float r;
    if (L <= pr.c0) r = 0.0f;
    else if (!newton_r(L, r)) return 0;
    float Rf = bf(R);
    float sc = ((L * r) - bf(pr.rad)) - Rf;
    if (!in_r_u(fb(sc))) return 0;
    if (fz && (bad_w_mid(fb(abw * t), fz) || bad_w_mid(fb(Cw), fz) || bad_w_mid(fb(dw), fz))) return 0;
    hit = sc < 0.0f;
    if (!hit) return 1;
    if (!in_r_u(Q[0]) || !in_r_u(Q[1]) || !in_r_u(Q[2]) || !in_r_u(f) || bad_w_in(Q[3], fz)) return 0;
    float nx = dx * r, ny = dy * r, nz = dz * r, nw = dw * r;
    float Sx = Cx + nx * bf(pr.rad), Sy = Cy + ny * bf(pr.rad), Sz = Cz + nz * bf(pr.rad);
    float m0 = nx * (px - Sx), m1 = ny * (py - Sy), m2 = nz * (pz - Sz);
    float mxy = m0 + m1;
    float dS = mxy + m2;
    float spv = Rf - dS;
    if (!in_r_u(fb(mxy)) || !in_r_u(fb(dS)) || !in_r_u(fb(spv))) return 0;
    float nsx = nx * spv, nsy = ny * spv, nsz = nz * spv, nsw = nw * spv;
    float Ppx = px + nsx, Ppy = py + nsy, Ppz = pz + nsz, Ppw = pw + nsw;
    if (!in_r_u(fb(Ppx)) || !in_r_u(fb(Ppy)) || !in_r_u(fb(Ppz))) return 0;
    float ux = Sx - bf(pr.c[0]), uy = Sy - bf(pr.c[1]), uz = Sz - bf(pr.c[2]);
    if (!in_r_u(fb(ux)) || !in_r_u(fb(uy)) || !in_r_u(fb(uz))) return 0;
    float wx = bf(pr.w[0]), wy = bf(pr.w[1]), wz = bf(pr.w[2]);
    float X = wy * uz - uy * wz, Y = wz * ux - uz * wx, Z = wx * uy - ux * wy;
    if (!in_r_u(fb(X)) || !in_r_u(fb(Y)) || !in_r_u(fb(Z))) return 0;
    float Vx = bf(pr.v[0]) + X, Vy = bf(pr.v[1]) + Y, Vz = bf(pr.v[2]) + Z, Vw = bf(pr.v[3]) + 0.0f;
    float dtf = bf(pr.dt);
    float qx = bf(Q[0]), qy = bf(Q[1]), qz = bf(Q[2]), qw = bf(Q[3]);
    float pqx = Ppx - qx, pqy = Ppy - qy, pqz = Ppz - qz, pqw = Ppw - qw;
    float rdx = pqx - Vx * dtf, rdy = pqy - Vy * dtf, rdz = pqz - Vz * dtf, rdw = pqw - Vw * dtf;
    if (!in_r_u(fb(pqx)) || !in_r_u(fb(pqy)) || !in_r_u(fb(pqz))) return 0;
    if (!in_r_u(fb(rdx)) || !in_r_u(fb(rdy)) || !in_r_u(fb(rdz))) return 0;
    float a0 = nx * rdx, a1 = ny * rdy, a2 = nz * rdz;
    float axy = a0 + a1;
    float dn = axy + a2;
    if (!in_r_u(fb(axy)) || !in_r_u(fb(dn))) return 0;
    float ff = bf(f);
    float Tx = rdx - nx * dn, Ty = rdy - ny * dn, Tz = rdz - nz * dn, Tw = rdw - nw * dn;
    float tfx = Tx * ff, tfy = Ty * ff, tfz = Tz * ff, tfw = Tw * ff;
    if (!in_r_u(fb(tfx)) || !in_r_u(fb(tfy)) || !in_r_u(fb(tfz))) return 0;
    if (fz) {
        uint32_t ws[10] = {fb(nw), fb(nsw), fb(Ppw), fb(pqw), fb(Vw), fb(Vw * dtf), fb(rdw), fb(nw * dn), fb(Tw), fb(tfw)};
        for (int i = 0; i < 10; i++)
            if (bad_w_mid(ws[i], fz)) return 0;
    }
    Pp[0] = fb(Ppx); Pp[1] = fb(Ppy); Pp[2] = fb(Ppz); Pp[3] = fb(Ppw);
    Prev[0] = fb(qx + tfx); Prev[1] = fb(qy + tfy); Prev[2] = fb(qz + tfz); Prev[3] = fb(qw + tfw);
    if (!in_r_u(Prev[0]) || !in_r_u(Prev[1]) || !in_r_u(Prev[2])) return 0;
    if (fz && bad_w_mid(Prev[3], fz)) return 0;
    return 1;
}

static void store4(uint8_t *p, const uint32_t w[4], int do_canon) {
    uint32_t o[4];
    for (int k = 0; k < 4; k++) o[k] = do_canon ? canon_u(w[k]) : w[k];
    memcpy(p, o, 16);
}

extern "C" void cloth_capsule_batches(ClothCapBatches *b) {
    Mx mx;
    (void)mx;
    Prep pr;
    make_prep(b->k, pr);
    for (uint32_t i = 0; i < b->n; i++) {
        uint16_t ix[16];
        memcpy(ix, b->indices + 32u * i, 32);
        if (dup16(ix)) {
            b->done = i;
            b->why = CLOTH_CAP_INDEX;
            return;
        }
        Slot s[16];
        uint16_t mask = 0;
        int via = 0;
        if (!CAP_FORCE_SLOW && pr.batch_r)
            via = pr.fz ? fast_batch<1>(pr, b, i, ix, s, &mask) : fast_batch<0>(pr, b, i, ix, s, &mask);
        if (!via) {
            if (!slow_batch(pr, b, i, ix, s, &mask)) {
                b->done = i;
                b->why = CLOTH_CAP_VALUES;
                return;
            }
        }
        store_slots(b->pos, b->prev, ix, s, via);
        if (b->contacts) b->contacts[i] = mask;
    }
    b->done = b->n;
    b->why = CLOTH_CAP_ALL;
}

extern "C" void cloth_capsule_singles(ClothCapSingles *s) {
    Mx mx;
    (void)mx;
    Prep pr;
    make_prep(s->k, pr);
    uint32_t count = 0;
    for (uint32_t i = 0; i < s->n; i++) {
        uint16_t ix;
        memcpy(&ix, s->indices + 2u * i, 2);
        uint32_t P[4], R, f, Q[4];
        load4(s->pos + 16u * ix, P, pr.fz);
        memcpy(&R, s->data + 16u * ix + 8, 4);
        R = flush_u(R, pr.fz);
        memcpy(&f, s->data + 16u * ix + 12, 4);
        f = flush_u(f, pr.fz);
        load4(s->prev + 16u * ix, Q, pr.fz);
        int hit = 0;
        uint32_t Pp[4], Prev[4];
        int via = 0;
        if (!CAP_FORCE_SLOW && pr.base_r && in_r_u(R) && in_r_u(P[0]) && in_r_u(P[1]) && in_r_u(P[2]) &&
            !bad_w_in(P[3], pr.fz))
            via = fast_single(pr, P, R, f, Q, hit, Pp, Prev);
        if (!via) {
            uint32_t TF[4], fl = 0;
            if (!slow_particle(pr, P, R, Q, f, 0, 0, 0, hit, Pp, TF)) {
                s->done = i;
                s->why = CLOTH_CAP_VALUES;
                s->contacts = count;
                return;
            }
            if (hit) {
                for (int k = 0; k < 4; k++) Prev[k] = cw::arm_add(Q[k], TF[k], (uint32_t)pr.fz, fl);
            }
        }
        if (hit) {
            count++;
            store4(s->pos + 16u * ix, Pp, via);
            store4(s->prev + 16u * ix, Prev, via);
        }
    }
    s->done = s->n;
    s->why = CLOTH_CAP_ALL;
    s->contacts = count;
}
