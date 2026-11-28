/* Prepare Matrices host kernel. SPEC.md formulas, operand order and evaluation order.
   Fast path: SSE mul/add when every operand input and every sum is a plain value
   (SPEC.md 5). Otherwise the Arm lane rules in cloth_wlane.hpp. */
#include "cloth_matrices.h"
#include "cloth_wlane.hpp"
#include <emmintrin.h>
#include <string.h>
#if defined(__SSE4_1__)
#include <smmintrin.h>
#define MX_SIMD 1
#else
#define MX_SIMD 0
#endif

#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
/* cloth_wlane.hpp's optimize pragma can drop -O2 for the rest of this file. */
#pragma GCC reset_options
#endif

#if defined(__GNUC__)
#define MX_INLINE __attribute__((always_inline)) inline
#define MX_COLD __attribute__((cold, noinline))
#else
#define MX_INLINE inline
#define MX_COLD
#endif

namespace {

struct Col {
    uint32_t u[4];
};

MX_INLINE __m128 ld(const uint8_t *p) {
    return _mm_loadu_ps(reinterpret_cast<const float *>(p));
}
MX_INLINE void st(uint8_t *p, __m128 v) {
    _mm_storeu_ps(reinterpret_cast<float *>(p), v);
}
template <int I>
MX_INLINE __m128 spl(__m128 v) {
    return _mm_shuffle_ps(v, v, _MM_SHUFFLE(I, I, I, I));
}
/* Lane 3 of C is never an operand. Zero it so the class test ignores it. */
MX_INLINE __m128 z3(__m128 v) {
    return _mm_and_ps(v, _mm_castsi128_ps(_mm_set_epi32(0, -1, -1, -1)));
}
MX_INLINE uint32_t load_u16(const uint8_t *p) {
    uint16_t u;
    memcpy(&u, p, 2);
    return u;
}
MX_INLINE Col load_col(const uint8_t *p) {
    Col c;
    memcpy(&c, p, 16);
    return c;
}
MX_INLINE void store_col(uint8_t *p, const Col &c) { memcpy(p, &c, 16); }

MX_INLINE Col vadd(Col a, Col b, uint32_t fz, uint32_t &fl) {
    Col r;
    for (int i = 0; i < 4; i++) r.u[i] = cw::arm_add(a.u[i], b.u[i], fz, fl);
    return r;
}
MX_INLINE Col vmul(Col a, uint32_t s, uint32_t fz, uint32_t &fl) {
    Col r;
    for (int i = 0; i < 4; i++) r.u[i] = cw::arm_mul(a.u[i], s, fz, fl);
    return r;
}

/* Plain class: zero, or magnitude bits in [0x20000000, 0x5effffff]. */
struct ClassAcc {
#if MX_SIMD
    __m128i mx, mn;
    MX_INLINE void reset() {
        mx = _mm_setzero_si128();
        mn = _mm_set1_epi32(-1);
    }
    MX_INLINE void add(__m128 v) {
        const __m128i a = _mm_and_si128(_mm_castps_si128(v), _mm_set1_epi32(0x7fffffff));
        mx = _mm_max_epu32(mx, a);
        mn = _mm_min_epu32(mn, _mm_sub_epi32(a, _mm_set1_epi32(1)));
    }
    MX_INLINE bool ok() const {
        const __m128i hi = _mm_set1_epi32(0x5effffff);
        const __m128i lo = _mm_set1_epi32(0x1fffffff);
        const __m128i e = _mm_and_si128(_mm_cmpeq_epi32(_mm_max_epu32(mx, hi), hi),
                                        _mm_cmpeq_epi32(_mm_min_epu32(mn, lo), lo));
        return _mm_movemask_epi8(e) == 0xffff;
    }
#else
    int bad;
    MX_INLINE void reset() { bad = 0; }
    MX_INLINE void add(__m128 v) {
        alignas(16) float f[4];
        _mm_store_ps(f, v);
        for (int i = 0; i < 4; i++) {
            uint32_t a;
            memcpy(&a, &f[i], 4);
            a &= 0x7fffffffu;
            bad |= !(a == 0u || (a - 0x20000000u) < 0x3f000000u);
        }
    }
    MX_INLINE bool ok() const { return bad == 0; }
#endif
    MX_INLINE void add3(__m128 v) { add(z3(v)); }
};

MX_INLINE bool model_plain(const uint8_t *m) {
    ClassAcc ch;
    ch.reset();
    ch.add(ld(m));
    ch.add(ld(m + 16));
    ch.add(ld(m + 32));
    ch.add(ld(m + 48));
    return ch.ok();
}

/* One entry, Arm lane rules. part receives U0,U1,U2,S,V (16 bytes each) when non-null.
   p_first selects S = P + M3; otherwise S = M3 + P. */
MX_COLD void scalar_at(uint32_t i, const uint8_t *bones, const uint8_t *index, const uint8_t *in,
                       uint8_t *out, const uint8_t *model, uint32_t fz, uint32_t &fl, bool p_first,
                       uint8_t *part) {
    const uint8_t *Bp = bones + ((size_t)load_u16(index + (i << 1)) << 6);
    const uint8_t *Cp = in + ((size_t)i << 6);
    uint8_t *Op = out + ((size_t)i << 6);
    const Col B0 = load_col(Bp), B1 = load_col(Bp + 16), B2 = load_col(Bp + 32), B3 = load_col(Bp + 48);
    const Col C0 = load_col(Cp), C1 = load_col(Cp + 16), C2 = load_col(Cp + 32), C3 = load_col(Cp + 48);
    const Col M0 = load_col(model), M1 = load_col(model + 16), M2 = load_col(model + 32), M3 = load_col(model + 48);

    const Col t0 = vadd(vadd(vmul(B0, C0.u[0], fz, fl), vmul(B1, C0.u[1], fz, fl), fz, fl),
                        vmul(B2, C0.u[2], fz, fl), fz, fl);
    const Col t1 = vadd(vmul(B2, C1.u[2], fz, fl),
                        vadd(vmul(B0, C1.u[0], fz, fl), vmul(B1, C1.u[1], fz, fl), fz, fl), fz, fl);
    const Col t2 = vadd(vmul(B2, C2.u[2], fz, fl),
                        vadd(vmul(B0, C2.u[0], fz, fl), vmul(B1, C2.u[1], fz, fl), fz, fl), fz, fl);
    const Col t3 = vadd(vmul(B2, C3.u[2], fz, fl),
                        vadd(vmul(B1, C3.u[1], fz, fl), vadd(B3, vmul(B0, C3.u[0], fz, fl), fz, fl), fz, fl),
                        fz, fl);

    const Col u0 = vadd(vmul(M0, t0.u[0], fz, fl), vmul(M1, t0.u[1], fz, fl), fz, fl);
    const Col o0 = vadd(u0, vmul(M2, t0.u[2], fz, fl), fz, fl);
    const Col u1 = vadd(vmul(M0, t1.u[0], fz, fl), vmul(M1, t1.u[1], fz, fl), fz, fl);
    const Col o1 = vadd(vmul(M2, t1.u[2], fz, fl), u1, fz, fl);
    const Col u2 = vadd(vmul(M0, t2.u[0], fz, fl), vmul(M1, t2.u[1], fz, fl), fz, fl);
    const Col o2 = vadd(vmul(M2, t2.u[2], fz, fl), u2, fz, fl);
    const Col P = vmul(M0, t3.u[0], fz, fl);
    const Col S = p_first ? vadd(P, M3, fz, fl) : vadd(M3, P, fz, fl);
    const Col V = vadd(vmul(M1, t3.u[1], fz, fl), S, fz, fl);
    const Col o3 = vadd(vmul(M2, t3.u[2], fz, fl), V, fz, fl);

    store_col(Op, o0);
    store_col(Op + 16, o1);
    store_col(Op + 32, o2);
    store_col(Op + 48, o3);
    if (part) {
        store_col(part + 0, u0);
        store_col(part + 16, u1);
        store_col(part + 32, u2);
        store_col(part + 48, S);
        store_col(part + 64, V);
    }
}

/* SSE entry. PFirst is the S order (true: P + M3). Save writes U0,U1,U2,S,V to part.
   Returns false without storing when the plain-value class does not hold. */
template <bool PFirst, bool Save>
MX_INLINE bool sse_at(uint32_t i, const uint8_t *bones, const uint8_t *index, const uint8_t *in, uint8_t *out,
                      __m128 M0, __m128 M1, __m128 M2, __m128 M3, uint8_t *part) {
    const uint8_t *B = bones + ((size_t)load_u16(index + (i << 1)) << 6);
    const uint8_t *C = in + ((size_t)i << 6);
    const __m128 B0 = ld(B), B1 = ld(B + 16), B2 = ld(B + 32), B3 = ld(B + 48);
    const __m128 C0 = ld(C), C1 = ld(C + 16), C2 = ld(C + 32), C3 = ld(C + 48);

    ClassAcc ch;
    ch.reset();
    ch.add(B0);
    ch.add(B1);
    ch.add(B2);
    ch.add(B3);
    ch.add3(C0);
    ch.add3(C1);
    ch.add3(C2);
    ch.add3(C3);

    const __m128 b0c00 = _mm_mul_ps(B0, spl<0>(C0));
    const __m128 b1c01 = _mm_mul_ps(B1, spl<1>(C0));
    const __m128 s0 = _mm_add_ps(b0c00, b1c01);
    ch.add(s0);
    const __m128 T0 = _mm_add_ps(s0, _mm_mul_ps(B2, spl<2>(C0)));
    ch.add(T0);

    const __m128 s1 = _mm_add_ps(_mm_mul_ps(B0, spl<0>(C1)), _mm_mul_ps(B1, spl<1>(C1)));
    ch.add(s1);
    const __m128 T1 = _mm_add_ps(_mm_mul_ps(B2, spl<2>(C1)), s1);
    ch.add(T1);

    const __m128 s2 = _mm_add_ps(_mm_mul_ps(B0, spl<0>(C2)), _mm_mul_ps(B1, spl<1>(C2)));
    ch.add(s2);
    const __m128 T2 = _mm_add_ps(_mm_mul_ps(B2, spl<2>(C2)), s2);
    ch.add(T2);

    const __m128 s3 = _mm_add_ps(B3, _mm_mul_ps(B0, spl<0>(C3)));
    ch.add(s3);
    const __m128 s4 = _mm_add_ps(_mm_mul_ps(B1, spl<1>(C3)), s3);
    ch.add(s4);
    const __m128 T3 = _mm_add_ps(_mm_mul_ps(B2, spl<2>(C3)), s4);
    ch.add(T3);

    const __m128 U0 = _mm_add_ps(_mm_mul_ps(M0, spl<0>(T0)), _mm_mul_ps(M1, spl<1>(T0)));
    ch.add(U0);
    const __m128 O0 = _mm_add_ps(U0, _mm_mul_ps(M2, spl<2>(T0)));
    ch.add(O0);

    const __m128 U1 = _mm_add_ps(_mm_mul_ps(M0, spl<0>(T1)), _mm_mul_ps(M1, spl<1>(T1)));
    ch.add(U1);
    const __m128 O1 = _mm_add_ps(_mm_mul_ps(M2, spl<2>(T1)), U1);
    ch.add(O1);

    const __m128 U2 = _mm_add_ps(_mm_mul_ps(M0, spl<0>(T2)), _mm_mul_ps(M1, spl<1>(T2)));
    ch.add(U2);
    const __m128 O2 = _mm_add_ps(_mm_mul_ps(M2, spl<2>(T2)), U2);
    ch.add(O2);

    const __m128 P = _mm_mul_ps(M0, spl<0>(T3));
    __m128 S;
    if constexpr (PFirst) S = _mm_add_ps(P, M3);
    else S = _mm_add_ps(M3, P);
    ch.add(S);
    const __m128 V = _mm_add_ps(_mm_mul_ps(M1, spl<1>(T3)), S);
    ch.add(V);
    const __m128 O3 = _mm_add_ps(_mm_mul_ps(M2, spl<2>(T3)), V);
    ch.add(O3);

    if (!ch.ok()) return false;
    uint8_t *O = out + ((size_t)i << 6);
    st(O, O0);
    st(O + 16, O1);
    st(O + 32, O2);
    st(O + 48, O3);
    if constexpr (Save) {
        st(part + 0, U0);
        st(part + 16, U1);
        st(part + 32, U2);
        st(part + 48, S);
        st(part + 64, V);
    }
    return true;
}

MX_INLINE void left_group(uint8_t *left, const uint8_t *out, uint32_t e) {
    static const uint8_t pos[7] = {1, 12, 0, 11, 10, 9, 8};
    for (int k = 0; k < 7; k++) memcpy(left + 16 * k, out + ((size_t)(e + pos[k]) << 6) + 48, 16);
}

MX_INLINE void left_tail(uint8_t *left, const uint8_t *out, uint32_t last, const uint8_t *model, const uint8_t *part) {
    memcpy(left + 0, out + ((size_t)last << 6) + 16, 16);
    memcpy(left + 16, part + 32, 16);
    memcpy(left + 32, part + 64, 16);
    memcpy(left + 48, part + 0, 16);
    memcpy(left + 64, model + 32, 16);
    memcpy(left + 80, part + 16, 16);
    memcpy(left + 96, part + 48, 16);
}

}  // namespace

