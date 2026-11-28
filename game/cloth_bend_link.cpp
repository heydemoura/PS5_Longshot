/* Bend-link pass: Gauss-Seidel min/max distance constraints, bit-exact inside SPEC.md 4.
   Lanes x,y,z (and a provable lane w) stay on SSE. The hand-back is written once, after the last link. */
#include "cloth_bend_link.h"
#include "cloth_wlane.hpp"
#include "cloth_common.hpp"
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif
#include <string.h>
#if KC_SIMD
#include <immintrin.h>
#endif

#if defined(__GNUC__)
#define BL_COLD __attribute__((noinline, cold))
#define BL_AINLINE __attribute__((always_inline)) inline
#define BL_LIKELY(x) __builtin_expect(!!(x), 1)
#define BL_UNLIKELY(x) __builtin_expect(!!(x), 0)
#else
#define BL_COLD
#define BL_AINLINE inline
#define BL_LIKELY(x) (x)
#define BL_UNLIKELY(x) (x)
#endif

static inline void bl_barrier(void) {
#if defined(__GNUC__)
    asm volatile("" ::: "memory");
#endif
}

static void bl_stop(ClothBendLinkPass *p, uint32_t done, int why, uint32_t fl, int fz, uint32_t slo,
                    uint32_t shi) {
    p->done = done;
    p->why = why;
    p->fpsr = fl | ((fz && done) ? (uint32_t)cw::IDC : 0u);
    p->slot_lo = slo;
    p->slot_hi = shi;
}

static inline void bl_note(uint32_t &slo, uint32_t &shi, uint32_t s) {
    if (s < slo) slo = s;
    if (s > shi) shi = s;
}

#if KC_SIMD

struct BlAcc {
    __m128i mx, mn;
    BL_AINLINE void reset() {
        mx = _mm_setzero_si128();
        mn = _mm_set1_epi32(-1);
    }
    BL_AINLINE void add(__m128 v, __m128i amask) {
        const __m128i a = _mm_and_si128(_mm_castps_si128(v), amask);
        mx = _mm_max_epu32(mx, a);
        mn = _mm_min_epu32(mn, _mm_sub_epi32(a, _mm_set1_epi32(1)));
    }
    BL_AINLINE bool ok() const {
        const __m128i hi = _mm_set1_epi32(0x5effffff), lo = _mm_set1_epi32(0x1fffffff);
        const __m128i e = _mm_and_si128(_mm_cmpeq_epi32(_mm_max_epu32(mx, hi), hi),
                                        _mm_cmpeq_epi32(_mm_min_epu32(mn, lo), lo));
        return _mm_movemask_epi8(e) == 0xffff;
    }
};

static inline __m128 bl_fmax4(__m128 a, __m128 b) {
    const __m128 eq = _mm_cmpeq_ps(a, b);
    return _mm_blendv_ps(_mm_max_ps(a, b), _mm_and_ps(a, b), eq);
}

/* Normalize L by an even power of two into [0.5, 2). Each bucket then has
   a fixed estimate and exact half-square (the significand has only 9 bits). */
struct BlRsqTable {
    struct Entry { uint32_t est; float half_square; };
    Entry t[512]{};
    constexpr BlRsqTable() {
        for (unsigned i = 0; i < 512; ++i) {
            const unsigned exponent = 127 - (i >> 8);
            const unsigned fraction = kc::RSQ.t[i];
            const float e = (256 + fraction) * ((i >> 8) ? 0x1p-9f : 0x1p-8f);
            t[i] = {(exponent << 23) | (fraction << 15), e * e * 0.5f};
        }
    }

    /* Check the full normalized interval of each bucket, including both endpoints.
       The product is linear in L and the binary64 endpoint products are exact. */
    constexpr bool step_in_range() const {
        for (unsigned i = 0; i < 512; ++i) {
            const double width = i < 256 ? 0x1p-9 : 0x1p-8;
            const double lo = (256 + (i & 255)) * width;
            if (lo * t[i].half_square < 0.25 || (lo + width) * t[i].half_square > 0.75)
                return false;
        }
        return true;
    }
};
static constexpr BlRsqTable bl_rsq_table{};
static_assert(bl_rsq_table.step_in_range(), "L * estimate^2 must stay in [0.5, 1.5]");

