/* Anchor pass host kernel. Fast path: SSE on records whose operands stay in the
   exactness class (lane w included, or lane w by the fast NaN rule). Anything
   else is one scalar record with Arm ops. A call is declined only when a range
   is unmapped, a count is negative, or scratch for a rare undo cannot be made. */
#include "cloth_anchor.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"

#include <xmmintrin.h>
#include <emmintrin.h>
#include <cstdint>
#include <cstring>
#include <cstdlib>

#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif

#if KC_SIMD
#include <smmintrin.h>
#endif

namespace {

struct Mx {
    unsigned v;
    Mx() : v(_mm_getcsr()) {}
    ~Mx() { _mm_setcsr(v); }
};

struct Undo {
    uint8_t *dst;
    uint8_t b[16];
};

thread_local Undo *g_undo = nullptr;
thread_local uint32_t g_cap = 0;

bool ensure_undo(uint32_t n) {
    if (n <= g_cap) return true;
    uint32_t c = g_cap ? g_cap : 64u;
    while (c < n) c *= 2u;
    void *p = realloc(g_undo, (size_t)c * sizeof(Undo));
    if (!p) return false;
    g_undo = (Undo *)p;
    g_cap = c;
    return true;
}

void undo_all(uint32_t n) {
    while (n) {
        n--;
        memcpy(g_undo[n].dst, g_undo[n].b, 16);
    }
}

void save_undo(uint32_t &n, uint8_t *dst) {
    memcpy(g_undo[n].b, dst, 16);
    g_undo[n].dst = dst;
    n++;
}

int decline(ClothAnchorResult *res, int why) {
    res->why = why;
    res->fpsr = 0;
    res->records = 0;
    res->slot_lo = 1;
    res->slot_hi = 0;
    return 0;
}

int finish(ClothAnchorResult *res, uint32_t fl, uint32_t nrec, uint32_t lo, uint32_t hi) {
    res->fpsr = fl;
    res->records = nrec;
    if (lo > hi) {
        res->slot_lo = 1;
        res->slot_hi = 0;
    } else {
        res->slot_lo = lo;
        res->slot_hi = hi;
    }
    res->why = CLOTH_ANCHOR_DONE;
    return 1;
}

void *map(ClothAnchorRange range, uint64_t addr, uint64_t len) {
    if (!len) return nullptr;
    return range(addr, len);
}

bool overlap(uint64_t a, uint64_t an, uint64_t b, uint64_t bn) {
    if (!an || !bn) return false;
    return a < b + bn && b < a + an;
}

/* quiet compare: 1 greater, -1 less, 0 equal, 2 unordered */
int qcmp(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    cw::flush_in(a, fz, fl);
    cw::flush_in(b, fz, fl);
    const bool na = (a & 0x7fffffffu) > 0x7f800000u;
    const bool nb = (b & 0x7fffffffu) > 0x7f800000u;
    if (na || nb) {
        if (na && !(a & 0x00400000u)) fl |= cw::IOC;
        if (nb && !(b & 0x00400000u)) fl |= cw::IOC;
        return 2;
    }
    const float fa = cw::f32(a), fb = cw::f32(b);
    if (fa > fb) return 1;
    if (fa < fb) return -1;
    return 0;
}

bool qlt_zero(uint32_t a_orig, uint32_t fz, uint32_t &fl) {
    uint32_t a = a_orig;
    cw::flush_in(a, fz, fl);
    if ((a & 0x7fffffffu) > 0x7f800000u) {
        if (!(a & 0x00400000u)) fl |= cw::IOC;
        return false;
    }
    return (a & 0x80000000u) && (a & 0x7fffffffu);
}

uint32_t fused_step(uint32_t L, uint32_t x, uint32_t fz, uint32_t &fl) {
    cw::flush_in(L, fz, fl);
    cw::flush_in(x, fz, fl);
    const uint32_t mL = L & 0x7fffffffu, mx = x & 0x7fffffffu;
    if (mL > 0x7f800000u || mx > 0x7f800000u) return cw::nans(L, x, fl);
    const bool Linf = mL == 0x7f800000u, xinf = mx == 0x7f800000u;
    const bool Lz = mL == 0, xz = mx == 0;
    if ((Linf && xz) || (Lz && xinf)) return 0x3fc00000u;
    if (Linf || xinf) return 0xff800000u;
    const double prod = (double)cw::f32(L) * (double)cw::f32(x);
    const double s = (3.0 - prod) * 0.5;
    if (s == 0.0) return 0u;
    return cw::round_from(s, s < 0.0 ? 0x80000000u : 0u, fz, fl);
}

uint32_t compute_r(uint32_t L, uint32_t fz, uint32_t &fl) {
    const int c = qcmp(L, 0x007fffffu, fz, fl);
    if (!(c == 1 || c == 2)) return 0u;
    const uint32_t mag = L & 0x7fffffffu;
    if (mag > 0x7f800000u || mag == 0x7f800000u) {
        uint32_t e;
        if (mag > 0x7f800000u) {
            if (!(L & 0x00400000u)) {
                fl |= cw::IOC;
                e = L | 0x00400000u;
            } else {
                e = L;
            }
        } else if (L & 0x80000000u) {
            fl |= cw::IOC;
            e = cw::DEFAULT_NAN;
        } else {
            e = 0u;
        }
        const uint32_t x = cw::arm_mul(e, e, fz, fl);
        const uint32_t s = fused_step(L, x, fz, fl);
        return cw::arm_mul(e, s, fz, fl);
    }
    if (kc::in_r_bits(L) && !(L & 0x80000000u)) {
        unsigned bad = 0;
        const float r = kc::rsq_step(cw::f32(L), bad);
        if (!bad) return cw::u32(r);
    }
    const uint32_t e = kc::frsqrte_bits(L);
    const uint32_t x = cw::arm_mul(e, e, fz, fl);
    const uint32_t s = fused_step(L, x, fz, fl);
    return cw::arm_mul(e, s, fz, fl);
}

void scalar_record(const uint8_t *c0p, const uint8_t *c1p, const uint8_t *c2p, const uint8_t *c3p,
                   const uint32_t eps[4], uint32_t lim0, uint32_t lim1, uint32_t kf, bool scaled, bool longrec,
                   const uint8_t *sx_p, const uint8_t *vp, const uint8_t *rec, uint8_t *slot, uint32_t fz,
                   uint32_t &fl, uint32_t &lo, uint32_t &hi) {
    uint32_t c0[4], c1[4], c2[4], c3[4], V[4], P[4];
    memcpy(c0, c0p, 16);
    memcpy(c1, c1p, 16);
    memcpy(c2, c2p, 16);
    memcpy(c3, c3p, 16);
    memcpy(V, vp, 16);
    memcpy(P, slot, 16);
    uint16_t pi;
    memcpy(&pi, rec, 2);
    uint32_t radius, stiff = 0;
    memcpy(&radius, rec + 4, 4);
    if (longrec) memcpy(&stiff, rec + 16, 4);

    uint32_t T0[4], T1[4], T2[4], D[4], Q[4];
    for (int i = 0; i < 4; i++) {
        T0[i] = cw::arm_mul(c0[i], V[0], fz, fl);
        T1[i] = cw::arm_mul(c1[i], V[1], fz, fl);
        T2[i] = cw::arm_mul(c2[i], V[2], fz, fl);
    }
    for (int i = 0; i < 4; i++) {
        const uint32_t s0 = cw::arm_add(c3[i], T0[i], fz, fl);
        const uint32_t s1 = cw::arm_add(s0, T1[i], fz, fl);
        const uint32_t W = cw::arm_add(s1, T2[i], fz, fl);
        const uint32_t DW = cw::arm_sub(P[i], W, fz, fl);
        D[i] = cw::arm_add(eps[i], DW, fz, fl);
        Q[i] = cw::arm_mul(D[i], D[i], fz, fl);
    }
    const uint32_t L = cw::arm_add(cw::arm_add(Q[0], Q[1], fz, fl), Q[2], fz, fl);
    const uint32_t r = compute_r(L, fz, fl);
    const uint32_t len = cw::arm_mul(L, r, fz, fl);
    const uint32_t k = longrec ? cw::arm_mul(kf, stiff, fz, fl) : kf;
    uint32_t rp = radius;
    if (scaled) {
        uint32_t sx;
        memcpy(&sx, sx_p, 4);
        const uint32_t sel = qlt_zero(sx, fz, fl) ? 0u : sx;
        rp = cw::arm_mul(sel, radius, fz, fl);
    }
    const uint32_t e = cw::arm_sub(rp, len, fz, fl);
    const uint32_t g = cw::arm_mul(e, k, fz, fl);
    const uint32_t c0s = cw::arm_fmin(g, lim0, fz, fl);
    const uint32_t c1s = cw::arm_fmin(g, lim1, fz, fl);
    const uint32_t coef[4] = {c0s, c1s, c0s, c1s};
    uint32_t Pp[4];
    for (int i = 0; i < 4; i++) {
        const uint32_t DD = cw::arm_mul(D[i], r, fz, fl);
        const uint32_t DEL = cw::arm_mul(DD, coef[i], fz, fl);
        Pp[i] = cw::arm_add(P[i], DEL, fz, fl);
    }
    memcpy(slot, Pp, 16);
    kc::note_slot(lo, hi, pi);
}

#if KC_SIMD

inline bool xyz_in_r(__m128 v) {
    const uint32_t x = (uint32_t)_mm_extract_ps(v, 0);
    const uint32_t y = (uint32_t)_mm_extract_ps(v, 1);
    const uint32_t z = (uint32_t)_mm_extract_ps(v, 2);
    return kc::in_r_bits(x) && kc::in_r_bits(y) && kc::in_r_bits(z);
}

inline bool all_in_r(__m128 v) { return xyz_in_r(v) && kc::in_r_bits(cw::lane3(v)); }

inline __m128 xyz_of(__m128 v) { return _mm_blend_ps(v, _mm_setzero_ps(), 8); }

/* Mode 0: every column lane is in class and the guest status does not already hold the fast-w flags,
   so lane w is proved with the class (no canonical-NaN store). Mode 1: lane w is the fast NaN path
   for the whole call. Mode 2: it can go either way per record. */
template <bool Scaled, bool Long, int Mode>
__attribute__((always_inline)) bool simd_record(bool fz_on, __m128 c0, __m128 c1, __m128 c2, __m128 c3, __m128 eps,
                                                __m128 lims, float kf, uint32_t sx_bits, const uint8_t *vp,
                                                const uint8_t *rec, uint8_t *slot, uint32_t &lo, uint32_t &hi) {
    uint16_t pi;
    memcpy(&pi, rec, 2);
    const __m128 V = kc::ld4(vp);
    const __m128 P = kc::ld4(slot);
    bool fast = Mode == 1;
    if (Mode == 1) {
        const uint32_t Pw = cw::lane3(P);
        if (!cw::canon_in(Pw) || (fz_on && !cw::fz_safe(Pw))) return false;
    } else if (Mode == 2) {
        const uint32_t Pw = cw::lane3(P);
        const bool pw_class = kc::in_r_bits(Pw);
        /* cols are in class in mode 2 only when the caller picked it; wposs is true there. */
        fast = !pw_class;
        if (fast && (!cw::canon_in(Pw) || (fz_on && !cw::fz_safe(Pw)))) return false;
    }

    const float radius = kc::ld1(rec + 4);
    float rp = radius;
    float k = kf;
    float stiff = 0.f;
    if (Long) {
        stiff = kc::ld1(rec + 16);
        k = kf * stiff;
    }
    if (Scaled) {
        const float sx = cw::f32(sx_bits);
        const float sel = ((sx_bits & 0x80000000u) && (sx_bits & 0x7fffffffu)) ? 0.f : sx;
        rp = sel * radius;
    }

    const __m128 T0 = _mm_mul_ps(c0, KC_SPLAT(V, 0));
    const __m128 T1 = _mm_mul_ps(c1, KC_SPLAT(V, 1));
    const __m128 T2 = _mm_mul_ps(c2, KC_SPLAT(V, 2));
    const __m128 S1 = _mm_add_ps(c3, T0);
    const __m128 S2 = _mm_add_ps(S1, T1);
    const __m128 W = _mm_add_ps(S2, T2);
    const __m128 DW = _mm_sub_ps(P, W);
    const __m128 D = _mm_add_ps(eps, DW);
    const __m128 Q = _mm_mul_ps(D, D);
    const float L = _mm_cvtss_f32(_mm_add_ss(_mm_add_ss(Q, _mm_shuffle_ps(Q, Q, 1)), _mm_movehl_ps(Q, Q)));
    if (!kc::in_r(L)) return false;

    unsigned bad = 0;
    float r = 0.f;
    if (L > 0.f) {
        r = kc::rsq_step(L, bad);
        if (bad) return false;
    }
    const float len = L * r;
    const float e = rp - len;
    const float g = e * k;
    const __m128 coef = kc::fmin_arm4(_mm_set1_ps(g), lims);
    const __m128 DD = _mm_mul_ps(D, _mm_set1_ps(r));
    const __m128 DELTA = _mm_mul_ps(DD, coef);
    __m128 Pp = _mm_add_ps(P, DELTA);

    kc::Checks ck;
    ck.reset();
    ck.add(xyz_of(V));
    ck.add(xyz_of(P));
    ck.add(xyz_of(D));
    ck.add(xyz_of(DELTA));
    ck.add(xyz_of(Pp));
    if (!fast) ck.add(_mm_movehl_ps(_mm_unpackhi_ps(DELTA, Pp), _mm_unpackhi_ps(P, D)));
    if (ck.mask() != 0xffff) return false;
    if (!kc::in_r(radius) || !kc::in_r(len) || !kc::in_r(e) || !kc::in_r(g)) return false;
    if (Scaled && !kc::in_r(rp)) return false;
    if (Long && (!kc::in_r(stiff) || !kc::in_r(k))) return false;
    if (fast) {
        if (fz_on) {   /* (every lane-3 result: a tiny one is a zero on the Arm; such a record goes its way) */
            kc::Checks w;
            w.reset();
            if (Mode == 1) w.add(T0), w.add(T1), w.add(T2), w.add(S1);   /* (mode 2: column lanes in the class, never tiny) */
            w.add(S2), w.add(W), w.add(DW), w.add(D), w.add(DD), w.add(DELTA), w.add(Pp);
            if (!cw::fz_safe_min3(w.mn)) return false;
        }
        Pp = cw::w_canon(Pp);
    }
    kc::st4(slot, Pp);
    kc::note_slot(lo, hi, pi);
    return true;
}

template <bool Scaled, bool Long, int Mode>
__attribute__((noinline, flatten)) int32_t fast_span(bool fz_on, __m128 c0, __m128 c1, __m128 c2, __m128 c3, __m128 eps,
                                                     float lim0, float lim1, float kf, uint32_t sx_bits,
                                                     const uint8_t *list, uint32_t rsize, const uint8_t *vspan,
                                                     uint8_t *sspan, uint32_t stride, uint32_t min_a, uint32_t min_p,
                                                     int32_t begin, int32_t n, uint32_t &lo, uint32_t &hi) {
    const __m128 lims = _mm_blend_ps(_mm_set1_ps(lim0), _mm_set1_ps(lim1), 0xA);
    for (int32_t i = begin; i < n; i++) {
        const uint8_t *rec = list + (size_t)i * rsize;
        uint16_t p, a;
        memcpy(&p, rec, 2);
        memcpy(&a, rec + 2, 2);
        const uint8_t *vp = vspan + (uint64_t)stride * ((uint32_t)a - min_a);
        uint8_t *sp = sspan + ((size_t)p - min_p) * 16u;
        if (!simd_record<Scaled, Long, Mode>(fz_on, c0, c1, c2, c3, eps, lims, kf, sx_bits, vp, rec, sp, lo, hi))
            return i;
    }
    return n;
}

template <bool Scaled, bool Long>
int32_t dispatch_span(bool wposs, bool cols_class, bool fz_on, __m128 c0, __m128 c1, __m128 c2, __m128 c3, __m128 eps,
                      float lim0, float lim1, float kf, uint32_t sx_bits, const uint8_t *list, uint32_t rsize,
                      const uint8_t *vspan, uint8_t *sspan, uint32_t stride, uint32_t min_a, uint32_t min_p,
                      int32_t begin, int32_t n, uint32_t &lo, uint32_t &hi) {
    if (cols_class && !wposs)
        return fast_span<Scaled, Long, 0>(fz_on, c0, c1, c2, c3, eps, lim0, lim1, kf, sx_bits, list, rsize, vspan,
                                          sspan, stride, min_a, min_p, begin, n, lo, hi);
    if (!cols_class && wposs)
        return fast_span<Scaled, Long, 1>(fz_on, c0, c1, c2, c3, eps, lim0, lim1, kf, sx_bits, list, rsize, vspan,
                                          sspan, stride, min_a, min_p, begin, n, lo, hi);
    return fast_span<Scaled, Long, 2>(fz_on, c0, c1, c2, c3, eps, lim0, lim1, kf, sx_bits, list, rsize, vspan, sspan,
                                      stride, min_a, min_p, begin, n, lo, hi);
}

#endif

}  // namespace

