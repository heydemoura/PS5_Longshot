/* Mesh Bone Deform, one bone loop. Formulas, operand order and evaluation order: SPEC.md. */
#include "cloth_bones.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"
#include <string.h>
#include <xmmintrin.h>
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif
#if KC_SIMD
#include <xmmintrin.h>
#include <emmintrin.h>
#include <smmintrin.h>
#endif

static int msb_u128(unsigned __int128 v) {
    uint64_t hi = (uint64_t)(v >> 64);
    if (hi) return 127 - (int)__builtin_clzll(hi);
    return 63 - (int)__builtin_clzll((uint64_t)v);
}

/* Round the exact value num * 2^S (num != 0) to binary32, ties to even. OFC on overflow. */
static uint32_t round_scaled(__int128 num, int S, uint32_t &fl) {
    uint32_t sign = 0;
    if (num < 0) {
        sign = 0x80000000u;
        num = -num;
    }
    int msb = msb_u128((unsigned __int128)num);
    int unbiased = S + msb;
    if (unbiased >= 128) {
        fl |= cw::OFC;
        return sign | 0x7f800000u;
    }
    int shift = msb - 23;
    uint32_t mant;
    int roundb = 0, sticky = 0;
    if (shift > 0) {
        mant = (uint32_t)((unsigned __int128)num >> shift);
        roundb = (int)(((unsigned __int128)num >> (shift - 1)) & 1);
        if (shift > 1) {
            unsigned __int128 mask = ((unsigned __int128)1 << (shift - 1)) - 1;
            sticky = ((unsigned __int128)num & mask) != 0;
        }
    } else if (shift < 0) {
        mant = (uint32_t)num << (-shift);
    } else {
        mant = (uint32_t)num;
    }
    if (roundb && (sticky || (mant & 1u))) {
        mant += 1u;
        if (mant == (1u << 24)) {
            mant >>= 1;
            unbiased += 1;
        }
    }
    if (unbiased >= 128) {
        fl |= cw::OFC;
        return sign | 0x7f800000u;
    }
    /* step's nonzero results are normals (SPEC.md 2.2); a subnormal here is a bug we still round */
    if (unbiased < -126) {
        int sh = S + 149; /* value = mant_real * 2^-149 */
        uint32_t sm;
        int rb = 0, st = 0;
        if (sh >= 0) {
            sm = (uint32_t)((unsigned __int128)num << sh);
        } else {
            int r = -sh;
            sm = (uint32_t)((unsigned __int128)num >> r);
            if (r > 0) rb = (int)(((unsigned __int128)num >> (r - 1)) & 1);
            if (r > 1) {
                unsigned __int128 mask = ((unsigned __int128)1 << (r - 1)) - 1;
                st = ((unsigned __int128)num & mask) != 0;
            }
        }
        if (rb && (st || (sm & 1u))) sm += 1u;
        if (sm >= 0x800000u) return sign | (1u << 23);
        return sign | sm;
    }
    return sign | ((uint32_t)(unbiased + 127) << 23) | (mant & 0x7fffffu);
}

/* (3 + prod) / 2 rounded once. prod is the exact binary64 product of two finite nonzero binary32 values. */
static uint32_t step_round(double prod, uint32_t &fl) {
    uint64_t db;
    memcpy(&db, &prod, 8);
    int signp = (int)(db >> 63);
    db &= 0x7fffffffffffffffULL;
    double ap;
    memcpy(&ap, &db, 8);
    /* |prod| >= 2^40: adding 1.5 cannot reach a binary32 midpoint (half-ulp >= 2^15). */
    if (ap >= 0x1p40) {
        float r = (float)(prod * 0.5);
        uint32_t b = cw::u32(r);
        if ((b & 0x7fffffffu) == 0x7f800000u) fl |= cw::OFC;
        return b;
    }
    /* |prod| <= 2^-23: result is within half an ulp of 1.5, and the tie (exactly half) falls on the even 1.5. */
    if (ap <= 0x1p-23) return 0x3fc00000u;

    int pe = (int)((db >> 52) & 0x7ff);
    uint64_t psig = (1ULL << 52) | (db & ((1ULL << 52) - 1));
    int pexp = pe - 1023 - 52; /* ap = psig * 2^pexp */
    const int S = -80;         /* value = num * 2^S, num exact for this magnitude window */
    int sh = pexp - 1 - S;
    __int128 three = (__int128)3 << (-1 - S);
    __int128 term2 = (__int128)psig << sh;
    __int128 num = signp ? (three - term2) : (three + term2);
    if (num == 0) return 0u;
    return round_scaled(num, S, fl);
}

