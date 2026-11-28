/* Shared float helpers for the cloth kernels (C++, header-only, inline). The Arm architecture's rules as the Arm
   Architecture Reference Manual defines them (its pseudocode functions are named where used); nothing here is taken
   from the game.

   The exactness class used by the kernels: every operand of every float operation zero or of magnitude in
   R = [2^-63, 2^63) (bits 0x20000000..0x5effffff). Then a product of two class values lies in [2^-126, 2^126) or is
   zero, a sum is zero or a multiple of 2^-86 below 2^64: never tiny, infinite or NaN, so IEEE round-to-nearest gives
   the Arm bits, flush-to-zero flushes nothing, and the only flag such an operation raises is Inexact (which the caller
   guarantees is already set in the guest's float status). */
#ifndef CLOTH_COMMON_HPP
#define CLOTH_COMMON_HPP
#include <float.h>
#include <stdint.h>
#include <string.h>
#if defined(__SSE4_1__)
#include <smmintrin.h>
#define KC_SIMD 1
#else
#define KC_SIMD 0
#endif

#if defined(__FAST_MATH__)
#error "the cloth kernels must not be built with -ffast-math: they rely on IEEE arithmetic"
#endif
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif

namespace kc {

/* ---- the Arm's single-precision reciprocal square root estimate (FPRSqrtEstimate / RecipSqrtEstimate in the Arm
   manual) for a positive normal input --------------------------------------------------------------------------------
   The index is the exponent's low bit and the fraction's top 8 bits (the pseudocode looks at 7 of them for an odd biased
   exponent); a in units of 1/512 (or 1/256), b the largest with a*b*b < 2^28, r = (b + 1) / 2, the result's fraction is
   r's low 8 bits, its exponent (380 - e) / 2. Checked against the Arm definition on every positive normal float, flush
   to zero off and on: equal, no flag raised. */
struct RsqTab {
    uint8_t t[512];
    constexpr RsqTab() : t() {
        for (uint32_t idx = 0; idx < 512; idx++) {
            uint32_t a = 0;
            if (idx >= 256) a = 2 * (128 + ((idx & 0xff) >> 1)) + 1;
            else a = 2 * ((256 + idx) | 1);
            uint32_t lo = 512, hi = 1024;
            while (lo + 1 < hi) {
                const uint32_t mid = (lo + hi) / 2;
                if ((uint64_t)a * mid * mid < (UINT64_C(1) << 28)) lo = mid; else hi = mid;
            }
            t[idx] = (uint8_t)(((lo + 1) / 2) & 255);
        }
    }
};
constexpr RsqTab RSQ{};

/* the estimate's bits for the bits u of a positive normal float */
inline uint32_t frsqrte_bits(uint32_t u) {
    const uint32_t e = u >> 23, f = u & 0x7fffffu;
    return (((380u - e) >> 1) << 23) | ((uint32_t)RSQ.t[((e & 1u) << 8) | (f >> 15)] << 15);
}

inline uint32_t fb(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
inline float bf(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
inline float ld1(const uint8_t *p) { float f; memcpy(&f, p, 4); return f; }
inline uint32_t ld16(const uint8_t *p) { uint16_t u; memcpy(&u, p, 2); return u; }
/* zero, or magnitude in [2^-63, 2^63) */
inline bool in_r(float x) { const uint32_t a = fb(x) & 0x7fffffffu; return a == 0 || a - 0x20000000u < 0x3f000000u; }
inline bool in_r_bits(uint32_t u) { return in_r(bf(u)); }
/* not a NaN and not a nonzero subnormal (a value only compared or only a minimum's operand) */
inline bool plain_bits(uint32_t u) {
    const uint32_t a = u & 0x7fffffffu;
    return a <= 0x7f800000u && (a == 0 || a >= 0x00800000u);
}

/* The reciprocal square root of a squared length L (zero or in R here), refined once: e = estimate(L), x = e*e,
   s = the Arm's fused step (3 - L*x) / 2 rounded once (FPRSqrtStepFused), r = e*s; or 0 when L is not above the
   largest subnormal 0x007fffff (for L zero or in R: exactly when L is zero, flush-to-zero or not).
   The step rounds (3 - L*x) / 2 once: with a hardware FMA (x86 FMA3, the x86-64-v3 level) that is fmaf(-L, x, 3) (one
   rounding of the exact 3 - L*x) halved exactly; without one, (float)(3.0 - (double)L*(double)x) * 0.5f, exact when L*x
   is in [1/8, 8]: the product is exact in binary64 (at most 48 significant bits), 3 - L*x then spans at most 53 bits so
   it is exact too, one rounding to binary32, and halving a value >= 2^-50 is exact. L*x is about 1 by the estimate's
   accuracy; `bad` is set when it is outside [1/8, 8] (the test is made either way; the caller then cannot vouch for
   the result). */
#if defined(__FMA__) && !defined(CLOTH_NO_FMA)
#define KC_FRSQRTS_FMA 1
#else
#define KC_FRSQRTS_FMA 0
#endif
inline float rsq_step(float L, unsigned &bad) {
    if (!(L > 0.0f)) return 0.0f;
    const float v = bf(frsqrte_bits(fb(L)));
    const float e = v * v;
    const double le = (double)L * (double)e;
    bad |= !(le >= 0.125 && le <= 8.0);
#if KC_FRSQRTS_FMA
    const float s = __builtin_fmaf(-L, e, 3.0f) * 0.5f;
#else
    const float s = (float)(3.0 - le) * 0.5f;
#endif
    return v * s;
}

/* The Arm's minimum (FPMin) of two values that are not NaN (and, under flush-to-zero, not subnormal): the smaller; of
   two equal ones the OR of their bits (min(-0, +0) = -0; equal nonzero values have equal bits). No flag. */
inline float fmin_arm(float a, float b) {
    if (a < b) return a;
    if (b < a) return b;
    return bf(fb(a) | fb(b));
}

inline void note_slot(uint32_t &lo, uint32_t &hi, uint32_t s) {
    if (s < lo) lo = s;
    if (s > hi) hi = s;
}

#if KC_SIMD
/* packed class check: unsigned max of |bits| and unsigned min of |bits| - 1 (a zero wraps to all ones) */
struct Checks {
    __m128i mx, mn;
    inline void reset() { mx = _mm_setzero_si128(), mn = _mm_set1_epi32(-1); }
    inline void add(__m128 v) {
        const __m128i a = _mm_and_si128(_mm_castps_si128(v), _mm_set1_epi32(0x7fffffff));
        mx = _mm_max_epu32(mx, a);
        mn = _mm_min_epu32(mn, _mm_sub_epi32(a, _mm_set1_epi32(1)));
    }
    /* byte mask of the lanes whose values are all in the class (0xffff: every lane; 0x0fff: lanes 0-2) */
    inline int mask() const {
        const __m128i hi = _mm_set1_epi32(0x5effffff), lo = _mm_set1_epi32(0x1fffffff);
        const __m128i e = _mm_and_si128(_mm_cmpeq_epi32(_mm_max_epu32(mx, hi), hi), _mm_cmpeq_epi32(_mm_min_epu32(mn, lo), lo));
        return _mm_movemask_epi8(e);
    }
    inline bool ok() const { return mask() == 0xffff; }
};
inline __m128 ld4(const uint8_t *p) { return _mm_loadu_ps((const float *)(const void *)p); }
inline void st4(uint8_t *p, __m128 v) { _mm_storeu_ps((float *)(void *)p, v); }
inline void st4u(uint32_t *p, __m128 v) { _mm_storeu_ps((float *)(void *)p, v); }
inline __m128 ld4u(const uint32_t *p) { return _mm_loadu_ps((const float *)(const void *)p); }
inline float lane0(__m128 v) { return _mm_cvtss_f32(v); }
inline float lane1(__m128 v) { return _mm_cvtss_f32(_mm_shuffle_ps(v, v, _MM_SHUFFLE(1, 1, 1, 1))); }
inline float lane2(__m128 v) { return _mm_cvtss_f32(_mm_movehl_ps(v, v)); }
#define KC_SPLAT(v, i) _mm_shuffle_ps((v), (v), _MM_SHUFFLE(i, i, i, i))
/* the Arm's minimum lane by lane for non-NaN operands: minps, but the OR of the two where they compare equal (signed
   zeros) */
inline __m128 fmin_arm4(__m128 a, __m128 b) {
    return _mm_blendv_ps(_mm_min_ps(a, b), _mm_or_ps(a, b), _mm_cmpeq_ps(a, b));
}
#endif

}  // namespace kc
#endif
