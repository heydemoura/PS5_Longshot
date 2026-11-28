/* Object-space skinning, one block of 16 vertices. SPEC.md formulas, bit-exact.
   Fast path: SSE when every operand is in the plain class (SPEC.md 5), or SSE plus
   default-NaN canonicalisation when the status flags are already raised. Otherwise
   one Arm operation per lane (cw::arm_mul / cw::arm_add). */
#include "cloth_skin.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"
#include <string.h>
#if KC_SIMD
#include <emmintrin.h>
#include <smmintrin.h>
#endif
#if defined(__AVX2__)
#include <immintrin.h>
#endif

#if defined(__GNUC__)
#define SKIN_NOINLINE __attribute__((noinline))
#else
#define SKIN_NOINLINE
#endif

namespace {

struct Col {
    uint32_t u[4];
};

static uint32_t bits_of_i(int32_t i) {
    float f = (float)i;
    uint32_t u;
    memcpy(&u, &f, 4);
    return u;
}

static const uint8_t *bone_at(const ClothSkinBlock *b, uint32_t idx) {
    return b->bones + (size_t)(idx - b->bone_base) * 64u;
}

static uint8_t *slot_at(const ClothSkinBlock *b, uint32_t num) {
    uint32_t o = (b->first + num) * b->stride;
    return b->out + (size_t)(o - b->out_base);
}

static void load_col(Col &c, const uint8_t *p) { memcpy(c.u, p, 16); }

static void mul_col(Col &d, const Col &a, uint32_t s, uint32_t fz, uint32_t &fl) {
    for (int l = 0; l < 4; l++) d.u[l] = cw::arm_mul(a.u[l], s, fz, fl);
}

static void add_col(Col &d, const Col &a, const Col &b, uint32_t fz, uint32_t &fl) {
    for (int l = 0; l < 4; l++) d.u[l] = cw::arm_add(a.u[l], b.u[l], fz, fl);
}

static void copy_col(uint32_t dst[4], const Col &c) { memcpy(dst, c.u, 16); }

/* Lane-by-lane reference for one vertex. Used for every vertex when the build has no
   SSE4.1, and as the fallback when a vector result is not covered by SPEC.md 5. */
template <int N>
SKIN_NOINLINE static uint32_t vertex_exact(const ClothSkinParams *p, const ClothSkinBlock *b,
                                            ClothSkinLast *last, int i) {
    const uint32_t fz = p->fz ? 1u : 0u;
    uint32_t fl = 0;
    const uint8_t *rec = b->record;
    uint32_t num = kc::ld16(rec + (size_t)(2 * i));
    const uint8_t *idxp = rec + 32 + (size_t)(2 * (N * i));
    const uint8_t *wp = rec + 32 + 32 * N + (size_t)((N >= 2 ? N : 0) * i);

    uint16_t P[4];
    memcpy(P, b->positions + (size_t)(8 * i), 8);
    uint32_t cbits[4], pos[4];
    uint32_t f = 0;
    for (int q = 0; q < 4; q++) {
        uint32_t z = ((uint32_t)P[q] << 16) | p->pos_low[q];
        if (q == 3) f = z;
        cbits[q] = bits_of_i((int32_t)z);
    }
    for (int q = 0; q < 4; q++) pos[q] = cw::arm_mul(cbits[q], f, fz, fl);

    uint32_t w[4];
    if (N >= 2) {
        uint32_t r[4];
        if (N == 3) {
            r[0] = bits_of_i(wp[0]);
            r[1] = bits_of_i(wp[1]);
            r[2] = bits_of_i(wp[2]);
            r[3] = p->fourth_weight;
        } else {
            for (int q = 0; q < 4; q++) r[q] = bits_of_i(wp[q]);
        }
        for (int q = 0; q < 4; q++) w[q] = cw::arm_mul(r[q], p->weight_scale[q], fz, fl);
    }

    Col B[4][4];
    for (int k = 0; k < N; k++) {
        uint32_t idx = kc::ld16(idxp + (size_t)(2 * k));
        const uint8_t *bp = bone_at(b, idx);
        for (int c = 0; c < 4; c++) load_col(B[k][c], bp + 16 * c);
    }

    Col T[4][4], M[4], head2;
    if (N == 1) {
        for (int c = 0; c < 4; c++) M[c] = B[0][c];
    } else {
        for (int k = 0; k < N; k++)
            for (int c = 0; c < 4; c++) mul_col(T[k][c], B[k][c], w[k], fz, fl);
        for (int c = 0; c < 4; c++) {
            Col acc = T[0][c];
            for (int k = 1; k < N; k++) {
                Col nxt;
                add_col(nxt, acc, T[k][c], fz, fl);
                acc = nxt;
            }
            M[c] = acc;
        }
        if (N == 2) head2 = T[0][2];
        if (N == 3) add_col(head2, T[0][2], T[1][2], fz, fl);
        if (N == 4) {
            Col s;
            add_col(s, T[0][2], T[1][2], fz, fl);
            add_col(head2, s, T[2][2], fz, fl);
        }
    }

    Col y, x, z, xsum, xy, out;
    for (int l = 0; l < 4; l++) y.u[l] = cw::arm_mul(M[1].u[l], pos[1], fz, fl);
    for (int l = 0; l < 4; l++) x.u[l] = cw::arm_mul(M[0].u[l], pos[0], fz, fl);
    for (int l = 0; l < 4; l++) z.u[l] = cw::arm_mul(M[2].u[l], pos[2], fz, fl);
    if (N >= 3) add_col(xsum, x, M[3], fz, fl);
    else add_col(xsum, M[3], x, fz, fl);
    add_col(xy, y, xsum, fz, fl);
    add_col(out, z, xy, fz, fl);

    memcpy(slot_at(b, num), out.u, 16);

    if (i == 15) {
        copy_col(last->out, out);
        copy_col(last->xy_sum, xy);
        copy_col(last->blend3, M[3]);
        if (N == 4 || N == 1) copy_col(last->x_sum, xsum);
        if (N >= 2) copy_col(last->y_term, y);
        if (N == 2) {
            for (int l = 0; l < 4; l++) last->pos[l] = pos[l];
        }
        if (N == 3 || N == 1) copy_col(last->blend2, M[2]);
        if (N >= 2) {
            copy_col(last->blend2_head, head2);
            copy_col(last->last_col2, B[N - 1][2]);
            copy_col(last->term[1][1], T[1][1]);
            copy_col(last->term[1][3], T[1][3]);
        }
        if (N >= 3) {
            copy_col(last->term[1][2], T[1][2]);
            copy_col(last->term[2][0], T[2][0]);
            copy_col(last->term[2][1], T[2][1]);
            copy_col(last->term[2][3], T[2][3]);
        }
        if (N == 4) {
            copy_col(last->term[2][2], T[2][2]);
            copy_col(last->term[3][1], T[3][1]);
            copy_col(last->term[3][3], T[3][3]);
        }
    }
    return fl;
}

#if KC_SIMD

[[maybe_unused]] static bool chk_fz_safe(const kc::Checks &ck) {
    const __m128i lim = _mm_set1_epi32(0x00800000);
    const __m128i ge = _mm_cmpeq_epi32(_mm_max_epu32(ck.mn, lim), ck.mn);
    return _mm_movemask_epi8(ge) == 0xffff;
}

static bool bones_canon(const ClothSkinBlock *b, int i, int n) {
    const uint8_t *idxp = b->record + 32 + (size_t)(2 * (n * i));
    for (int k = 0; k < n; k++) {
        uint32_t idx = kc::ld16(idxp + (size_t)(2 * k));
        const uint8_t *bp = bone_at(b, idx);
        for (int c = 0; c < 4; c++) {
            uint32_t lane[4];
            memcpy(lane, bp + 16 * c, 16);
            for (int l = 0; l < 4; l++)
                if (!cw::canon_in(lane[l])) return false;
        }
    }
    return true;
}

static __m128 maybe_canon(__m128 v, bool canon) { return canon ? cw::w_canon(v) : v; }

static void st_last(uint32_t dst[4], __m128 v) { kc::st4u(dst, v); }

template <int N, bool Save>
static uint32_t vertex_simd(const ClothSkinParams *p, const ClothSkinBlock *b, ClothSkinLast *last, int i,
                            __m128 scale, __m128i plow, bool sc_r, bool sc_b2, bool flags_ready) {
    const uint32_t fz = p->fz ? 1u : 0u;
    (void)fz;
    const uint8_t *rec = b->record;
    uint32_t num = kc::ld16(rec + (size_t)(2 * i));
    const uint8_t *idxp = rec + 32 + (size_t)(2 * (N * i));
    const uint8_t *wp = rec + 32 + 32 * N;

    __m128i p16 = _mm_loadl_epi64((const __m128i *)(const void *)(b->positions + (size_t)(8 * i)));
    __m128i zi = _mm_or_si128(_mm_slli_epi32(_mm_cvtepu16_epi32(p16), 16), plow);
    __m128 cf = _mm_cvtepi32_ps(zi);
    uint32_t fbits = (uint32_t)_mm_extract_epi32(zi, 3);
    __m128 pos = _mm_mul_ps(cf, _mm_castsi128_ps(_mm_set1_epi32((int)fbits)));

    kc::Checks ck;
    ck.reset();
    bool f_r = kc::in_r_bits(fbits);
    ck.add(pos);

    __m128 wv = _mm_setzero_ps();
    if (N >= 2) {
        __m128 r;
        if (N == 3) {
            const uint8_t *w3 = wp + (size_t)(3 * i);
            uint32_t bytes = (uint32_t)w3[0] | ((uint32_t)w3[1] << 8) | ((uint32_t)w3[2] << 16);
            r = _mm_cvtepi32_ps(_mm_cvtepu8_epi32(_mm_cvtsi32_si128((int)bytes)));
            r = cw::with3(r, p->fourth_weight);
        } else {
            const uint8_t *w4 = wp + (size_t)((N == 4 ? 4 : 2) * i);
            uint32_t bytes;
            memcpy(&bytes, w4, 4);
            r = _mm_cvtepi32_ps(_mm_cvtepu8_epi32(_mm_cvtsi32_si128((int)bytes)));
        }
        wv = _mm_mul_ps(r, scale);
        ck.add(wv);
    }

    __m128 M[4];
    __m128 head2 = _mm_setzero_ps();
    __m128 lastc2 = _mm_setzero_ps();
    __m128 term_s[4][4];

    for (int k = 0; k < N; k++) {
        uint32_t idx = kc::ld16(idxp + (size_t)(2 * k));
        const uint8_t *bp = bone_at(b, idx);
        __m128 Bk[4];
        for (int c = 0; c < 4; c++) {
            Bk[c] = kc::ld4(bp + 16 * c);
            ck.add(Bk[c]);
        }
        if (N == 1) {
            for (int c = 0; c < 4; c++) M[c] = Bk[c];
        } else {
            __m128 wk = k == 0 ? KC_SPLAT(wv, 0) : k == 1 ? KC_SPLAT(wv, 1) : k == 2 ? KC_SPLAT(wv, 2) : KC_SPLAT(wv, 3);
            for (int c = 0; c < 4; c++) {
                __m128 T = _mm_mul_ps(Bk[c], wk);
                ck.add(T);
                if (k == 0) M[c] = T;
                else {
                    M[c] = _mm_add_ps(M[c], T);
                    ck.add(M[c]);
                }
                if (Save) term_s[k][c] = T;
            }
            if (Save && k == N - 2) head2 = M[2];
        }
        if (Save && N >= 2 && k == N - 1) lastc2 = Bk[2];
    }

    __m128 y = _mm_mul_ps(M[1], KC_SPLAT(pos, 1));
    __m128 x = _mm_mul_ps(M[0], KC_SPLAT(pos, 0));
    __m128 z = _mm_mul_ps(M[2], KC_SPLAT(pos, 2));
    ck.add(y);
    ck.add(x);
    ck.add(z);
    __m128 xsum = (N >= 3) ? _mm_add_ps(x, M[3]) : _mm_add_ps(M[3], x);
    ck.add(xsum);
    __m128 xy = _mm_add_ps(y, xsum);
    ck.add(xy);
    __m128 outv = _mm_add_ps(z, xy);
    ck.add(outv);

    const bool b1 = sc_r && f_r && ck.ok();
    bool b2 = false;
    if (!b1 && sc_b2 && flags_ready && cw::canon_in(fbits) && (!p->fz || (cw::fz_safe(fbits) && chk_fz_safe(ck))))
        b2 = bones_canon(b, i, N);

    if (!b1 && !b2) return vertex_exact<N>(p, b, last, i);

    const bool canon = b2;
    kc::st4(slot_at(b, num), maybe_canon(outv, canon));
    if (Save) {
        st_last(last->out, maybe_canon(outv, canon));
        st_last(last->xy_sum, maybe_canon(xy, canon));
        st_last(last->blend3, maybe_canon(M[3], canon && N != 1));
        if (N == 1) st_last(last->blend3, M[3]);
        if (N == 4 || N == 1) st_last(last->x_sum, maybe_canon(xsum, canon));
        if (N >= 2) st_last(last->y_term, maybe_canon(y, canon));
        if (N == 2) st_last(last->pos, maybe_canon(pos, canon));
        if (N == 3) st_last(last->blend2, maybe_canon(M[2], canon));
        if (N == 1) st_last(last->blend2, M[2]);
        if (N >= 2) {
            st_last(last->blend2_head, maybe_canon(head2, canon));
            st_last(last->last_col2, lastc2);
            st_last(last->term[1][1], maybe_canon(term_s[1][1], canon));
            st_last(last->term[1][3], maybe_canon(term_s[1][3], canon));
        }
        if (N >= 3) {
            st_last(last->term[1][2], maybe_canon(term_s[1][2], canon));
            st_last(last->term[2][0], maybe_canon(term_s[2][0], canon));
            st_last(last->term[2][1], maybe_canon(term_s[2][1], canon));
            st_last(last->term[2][3], maybe_canon(term_s[2][3], canon));
        }
        if (N == 4) {
            st_last(last->term[2][2], maybe_canon(term_s[2][2], canon));
            st_last(last->term[3][1], maybe_canon(term_s[3][1], canon));
            st_last(last->term[3][3], maybe_canon(term_s[3][3], canon));
        }
    }
    return 0;
}

#if defined(__AVX2__)

struct Chk8 {
    __m256i mx, mn;
    void reset() {
        mx = _mm256_setzero_si256();
        mn = _mm256_set1_epi32(-1);
    }
    void add256(__m256 v) {
        const __m256i a = _mm256_and_si256(_mm256_castps_si256(v), _mm256_set1_epi32(0x7fffffff));
        mx = _mm256_max_epu32(mx, a);
        mn = _mm256_min_epu32(mn, _mm256_sub_epi32(a, _mm256_set1_epi32(1)));
    }
    void add128(__m128 v) { add256(_mm256_zextps128_ps256(v)); }
    bool ok() const {
        const __m256i hi = _mm256_set1_epi32(0x5effffff), lo = _mm256_set1_epi32(0x1fffffff);
        const __m256i e = _mm256_and_si256(_mm256_cmpeq_epi32(_mm256_max_epu32(mx, hi), hi),
                                            _mm256_cmpeq_epi32(_mm256_min_epu32(mn, lo), lo));
        return (uint32_t)_mm256_movemask_epi8(e) == 0xffffffffu;
    }
    bool fz_ok() const {
        const __m256i lim = _mm256_set1_epi32(0x00800000);
        const __m256i ge = _mm256_cmpeq_epi32(_mm256_max_epu32(mn, lim), mn);
        return (uint32_t)_mm256_movemask_epi8(ge) == 0xffffffffu;
    }
};

template <int L>
static __m256 bcast_lane(__m128 v) {
    __m128 s = KC_SPLAT(v, L);
    return _mm256_insertf128_ps(_mm256_castps128_ps256(s), s, 1);
}

template <int N, bool Save>
static uint32_t vertex_avx(const ClothSkinParams *p, const ClothSkinBlock *b, ClothSkinLast *last, int i,
                           __m128 scale, __m128i plow, bool sc_r, bool sc_b2, bool flags_ready) {
    const uint8_t *rec = b->record;
    uint32_t num = kc::ld16(rec + (size_t)(2 * i));
    const uint8_t *idxp = rec + 32 + (size_t)(2 * (N * i));
    const uint8_t *wp = rec + 32 + 32 * N;

    __m128i p16 = _mm_loadl_epi64((const __m128i *)(const void *)(b->positions + (size_t)(8 * i)));
    __m128i zi = _mm_or_si128(_mm_slli_epi32(_mm_cvtepu16_epi32(p16), 16), plow);
    __m128 cf = _mm_cvtepi32_ps(zi);
    uint32_t fbits = (uint32_t)_mm_extract_epi32(zi, 3);
    __m128 pos = _mm_mul_ps(cf, _mm_castsi128_ps(_mm_shuffle_epi32(zi, 0xff)));

    Chk8 ck;
    ck.reset();
    ck.add128(pos);
    const bool f_r = kc::in_r_bits(fbits);

    __m128 wv = _mm_setzero_ps();
    if (N >= 2) {
        __m128 r;
        if (N == 3) {
            const uint8_t *w3 = wp + (size_t)(3 * i);
            uint32_t bytes = (uint32_t)w3[0] | ((uint32_t)w3[1] << 8) | ((uint32_t)w3[2] << 16);
            r = _mm_cvtepi32_ps(_mm_cvtepu8_epi32(_mm_cvtsi32_si128((int)bytes)));
            r = cw::with3(r, p->fourth_weight);
        } else {
            const uint8_t *w4 = wp + (size_t)((N == 4 ? 4 : 2) * i);
            uint32_t bytes;
            memcpy(&bytes, w4, 4);
            r = _mm_cvtepi32_ps(_mm_cvtepu8_epi32(_mm_cvtsi32_si128((int)bytes)));
        }
        wv = _mm_mul_ps(r, scale);
        ck.add128(wv);
    }

    __m256 M01 = _mm256_setzero_ps(), M23 = _mm256_setzero_ps();
    __m128 head2 = _mm_setzero_ps(), lastc2 = _mm_setzero_ps();
    __m128 tc[4][4];

    for (int k = 0; k < N; k++) {
        uint32_t idx = kc::ld16(idxp + (size_t)(2 * k));
        const uint8_t *bp = bone_at(b, idx);
        __m256 b01 = _mm256_loadu_ps((const float *)(const void *)bp);
        __m256 b23 = _mm256_loadu_ps((const float *)(const void *)(bp + 32));
        ck.add256(b01);
        ck.add256(b23);
        if (N == 1) {
            M01 = b01;
            M23 = b23;
        } else {
            __m256 wd = k == 0 ? bcast_lane<0>(wv) : k == 1 ? bcast_lane<1>(wv) : k == 2 ? bcast_lane<2>(wv) : bcast_lane<3>(wv);
            __m256 t01 = _mm256_mul_ps(b01, wd);
            __m256 t23 = _mm256_mul_ps(b23, wd);
            ck.add256(t01);
            ck.add256(t23);
            if (k == 0) {
                M01 = t01;
                M23 = t23;
            } else {
                M01 = _mm256_add_ps(M01, t01);
                M23 = _mm256_add_ps(M23, t23);
                ck.add256(M01);
                ck.add256(M23);
            }
            if (Save) {
                tc[k][0] = _mm256_castps256_ps128(t01);
                tc[k][1] = _mm256_extractf128_ps(t01, 1);
                tc[k][2] = _mm256_castps256_ps128(t23);
                tc[k][3] = _mm256_extractf128_ps(t23, 1);
            }
            if (Save && k == N - 2) head2 = _mm256_castps256_ps128(M23);
        }
        if (Save && N >= 2 && k == N - 1) lastc2 = _mm256_castps256_ps128(b23);
    }

    __m128 M0 = _mm256_castps256_ps128(M01);
    __m128 M1 = _mm256_extractf128_ps(M01, 1);
    (void)M0, (void)M1;
    __m128 M2 = _mm256_castps256_ps128(M23);
    __m128 M3 = _mm256_extractf128_ps(M23, 1);
    __m128 ps0 = KC_SPLAT(pos, 0);
    __m128 ps1 = KC_SPLAT(pos, 1);
    __m256 yx = _mm256_mul_ps(M01, _mm256_insertf128_ps(_mm256_castps128_ps256(ps0), ps1, 1));
    ck.add256(yx);
    __m128 x = _mm256_castps256_ps128(yx);
    __m128 y = _mm256_extractf128_ps(yx, 1);
    __m128 z = _mm_mul_ps(M2, KC_SPLAT(pos, 2));
    ck.add128(z);
    __m128 xsum = (N >= 3) ? _mm_add_ps(x, M3) : _mm_add_ps(M3, x);
    ck.add128(xsum);
    __m128 xy = _mm_add_ps(y, xsum);
    ck.add128(xy);
    __m128 outv = _mm_add_ps(z, xy);
    ck.add128(outv);

    const bool b1 = sc_r && f_r && ck.ok();
    bool b2 = false;
    if (!b1 && sc_b2 && flags_ready && cw::canon_in(fbits) && (!p->fz || (cw::fz_safe(fbits) && ck.fz_ok())))
        b2 = bones_canon(b, i, N);
    if (!b1 && !b2) return vertex_exact<N>(p, b, last, i);

    const bool canon = b2;
    kc::st4(slot_at(b, num), maybe_canon(outv, canon));
    if (Save) {
        st_last(last->out, maybe_canon(outv, canon));
        st_last(last->xy_sum, maybe_canon(xy, canon));
        if (N == 1) st_last(last->blend3, M3);
        else st_last(last->blend3, maybe_canon(M3, canon));
        if (N == 4 || N == 1) st_last(last->x_sum, maybe_canon(xsum, canon));
        if (N >= 2) st_last(last->y_term, maybe_canon(y, canon));
        if (N == 2) st_last(last->pos, maybe_canon(pos, canon));
        if (N == 3) st_last(last->blend2, maybe_canon(M2, canon));
        if (N == 1) st_last(last->blend2, M2);
        if (N >= 2) {
            st_last(last->blend2_head, maybe_canon(head2, canon));
            st_last(last->last_col2, lastc2);
            st_last(last->term[1][1], maybe_canon(tc[1][1], canon));
            st_last(last->term[1][3], maybe_canon(tc[1][3], canon));
        }
        if (N >= 3) {
            st_last(last->term[1][2], maybe_canon(tc[1][2], canon));
            st_last(last->term[2][0], maybe_canon(tc[2][0], canon));
            st_last(last->term[2][1], maybe_canon(tc[2][1], canon));
            st_last(last->term[2][3], maybe_canon(tc[2][3], canon));
        }
        if (N == 4) {
            st_last(last->term[2][2], maybe_canon(tc[2][2], canon));
            st_last(last->term[3][1], maybe_canon(tc[3][1], canon));
            st_last(last->term[3][3], maybe_canon(tc[3][3], canon));
        }
    }
    return 0;
}

#endif

template <int N>
static uint32_t run_simd(const ClothSkinParams *p, const ClothSkinBlock *b, ClothSkinLast *last) {
    bool sc_r = true, sc_c = true, sc_z = true;
    if (N >= 2) {
        for (int q = 0; q < 4; q++) {
            uint32_t s = p->weight_scale[q];
            sc_r = sc_r && kc::in_r_bits(s);
            sc_c = sc_c && cw::canon_in(s);
            sc_z = sc_z && cw::fz_safe(s);
        }
    }
    if (N == 3) {
        sc_r = sc_r && kc::in_r_bits(p->fourth_weight);
        sc_c = sc_c && cw::canon_in(p->fourth_weight);
        sc_z = sc_z && cw::fz_safe(p->fourth_weight);
    }
    const bool flags_ready = (p->fpsr_in & cw::FAST_FLAGS) == cw::FAST_FLAGS;
    const bool sc_b2 = sc_c && (!p->fz || sc_z);
    __m128 scale = N >= 2 ? kc::ld4u(p->weight_scale) : _mm_setzero_ps();
    __m128i plow = _mm_cvtepu16_epi32(_mm_loadl_epi64((const __m128i *)(const void *)p->pos_low));

    uint32_t fl = 0;
    for (int i = 0; i < 15; i++)
#if defined(__AVX2__)
        fl |= vertex_avx<N, false>(p, b, last, i, scale, plow, sc_r, sc_b2, flags_ready);
    fl |= vertex_avx<N, true>(p, b, last, 15, scale, plow, sc_r, sc_b2, flags_ready);
#else
        fl |= vertex_simd<N, false>(p, b, last, i, scale, plow, sc_r, sc_b2, flags_ready);
    fl |= vertex_simd<N, true>(p, b, last, 15, scale, plow, sc_r, sc_b2, flags_ready);
#endif
    return fl;
}

#endif

template <int N>
static uint32_t run_exact(const ClothSkinParams *p, const ClothSkinBlock *b, ClothSkinLast *last) {
    uint32_t fl = 0;
    for (int i = 0; i < 16; i++) fl |= vertex_exact<N>(p, b, last, i);
    return fl;
}

template <int N>
static uint32_t run(const ClothSkinParams *p, const ClothSkinBlock *b, ClothSkinLast *last) {
#if KC_SIMD
    return run_simd<N>(p, b, last);
#else
    return run_exact<N>(p, b, last);
#endif
}

}  // namespace

uint32_t cloth_skin_81db9a40(const ClothSkinParams *p, const ClothSkinBlock *b, ClothSkinLast *last) {
    switch (b->influences) {
    case 4: return run<4>(p, b, last);
    case 3: return run<3>(p, b, last);
    case 2: return run<2>(p, b, last);
    default: return run<1>(p, b, last);
    }
}