static uint32_t est_bits(uint32_t x, uint32_t fz, uint32_t &fl) {
    cw::flush_in(x, fz, fl);
    uint32_t mag = x & 0x7fffffffu;
    if (mag > 0x7f800000u) {
        if ((x & 0x00400000u) == 0) {
            fl |= cw::IOC;
            return x | 0x00400000u;
        }
        return x;
    }
    if (mag == 0) {
        fl |= cw::DZC;
        return (x & 0x80000000u) | 0x7f800000u;
    }
    if (x & 0x80000000u) {
        fl |= cw::IOC;
        return cw::DEFAULT_NAN;
    }
    if (mag == 0x7f800000u) return 0u;
    if (mag >= 0x00800000u) return kc::frsqrte_bits(x);
    int32_t e = 0;
    uint32_t m = mag;
    while ((m & 0x00400000u) == 0) {
        m <<= 1;
        e -= 1;
    }
    m = (m << 1) & 0x7fffffu;
    uint32_t index = (uint32_t)(256 * (e & 1)) + (m >> 15);
    int32_t exp = (380 - e) / 2;
    return ((uint32_t)exp << 23) | ((uint32_t)kc::RSQ.t[index] << 15);
}

static uint32_t step_bits(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    uint32_t ap = a ^ 0x80000000u;
    cw::flush_in(ap, fz, fl);
    cw::flush_in(b, fz, fl);
    uint32_t ma = ap & 0x7fffffffu, mb = b & 0x7fffffffu;
    if (ma > 0x7f800000u || mb > 0x7f800000u) return cw::nans(ap, b, fl);
    int az = ma == 0, bz = mb == 0, ai = ma == 0x7f800000u, bi = mb == 0x7f800000u;
    if ((ai && bz) || (bi && az)) return 0x3fc00000u;
    if (ai || bi) return ((ap ^ b) & 0x80000000u) | 0x7f800000u;
    if (az || bz) return 0x3fc00000u;
    double prod = (double)cw::f32(ap) * (double)cw::f32(b);
    return step_round(prod, fl);
}

static bool le_k(uint32_t L, uint32_t fz, uint32_t &fl) {
    uint32_t K = 0x007fffffu;
    cw::flush_in(L, fz, fl);
    cw::flush_in(K, fz, fl);
    if ((L & 0x7fffffffu) > 0x7f800000u) {
        if ((L & 0x00400000u) == 0) fl |= cw::IOC;
        return false;
    }
    return cw::f32(L) <= cw::f32(K);
}

struct Chain {
    uint32_t sc, fc, gc;
};

static Chain chain_exact(uint32_t L, uint32_t fz, uint32_t &fl) {
    Chain c;
    uint32_t ec = est_bits(L, fz, fl);
    c.sc = cw::arm_mul(ec, ec, fz, fl);
    c.fc = step_bits(L, c.sc, fz, fl);
    c.gc = cw::arm_mul(ec, c.fc, fz, fl);
    return c;
}

/* Positive normal L whose chain stays normal and inside the once-rounded step window. No flag but IXC. */
[[maybe_unused]] static bool chain_fast(uint32_t L, Chain &c) {
    uint32_t mag = L & 0x7fffffffu;
    if ((L & 0x80000000u) || mag < 0x00800000u || mag > 0x7f7fffffu) return false;
    uint32_t ec = kc::frsqrte_bits(L);
    float ef = cw::f32(ec);
    float Lf = cw::f32(L);
    float scf = ef * ef;
    c.sc = cw::u32(scf);
    uint32_t sm = c.sc & 0x7fffffffu;
    if (sm < 0x00800000u || sm >= 0x7f800000u) return false;
    double le = (double)Lf * (double)scf;
    if (!(le >= 0.125 && le <= 8.0)) return false;
#if KC_FRSQRTS_FMA
    float fcf = __builtin_fmaf(-Lf, scf, 3.0f) * 0.5f;
#else
    float fcf = (float)(3.0 - le) * 0.5f;
#endif
    float gcf = ef * fcf;
    c.fc = cw::u32(fcf);
    c.gc = cw::u32(gcf);
    uint32_t fm = c.fc & 0x7fffffffu, gm = c.gc & 0x7fffffffu;
    if (fm < 0x00800000u || fm >= 0x7f800000u) return false;
    if (gm < 0x00800000u || gm >= 0x7f800000u) return false;
    return true;
}