static BL_AINLINE __m128 bl_rsq(__m128 Lv) {
    const uint32_t u = (uint32_t)_mm_cvtsi128_si32(_mm_castps_si128(Lv));
    if (BL_UNLIKELY(!u)) return _mm_setzero_ps();
    const auto &entry = bl_rsq_table.t[(u >> 15) & 511];
    const float normalized = cw::f32((u & 0xffffffu) | 0x3f000000u);
    /* L = normalized * 2^(2k). Subtract k from the table estimate's exponent. */
    const uint32_t adjust = (63u << 23) - ((u >> 1) & (127u << 23));
    const float estimate = cw::f32(entry.est + adjust);
    /* The table bounds prove the STEP check for all L in R. The factor
       1/2 is exact in half_square, so the subtraction still rounds only once. */
#if KC_FRSQRTS_FMA
    const __m128 step = _mm_fnmadd_ss(_mm_set_ss(normalized), _mm_set_ss(entry.half_square), _mm_set_ss(1.5f));
#else
    const __m128 step = _mm_set_ss((float)(1.5 - (double)normalized * entry.half_square));
#endif
    const __m128 r = _mm_mul_ss(_mm_set_ss(estimate), step);
    return _mm_shuffle_ps(r, r, 0);
}

/* Lane w from the original bits, when the host lane is not provable. */
BL_COLD static void bl_patch_w(__m128 &P1n, __m128 &P2n, __m128 &dv2, uint32_t w1, uint32_t w2, uint32_t rb,
                               uint32_t corr1, uint32_t m1b, uint32_t m2b, int same, uint32_t fz,
                               uint32_t &wflags) {
    uint32_t fl = 0;
    const uint32_t dw = cw::arm_sub(w2, w1, fz, fl);
    (void)cw::arm_mul(dw, dw, fz, fl);
    const uint32_t dnw = cw::arm_mul(dw, rb, fz, fl);
    const uint32_t dn2w = cw::arm_mul(dnw, corr1, fz, fl);
    const uint32_t dv1w = cw::arm_mul(dn2w, m1b, fz, fl);
    const uint32_t p1w = cw::arm_add(w1, dv1w, fz, fl);
    const uint32_t dv2w = cw::arm_mul(dn2w, m2b, fz, fl);
    const uint32_t p2rw = same ? p1w : w2;
    const uint32_t p2w = cw::arm_sub(p2rw, dv2w, fz, fl);
    P1n = cw::with3(P1n, p1w);
    P2n = cw::with3(P2n, p2w);
    dv2 = cw::with3(dv2, dv2w);
    wflags |= fl;
}

/* 0: signed zeros. 1: all w outputs are the default NaN. 2: Arm lane w.
   3: host lane with NaN canonicalization. */
static BL_AINLINE int bl_w_early(uint32_t w1, uint32_t w2, int fz, int fast) {
    if (((w1 | w2) & 0x7fffffffu) == 0u) return 0;
    const int cin = (int)cw::canon_in(w1) & (int)cw::canon_in(w2);
    if (cin && (w1 == cw::DEFAULT_NAN || w2 == cw::DEFAULT_NAN)) return 1;
    if (cin && !fz && fast) return 3;
    return 2;
}