extern "C" int cloth_anchor_81c7bd00(const ClothAnchorCall *call, ClothAnchorResult *res) {
    Mx mxcsr;
    uint32_t fl = 0;
    const uint32_t fz = call->fz ? 1u : 0u;
    ClothAnchorRange range = call->range;

    void *pstr = map(range, call->constraint + 0x6c, 4);
    void *pfac = map(range, call->factor, 8);
    if (!pstr || !pfac) return decline(res, CLOTH_ANCHOR_RANGE);
    uint32_t strength, factor;
    memcpy(&strength, pstr, 4);
    memcpy(&factor, pfac, 4);
    const uint32_t kf_bits = cw::arm_mul(strength, factor, fz, fl);

    bool scaled = false;
    const uint8_t *scale_p = nullptr;
    uint32_t sx0 = 0, sy0 = 0, sz0 = 0;
    if (call->scale) {
        void *ps = map(range, call->scale + 0x18, 12);
        if (!ps) return decline(res, CLOTH_ANCHOR_RANGE);
        scale_p = (const uint8_t *)ps;
        memcpy(&sx0, scale_p, 4);
        memcpy(&sy0, scale_p + 4, 4);
        memcpy(&sz0, scale_p + 8, 4);
        scaled = true;
        const uint32_t one = 0x3f800000u;
        if (qcmp(sx0, one, fz, fl) == 0 && qcmp(sy0, one, fz, fl) == 0 && qcmp(sz0, one, fz, fl) == 0) scaled = false;
    }

    void *psc = map(range, call->constraint + 0x30, 4);
    if (!psc) return decline(res, CLOTH_ANCHOR_RANGE);
    int32_t nshort;
    memcpy(&nshort, psc, 4);

    bool longrec = false;
    int32_t n = 0;
    uint64_t list_ga = 0;
    if (nshort != 0) {
        if (nshort < 0) return decline(res, CLOTH_ANCHOR_COUNT);
        void *pla = map(range, call->constraint + 0x28, 8);
        if (!pla) return decline(res, CLOTH_ANCHOR_RANGE);
        memcpy(&list_ga, pla, 8);
        n = nshort;
        longrec = false;
    } else {
        void *plc = map(range, call->constraint + 0x40, 4);
        if (!plc) return decline(res, CLOTH_ANCHOR_RANGE);
        int32_t nlong;
        memcpy(&nlong, plc, 4);
        if (nlong < 0) return decline(res, CLOTH_ANCHOR_COUNT);
        if (nlong == 0) return finish(res, fl, 0, 1, 0);
        void *pla = map(range, call->constraint + 0x38, 8);
        if (!pla) return decline(res, CLOTH_ANCHOR_RANGE);
        memcpy(&list_ga, pla, 8);
        n = nlong;
        longrec = true;
    }

    const uint32_t rsize = longrec ? 20u : 16u;
    void *plist = map(range, list_ga, (uint64_t)n * rsize);
    if (!plist) return decline(res, CLOTH_ANCHOR_RANGE);
    const uint8_t *list = (const uint8_t *)plist;

    void *pcell = map(range, CLOTH_ANCHOR_CONSTANTS_CELL, 8);
    if (!pcell) return decline(res, CLOTH_ANCHOR_RANGE);
    uint64_t cbase;
    memcpy(&cbase, pcell, 8);
    void *plim = map(range, cbase + 0x50, 8);
    void *peps = map(range, cbase + 0x270, 16);
    if (!plim || !peps) return decline(res, CLOTH_ANCHOR_RANGE);
    uint32_t lim0, lim1;
    memcpy(&lim0, plim, 4);
    memcpy(&lim1, (const uint8_t *)plim + 4, 4);
    uint32_t eps_u[4];
    memcpy(eps_u, peps, 16);

    void *pvb = map(range, call->skin + 0x18, 8);
    void *pst = map(range, call->skin + 0x24, 1);
    void *pcol = map(range, call->skin + 0x90, 0x40);
    if (!pvb || !pst || !pcol) return decline(res, CLOTH_ANCHOR_RANGE);
    const uint8_t *vbase_p = (const uint8_t *)pvb;
    const uint8_t *stride_p = (const uint8_t *)pst;
    const uint8_t *col = (const uint8_t *)pcol;

    uint16_t min_p = 0xffff, max_p = 0, min_a = 0xffff, max_a = 0;
    for (int32_t i = 0; i < n; i++) {
        uint16_t p, a;
        memcpy(&p, list + (size_t)i * rsize, 2);
        memcpy(&a, list + (size_t)i * rsize + 2, 2);
        if (p < min_p) min_p = p;
        if (p > max_p) max_p = p;
        if (a < min_a) min_a = a;
        if (a > max_a) max_a = a;
    }

    uint64_t vb0;
    memcpy(&vb0, vbase_p, 8);
    const uint8_t stride0 = *stride_p;
    const uint64_t slot_ga = call->slots + (uint64_t)min_p * 16u;
    const uint64_t slot_len = (uint64_t)(max_p - min_p + 1u) * 16u;
    const uint64_t vert_ga = vb0 + (uint64_t)stride0 * min_a;
    const uint64_t vert_len = (vb0 + (uint64_t)stride0 * max_a + 16u) - vert_ga;
    const uint64_t list_len = (uint64_t)n * rsize;

    const bool live_rec = overlap(slot_ga, slot_len, list_ga, list_len);
    bool live_skin = overlap(slot_ga, slot_len, call->skin + 0x18, 8) || overlap(slot_ga, slot_len, call->skin + 0x24, 1) ||
                     overlap(slot_ga, slot_len, call->skin + 0x90, 0x40);
    if (scaled && overlap(slot_ga, slot_len, call->scale + 0x18, 4)) live_skin = true;
    uint8_t *sspan = nullptr;
    const uint8_t *vspan = nullptr;
    bool span_ok = !live_rec;
    if (span_ok) {
        sspan = (uint8_t *)map(range, slot_ga, slot_len);
        vspan = (const uint8_t *)map(range, vert_ga, vert_len);
        if (!sspan || !vspan) span_ok = false;
    }
    /* Vertex bytes are loaded per record, so an overlap with slots stays correct
       as long as both spans are mapped. A live record can change p or a, so the
       span no longer covers the call. */
    const bool fastmap = span_ok && !live_skin;
    const bool need_undo = !fastmap;
    if (need_undo && !ensure_undo((uint32_t)n)) return decline(res, CLOTH_ANCHOR_LIMIT);

    if (fz && n >= 1) fl |= cw::IDC;

    const float kf = cw::f32(kf_bits);
    const float lim0f = cw::f32(lim0);
    const float lim1f = cw::f32(lim1);

#if KC_SIMD
    __m128 c0h = kc::ld4(col);
    __m128 c1h = kc::ld4(col + 0x10);
    __m128 c2h = kc::ld4(col + 0x20);
    __m128 c3h = kc::ld4(col + 0x30);
    __m128 epsh = kc::ld4((const uint8_t *)eps_u);
    bool allow = xyz_in_r(c0h) && xyz_in_r(c1h) && xyz_in_r(c2h) && xyz_in_r(c3h) && xyz_in_r(epsh);
    allow = allow && kc::in_r(kf) && kc::in_r(lim0f) && kc::in_r(lim1f);
    const bool cols_class = all_in_r(c0h) && all_in_r(c1h) && all_in_r(c2h) && all_in_r(c3h) && all_in_r(epsh);
    bool wposs = (call->fpsr & cw::FAST_FLAGS) == cw::FAST_FLAGS;
    if (wposs) {
        const uint32_t ws[5] = {cw::lane3(c0h), cw::lane3(c1h), cw::lane3(c2h), cw::lane3(c3h), cw::lane3(epsh)};
        for (uint32_t w : ws) {
            if (!cw::canon_in(w) || (fz && !cw::fz_safe(w))) wposs = false;
        }
    }
    if (allow && !cols_class && !wposs) allow = false;
    if (scaled && allow && !kc::in_r_bits(sx0)) allow = false;
    /* Aliased calls re-read through the scalar path; the span is only safe when fastmap. */
    if (!fastmap) allow = false;
    const bool hoist = allow;
#else
    const bool allow = false;
    const bool hoist = false;
    const bool wposs = false;
    const bool cols_class = true;
#endif

    uint32_t lo = 0xffffffffu, hi = 0;
    uint32_t undo_n = 0;

#if KC_SIMD
    if (allow) {
        const bool fz_on = fz != 0;
        int32_t i = 0;
        while (i < n) {
            int32_t k;
            if (!scaled && !longrec)
                k = dispatch_span<false, false>(wposs, cols_class, fz_on, c0h, c1h, c2h, c3h, epsh, lim0f, lim1f, kf, sx0,
                                            list, rsize, vspan, sspan, stride0, min_a, min_p, i, n, lo, hi);
            else if (!scaled && longrec)
                k = dispatch_span<false, true>(wposs, cols_class, fz_on, c0h, c1h, c2h, c3h, epsh, lim0f, lim1f, kf, sx0,
                                           list, rsize, vspan, sspan, stride0, min_a, min_p, i, n, lo, hi);
            else if (scaled && !longrec)
                k = dispatch_span<true, false>(wposs, cols_class, fz_on, c0h, c1h, c2h, c3h, epsh, lim0f, lim1f, kf, sx0,
                                           list, rsize, vspan, sspan, stride0, min_a, min_p, i, n, lo, hi);
            else
                k = dispatch_span<true, true>(wposs, cols_class, fz_on, c0h, c1h, c2h, c3h, epsh, lim0f, lim1f, kf, sx0, list,
                                         rsize, vspan, sspan, stride0, min_a, min_p, i, n, lo, hi);
            if (k >= n) break;
            const uint8_t *rec = list + (size_t)k * rsize;
            uint16_t p, a;
            memcpy(&p, rec, 2);
            memcpy(&a, rec + 2, 2);
            const uint8_t *vp = vspan + (uint64_t)stride0 * ((uint32_t)a - min_a);
            uint8_t *sp = sspan + ((size_t)p - min_p) * 16u;
            scalar_record(col, col + 0x10, col + 0x20, col + 0x30, eps_u, lim0, lim1, kf_bits, scaled, longrec, scale_p,
                          vp, rec, sp, fz, fl, lo, hi);
            i = k + 1;
        }
        return finish(res, fl, (uint32_t)n, lo, hi);
    }
#else
    (void)wposs;
    (void)cols_class;
    (void)allow;
    (void)kf;
    (void)lim0f;
    (void)lim1f;
#endif

    for (int32_t i = 0; i < n; i++) {
        const uint8_t *rec = list + (size_t)i * rsize;
        uint16_t p, a;
        memcpy(&p, rec, 2);
        memcpy(&a, rec + 2, 2);

        const uint8_t *c0p = col;
        const uint8_t *c1p = col + 0x10;
        const uint8_t *c2p = col + 0x20;
        const uint8_t *c3p = col + 0x30;
        uint64_t vb;
        uint8_t stride;
        if (hoist) {
            vb = vb0;
            stride = stride0;
        } else {
            memcpy(&vb, vbase_p, 8);
            stride = *stride_p;
        }

        const uint8_t *vp = nullptr;
        uint8_t *sp = nullptr;
        if (fastmap) {
            vp = vspan + (uint64_t)stride * (uint64_t)(a - min_a);
            sp = sspan + (size_t)(p - min_p) * 16u;
        } else {
            const uint64_t vga = vb + (uint64_t)stride * a;
            const uint64_t sga = call->slots + (uint64_t)p * 16u;
            /* Prefer the already mapped span when this index still lies inside it. */
            if (sspan && p >= min_p && p <= max_p && !live_rec) {
                sp = sspan + (size_t)(p - min_p) * 16u;
            } else {
                sp = (uint8_t *)map(range, sga, 16);
            }
            if (vspan && !live_skin && !live_rec && a >= min_a && a <= max_a && vb == vb0 && stride == stride0) {
                vp = vspan + (uint64_t)stride * (uint64_t)(a - min_a);
            } else {
                vp = (const uint8_t *)map(range, vga, 16);
            }
            if (!vp || !sp) {
                undo_all(undo_n);
                return decline(res, CLOTH_ANCHOR_RANGE);
            }
        }

        if (need_undo) save_undo(undo_n, sp);
        scalar_record(c0p, c1p, c2p, c3p, eps_u, lim0, lim1, kf_bits, scaled, longrec, scale_p, vp, rec, sp, fz, fl, lo,
                      hi);
    }

    return finish(res, fl, (uint32_t)n, lo, hi);
}