static void col_add(uint32_t d[4], const uint32_t a[4], const uint32_t b[4], uint32_t fz, uint32_t &fl) {
    for (int i = 0; i < 4; i++) d[i] = cw::arm_add(a[i], b[i], fz, fl);
}
static void col_sub(uint32_t d[4], const uint32_t a[4], const uint32_t b[4], uint32_t fz, uint32_t &fl) {
    for (int i = 0; i < 4; i++) d[i] = cw::arm_sub(a[i], b[i], fz, fl);
}
static void col_mul_s(uint32_t d[4], const uint32_t a[4], uint32_t s, uint32_t fz, uint32_t &fl) {
    for (int i = 0; i < 4; i++) d[i] = cw::arm_mul(a[i], s, fz, fl);
}
static void col_mul(uint32_t d[4], const uint32_t a[4], const uint32_t b[4], uint32_t fz, uint32_t &fl) {
    for (int i = 0; i < 4; i++) d[i] = cw::arm_mul(a[i], b[i], fz, fl);
}

static const int kRot[4] = {1, 2, 0, 1};

/* D[q] = a[q]*b[r(q)] - b[q]*a[r(q)], then (D1, D2, D0, +0). */
static void cross_ab(uint32_t dst[4], const uint32_t a[4], const uint32_t b[4], uint32_t fz, uint32_t &fl) {
    uint32_t d[4];
    for (int q = 0; q < 4; q++) {
        uint32_t p1 = cw::arm_mul(a[q], b[kRot[q]], fz, fl);
        uint32_t p2 = cw::arm_mul(b[q], a[kRot[q]], fz, fl);
        d[q] = cw::arm_sub(p1, p2, fz, fl);
    }
    dst[0] = d[1];
    dst[1] = d[2];
    dst[2] = d[0];
    dst[3] = 0u;
}

/* D[q] = b[r(q)]*a[q] - b[q]*a[r(q)] (Y x Z operand order), then (D1, D2, D0, +0). */
static void cross_yx_z(uint32_t dst[4], const uint32_t y[4], const uint32_t z[4], uint32_t fz, uint32_t &fl) {
    uint32_t d[4];
    for (int q = 0; q < 4; q++) {
        uint32_t p1 = cw::arm_mul(z[kRot[q]], y[q], fz, fl);
        uint32_t p2 = cw::arm_mul(z[q], y[kRot[q]], fz, fl);
        d[q] = cw::arm_sub(p1, p2, fz, fl);
    }
    dst[0] = d[1];
    dst[1] = d[2];
    dst[2] = d[0];
    dst[3] = 0u;
}

static void combine(uint32_t dst[4], const uint32_t Mp[4], const uint32_t E0[4], const uint32_t E1[4],
                    const uint32_t N[4], const uint32_t R[4], uint32_t fz, uint32_t &fl) {
    uint32_t t0[4], t1[4], s[4], t2[4], s2[4], t3[4];
    col_mul_s(t0, E0, R[0], fz, fl);
    col_mul_s(t1, E1, R[1], fz, fl);
    col_add(s, t0, t1, fz, fl);
    col_mul_s(t2, N, R[2], fz, fl);
    col_add(s2, s, t2, fz, fl);
    col_mul_s(t3, Mp, R[3], fz, fl);
    col_add(dst, t3, s2, fz, fl);
}

static uint32_t sum2(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) { return cw::arm_add(a, b, fz, fl); }