template <bool ZeroClamp>
static void bl_loop(ClothBendLinkPass *p) {
    const int fz = p->fz;
    const uint32_t n = p->n;
    const int fast = ((p->fpsr_in & cw::FAST_FLAGS) == cw::FAST_FLAGS);
    uint8_t *const slots = p->slots;
    const uint8_t *const particles = p->particles;
    const uint8_t *const links = p->links;
    const float scale = cw::f32(p->scale);
    const __m128 clamp4 = ZeroClamp ? _mm_setzero_ps()
                                     : _mm_set_ps(cw::f32(p->clamp[1]), cw::f32(p->clamp[0]),
                                                  cw::f32(p->clamp[1]), cw::f32(p->clamp[0]));
    const __m128 zero = _mm_setzero_ps();
    const __m128i abs4 = _mm_set1_epi32(0x7fffffff);
    const __m128i abs3 = _mm_set_epi32(0, 0x7fffffff, 0x7fffffff, 0x7fffffff);

    uint32_t slo = 0xffffffffu, shi = 0, wflags = 0;
    __m128 end_P2 = zero, end_dv = zero;
    uint32_t end_i1 = 0, end_i2 = 0, end_pa0 = 0, end_pa1 = 0, end_klo = 0, end_khi = 0;
    float end_m2 = 0.f, end_r = 0.f;

    for (uint32_t i = 0; i < n; i++) {
        const uint8_t *lp = links + (size_t)i * 20u;
        uint32_t hdr;
        memcpy(&hdr, lp, 4);
        const uint32_t i1 = hdr & 0xffffu;
        const uint32_t i2 = hdr >> 16;
        const __m128 lf = _mm_loadu_ps((const float *)(const void *)(lp + 4));
        uint8_t *const sp1 = slots + ((size_t)i1 << 4);
        uint8_t *const sp2 = slots + ((size_t)i2 << 4);
        const __m128 P1 = _mm_loadu_ps((const float *)(const void *)sp1);
        const __m128 P2 = _mm_loadu_ps((const float *)(const void *)sp2);
        const __m128 m1s = _mm_load_ss((const float *)(const void *)(particles + ((size_t)i1 << 4) + 4));
        const __m128 m2s = _mm_load_ss((const float *)(const void *)(particles + ((size_t)i2 << 4) + 4));
        const __m128 m1v = _mm_shuffle_ps(m1s, m1s, 0);
        const __m128 m2v = _mm_shuffle_ps(m2s, m2s, 0);

        const uint32_t w1 = (uint32_t)_mm_extract_ps(P1, 3);
        const uint32_t w2 = (uint32_t)_mm_extract_ps(P2, 3);
        const int early = bl_w_early(w1, w2, fz, fast);
        const int same = (i1 == i2);

        const __m128 d = _mm_sub_ps(P2, P1);
        const __m128 q = _mm_mul_ps(d, d);
        const __m128 Sy = _mm_add_ss(q, _mm_movehdup_ps(q));
        const __m128 Lv = _mm_add_ss(Sy, _mm_movehl_ps(q, q));

        BlAcc acc;
        acc.reset();
        acc.add(P1, abs3);
        acc.add(P2, abs3);
        acc.add(lf, abs4);
        /* q is nonnegative. Once q and L are in R, the partial sum is too. */
        acc.add(_mm_movelh_ps(_mm_unpacklo_ps(m1s, m2s), Lv), abs3);
        acc.add(d, abs3);
        acc.add(q, abs3);

        const __m128 rv = bl_rsq(Lv);
        const __m128 lenv = _mm_mul_ps(_mm_shuffle_ps(Lv, Lv, 0), rv);
        const __m128 dn = _mm_mul_ps(d, rv);
        /* For nonzero L in R, |k| <= 31. The normalized estimate is in [0.5,2)
           and the step in [0.75,1.25]. Scaling their product by 2^-k for r,
           or by normalized * 2^k for len, stays well inside R after rounding. */
        acc.add(dn, abs3);

        /* Evaluate the low and high bound together, with one pair of lanes per bound. */
        const __m128 limits = _mm_unpacklo_ps(lf, lf);
        __m128 errors;
        if constexpr (ZeroClamp) {
            /* Reversing the high subtraction can change the sign of an exact zero.
               Both signs pass the class test and max(error, +0) gives +0. */
            errors = _mm_xor_ps(_mm_sub_ps(limits, lenv), _mm_set_ps(-0.f, -0.f, 0.f, 0.f));
        } else {
            errors = _mm_sub_ps(_mm_blend_ps(limits, lenv, 12), _mm_blend_ps(lenv, limits, 12));
        }
        const __m128 bounded = ZeroClamp ? _mm_max_ps(errors, zero) : bl_fmax4(clamp4, errors);
        const __m128 stiffness = _mm_unpackhi_ps(lf, lf);
        const __m128 pushes = _mm_mul_ps(bounded, stiffness);
        const __m128 df = _mm_sub_ps(_mm_movehl_ps(pushes, pushes), _mm_movelh_ps(pushes, pushes));
        const __m128 corr = _mm_mul_ps(df, _mm_set1_ps(scale));
        acc.add(errors, abs4);
        acc.add(pushes, abs4);
        acc.add(df, abs4);
        acc.add(corr, abs4);

        const __m128 dn2 = _mm_mul_ps(dn, corr);
        const __m128 dv1 = _mm_mul_ps(dn2, m1v);
        const __m128 dv2m = _mm_mul_ps(dn2, m2v);
        acc.add(dn2, abs3);
        acc.add(dv1, abs3);
        acc.add(dv2m, abs3);
        __m128 P1n = _mm_add_ps(P1, dv1);
        __m128 P2r = P2;
        if (BL_UNLIKELY(same)) {
            P2r = P1n;
            acc.add(P1n, abs3);
        }
        __m128 dv2 = dv2m;
        __m128 P2n = _mm_sub_ps(P2r, dv2);

        if (BL_UNLIKELY(!acc.ok())) {
            bl_stop(p, i, CLOTH_BEND_LINK_CLASS, wflags, fz, slo, shi);
            return;
        }

        if (BL_UNLIKELY(early == 2)) {
            const uint32_t corr1 = (uint32_t)_mm_extract_ps(corr, 1);
            const uint32_t m1b = (uint32_t)_mm_cvtsi128_si32(_mm_castps_si128(m1s));
            const uint32_t m2b = (uint32_t)_mm_cvtsi128_si32(_mm_castps_si128(m2s));
            bl_patch_w(P1n, P2n, dv2, w1, w2, (uint32_t)_mm_cvtsi128_si32(_mm_castps_si128(rv)), corr1, m1b,
                       m2b, same, (uint32_t)fz, wflags);
        } else if (early == 1) {
            /* The default NaN propagates through every w result. A subnormal
               other input under FZ can only add IDC, already due to the tiny test. */
            const __m128 nan = _mm_castsi128_ps(_mm_set1_epi32(cw::DEFAULT_NAN));
            P1n = _mm_blend_ps(P1n, nan, 8);
            P2n = _mm_blend_ps(P2n, nan, 8);
            dv2 = _mm_blend_ps(dv2, nan, 8);
        } else if (early == 3) {
            P1n = cw::w_canon(P1n);
            P2n = cw::w_canon(P2n);
            dv2 = cw::w_canon(dv2);
        }

        if (BL_UNLIKELY(i + 1u == n)) {
            end_i1 = i1;
            end_i2 = i2;
            end_P2 = P2n;
            end_dv = dv2;
            end_pa0 = (uint32_t)_mm_extract_ps(pushes, 0);
            end_pa1 = (uint32_t)_mm_extract_ps(pushes, 1);
            end_klo = (uint32_t)_mm_extract_ps(lf, 2);
            end_khi = (uint32_t)_mm_extract_ps(lf, 3);
            end_m2 = _mm_cvtss_f32(m2s);
            end_r = _mm_cvtss_f32(rv);
        }
        _mm_storeu_ps((float *)(void *)sp1, P1n);
        bl_barrier();
        _mm_storeu_ps((float *)(void *)sp2, P2n);
        bl_barrier();
        bl_note(slo, shi, i1);
        bl_note(slo, shi, i2);
    }

    p->done = n;
    p->why = CLOTH_BEND_LINK_ALL;
    p->fpsr = wflags | (fz ? (uint32_t)cw::IDC : 0u);
    p->slot_lo = slo;
    p->slot_hi = shi;
    p->end.p1 = end_i1;
    p->end.p2 = end_i2;
    kc::st4u(p->end.p2_new, end_P2);
    kc::st4u(p->end.p2_step, end_dv);
    p->end.m2 = cw::u32(end_m2);
    p->end.r = cw::u32(end_r);
    p->end.push[0] = end_pa0;
    p->end.push[1] = end_pa1;
    p->end.k_lo = end_klo;
    p->end.k_hi = end_khi;
}