extern "C" uint32_t cloth_matrices_81cdff40(const ClothMatricesArgs *a) {
    const unsigned csr = _mm_getcsr();
    const uint32_t G = (a->count_word >> 4) & 0xffffu;
    const uint32_t R = a->count_word & 15u;
    const uint32_t N = (G << 4) + R;
    const uint32_t grouped = G << 4;
    const uint32_t fz = a->fz ? 1u : 0u;
    uint32_t fl = 0;
    const uint8_t *bones = a->bones;
    const uint8_t *index = a->index;
    const uint8_t *in = a->in;
    uint8_t *out = a->out;
    const uint8_t *model = a->model;
    alignas(16) uint8_t part[80];

    if (model_plain(model)) {
        const __m128 M0 = ld(model), M1 = ld(model + 16), M2 = ld(model + 32), M3 = ld(model + 48);
        for (uint32_t g = 0; g < G; g++) {
            const uint32_t b = g << 4;
            for (uint32_t p = 0; p < 15; p++) {
                if (!sse_at<true, false>(b + p, bones, index, in, out, M0, M1, M2, M3, nullptr))
                    scalar_at(b + p, bones, index, in, out, model, fz, fl, true, nullptr);
            }
            if (!sse_at<false, false>(b + 15, bones, index, in, out, M0, M1, M2, M3, nullptr))
                scalar_at(b + 15, bones, index, in, out, model, fz, fl, false, nullptr);
        }
        if (R) {
            for (uint32_t i = grouped; i + 1 < N; i++) {
                if (!sse_at<false, false>(i, bones, index, in, out, M0, M1, M2, M3, nullptr))
                    scalar_at(i, bones, index, in, out, model, fz, fl, false, nullptr);
            }
            if (!sse_at<false, true>(N - 1, bones, index, in, out, M0, M1, M2, M3, part))
                scalar_at(N - 1, bones, index, in, out, model, fz, fl, false, part);
            left_tail(a->left, out, N - 1, model, part);
        } else {
            left_group(a->left, out, (G - 1u) << 4);
        }
    } else {
        for (uint32_t i = 0; i < N; i++) {
            const bool pf = (i < grouped) && ((i & 15u) != 15u);
            uint8_t *pp = (R && i + 1u == N) ? part : nullptr;
            scalar_at(i, bones, index, in, out, model, fz, fl, pf, pp);
        }
        if (R) left_tail(a->left, out, N - 1, model, part);
        else left_group(a->left, out, (G - 1u) << 4);
    }

    _mm_setcsr(csr);
    return fl;
}