static void bone_scalar(const uint32_t P0[4], const uint32_t P1[4], const uint32_t P2[4], const uint32_t R0[4],
                        const uint32_t R2[4], const uint32_t R3[4], const uint32_t C[4], uint32_t T, uint32_t j1,
                        uint32_t j2, uint32_t fz, uint32_t &fl, uint8_t *dst, ClothBonesLast *hb) {
    uint32_t S01[4], S[4], M[4], E0[4], E1[4], N[4];
    col_add(S01, P0, P1, fz, fl);
    col_add(S, S01, P2, fz, fl);
    col_mul(M, S, C, fz, fl);
    col_sub(E0, P0, M, fz, fl);
    col_sub(E1, P1, M, fz, fl);
    cross_ab(N, E0, E1, fz, fl);

    uint32_t E0p[4] = {E0[0], E0[1], E0[2], 0u};
    uint32_t E1p[4] = {E1[0], E1[1], E1[2], 0u};
    uint32_t Mp[4] = {M[0], M[1], M[2], T};

    uint32_t A[4], Z[4], Y[4], X[4], O[4];
    combine(A, Mp, E0p, E1p, N, R0, fz, fl);
    combine(Z, Mp, E0p, E1p, N, R2, fz, fl);
    cross_ab(Y, Z, A, fz, fl);
    cross_yx_z(X, Y, Z, fz, fl);

    uint32_t QZ[4], QY[4], QX[4];
    col_mul(QZ, Z, Z, fz, fl);
    col_mul(QY, Y, Y, fz, fl);
    col_mul(QX, X, X, fz, fl);
    uint32_t Lz = sum2(sum2(QZ[0], QZ[1], fz, fl), QZ[2], fz, fl);
    uint32_t Ly = sum2(QY[2], sum2(QY[0], QY[1], fz, fl), fz, fl);
    uint32_t Lx = sum2(QX[2], sum2(QX[0], QX[1], fz, fl), fz, fl);

    Chain cz = chain_exact(Lz, fz, fl);
    bool z_le = le_k(Lz, fz, fl);
    uint32_t invz = z_le ? 0u : cz.gc;

    uint32_t chain_a, chain_b, invy, invx;
    bool y_le = le_k(Ly, fz, fl);
    if (y_le) {
        invy = 0u;
        chain_a = Ly;
        chain_b = QY[2];
    } else {
        Chain cy = chain_exact(Ly, fz, fl);
        invy = cy.gc;
        chain_a = cy.fc;
        chain_b = cy.sc;
    }
    bool x_le = le_k(Lx, fz, fl);
    if (x_le) {
        invx = 0u;
    } else {
        Chain cx = chain_exact(Lx, fz, fl);
        invx = cx.gc;
        chain_a = cx.sc;
    }

    uint32_t UZ[4], UY[4], UX[4];
    col_mul_s(UZ, Z, invz, fz, fl);
    col_mul_s(UY, Y, invy, fz, fl);
    col_mul_s(UX, X, invx, fz, fl);
    combine(O, Mp, E0p, E1p, N, R3, fz, fl);

    uint32_t col0[4] = {UX[0], UX[1], UX[2], 0u};
    uint32_t col1[4] = {UY[0], UY[1], UY[2], 0u};
    uint32_t col2[4] = {UZ[0], UZ[1], UZ[2], 0u};
    uint32_t col3[4] = {O[0], O[1], O[2], T};
    memcpy(dst + 0, col0, 16);
    memcpy(dst + 16, col1, 16);
    memcpy(dst + 32, col2, 16);
    memcpy(dst + 48, col3, 16);

    memcpy(hb->origin, col3, 16);
    memcpy(hb->unit_z, UZ, 16);
    memcpy(hb->unit_y, UY, 16);
    memcpy(hb->unit_x, UX, 16);
    memcpy(hb->axis_z, Z, 16);
    memcpy(hb->axis_y, Y, 16);
    memcpy(hb->axis_x, X, 16);
    hb->inv_len_z = invz;
    hb->inv_len_y = invy;
    hb->inv_len_x = invx;
    memcpy(hb->origin_coords, R3, 16);
    hb->chain_a = chain_a;
    hb->chain_b = chain_b;
    hb->tri_second = j1;
    hb->tri_third = j2;
}

#if KC_SIMD

struct Acc {
    __m128i mx, mn, mask, one;
    Acc()
        : mx(_mm_setzero_si128()), mn(_mm_set1_epi32(-1)), mask(_mm_set1_epi32(0x7fffffff)),
          one(_mm_set1_epi32(1)) {}
    void add(__m128 v) {
        __m128i a = _mm_and_si128(_mm_castps_si128(v), mask);
        mx = _mm_max_epu32(mx, a);
        mn = _mm_min_epu32(mn, _mm_sub_epi32(a, one));
    }
    /* lanes 0..2 of v; lane 3 treated as 1.0 so a flag-only lane cannot fail the class */
    void add3(__m128 v, __m128 one_ps) { add(_mm_blend_ps(v, one_ps, 8)); }
    bool ok() const {
        __m128i hi = _mm_set1_epi32(0x5effffff), lo = _mm_set1_epi32(0x1fffffff);
        __m128i e = _mm_and_si128(_mm_cmpeq_epi32(_mm_max_epu32(mx, hi), hi),
                                  _mm_cmpeq_epi32(_mm_min_epu32(mn, lo), lo));
        return _mm_movemask_epi8(e) == 0xffff;
    }
};

