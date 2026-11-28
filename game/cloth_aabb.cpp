/* Particle bounding boxes. Fast path: SSE arithmetic where SPEC.md 5 says it matches the Arm,
   with min/max adjusted for signed zeros and lane-3 default NaNs. Anything else is one
   lane-at-a-time pass (cloth_wlane.hpp). */
#include "cloth_aabb.h"
#include "cloth_wlane.hpp"
#include "cloth_common.hpp"

#include <emmintrin.h>
#include <string.h>
#if defined(__SSE4_1__)
#include <smmintrin.h>
#endif

namespace {

constexpr uint32_t kHalf = 0x3f000000u;
constexpr uint32_t kThreeHalves = 0x3fc00000u;
constexpr uint32_t kDefaultNan = 0x7fc00000u;
constexpr uint32_t kMag2m100 = 0x0d800000u; /* smallest binary32 >= 2^-100 */

inline uint32_t load_u32(const uint8_t *p) {
    uint32_t u;
    memcpy(&u, p, 4);
    return u;
}

inline __m128 load4(const uint8_t *p) {
    return _mm_loadu_ps(reinterpret_cast<const float *>(p));
}

inline void store4(uint32_t *p, __m128 v) {
    _mm_storeu_ps(reinterpret_cast<float *>(p), v);
}

/* Arm min/max for non-NaN lanes (signed zeros: min ORs, max ANDs). A NaN lane follows SSE
   min/max; the caller replaces any lane that ever saw a NaN with the default NaN. */
inline __m128 fmin4(__m128 a, __m128 b) {
    const __m128 eq = _mm_cmpeq_ps(a, b);
#if defined(__SSE4_1__)
    return _mm_blendv_ps(_mm_min_ps(a, b), _mm_or_ps(a, b), eq);
#else
    return _mm_or_ps(_mm_andnot_ps(eq, _mm_min_ps(a, b)), _mm_and_ps(eq, _mm_or_ps(a, b)));
#endif
}

inline __m128 fmax4(__m128 a, __m128 b) {
    const __m128 eq = _mm_cmpeq_ps(a, b);
#if defined(__SSE4_1__)
    return _mm_blendv_ps(_mm_max_ps(a, b), _mm_and_ps(a, b), eq);
#else
    return _mm_or_ps(_mm_andnot_ps(eq, _mm_max_ps(a, b)), _mm_and_ps(eq, _mm_and_ps(a, b)));
#endif
}

[[maybe_unused]] inline __m128 class_ok_mask(__m128 v) {
    const __m128 ax = _mm_and_ps(v, _mm_castsi128_ps(_mm_set1_epi32(0x7fffffff)));
    const __m128 z = _mm_cmpeq_ps(ax, _mm_setzero_ps());
    const __m128i ai = _mm_castps_si128(ax);
    const __m128i ge = _mm_cmpgt_epi32(ai, _mm_set1_epi32(0x1fffffff));
    const __m128i le = _mm_xor_si128(_mm_cmpgt_epi32(ai, _mm_set1_epi32(0x5effffff)), _mm_set1_epi32(-1));
    return _mm_or_ps(z, _mm_castsi128_ps(_mm_and_si128(ge, le)));
}

inline uint32_t lane3_bits(__m128 v) {
    return (uint32_t)_mm_cvtsi128_si32(_mm_castps_si128(_mm_shuffle_ps(v, v, 0xFF)));
}

/* Lane 3 of a value read from memory. bad sticks when the fast lane-3 rule cannot apply. */
inline void note_l3(__m128 v, int fz, int &bad) {
    const uint32_t u = lane3_bits(v);
    const uint32_t a = u & 0x7fffffffu;
    const uint32_t nondet_nan = (a > 0x7f800000u) & (u != kDefaultNan);
    const uint32_t tiny = (a != 0u) & (a < kMag2m100);
    bad |= (int)(nondet_nan | (tiny & (uint32_t)fz));
}

inline int start_ok(const uint32_t s[4], int fz) {
    for (int k = 0; k < 4; k++) {
        const uint32_t a = s[k] & 0x7fffffffu;
        if (a > 0x7f800000u) return 0;                         /* NaN, signalling included */
        if (fz && a - 1u < 0x007fffffu) return 0;              /* nonzero subnormal, flushed */
    }
    return 1;
}

inline __m128 nan_mask_from_bits(int bits) {
    return _mm_castsi128_ps(_mm_set_epi32(
        (bits & 8) ? -1 : 0, (bits & 4) ? -1 : 0, (bits & 2) ? -1 : 0, (bits & 1) ? -1 : 0));
}

inline __m128 apply_default_nan(__m128 v, int bits) {
    if (bits == 0) return v;
    const __m128 def = _mm_castsi128_ps(_mm_set1_epi32((int)kDefaultNan));
    const __m128 k = nan_mask_from_bits(bits);
    return _mm_or_ps(_mm_andnot_ps(k, v), _mm_and_ps(k, def));
}

inline __m128 canon_nan(__m128 v) {
    const __m128 n = _mm_cmpunord_ps(v, v);
    const __m128 def = _mm_castsi128_ps(_mm_set1_epi32((int)kDefaultNan));
    return _mm_or_ps(_mm_andnot_ps(n, v), _mm_and_ps(n, def));
}

struct ClassAcc {
#if KC_SIMD
    kc::Checks c;
    inline void reset() { c.reset(); }
    inline void add(__m128 v) { c.add(v); }
    inline void lanes(int &l012, int &l3) const {
        const int m = c.mask();
        l012 = (m & 0x0fff) == 0x0fff;
        l3 = (m & 0xf000) == 0xf000;
    }
#else
    __m128 ok;
    inline void reset() { ok = _mm_castsi128_ps(_mm_set1_epi32(-1)); }
    inline void add(__m128 v) { ok = _mm_and_ps(ok, class_ok_mask(v)); }
    inline void lanes(int &l012, int &l3) const {
        const int m = _mm_movemask_ps(ok);
        l012 = (m & 7) == 7;
        l3 = (m & 8) != 0;
    }
#endif
};

/* Safe: Arm signed-zero min/max, spec order. Not Safe: SSE min/max, which matches the Arm
   when no lane is a zero (the only equal values with different bits). 2 = redo with Safe. */
template <bool Safe>
#if defined(__GNUC__)
__attribute__((noinline))
#endif
int aabb_fast(const ClothAabbArgs *a, ClothAabbResult *r) {
    const int fz = a->fz ? 1 : 0;
    if (!start_ok(a->start, fz)) return 0;
    const int flags_ok = ((a->fpsr_in & cw::FAST_FLAGS) == cw::FAST_FLAGS);

    const __m128 half = _mm_set1_ps(0.5f);
    const __m128 three_half = _mm_set1_ps(1.5f);
    const __m128 zero = _mm_setzero_ps();
    const __m128 S = load4(reinterpret_cast<const uint8_t *>(a->start));
    const __m128 nS = _mm_xor_ps(S, _mm_castsi128_ps(_mm_set1_epi32((int)0x80000000u)));

    __m128 pos_min = S, pos_max = nS;
    __m128 mov_min = S, mov_max = nS;
    __m128 mrk_min = S, mrk_max = nS;
    ClassAcc cls;
    cls.reset();
    int pos_nan = 0, mov_nan = 0, mrk_nan = 0;
    int l3_bad = 0, mass_bad = 0;
    int moved = 0;
    uint32_t moved_i = 0;
    __m128 anyz = _mm_setzero_ps();
    if constexpr (!Safe) anyz = _mm_cmpeq_ps(S, zero);

    const uint32_t n = a->n;
    const uint8_t *pos = a->pos;
    const uint8_t *prev = a->prev;
    const uint8_t *mask = a->mask;
    const uint8_t *inv = a->inv_mass;

    for (uint32_t i = 0; i < n; i++) {
        const __m128 P = load4(pos + (size_t)i * 16u);
        const uint32_t m = load_u32(mask + (size_t)i * 4u);
        cls.add(P);
        note_l3(P, fz, l3_bad);
        pos_nan |= _mm_movemask_ps(_mm_cmpunord_ps(P, P));
        if constexpr (!Safe) anyz = _mm_or_ps(anyz, _mm_cmpeq_ps(P, zero));
        if constexpr (Safe) {
            pos_min = fmin4(pos_min, P);
            pos_max = fmax4(pos_max, P);
        } else {
            pos_min = _mm_min_ps(pos_min, P);
            pos_max = _mm_max_ps(pos_max, P);
        }

        int skip = 0;
        if (m == 0u) {
            const uint32_t w = load_u32(inv + (size_t)i * 16u);
            const uint32_t aw = w & 0x7fffffffu;
            if (aw > 0x7f800000u || (fz && aw - 1u < 0x007fffffu)) mass_bad = 1;
            else skip = (aw == 0u);
        }
        if (!skip) {
            const __m128 Q = load4(prev + (size_t)i * 16u);
            cls.add(Q);
            note_l3(Q, fz, l3_bad);
            const __m128 D = _mm_sub_ps(P, Q);
            const __m128 mid = _mm_add_ps(Q, _mm_mul_ps(D, half));
            const __m128 ahead = _mm_add_ps(Q, _mm_mul_ps(D, three_half));
            mov_nan |= _mm_movemask_ps(_mm_or_ps(_mm_cmpunord_ps(mid, mid), _mm_cmpunord_ps(ahead, ahead)));
            if constexpr (!Safe) {
                anyz = _mm_or_ps(anyz, _mm_or_ps(_mm_cmpeq_ps(mid, zero), _mm_cmpeq_ps(ahead, zero)));
            }
            if constexpr (Safe) {
                mov_min = fmin4(fmin4(mov_min, mid), ahead);
                mov_max = fmax4(fmax4(mov_max, mid), ahead);
            } else {
                mov_min = _mm_min_ps(mov_min, _mm_min_ps(mid, ahead));
                mov_max = _mm_max_ps(mov_max, _mm_max_ps(mid, ahead));
            }
            if (m & 0x80000000u) {
                mrk_nan |= _mm_movemask_ps(_mm_or_ps(_mm_cmpunord_ps(mid, mid), _mm_cmpunord_ps(ahead, ahead)));
                if constexpr (Safe) {
                    mrk_min = fmin4(fmin4(mrk_min, mid), ahead);
                    mrk_max = fmax4(fmax4(mrk_max, mid), ahead);
                } else {
                    mrk_min = _mm_min_ps(mrk_min, _mm_min_ps(mid, ahead));
                    mrk_max = _mm_max_ps(mrk_max, _mm_max_ps(mid, ahead));
                }
            }
            moved = 1;
            moved_i = i;
        }
    }

    if (mass_bad) return 0;
    int l012 = 0, l3 = 0;
    cls.lanes(l012, l3);
    if (!l012) return 0;
    if (!l3 && !(flags_ok && !l3_bad)) return 0;
    if constexpr (!Safe) {
        if (_mm_movemask_ps(anyz)) return 2;
    }

    store4(r->pos_min, apply_default_nan(pos_min, pos_nan));
    store4(r->pos_max, apply_default_nan(pos_max, pos_nan));
    store4(r->moving_min, apply_default_nan(mov_min, mov_nan));
    store4(r->moving_max, apply_default_nan(mov_max, mov_nan));
    store4(r->marked_min, apply_default_nan(mrk_min, mrk_nan));
    store4(r->marked_max, apply_default_nan(mrk_max, mrk_nan));

    const __m128 lastP = load4(pos + (size_t)(n - 1u) * 16u);
    const uint32_t lastm = load_u32(mask + (size_t)(n - 1u) * 4u);
    store4(r->last_pos, lastP);
    r->last_mask = lastm;
    r->moved = moved ? 1u : 0u;

    int last_skip = 0;
    uint32_t last_w = 0;
    if (lastm == 0u) {
        last_w = load_u32(inv + (size_t)(n - 1u) * 16u);
        last_skip = ((last_w & 0x7fffffffu) == 0u);
    }
    if (last_skip) {
        store4(r->last_mid, _mm_castsi128_ps(_mm_cvtsi32_si128((int)last_w)));
    } else {
        const __m128 Q = load4(prev + (size_t)(n - 1u) * 16u);
        const __m128 D = _mm_sub_ps(lastP, Q);
        store4(r->last_mid, canon_nan(_mm_add_ps(Q, _mm_mul_ps(D, half))));
    }
    if (moved) {
        const __m128 P = load4(pos + (size_t)moved_i * 16u);
        const __m128 Q = load4(prev + (size_t)moved_i * 16u);
        const __m128 D = _mm_sub_ps(P, Q);
        const __m128 step = _mm_mul_ps(D, three_half);
        store4(r->ahead, canon_nan(_mm_add_ps(Q, step)));
        store4(r->ahead_step, canon_nan(step));
    } else {
        store4(r->ahead, zero);
        store4(r->ahead_step, zero);
    }
    return 1;
}

uint32_t aabb_scalar(const ClothAabbArgs *a, ClothAabbResult *r) {
    uint32_t fl = 0;
    const uint32_t fz = a->fz ? 1u : 0u;
    uint32_t pmin[4], pmax[4], vmin[4], vmax[4], mmin[4], mmax[4];
    for (int k = 0; k < 4; k++) {
        const uint32_t s = a->start[k];
        pmin[k] = vmin[k] = mmin[k] = s;
        pmax[k] = vmax[k] = mmax[k] = s ^ 0x80000000u;
    }
    const uint32_t n = a->n;
    uint32_t moved = 0;
    uint32_t ahead[4] = {}, step[4] = {};
    uint32_t lastP[4] = {}, last_mid[4] = {};
    uint32_t lastm = 0;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t P[4], Q[4];
        memcpy(P, a->pos + (size_t)i * 16u, 16);
        const uint32_t m = load_u32(a->mask + (size_t)i * 4u);
        int skip = 0;
        uint32_t w = 0;
        if (m == 0u) {
            w = load_u32(a->inv_mass + (size_t)i * 16u);
            if (cw::arm_fcmp_zero_z(w, fz, fl)) skip = 1;
        }
        if (!skip) {
            memcpy(Q, a->prev + (size_t)i * 16u, 16);
            uint32_t mid[4], ah[4], T[4];
            for (int k = 0; k < 4; k++) {
                const uint32_t D = cw::arm_sub(P[k], Q[k], fz, fl);
                const uint32_t H = cw::arm_mul(D, kHalf, fz, fl);
                T[k] = cw::arm_mul(D, kThreeHalves, fz, fl);
                mid[k] = cw::arm_add(Q[k], H, fz, fl);
                ah[k] = cw::arm_add(Q[k], T[k], fz, fl);
                vmin[k] = cw::arm_fmin(vmin[k], mid[k], fz, fl);
                vmin[k] = cw::arm_fmin(vmin[k], ah[k], fz, fl);
                vmax[k] = cw::arm_fmax(vmax[k], mid[k], fz, fl);
                vmax[k] = cw::arm_fmax(vmax[k], ah[k], fz, fl);
            }
            if (m & 0x80000000u) {
                for (int k = 0; k < 4; k++) {
                    mmin[k] = cw::arm_fmin(mmin[k], mid[k], fz, fl);
                    mmin[k] = cw::arm_fmin(mmin[k], ah[k], fz, fl);
                    mmax[k] = cw::arm_fmax(mmax[k], mid[k], fz, fl);
                    mmax[k] = cw::arm_fmax(mmax[k], ah[k], fz, fl);
                }
            }
            moved = 1;
            memcpy(ahead, ah, 16);
            memcpy(step, T, 16);
            if (i + 1u == n) memcpy(last_mid, mid, 16);
        } else if (i + 1u == n) {
            last_mid[0] = w;
            last_mid[1] = last_mid[2] = last_mid[3] = 0;
        }
        for (int k = 0; k < 4; k++) {
            pmin[k] = cw::arm_fmin(pmin[k], P[k], fz, fl);
            pmax[k] = cw::arm_fmax(pmax[k], P[k], fz, fl);
        }
        if (i + 1u == n) {
            memcpy(lastP, P, 16);
            lastm = m;
        }
    }
    memcpy(r->pos_min, pmin, 16);
    memcpy(r->pos_max, pmax, 16);
    memcpy(r->moving_min, vmin, 16);
    memcpy(r->moving_max, vmax, 16);
    memcpy(r->marked_min, mmin, 16);
    memcpy(r->marked_max, mmax, 16);
    memcpy(r->last_pos, lastP, 16);
    r->last_mask = lastm;
    memcpy(r->last_mid, last_mid, 16);
    r->moved = moved;
    memcpy(r->ahead, ahead, 16);
    memcpy(r->ahead_step, step, 16);
    return fl;
}

}  // namespace

uint32_t cloth_aabb_81b38800(const ClothAabbArgs *a, ClothAabbResult *r) {
    const unsigned mx = _mm_getcsr();
    int fast = aabb_fast<false>(a, r);
    if (fast == 2) fast = aabb_fast<true>(a, r);
    const uint32_t fl = (fast == 1) ? 0u : aabb_scalar(a, r);
    _mm_setcsr(mx);
    return fl;
}
