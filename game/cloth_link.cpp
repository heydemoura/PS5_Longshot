/* One pass of cloth link (stretch) constraints. SPEC.md formulas, operand order and
   memory order; the exactness class is SPEC.md section 4. Lane w stays off the xyz chain. */
#include "cloth_link.h"
#include "cloth_wlane.hpp"
#include "cloth_common.hpp"
#include <string.h>
#include <xmmintrin.h>
#include <emmintrin.h>
#if defined(__SSE4_1__)
#include <smmintrin.h>
#endif

#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif

namespace {

constexpr uint32_t DN = cw::DEFAULT_NAN;

static inline int in_g(uint32_t u) {
    const uint32_t a = u & 0x7fffffffu;
    return ((a - 1u) >= 0x00ffffffu) & (a <= 0x7f7fffffu);
}
static inline int in_h(uint32_t u) {
    const uint32_t a = u & 0x7fffffffu;
    return ((a - 1u) >= 0x1a7fffffu) & (a <= 0x7f7fffffu);
}
static inline int prod_ok(uint32_t a, uint32_t b, uint32_t p) {
    return !((a & 0x7fffffffu) && (b & 0x7fffffffu) && !(p & 0x7fffffffu));
}
static inline int triv_w(uint32_t u) { return ((u & 0x7fffffffu) == 0u) | (u == DN); }

static inline uint32_t ssbits(__m128 v) {
#if defined(__SSE4_1__)
    return (uint32_t)_mm_extract_ps(v, 0);
#else
    return kc::fb(_mm_cvtss_f32(v));
#endif
}
static inline uint32_t b3(__m128 v) {
#if defined(__SSE4_1__)
    return (uint32_t)_mm_extract_ps(v, 3);
#else
    return kc::fb(_mm_cvtss_f32(_mm_shuffle_ps(v, v, 0xff)));
#endif
}
#if !defined(__SSE4_1__)
static inline uint32_t b0(__m128 v) { return kc::fb(_mm_cvtss_f32(v)); }
static inline uint32_t b1(__m128 v) { return kc::fb(_mm_cvtss_f32(_mm_shuffle_ps(v, v, 1))); }
static inline uint32_t b2(__m128 v) { return kc::fb(_mm_cvtss_f32(_mm_movehl_ps(v, v))); }
#endif

static inline __m128 spl_ss(__m128 s) { return _mm_shuffle_ps(s, s, 0); }
static inline __m128 ld4(const uint8_t *p) { return _mm_loadu_ps((const float *)(const void *)p); }
static inline void st4(uint8_t *p, __m128 v) { _mm_storeu_ps((float *)(void *)p, v); }
static inline __m128 ldss(const uint8_t *p) { return _mm_load_ss((const float *)(const void *)p); }

static inline __m128 with3(__m128 v, uint32_t w) {
#if defined(__SSE4_1__)
    return cw::with3(v, w);
#else
    const __m128 m = _mm_castsi128_ps(_mm_set_epi32(-1, 0, 0, 0));
    const __m128 wv = _mm_castsi128_ps(_mm_set1_epi32((int)w));
    return _mm_or_ps(_mm_andnot_ps(m, v), _mm_and_ps(wv, m));
#endif
}
static inline __m128 canon_w(__m128 v) {
#if defined(__SSE4_1__)
    return cw::w_canon(v);
#else
    const __m128 nan = _mm_cmpunord_ps(v, v);
    const __m128 d = _mm_castsi128_ps(_mm_set1_epi32((int)DN));
    return _mm_or_ps(_mm_andnot_ps(nan, v), _mm_and_ps(d, nan));
#endif
}

#if defined(__SSE4_1__)
static inline int acc_ok(__m128i mn, __m128i mx, __m128i lo, __m128i hi) {
    const __m128i mn_ok = _mm_cmpeq_epi32(_mm_max_epu32(mn, lo), mn);
    const __m128i mx_ok = _mm_cmpeq_epi32(_mm_min_epu32(mx, hi), mx);
    return _mm_movemask_epi8(_mm_and_si128(mn_ok, mx_ok)) == 0xffff;
}
#endif

/* Lane w outside the trivial fast case. preserve_all keeps the hot loop's xmm live
   without spills at the call. Returns flags; *mode is 1 to canon the host lane, 2 to
   splice *wA / *wB in. */
#if defined(__clang__)
__attribute__((preserve_all, noinline, cold))
#elif defined(__GNUC__)
__attribute__((noinline, cold))
#endif
static uint32_t lane_w_slow(uint8_t *slot_a, uint8_t *slot_b, uint32_t a, uint32_t b, uint32_t aw, uint32_t bw,
                            float r, float c, uint32_t r_b, uint32_t c_b, uint32_t ma_b, uint32_t mb_b, int fz_on,
                            int flags_ok, int *mode, uint32_t *wA, uint32_t *wB) {
    const __m128 A = ld4(slot_a);
    const __m128 B = ld4(slot_b);
    const __m128 D = _mm_sub_ps(B, A);
    const __m128 Q = _mm_mul_ps(D, D);
    const __m128 rv = spl_ss(_mm_set_ss(r));
    const __m128 N = _mm_mul_ps(D, rv);
    const __m128 T = _mm_mul_ps(N, spl_ss(_mm_set_ss(c)));
    const __m128 TA = _mm_mul_ps(T, spl_ss(_mm_set_ss(kc::bf(ma_b))));
    const __m128 TB = _mm_mul_ps(T, spl_ss(_mm_set_ss(kc::bf(mb_b))));
    const __m128 Ap = _mm_add_ps(A, TA);
    const __m128 Bp = (a == b) ? _mm_sub_ps(Ap, TB) : _mm_sub_ps(B, TB);
    const int cin = (int)cw::canon_in(aw) & (int)cw::canon_in(bw);
    if (flags_ok && cin) {
        int safe = !fz_on;
        if (fz_on) {
            safe = (int)cw::fz_safe(aw) & (int)cw::fz_safe(bw) & (int)cw::fz_safe(b3(D)) & (int)cw::fz_safe(b3(Q)) &
                   (int)cw::fz_safe(b3(N)) & (int)cw::fz_safe(b3(T)) & (int)cw::fz_safe(b3(TA)) &
                   (int)cw::fz_safe(b3(TB)) & (int)cw::fz_safe(b3(Ap)) & (int)cw::fz_safe(b3(Bp));
        }
        if (safe) {
            *mode = 1;
            return 0;
        }
    }
    uint32_t fl = 0;
    const uint32_t fz = fz_on ? 1u : 0u;
    const uint32_t Dw = cw::arm_sub(bw, aw, fz, fl);
    const uint32_t Qw = cw::arm_mul(Dw, Dw, fz, fl);
    (void)Qw;
    const uint32_t Nw = cw::arm_mul(Dw, r_b, fz, fl);
    const uint32_t Tw = cw::arm_mul(Nw, c_b, fz, fl);
    const uint32_t TAw = cw::arm_mul(Tw, ma_b, fz, fl);
    const uint32_t TBw = cw::arm_mul(Tw, mb_b, fz, fl);
    const uint32_t Apw = cw::arm_add(aw, TAw, fz, fl);
    const uint32_t Brw = (a == b) ? Apw : bw;
    *wA = Apw;
    *wB = cw::arm_sub(Brw, TBw, fz, fl);
    *mode = 2;
    return fl;
}

static inline void note(uint32_t &lo, uint32_t &hi, uint32_t s) {
    lo = s < lo ? s : lo;
    hi = s > hi ? s : hi;
}

}  // namespace

