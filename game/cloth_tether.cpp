/* Host kernel for one Havok cloth tether pass (routine 81c88940).
   Formulas and operand order follow SPEC.md. A record is stored only when every
   operand of every xyz operation is inside the exactness class (cloth_common.hpp).
   Lane w is then either already inside that class, covered by cloth_wlane.hpp's
   fast way (default-NaN fixup), or computed with the Arm helpers. */
#include "cloth_tether.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"

#include <xmmintrin.h>
#include <emmintrin.h>

#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif
#if defined(__clang__)   /* (the build's warnings: an & of two bools here is meant, branch-free) */
#pragma clang diagnostic ignored "-Wbitwise-instead-of-logical"
#endif

namespace {

inline uint32_t lane3_u(__m128 v) {
#if defined(__SSE4_1__)
    return static_cast<uint32_t>(_mm_extract_ps(v, 3));
#else
    return kc::fb(_mm_cvtss_f32(_mm_shuffle_ps(v, v, _MM_SHUFFLE(3, 3, 3, 3))));
#endif
}

inline __m128 set_lane3(__m128 v, uint32_t w) {
#if defined(__SSE4_1__)
    return _mm_insert_ps(v, _mm_castsi128_ps(_mm_cvtsi32_si128(static_cast<int>(w))), 0x30);
#else
    __m128 wv = _mm_castsi128_ps(_mm_cvtsi32_si128(static_cast<int>(w)));
    wv = _mm_shuffle_ps(wv, wv, _MM_SHUFFLE(0, 0, 0, 0));
    const __m128 mask = _mm_castsi128_ps(_mm_set_epi32(-1, 0, 0, 0));
    return _mm_or_ps(_mm_and_ps(wv, mask), _mm_andnot_ps(mask, v));
#endif
}

inline __m128 canon_w(__m128 v) {
#if defined(__SSE4_1__)
    return cw::w_canon(v);
#else
    uint32_t w = lane3_u(v);
    if ((w & 0x7fffffffu) > 0x7f800000u) w = cw::DEFAULT_NAN;
    return set_lane3(v, w);
#endif
}

inline float lane_f(__m128 v, int which) {
    if (which == 0) return _mm_cvtss_f32(v);
    if (which == 1) return _mm_cvtss_f32(_mm_shuffle_ps(v, v, _MM_SHUFFLE(1, 1, 1, 1)));
    if (which == 2) return _mm_cvtss_f32(_mm_movehl_ps(v, v));
    return _mm_cvtss_f32(_mm_shuffle_ps(v, v, _MM_SHUFFLE(3, 3, 3, 3)));
}

[[maybe_unused]] inline bool in_r3(__m128 v) {   /* (the build's warnings: only the plain x86-64 path uses it) */
    return kc::in_r(lane_f(v, 0)) & kc::in_r(lane_f(v, 1)) & kc::in_r(lane_f(v, 2));
}

struct WArm {
    __m128 moved, delta;
    uint32_t fl;
};

__attribute__((noinline)) WArm w_arm(__m128 Pp, __m128 DELTA, uint32_t iw, uint32_t jw, float r, float g,
                                    uint32_t fz) {
    uint32_t fl = 0;
    const uint32_t dw = cw::arm_sub(jw, iw, fz, fl);
    (void)cw::arm_mul(dw, dw, fz, fl);
    const uint32_t ddw = cw::arm_mul(dw, kc::fb(r), fz, fl);
    const uint32_t delw = cw::arm_mul(ddw, kc::fb(g), fz, fl);
    const uint32_t pw = cw::arm_add(jw, delw, fz, fl);
    WArm o;
    o.moved = set_lane3(Pp, pw);
    o.delta = set_lane3(DELTA, delw);
    o.fl = fl;
    return o;
}

struct MxGuard {
    unsigned v;
    MxGuard() : v(_mm_getcsr()) {}
    ~MxGuard() { _mm_setcsr(v); }
};

struct Last {
    __m128 moved, delta;
    float g, len, r;
    uint32_t stiff, i, j;
};

/* Wfast: guest status already holds the flags lane w can raise, so a provable w
   lane is the host lane with NaNs rewritten to the default NaN.
   Fz: this call flushes subnormals; every completed record raises IDC. */
template <bool Wfast, bool Fz>
void run(uint8_t *slots, const uint8_t *rec, uint32_t count, float s, bool s_ok, uint32_t &done, uint32_t &flags,
         uint32_t &lo, uint32_t &hi, Last &last) {
    for (uint32_t k = 0; k < count; ++k) {
        uint32_t ij, rest_b, stiff_b;
        __builtin_memcpy(&ij, rec, 4);
        __builtin_memcpy(&rest_b, rec + 4, 4);
        __builtin_memcpy(&stiff_b, rec + 8, 4);
        const uint32_t i = ij & 0xffffu;
        const uint32_t j = ij >> 16;
        const float rest = kc::bf(rest_b);
        const float stiff = kc::bf(stiff_b);

        const __m128 pi = _mm_loadu_ps(reinterpret_cast<const float *>(slots + (static_cast<size_t>(i) << 4)));
        const __m128 pj = _mm_loadu_ps(reinterpret_cast<const float *>(slots + (static_cast<size_t>(j) << 4)));
        const uint32_t iw = Wfast ? lane3_u(pi) : 0u;
        const uint32_t jw = Wfast ? lane3_u(pj) : 0u;

        const __m128 D = _mm_sub_ps(pj, pi);
        const __m128 Q = _mm_mul_ps(D, D);
        const float qx = _mm_cvtss_f32(Q);
        const float qy = _mm_cvtss_f32(_mm_shuffle_ps(Q, Q, _MM_SHUFFLE(1, 1, 1, 1)));
        const float qz = _mm_cvtss_f32(_mm_movehl_ps(Q, Q));
        const float L = (qx + qy) + qz;

        unsigned bad = 0;
        const float r = kc::rsq_step(L, bad);
        const float len = L * r;
        const float ex = rest - len;
        const float mn = kc::fmin_arm(ex, 0.0f);
        const float fv = stiff * mn;
        const float g = fv * s;

        const __m128 rs = _mm_set1_ps(r);
        const __m128 gs = _mm_set1_ps(g);
        const __m128 DD = _mm_mul_ps(D, rs);
        const __m128 DELTA = _mm_mul_ps(DD, gs);
        const __m128 Pp = _mm_add_ps(pj, DELTA);

#if KC_SIMD
        kc::Checks ck;
        ck.reset();
        ck.add(pi);
        ck.add(pj);
        ck.add(D);
        ck.add(Q);
        ck.add(DD);
        ck.add(DELTA);
        const int m = ck.mask();
        bool fz_w_ok = true;
        if constexpr (Wfast && Fz) {
            ck.add(Pp); /* w of the stored vector; xyz mask already captured */
            fz_w_ok = cw::fz_safe_min3(ck.mn);
        }
#else
        const bool xyz = in_r3(pi) & in_r3(pj) & in_r3(D) & in_r3(Q) & in_r3(DD) & in_r3(DELTA);
        const bool wall = kc::in_r(lane_f(pi, 3)) & kc::in_r(lane_f(pj, 3)) & kc::in_r(lane_f(D, 3)) &
                          kc::in_r(lane_f(Q, 3)) & kc::in_r(lane_f(DD, 3)) & kc::in_r(lane_f(DELTA, 3));
        const int m = xyz ? (wall ? 0xffff : 0x0fff) : 0;
        bool fz_w_ok = true;
        if constexpr (Wfast && Fz) {
            fz_w_ok = cw::fz_safe(iw) && cw::fz_safe(jw) && cw::fz_safe(lane3_u(D)) && cw::fz_safe(lane3_u(Q)) &&
                      cw::fz_safe(lane3_u(DD)) && cw::fz_safe(lane3_u(DELTA)) && cw::fz_safe(lane3_u(Pp)) &&
                      cw::fz_safe(kc::fb(r)) && cw::fz_safe(kc::fb(g));
        }
#endif
        const int sc = static_cast<int>(s_ok) & static_cast<int>(kc::in_r(rest)) & static_cast<int>(kc::in_r(stiff)) &
                       static_cast<int>(kc::in_r(L)) & static_cast<int>(kc::in_r(r)) & static_cast<int>(kc::in_r(len)) &
                       static_cast<int>(kc::in_r(ex)) & static_cast<int>(kc::in_r(mn)) & static_cast<int>(kc::in_r(fv)) &
                       static_cast<int>(kc::in_r(g));
        if (__builtin_expect(bad | ((m & 0x0fff) != 0x0fff) | (sc == 0), 0)) break;

        __m128 moved;
        __m128 delta;
        if constexpr (Wfast) {
            moved = canon_w(Pp);
            delta = canon_w(DELTA);
            const bool unsafe = !cw::canon_in(iw) || !cw::canon_in(jw) || !fz_w_ok;
            if (__builtin_expect(unsafe, 0)) {
                const WArm w = w_arm(Pp, DELTA, iw, jw, r, g, Fz ? 1u : 0u);
                moved = w.moved;
                delta = w.delta;
                flags |= w.fl;
            }
        } else if (m != 0xffff) {
            const WArm w = w_arm(Pp, DELTA, lane3_u(pi), lane3_u(pj), r, g, Fz ? 1u : 0u);
            moved = w.moved;
            delta = w.delta;
            flags |= w.fl;
        } else {
            moved = Pp;
            delta = DELTA;
        }

        _mm_storeu_ps(reinterpret_cast<float *>(slots + (static_cast<size_t>(j) << 4)), moved);
        kc::note_slot(lo, hi, j);
        done = k + 1;
        rec += 12;
        if (done == count) {
            last.moved = moved;
            last.delta = delta;
            last.g = g;
            last.len = len;
            last.r = r;
            last.stiff = stiff_b;
            last.i = i;
            last.j = j;
        }
    }
}

}  // namespace