void cloth_bend_link_pass(ClothBendLinkPass *p) {
    if (!kc::in_r_bits(p->clamp[0]) || !kc::in_r_bits(p->clamp[1]) || !kc::in_r_bits(p->scale)) {
        bl_stop(p, 0, CLOTH_BEND_LINK_ENTRY, 0, p->fz, 1u, 0u);
        return;
    }
    if (p->clamp[0] == 0u && p->clamp[1] == 0u) bl_loop<true>(p);
    else bl_loop<false>(p);
}

#else

static inline int bl_in(float x) { return (int)kc::in_r(x); }

static inline float bl_fmax(float a, float b) {
    if (a > b) return a;
    if (b > a) return b;
    return cw::f32(cw::u32(a) & cw::u32(b));
}

struct BlV {
    float x, y, z, w;
};

static inline BlV bl_ld4(const uint8_t *p) {
    BlV v;
    memcpy(&v, p, 16);
    return v;
}

static inline void bl_st4(uint8_t *p, float x, float y, float z, float w) {
    float a[4] = {x, y, z, w};
    memcpy(p, a, 16);
}

void cloth_bend_link_pass(ClothBendLinkPass *p) {
    const int fz = p->fz;
    const uint32_t fz_u = (uint32_t)fz;
    const uint32_t n = p->n;
    if (!kc::in_r_bits(p->clamp[0]) || !kc::in_r_bits(p->clamp[1]) || !kc::in_r_bits(p->scale)) {
        bl_stop(p, 0, CLOTH_BEND_LINK_ENTRY, 0, fz, 1u, 0u);
        return;
    }
    uint8_t *const slots = p->slots;
    const uint8_t *const particles = p->particles;
    const uint8_t *const links = p->links;
    const float scale = cw::f32(p->scale);
    const float c0 = cw::f32(p->clamp[0]);
    const float c1 = cw::f32(p->clamp[1]);

    uint32_t slo = 0xffffffffu, shi = 0, wflags = 0;
    uint32_t last_i1 = 0, last_i2 = 0, last_p2w = 0, last_dv2w = 0;
    float last_p2x = 0, last_p2y = 0, last_p2z = 0;
    float last_dv2x = 0, last_dv2y = 0, last_dv2z = 0;
    float last_m2 = 0, last_r = 0, last_pa0 = 0, last_pa1 = 0, last_klo = 0, last_khi = 0;

    for (uint32_t i = 0; i < n; i++) {
        const uint8_t *lp = links + (size_t)i * 20u;
        const uint32_t i1 = kc::ld16(lp);
        const uint32_t i2 = kc::ld16(lp + 2);
        float linkf[4];
        memcpy(linkf, lp + 4, 16);
        const float lo = linkf[0], hi = linkf[1], klo = linkf[2], khi = linkf[3];
        const BlV P1 = bl_ld4(slots + ((size_t)i1 << 4));
        const BlV P2 = bl_ld4(slots + ((size_t)i2 << 4));
        const float m1 = kc::ld1(particles + ((size_t)i1 << 4) + 4);
        const float m2 = kc::ld1(particles + ((size_t)i2 << 4) + 4);
        const int same = (i1 == i2);

        int ok = bl_in(P1.x) & bl_in(P1.y) & bl_in(P1.z);
        ok &= bl_in(P2.x) & bl_in(P2.y) & bl_in(P2.z);
        ok &= bl_in(lo) & bl_in(hi) & bl_in(klo) & bl_in(khi);
        ok &= bl_in(m1) & bl_in(m2);

        const float dx = P2.x - P1.x, dy = P2.y - P1.y, dz = P2.z - P1.z;
        ok &= bl_in(dx) & bl_in(dy) & bl_in(dz);
        const float qx = dx * dx, qy = dy * dy, qz = dz * dz;
        ok &= bl_in(qx) & bl_in(qy) & bl_in(qz);
        const float S = qx + qy;
        const float L = S + qz;
        ok &= bl_in(S) & bl_in(L);

        unsigned bad = 0;
        const float r = kc::rsq_step(L, bad);
        const float len = L * r;
        ok &= bl_in(r) & bl_in(len);
        const float dnx = dx * r, dny = dy * r, dnz = dz * r;
        ok &= bl_in(dnx) & bl_in(dny) & bl_in(dnz);

        const float ea = lo - len;
        const float eb = len - hi;
        ok &= bl_in(ea) & bl_in(eb);
        const float a0 = bl_fmax(c0, ea), a1 = bl_fmax(c1, ea);
        const float b0 = bl_fmax(c0, eb), b1 = bl_fmax(c1, eb);
        const float pa0 = a0 * klo, pa1 = a1 * klo;
        const float pb0 = b0 * khi, pb1 = b1 * khi;
        ok &= bl_in(pa0) & bl_in(pa1) & bl_in(pb0) & bl_in(pb1);
        const float df0 = pb0 - pa0, df1 = pb1 - pa1;
        ok &= bl_in(df0) & bl_in(df1);
        const float corr0 = df0 * scale, corr1 = df1 * scale;
        ok &= bl_in(corr0) & bl_in(corr1);

        const float dn2x = dnx * corr0, dn2y = dny * corr1, dn2z = dnz * corr0;
        ok &= bl_in(dn2x) & bl_in(dn2y) & bl_in(dn2z);
        const float dv1x = dn2x * m1, dv1y = dn2y * m1, dv1z = dn2z * m1;
        ok &= bl_in(dv1x) & bl_in(dv1y) & bl_in(dv1z);
        const float p1x = P1.x + dv1x, p1y = P1.y + dv1y, p1z = P1.z + dv1z;
        const float dv2x = dn2x * m2, dv2y = dn2y * m2, dv2z = dn2z * m2;
        ok &= bl_in(dv2x) & bl_in(dv2y) & bl_in(dv2z);
        const float p2rx = same ? p1x : P2.x;
        const float p2ry = same ? p1y : P2.y;
        const float p2rz = same ? p1z : P2.z;
        ok &= bl_in(p2rx) & bl_in(p2ry) & bl_in(p2rz);
        const float p2x = p2rx - dv2x, p2y = p2ry - dv2y, p2z = p2rz - dv2z;

        if (!ok) {
            bl_stop(p, i, CLOTH_BEND_LINK_CLASS, wflags, fz, slo, shi);
            return;
        }
        if (bad) {
            bl_stop(p, i, CLOTH_BEND_LINK_STEP, wflags, fz, slo, shi);
            return;
        }

        uint32_t fl = 0;
        const uint32_t uw1 = cw::u32(P1.w), uw2 = cw::u32(P2.w);
        const uint32_t dw = cw::arm_sub(uw2, uw1, fz_u, fl);
        (void)cw::arm_mul(dw, dw, fz_u, fl);
        const uint32_t dnw = cw::arm_mul(dw, cw::u32(r), fz_u, fl);
        const uint32_t dn2w = cw::arm_mul(dnw, cw::u32(corr1), fz_u, fl);
        const uint32_t dv1w = cw::arm_mul(dn2w, cw::u32(m1), fz_u, fl);
        const uint32_t p1w = cw::arm_add(uw1, dv1w, fz_u, fl);
        const uint32_t dv2w = cw::arm_mul(dn2w, cw::u32(m2), fz_u, fl);
        const uint32_t p2rw = same ? p1w : uw2;
        const uint32_t p2w = cw::arm_sub(p2rw, dv2w, fz_u, fl);
        wflags |= fl;

        bl_st4(slots + ((size_t)i1 << 4), p1x, p1y, p1z, cw::f32(p1w));
        bl_barrier();
        bl_st4(slots + ((size_t)i2 << 4), p2x, p2y, p2z, cw::f32(p2w));
        bl_barrier();
        bl_note(slo, shi, i1);
        bl_note(slo, shi, i2);

        last_i1 = i1;
        last_i2 = i2;
        last_p2x = p2x;
        last_p2y = p2y;
        last_p2z = p2z;
        last_p2w = p2w;
        last_dv2x = dv2x;
        last_dv2y = dv2y;
        last_dv2z = dv2z;
        last_dv2w = dv2w;
        last_m2 = m2;
        last_r = r;
        last_pa0 = pa0;
        last_pa1 = pa1;
        last_klo = klo;
        last_khi = khi;
    }

    p->done = n;
    p->why = CLOTH_BEND_LINK_ALL;
    p->fpsr = wflags | (fz ? (uint32_t)cw::IDC : 0u);
    p->slot_lo = slo;
    p->slot_hi = shi;
    p->end.p1 = last_i1;
    p->end.p2 = last_i2;
    p->end.p2_new[0] = cw::u32(last_p2x);
    p->end.p2_new[1] = cw::u32(last_p2y);
    p->end.p2_new[2] = cw::u32(last_p2z);
    p->end.p2_new[3] = last_p2w;
    p->end.p2_step[0] = cw::u32(last_dv2x);
    p->end.p2_step[1] = cw::u32(last_dv2y);
    p->end.p2_step[2] = cw::u32(last_dv2z);
    p->end.p2_step[3] = last_dv2w;
    p->end.m2 = cw::u32(last_m2);
    p->end.r = cw::u32(last_r);
    p->end.push[0] = cw::u32(last_pa0);
    p->end.push[1] = cw::u32(last_pa1);
    p->end.k_lo = cw::u32(last_klo);
    p->end.k_hi = cw::u32(last_khi);
}

#endif