static __m128 zero3(__m128 v) { return _mm_insert_ps(v, v, 0x08); }

static __m128 set3(__m128 v, uint32_t bits) {
    return _mm_insert_ps(v, _mm_castsi128_ps(_mm_cvtsi32_si128((int)bits)), 0x30);
}

/* (D1, D2, D0, +0) with D[q] = a[q]*b[r(q)] - b[q]*a[r(q)]. NaN-free, so operand order does not matter. */
static __m128 cross4(__m128 a, __m128 b, Acc &ck, bool xyz, __m128 one_ps) {
    __m128 ra = _mm_shuffle_ps(a, a, _MM_SHUFFLE(1, 0, 2, 1));
    __m128 rb = _mm_shuffle_ps(b, b, _MM_SHUFFLE(1, 0, 2, 1));
    __m128 p1 = _mm_mul_ps(a, rb);
    __m128 p2 = _mm_mul_ps(b, ra);
    __m128 d = _mm_sub_ps(p1, p2);
    if (xyz) {
        ck.add3(p1, one_ps);
        ck.add3(p2, one_ps);
        ck.add3(d, one_ps);
    } else {
        ck.add(p1);
        ck.add(p2);
        ck.add(d);
    }
    return zero3(_mm_shuffle_ps(d, d, _MM_SHUFFLE(1, 0, 2, 1)));
}

static __m128 combine4(__m128 Mp, __m128 E0, __m128 E1, __m128 N, __m128 R, Acc &ck) {
    __m128 t0 = _mm_mul_ps(E0, KC_SPLAT(R, 0));
    __m128 t1 = _mm_mul_ps(E1, KC_SPLAT(R, 1));
    __m128 s = _mm_add_ps(t0, t1);
    __m128 t2 = _mm_mul_ps(N, KC_SPLAT(R, 2));
    __m128 s2 = _mm_add_ps(s, t2);
    __m128 t3 = _mm_mul_ps(Mp, KC_SPLAT(R, 3));
    __m128 r = _mm_add_ps(t3, s2);
    ck.add(t0);
    ck.add(t1);
    ck.add(s);
    ck.add(t2);
    ck.add(s2);
    ck.add(t3);
    ck.add(r);
    return r;
}

static bool sub3(uint32_t u) { return (u & 0x7fffffffu) - 1u < 0x007fffffu; }

/* Vector bone. Writes dst and *hb and ORs flags only when every column operand and result is in the
   plain class of SPEC.md 7 (zero or [2^-63, 2^63)), so SSE matches the Arm and only IXC would be raised.
   Lane 3 of steps 1..3 is ignored when IOC, OFC and UFC are already set. Returns false to mean "not vouched". */