extern "C" void cloth_tether_81c88940(const ClothTetherCall *call, ClothTetherResult *res) {
    MxGuard mxcsr;

    const uint32_t fz = call->fz ? 1u : 0u;
    const bool wfast = (call->fpsr & cw::FAST_FLAGS) == cw::FAST_FLAGS;
    const float s = kc::bf(call->scale);
    const bool s_ok = kc::in_r(s);

    uint32_t lo = 0xffffffffu;
    uint32_t hi = 0;
    uint32_t flags = 0;
    uint32_t done = 0;
    Last last{};

    if (wfast) {
        if (fz) run<true, true>(call->slots, call->records, call->count, s, s_ok, done, flags, lo, hi, last);
        else run<true, false>(call->slots, call->records, call->count, s, s_ok, done, flags, lo, hi, last);
    } else if (fz) {
        run<false, true>(call->slots, call->records, call->count, s, s_ok, done, flags, lo, hi, last);
    } else {
        run<false, false>(call->slots, call->records, call->count, s, s_ok, done, flags, lo, hi, last);
    }

    res->done = done;
    res->fpsr = 0;
    if (done) {
        res->fpsr = flags;
        if (fz) res->fpsr |= cw::IDC;
    }
    res->slot_lo = lo;
    res->slot_hi = hi;
    res->why = (done == call->count) ? CLOTH_TETHER_DONE : CLOTH_TETHER_VALUE;
    if (done == call->count) {
        res->last.i = last.i;
        res->last.j = last.j;
        _mm_storeu_ps(reinterpret_cast<float *>(res->last.moved), last.moved);
        _mm_storeu_ps(reinterpret_cast<float *>(res->last.delta), last.delta);
        res->last.pull = kc::fb(last.g);
        res->last.len = kc::fb(last.len);
        res->last.rlen = kc::fb(last.r);
        res->last.stiffness = last.stiff;
    }
}