extern "C" void cloth_link_pass(ClothLinkPass *p) {
    const uint32_t scale_b = p->scale;
    if (!in_g(scale_b)) {
        p->done = 0;
        p->why = CLOTH_LINK_ENTRY;
        p->fpsr = 0;
        p->slot_lo = 1;
        p->slot_hi = 0;
        return;
    }
    const float s = kc::bf(scale_b);
    const __m128 s_ss = _mm_set_ss(s);
    const int fz_on = p->fz != 0;
    const int flags_ok = (p->fpsr_in & cw::FAST_FLAGS) == cw::FAST_FLAGS;
    uint8_t *slots = p->slots;
    const uint8_t *particles = p->particles;
    const uint8_t *rec = p->links;
    const uint32_t n = p->n;

#if defined(__SSE4_1__)
    const __m128i sign = _mm_set1_epi32(0x7fffffff);
    const __m128i xyz = _mm_set_epi32(0, -1, -1, -1);
    const __m128i one = _mm_set1_epi32(1);
    const __m128i glo = _mm_set1_epi32(0x00ffffff);
    const __m128i hlo = _mm_set1_epi32(0x1a7fffff);
    const __m128i ghi = _mm_set1_epi32(0x7f7fffff);
    const __m128i zeroi = _mm_setzero_si128();
    const __m128i all1 = _mm_set1_epi32(-1);
#define ADDV(mn, mx, v)                                          \
    do {                                                         \
        __m128i a__ = _mm_and_si128(_mm_castps_si128(v), sign);  \
        a__ = _mm_and_si128(a__, xyz);                           \
        mn = _mm_min_epu32(mn, _mm_sub_epi32(a__, one));         \
        mx = _mm_max_epu32(mx, a__);                             \
    } while (0)
#else
#define ADDV(mn, mx, v)                                                                        \
    do {                                                                                       \
        sok &= in_g(b0(v)) & in_g(b1(v)) & in_g(b2(v));                                        \
        (void)(mn);                                                                            \
        (void)(mx);                                                                            \
    } while (0)
#endif

    uint32_t lo = 0xffffffffu, hi = 0, wflags = 0, done = 0;
    p->why = CLOTH_LINK_CLASS;

    for (uint32_t i = 0; i < n; i++) {
        const uint32_t ab = ({ uint32_t u; memcpy(&u, rec, 4); u; });
        const uint32_t a = ab & 0xffffu;
        const uint32_t b = (ab >> 16) & 0xffffu;
        uint8_t *sa = slots + ((size_t)a << 4);
        uint8_t *sb = slots + ((size_t)b << 4);
        const __m128 rest_ss = ldss(rec + 4);
        const __m128 k_ss = ldss(rec + 8);
        const uint32_t rest_b = ssbits(rest_ss);
        const uint32_t k_b = ssbits(k_ss);
        const __m128 A = ld4(sa);
        const __m128 B = ld4(sb);
        const __m128 ma_ss = ldss(particles + ((size_t)a << 4) + 4);
        const __m128 mb_ss = ldss(particles + ((size_t)b << 4) + 4);
        const uint32_t ma_b = ssbits(ma_ss);
        const uint32_t mb_b = ssbits(mb_ss);
        const uint32_t aw = b3(A);
        const uint32_t bw = b3(B);

        int sok = in_g(rest_b) & in_g(k_b) & in_h(ma_b) & in_h(mb_b);
#if defined(__SSE4_1__)
        __m128i gmn = all1, gmx = zeroi, hmn = all1, hmx = zeroi;
        ADDV(gmn, gmx, A);
        ADDV(gmn, gmx, B);
#else
        __m128i gmn = _mm_setzero_si128(), gmx = gmn, hmn = gmn, hmx = gmn;
        (void)gmx, (void)hmn, (void)hmx;   /* (the build's warnings: only the SSE4.1 path reads them) */
        sok &=in_g(b0(A)) & in_g(b1(A)) & in_g(b2(A));
        sok &= in_g(b0(B)) & in_g(b1(B)) & in_g(b2(B));
#endif

        const __m128 D = _mm_sub_ps(B, A);
#if defined(__SSE4_1__)
        ADDV(hmn, hmx, D);
#else
        sok &= in_h(b0(D)) & in_h(b1(D)) & in_h(b2(D));
#endif
        const __m128 Q = _mm_mul_ps(D, D);
#if defined(__SSE4_1__)
        ADDV(gmn, gmx, Q);
#else
        sok &= in_g(b0(Q)) & in_g(b1(Q)) & in_g(b2(Q));
#endif

        const __m128 sxy_v = _mm_add_ss(Q, _mm_shuffle_ps(Q, Q, 1));
        const __m128 L_v = _mm_add_ss(sxy_v, _mm_movehl_ps(Q, Q));
        const uint32_t sxy_b = ssbits(sxy_v);
        const uint32_t L_b = ssbits(L_v);
        const float L = _mm_cvtss_f32(L_v);
        sok &= in_g(sxy_b) & in_g(L_b);

        /* L > 0 matches the tiny test for every non-NaN. A NaN L is outside G.
           For L in G, L*e2 of the fused step stays in [1/8, 8] (SPEC.md 4.3). */
        __m128 r_ss = _mm_setzero_ps();
        if (L > 0.0f) {
            const __m128 e_ss = _mm_castsi128_ps(_mm_cvtsi32_si128((int)kc::frsqrte_bits(L_b)));
            const __m128 e2_ss = _mm_mul_ss(e_ss, e_ss);
            const float e2 = _mm_cvtss_f32(e2_ss);
#if defined(__FMA__)
            const float g = __builtin_fmaf(-L, e2, 3.0f) * 0.5f;
#else
            const float g = (float)(3.0 - (double)L * (double)e2) * 0.5f;
#endif
            const __m128 g_ss = _mm_set_ss(g);
            r_ss = _mm_mul_ss(e_ss, g_ss);
            const uint32_t e_b = ssbits(e_ss);
            const uint32_t e2_b = ssbits(e2_ss);
            const uint32_t g_b = ssbits(g_ss);
            const uint32_t r_b0 = ssbits(r_ss);
            sok &= in_g(e2_b) & in_g(g_b) & in_h(r_b0);
            sok &= prod_ok(e_b, e_b, e2_b) & prod_ok(e_b, g_b, r_b0);
        }
        const uint32_t r_b = ssbits(r_ss);
        sok &= in_h(r_b);

        const float rest = _mm_cvtss_f32(rest_ss);
        const float k = _mm_cvtss_f32(k_ss);
        const __m128 len_ss = _mm_mul_ss(L_v, r_ss);
        const __m128 dif_ss = _mm_sub_ss(len_ss, rest_ss);
        const __m128 c1_ss = _mm_mul_ss(dif_ss, k_ss);
        const __m128 c_ss = _mm_mul_ss(c1_ss, s_ss);
        const uint32_t len_b = ssbits(len_ss);
        const uint32_t dif_b = ssbits(dif_ss);
        const uint32_t c1_b = ssbits(c1_ss);
        const uint32_t c_b = ssbits(c_ss);
        sok &= in_g(len_b) & in_g(dif_b) & in_g(c1_b) & in_h(c_b);
        sok &= prod_ok(L_b, r_b, len_b) & prod_ok(dif_b, k_b, c1_b) & prod_ok(c1_b, scale_b, c_b);

        const __m128 N = _mm_mul_ps(D, spl_ss(r_ss));
        const __m128 T = _mm_mul_ps(N, spl_ss(c_ss));
        const __m128 TA = _mm_mul_ps(T, spl_ss(ma_ss));
        const __m128 TB = _mm_mul_ps(T, spl_ss(mb_ss));
        const __m128 Ap = _mm_add_ps(A, TA);
        const __m128 Bp = (__builtin_expect(a != b, 1)) ? _mm_sub_ps(B, TB) : _mm_sub_ps(Ap, TB);
#if defined(__SSE4_1__)
        ADDV(hmn, hmx, N);
        ADDV(hmn, hmx, T);
        ADDV(gmn, gmx, TA);
        ADDV(gmn, gmx, TB);
        ADDV(gmn, gmx, Ap);
        ADDV(gmn, gmx, Bp);
        sok &= acc_ok(gmn, gmx, glo, ghi) & acc_ok(hmn, hmx, hlo, ghi);
#else
        sok &= in_h(b0(N)) & in_h(b1(N)) & in_h(b2(N));
        sok &= in_h(b0(T)) & in_h(b1(T)) & in_h(b2(T));
        sok &= in_g(b0(TA)) & in_g(b1(TA)) & in_g(b2(TA));
        sok &= in_g(b0(TB)) & in_g(b1(TB)) & in_g(b2(TB));
        sok &= in_g(b0(Ap)) & in_g(b1(Ap)) & in_g(b2(Ap));
        sok &= in_g(b0(Bp)) & in_g(b1(Bp)) & in_g(b2(Bp));
#endif
        if (__builtin_expect(!sok, 0)) break;

        __m128 sA, sB;
        if ((aw | bw) == 0u) {
            sA = Ap;
            sB = Bp;
        } else if (triv_w(aw) & triv_w(bw)) {
            if (aw == DN || bw == DN) {
                sA = with3(Ap, DN);
                sB = with3(Bp, DN);
            } else {
                sA = Ap;
                sB = Bp;
            }
        } else {
            int mode = 2;
            uint32_t wA = 0, wB = 0;
            wflags |= lane_w_slow(sa, sb, a, b, aw, bw, _mm_cvtss_f32(r_ss), _mm_cvtss_f32(c_ss), r_b, c_b, ma_b,
                                  mb_b, fz_on, flags_ok, &mode, &wA, &wB);
            if (mode == 1) {
                sA = canon_w(Ap);
                sB = canon_w(Bp);
            } else {
                sA = with3(Ap, wA);
                sB = with3(Bp, wB);
            }
        }

        st4(sa, sA);
        st4(sb, sB);
        note(lo, hi, a);
        note(lo, hi, b);
        done = i + 1;
        if (done == n) {
            ClothLinkEnd &e = p->end;
            e.a = a;
            e.b = b;
            e.inv_len = r_b;
            e.inv_mass_a = ma_b;
            e.inv_mass_b = mb_b;
            e.stiffness = k_b;
            st4((uint8_t *)e.pos_a, A);
            st4((uint8_t *)e.pos_b_again, (a == b) ? sA : B);
            st4((uint8_t *)e.pos_b_new, sB);
            (void)rest;
            (void)k;
        }
        rec += 12;
    }

    p->done = done;
    if (done == n) p->why = CLOTH_LINK_ALL;
    p->fpsr = done ? (wflags | (fz_on ? (uint32_t)cw::IDC : 0u)) : 0u;
    p->slot_lo = lo;
    p->slot_hi = hi;
#undef ADDV
}