static bool bone_fast(const ClothBonesArgs *a, uint32_t i, __m128 Cv, uint32_t T, uint32_t fz, int cover,
                      uint32_t &fl, ClothBonesLast *hb) {
    const uint8_t *pr = a->pairs + 4u * i;
    uint32_t o_i = kc::ld16(pr);
    uint32_t t_i = kc::ld16(pr + 2);
    const uint8_t *tri = a->triangles + (t_i - a->tri_first);
    uint32_t j0 = kc::ld16(tri);
    uint32_t j1 = kc::ld16(tri + 2);
    uint32_t j2 = kc::ld16(tri + 4);
    const uint8_t *base = a->positions;
    __m128 P0 = kc::ld4(base + 16u * (j0 - a->pos_first));
    __m128 P1 = kc::ld4(base + 16u * (j1 - a->pos_first));
    __m128 P2 = kc::ld4(base + 16u * (j2 - a->pos_first));
    const uint8_t *rec = a->records + 64u * i;
    __m128 R0 = kc::ld4(rec);
    __m128 R2 = kc::ld4(rec + 32);
    __m128 R3 = kc::ld4(rec + 48);

    __m128 one_ps = _mm_set1_ps(1.0f);
    bool xyz = cover != 0;
    Acc ck;
    if (xyz) {
        ck.add3(P0, one_ps);
        ck.add3(P1, one_ps);
        ck.add3(P2, one_ps);
        ck.add3(Cv, one_ps);
    } else {
        ck.add(P0);
        ck.add(P1);
        ck.add(P2);
        ck.add(Cv);
    }
    ck.add(R0);
    ck.add(R2);
    ck.add(R3);
    if (!kc::in_r_bits(T) || !ck.ok()) return false;

    __m128 S01 = _mm_add_ps(P0, P1);
    __m128 S = _mm_add_ps(S01, P2);
    __m128 M = _mm_mul_ps(S, Cv);
    __m128 E0 = _mm_sub_ps(P0, M);
    __m128 E1 = _mm_sub_ps(P1, M);
    if (xyz) {
        ck.add3(S01, one_ps);
        ck.add3(S, one_ps);
        ck.add3(M, one_ps);
        ck.add3(E0, one_ps);
        ck.add3(E1, one_ps);
    } else {
        ck.add(S01);
        ck.add(S);
        ck.add(M);
        ck.add(E0);
        ck.add(E1);
    }
    __m128 N = cross4(E0, E1, ck, xyz, one_ps);
    __m128 E0p = zero3(E0);
    __m128 E1p = zero3(E1);
    __m128 Mp = set3(M, T);
    ck.add(N);
    ck.add(E0p);
    ck.add(E1p);
    ck.add(Mp);

    __m128 A = combine4(Mp, E0p, E1p, N, R0, ck);
    __m128 Z = combine4(Mp, E0p, E1p, N, R2, ck);
    __m128 Y = cross4(Z, A, ck, false, one_ps);
    __m128 X = cross4(Y, Z, ck, false, one_ps);
    ck.add(Y);
    ck.add(X);
    __m128 QZ = _mm_mul_ps(Z, Z);
    __m128 QY = _mm_mul_ps(Y, Y);
    __m128 QX = _mm_mul_ps(X, X);
    ck.add(QZ);
    ck.add(QY);
    ck.add(QX);

    float qz0 = kc::lane0(QZ), qz1 = kc::lane1(QZ), qz2 = kc::lane2(QZ);
    float qy0 = kc::lane0(QY), qy1 = kc::lane1(QY), qy2 = kc::lane2(QY);
    float qx0 = kc::lane0(QX), qx1 = kc::lane1(QX), qx2 = kc::lane2(QX);
    uint32_t s01 = cw::u32(qz0 + qz1);
    uint32_t Lz = cw::u32(cw::f32(s01) + qz2);
    uint32_t sy = cw::u32(qy0 + qy1);
    uint32_t Ly = cw::u32(qy2 + cw::f32(sy));
    uint32_t sx = cw::u32(qx0 + qx1);
    uint32_t Lx = cw::u32(qx2 + cw::f32(sx));
    if ((Lz | Ly | Lx) & 0x80000000u) return false;
    if (!kc::in_r_bits(s01) || !kc::in_r_bits(sy) || !kc::in_r_bits(sx)) return false;
    if (!kc::in_r_bits(Lz) || !kc::in_r_bits(Ly) || !kc::in_r_bits(Lx)) return false;
    if (!ck.ok()) return false;

    uint32_t bone_fl = fz ? cw::IDC : 0u;
    if (xyz && fz && (a->fpsr_in & cw::IDC) == 0) {
        if (sub3(cw::lane3(P0)) || sub3(cw::lane3(P1)) || sub3(cw::lane3(P2)) || sub3(cw::lane3(Cv)))
            bone_fl |= cw::IDC;
    }

    uint32_t invz, invy, invx, chain_a, chain_b;
    if ((Lz & 0x7fffffffu) == 0) {
        chain_exact(Lz, fz, bone_fl);
        invz = 0u;
    } else {
        Chain c;
        if (!chain_fast(Lz, c) || !kc::in_r_bits(c.gc)) return false;
        invz = c.gc;
    }
    if ((Ly & 0x7fffffffu) == 0) {
        invy = 0u;
        chain_a = Ly;
        chain_b = cw::u32(qy2);
    } else {
        Chain c;
        if (!chain_fast(Ly, c) || !kc::in_r_bits(c.gc)) return false;
        invy = c.gc;
        chain_a = c.fc;
        chain_b = c.sc;
    }
    if ((Lx & 0x7fffffffu) == 0) {
        invx = 0u;
    } else {
        Chain c;
        if (!chain_fast(Lx, c) || !kc::in_r_bits(c.gc)) return false;
        invx = c.gc;
        chain_a = c.sc;
    }

    __m128 invzv = _mm_castsi128_ps(_mm_set1_epi32((int)invz));
    __m128 invyv = _mm_castsi128_ps(_mm_set1_epi32((int)invy));
    __m128 invxv = _mm_castsi128_ps(_mm_set1_epi32((int)invx));
    __m128 UZ = _mm_mul_ps(Z, invzv);
    __m128 UY = _mm_mul_ps(Y, invyv);
    __m128 UX = _mm_mul_ps(X, invxv);
    __m128 O = combine4(Mp, E0p, E1p, N, R3, ck);
    ck.add(UZ);
    ck.add(UY);
    ck.add(UX);
    if (!ck.ok()) return false;

    __m128 c0 = zero3(UX);
    __m128 c1 = zero3(UY);
    __m128 c2 = zero3(UZ);
    __m128 c3 = set3(O, T);
    uint8_t *dst = a->out + (o_i - a->out_first);
    kc::st4(dst, c0);
    kc::st4(dst + 16, c1);
    kc::st4(dst + 32, c2);
    kc::st4(dst + 48, c3);

    kc::st4u(hb->origin, c3);
    kc::st4u(hb->unit_z, UZ);
    kc::st4u(hb->unit_y, UY);
    kc::st4u(hb->unit_x, UX);
    kc::st4u(hb->axis_z, Z);
    kc::st4u(hb->axis_y, Y);
    kc::st4u(hb->axis_x, X);
    hb->inv_len_z = invz;
    hb->inv_len_y = invy;
    hb->inv_len_x = invx;
    kc::st4u(hb->origin_coords, R3);
    hb->chain_a = chain_a;
    hb->chain_b = chain_b;
    hb->tri_second = j1;
    hb->tri_third = j2;
    fl |= bone_fl;
    return true;
}

#endif

extern "C" uint32_t cloth_bones_81ce6500(const ClothBonesArgs *a) {
#if defined(__SSE2__)
    uint32_t mx = _mm_getcsr();
#endif
    uint32_t fl = 0;
    uint32_t fz = a->fz ? 1u : 0u;
    uint32_t C[4];
    memcpy(C, a->centre_weight, 16);
    uint32_t T = a->t;
    ClothBonesLast hb;
    memset(&hb, 0, sizeof hb);
#if KC_SIMD
    __m128 Cv = kc::ld4u(a->centre_weight);
    int cover = (a->fpsr_in & cw::FAST_FLAGS) == cw::FAST_FLAGS;
#endif
    for (uint32_t i = 0; i < a->count; i++) {
#if KC_SIMD
        if (bone_fast(a, i, Cv, T, fz, cover, fl, &hb)) continue;
#endif
        const uint8_t *pr = a->pairs + 4u * i;
        uint32_t o_i = kc::ld16(pr);
        uint32_t t_i = kc::ld16(pr + 2);
        const uint8_t *tri = a->triangles + (t_i - a->tri_first);
        uint32_t j0 = kc::ld16(tri);
        uint32_t j1 = kc::ld16(tri + 2);
        uint32_t j2 = kc::ld16(tri + 4);
        const uint8_t *base = a->positions;
        uint32_t P0[4], P1[4], P2[4], R0[4], R2[4], R3[4];
        memcpy(P0, base + 16u * (j0 - a->pos_first), 16);
        memcpy(P1, base + 16u * (j1 - a->pos_first), 16);
        memcpy(P2, base + 16u * (j2 - a->pos_first), 16);
        const uint8_t *rec = a->records + 64u * i;
        memcpy(R0, rec, 16);
        memcpy(R2, rec + 32, 16);
        memcpy(R3, rec + 48, 16);
        bone_scalar(P0, P1, P2, R0, R2, R3, C, T, j1, j2, fz, fl, a->out + (o_i - a->out_first), &hb);
    }
    *a->last = hb;
#if defined(__SSE2__)
    _mm_setcsr(mx);
#endif
    return fl;
}
