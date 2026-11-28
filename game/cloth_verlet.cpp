/* Verlet integration, one cloth loop. Arithmetic and hand-back: SPEC.md.
   Host SSE is used only where SPEC.md 6 (or the comfort range below) proves it
   matches the Arm; every other particle is done lane by lane with cw::arm_*. */
#include "cloth_verlet.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"
#include <xmmintrin.h>
#include <cstring>

#if defined(__FAST_MATH__)
#error "cloth_verlet must not be built with -ffast-math"
#endif
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif

#if defined(__GNUC__)
#define CL_AI __attribute__((always_inline)) inline
#define CL_NOINLINE __attribute__((noinline))
#define CL_EXPECT(x, v) __builtin_expect(!!(x), (v))
#else
#define CL_AI inline
#define CL_NOINLINE
#define CL_EXPECT(x, v) (x)
#endif

/* Comfort, shared G, D, h and the scales: {0} or [2^-12, 2^12).
   Wide, per particle P, Q and A: {0} or [2^-60, 2^60). With the scales and
   the shared values in comfort, every product and every sum (a cancelled
   sum included) is then 0 or at least 2^-107 and far below 2^128: never
   tiny, infinite or NaN, so SSE matches the Arm with flush-to-zero either
   way and the only flag is inexact. SPEC.md 6's plain class is wider still
   on some inputs (a scale, or a value past 2^60 whose results stay inside
   [2^-63, 2^63)); those particles take the plain check, and only a particle
   that fails that goes lane by lane. */
enum : uint32_t {
    CF_LO = 0x39800000u, /* 2^-12 */
    CF_HI = 0x45800000u, /* 2^12 */
    CF_MN = CF_LO - 1u,
    CF_MX = CF_HI - 1u,
    WD_LO = 0x21800000u, /* 2^-60 */
    WD_HI = 0x5D800000u, /* 2^60 */
    WD_MN = WD_LO - 1u,
    WD_MX = WD_HI - 1u,
    PL_LO = 0x20000000u, /* 2^-63 */
    PL_HI = 0x5F000000u, /* 2^63 */
    PL_MN = PL_LO - 1u,
    PL_MX = PL_HI - 1u
};

enum Mode { MODE_COMFORT = 0, MODE_PLAIN = 1, MODE_FASTFZ = 2, MODE_FAST = 3, MODE_SCALAR = 4 };

struct Hand {
    uint32_t *old;
    uint32_t *moved;
    uint32_t *neu;
    uint32_t *push;
    uint32_t *damped;
    uint32_t *force;
    uint32_t *sb;
};

#if KC_SIMD
struct K {
    __m128 G, D, H, def;
    __m128i abs_mask, one, cf_mx, cf_mn, wd_mx, wd_mn, pl_mx, pl_mn, fz_lim, inf_bits, def_bits;
};
#endif

static Hand group_hand(ClothVerletLast *L, int k) {
    Hand h;
    h.old = L->group_old[k];
    h.moved = L->group_moved[k];
    h.neu = nullptr;
    h.push = nullptr;
    h.damped = nullptr;
    h.force = nullptr;
    h.sb = nullptr;
    if (k >= 2) {
        h.neu = L->group_new[k - 2];
        h.push = L->group_push[k - 2];
        h.damped = L->group_damped[k - 2];
    }
    return h;
}

static Hand single_hand(ClothVerletLast *L) {
    Hand h;
    h.old = L->single_old;
    h.moved = L->single_moved;
    h.neu = L->single_new;
    h.push = nullptr;
    h.damped = nullptr;
    h.force = L->single_force;
    h.sb = &L->single_scale_b;
    return h;
}

CL_NOINLINE static uint32_t scalar_one(uint8_t *pos, uint8_t *prev, const uint8_t *force, const uint8_t *scale,
                                       const uint32_t G[4], const uint32_t D[4], uint32_t h, uint32_t fz, Hand *hand) {
    uint32_t P[4], Q[4], A[4], Pp[4], M[4], F[4], E[4];
    uint32_t sa, sb, fl = 0;
    memcpy(P, pos, 16);
    memcpy(Q, prev, 16);
    memcpy(A, force, 16);
    memcpy(&sa, scale, 4);
    memcpy(&sb, scale + 4, 4);
    for (int l = 0; l < 4; l++) {
        const uint32_t g = cw::arm_mul(G[l], sa, fz, fl);
        const uint32_t t1 = cw::arm_add(A[l], g, fz, fl);
        F[l] = cw::arm_mul(t1, sb, fz, fl);
        const uint32_t t3 = cw::arm_mul(F[l], h, fz, fl);
        const uint32_t d = cw::arm_sub(P[l], Q[l], fz, fl);
        E[l] = cw::arm_mul(D[l], d, fz, fl);
        M[l] = cw::arm_add(P[l], E[l], fz, fl);
        Pp[l] = cw::arm_add(M[l], t3, fz, fl);
    }
    memcpy(pos, Pp, 16);
    memcpy(prev, P, 16);
    if (hand) {
        if (hand->old) memcpy(hand->old, P, 16);
        if (hand->moved) memcpy(hand->moved, M, 16);
        if (hand->neu) memcpy(hand->neu, Pp, 16);
        if (hand->push) memcpy(hand->push, F, 16);
        if (hand->damped) memcpy(hand->damped, E, 16);
        if (hand->force) memcpy(hand->force, A, 16);
        if (hand->sb) *hand->sb = sb;
    }
    return fl;
}

static uint32_t run_scalar(const ClothVerletArgs *a, uint32_t fz, const uint32_t G[4], const uint32_t D[4]) {
    uint32_t fl = 0;
    uint8_t *pos = a->pos;
    uint8_t *prev = a->prev;
    const uint8_t *force = a->force;
    const uint8_t *scale = a->scale;
    const uint32_t groups = a->groups;
    const uint32_t singles = a->singles;
    const uint32_t h = a->dt2;
    auto advance = [&]() {
        pos += 16;
        prev += 16;
        force += 16;
        scale += 16;
    };
    if (groups) {
        for (uint32_t gi = 0; gi + 1 < groups; ++gi) {
            for (int k = 0; k < 4; k++) {
                fl |= scalar_one(pos, prev, force, scale, G, D, h, fz, nullptr);
                advance();
            }
        }
        for (int k = 0; k < 4; k++) {
            Hand hand = group_hand(a->last, k);
            fl |= scalar_one(pos, prev, force, scale, G, D, h, fz, &hand);
            advance();
        }
    }
    for (uint32_t s = 0; s < singles; ++s) {
        if (s + 1 == singles) {
            Hand hand = single_hand(a->last);
            fl |= scalar_one(pos, prev, force, scale, G, D, h, fz, &hand);
        } else {
            fl |= scalar_one(pos, prev, force, scale, G, D, h, fz, nullptr);
        }
        advance();
    }
    return fl;
}

#if KC_SIMD

struct VAcc {
    __m128i mx, mn;
    CL_AI void reset() {
        mx = _mm_setzero_si128();
        mn = _mm_set1_epi32(-1);
    }
    CL_AI void add(__m128 v, const K &k) {
        const __m128i a = _mm_and_si128(_mm_castps_si128(v), k.abs_mask);
        mx = _mm_max_epu32(mx, a);
        mn = _mm_min_epu32(mn, _mm_sub_epi32(a, k.one));
    }
    CL_AI bool le(__m128i lim) const {
        return _mm_movemask_epi8(_mm_cmpeq_epi32(_mm_min_epu32(mx, lim), mx)) == 0xffff;
    }
    CL_AI bool ge_mn(__m128i lim) const {
        return _mm_movemask_epi8(_mm_cmpeq_epi32(_mm_max_epu32(mn, lim), mn)) == 0xffff;
    }
};

CL_AI static void add_nan(__m128 v, const K &k, __m128i &bad) {
    const __m128i b = _mm_castps_si128(v);
    const __m128i abs = _mm_and_si128(b, k.abs_mask);
    const __m128i is_nan = _mm_cmpgt_epi32(abs, k.inf_bits);
    const __m128i is_def = _mm_cmpeq_epi32(b, k.def_bits);
    bad = _mm_or_si128(bad, _mm_andnot_si128(is_def, is_nan));
}

CL_AI static bool nan_ok(__m128 v, const K &k) {
    __m128i bad = _mm_setzero_si128();
    add_nan(v, k, bad);
    return _mm_movemask_epi8(bad) == 0;
}

CL_AI static bool comfort_ok(__m128 v, const K &k) {
    VAcc a;
    a.reset();
    a.add(v, k);
    return a.le(k.cf_mx) && a.ge_mn(k.cf_mn);
}

CL_AI static bool fz_ok(__m128 v, const K &k) {
    VAcc a;
    a.reset();
    a.add(v, k);
    return a.ge_mn(k.fz_lim);
}

CL_AI static bool comfort_bits(uint32_t u) {
    const uint32_t a = u & 0x7fffffffu;
    return a == 0u || (a >= CF_LO && a <= CF_MX);
}

CL_AI static __m128 canon(__m128 v, __m128 def) {
    return _mm_blendv_ps(v, def, _mm_cmpunord_ps(v, v));
}

template <bool Full>
CL_AI static __m128 load_sc(const uint8_t *sc) {
    if constexpr (Full) return kc::ld4(sc);
    alignas(8) unsigned char b[8];
    memcpy(b, sc, 8);
    return _mm_castpd_ps(_mm_load_sd(reinterpret_cast<const double *>(b)));
}

template <bool Save>
CL_AI static void save_sse(Hand *hand, const uint8_t *scale, __m128 P, __m128 M, __m128 Pp, __m128 F, __m128 E, __m128 A) {
    if constexpr (!Save) return;
    if (hand->old) kc::st4u(hand->old, P);
    if (hand->moved) kc::st4u(hand->moved, M);
    if (hand->neu) kc::st4u(hand->neu, Pp);
    if (hand->push) kc::st4u(hand->push, F);
    if (hand->damped) kc::st4u(hand->damped, E);
    if (hand->force) kc::st4u(hand->force, A);
    if (hand->sb) {
        uint32_t sb;
        memcpy(&sb, scale + 4, 4);
        *hand->sb = sb;
    }
}

template <int Mode, bool Full, bool Save>
CL_AI static uint32_t step_one(uint8_t *pos, uint8_t *prev, const uint8_t *force, const uint8_t *scale, const K &k,
                               const uint32_t *Gb, const uint32_t *Db, uint32_t h, uint32_t fz, Hand *hand) {
    const __m128 P = kc::ld4(pos);
    const __m128 Q = kc::ld4(prev);
    const __m128 A = kc::ld4(force);
    const __m128 sc = load_sc<Full>(scale);
    const __m128 sa = _mm_shuffle_ps(sc, sc, _MM_SHUFFLE(0, 0, 0, 0));
    const __m128 sbv = _mm_shuffle_ps(sc, sc, _MM_SHUFFLE(1, 1, 1, 1));

    const __m128 g = _mm_mul_ps(k.G, sa);
    const __m128 t1 = _mm_add_ps(A, g);
    const __m128 F = _mm_mul_ps(t1, sbv);
    const __m128 t3 = _mm_mul_ps(F, k.H);
    const __m128 d = _mm_sub_ps(P, Q);
    const __m128 E = _mm_mul_ps(k.D, d);
    const __m128 M = _mm_add_ps(P, E);
    const __m128 Pp = _mm_add_ps(M, t3);

    bool ok;
    if constexpr (Mode == MODE_COMFORT) {
        /* P, Q and A use the wide band; the scales stay in comfort.
           A miss is usually still SPEC.md 6: check the plain class before
           the lane-by-lane path. */
        VAcc wide, scl;
        wide.reset();
        scl.reset();
        wide.add(P, k);
        wide.add(Q, k);
        wide.add(A, k);
        scl.add(sa, k);
        scl.add(sbv, k);
        ok = wide.le(k.wd_mx) && wide.ge_mn(k.wd_mn) && scl.le(k.cf_mx) && scl.ge_mn(k.cf_mn);
        if (CL_EXPECT(!ok, 0)) {
            VAcc pl;
            pl.reset();
            pl.add(P, k);
            pl.add(Q, k);
            pl.add(A, k);
            pl.add(sa, k);
            pl.add(sbv, k);
            pl.add(g, k);
            pl.add(t1, k);
            pl.add(F, k);
            pl.add(t3, k);
            pl.add(d, k);
            pl.add(E, k);
            pl.add(M, k);
            pl.add(Pp, k);
            ok = pl.le(k.pl_mx) && pl.ge_mn(k.pl_mn);
        }
    } else if constexpr (Mode == MODE_PLAIN) {
        kc::Checks acc;
        acc.reset();
        acc.add(P);
        acc.add(Q);
        acc.add(A);
        acc.add(sa);
        acc.add(sbv);
        acc.add(g);
        acc.add(t1);
        acc.add(F);
        acc.add(t3);
        acc.add(d);
        acc.add(E);
        acc.add(M);
        acc.add(Pp);
        ok = acc.ok();
    } else if constexpr (Mode == MODE_FASTFZ) {
        VAcc acc;
        acc.reset();
        acc.add(P, k);
        acc.add(Q, k);
        acc.add(A, k);
        acc.add(sa, k);
        acc.add(sbv, k);
        acc.add(g, k);
        acc.add(t1, k);
        acc.add(F, k);
        acc.add(t3, k);
        acc.add(d, k);
        acc.add(E, k);
        acc.add(M, k);
        acc.add(Pp, k);
        __m128i bad = _mm_setzero_si128();
        add_nan(P, k, bad);
        add_nan(Q, k, bad);
        add_nan(A, k, bad);
        add_nan(sa, k, bad);
        add_nan(sbv, k, bad);
        ok = acc.ge_mn(k.fz_lim) && _mm_movemask_epi8(bad) == 0;
    } else {
        __m128i bad = _mm_setzero_si128();
        add_nan(P, k, bad);
        add_nan(Q, k, bad);
        add_nan(A, k, bad);
        add_nan(sa, k, bad);
        add_nan(sbv, k, bad);
        ok = _mm_movemask_epi8(bad) == 0;
    }

    if (CL_EXPECT(!ok, 0)) return scalar_one(pos, prev, force, scale, Gb, Db, h, fz, hand);

    __m128 PpS = Pp, MS = M, FS = F, ES = E;
    if constexpr (Mode == MODE_FASTFZ || Mode == MODE_FAST) {
        PpS = canon(Pp, k.def);
        if constexpr (Save) {
            MS = canon(M, k.def);
            FS = canon(F, k.def);
            ES = canon(E, k.def);
        }
    }
    kc::st4(pos, PpS);
    kc::st4(prev, P);
    save_sse<Save>(hand, scale, P, MS, PpS, FS, ES, A);
    return 0;
}

template <int Mode>
static uint32_t run_mode(const ClothVerletArgs *a, const K &k, const uint32_t *Gb, const uint32_t *Db, uint32_t fz) {
    uint32_t fl = 0;
    uint8_t *pos = a->pos;
    uint8_t *prev = a->prev;
    const uint8_t *force = a->force;
    const uint8_t *scale = a->scale;
    const uint32_t groups = a->groups;
    const uint32_t singles = a->singles;
    const uint32_t h = a->dt2;
    auto advance4 = [&]() {
        pos += 64;
        prev += 64;
        force += 64;
        scale += 64;
    };
    auto advance1 = [&]() {
        pos += 16;
        prev += 16;
        force += 16;
        scale += 16;
    };
    if (groups) {
        for (uint32_t gi = 0; gi + 1 < groups; ++gi) {
            fl |= step_one<Mode, true, false>(pos, prev, force, scale, k, Gb, Db, h, fz, nullptr);
            fl |= step_one<Mode, true, false>(pos + 16, prev + 16, force + 16, scale + 16, k, Gb, Db, h, fz, nullptr);
            fl |= step_one<Mode, true, false>(pos + 32, prev + 32, force + 32, scale + 32, k, Gb, Db, h, fz, nullptr);
            fl |= step_one<Mode, true, false>(pos + 48, prev + 48, force + 48, scale + 48, k, Gb, Db, h, fz, nullptr);
            advance4();
        }
        for (int knd = 0; knd < 3; knd++) {
            Hand hand = group_hand(a->last, knd);
            fl |= step_one<Mode, true, true>(pos, prev, force, scale, k, Gb, Db, h, fz, &hand);
            advance1();
        }
        {
            Hand hand = group_hand(a->last, 3);
            if (singles == 0)
                fl |= step_one<Mode, false, true>(pos, prev, force, scale, k, Gb, Db, h, fz, &hand);
            else
                fl |= step_one<Mode, true, true>(pos, prev, force, scale, k, Gb, Db, h, fz, &hand);
            advance1();
        }
    }
    for (uint32_t s = 0; s < singles; ++s) {
        if (s + 1 == singles) {
            Hand hand = single_hand(a->last);
            fl |= step_one<Mode, false, true>(pos, prev, force, scale, k, Gb, Db, h, fz, &hand);
        } else {
            fl |= step_one<Mode, true, false>(pos, prev, force, scale, k, Gb, Db, h, fz, nullptr);
        }
        advance1();
    }
    return fl;
}

static int select_mode(const K &k, __m128 G, __m128 D, uint32_t h, uint32_t fz, bool flags_full) {
    /* Shared G, D and h can sit in the comfort range (G's lane 3 = 0 or 1)
       while each particle's lane 3 is the game's mix. That mix still takes
       the flags-already-set way (SPEC.md 6); choosing comfort for the whole
       call would send nearly every particle down the lane-by-lane path.
       Comfort and plain stay for calls whose flags are not already set. */
    if (flags_full && nan_ok(G, k) && nan_ok(D, k) && cw::canon_in(h)) {
        if (!fz) return MODE_FAST;
        if (fz_ok(G, k) && fz_ok(D, k) && cw::fz_safe(h)) return MODE_FASTFZ;
    }
    if (comfort_ok(G, k) && comfort_ok(D, k) && comfort_bits(h)) return MODE_COMFORT;
    if ([&]() {
            kc::Checks c;
            c.reset();
            c.add(G);
            c.add(D);
            return c.ok() && kc::in_r_bits(h);
        }())
        return MODE_PLAIN;
    return MODE_SCALAR;
}

#endif /* KC_SIMD */

uint32_t cloth_verlet_81c66a60(const ClothVerletArgs *a) {
    const unsigned mxcsr = _mm_getcsr();
    const uint32_t fz = a->fz ? 1u : 0u;
    uint32_t Gb[4], Db[4];
    memcpy(Gb, a->gravity, 16);
    memcpy(Db, a->damping, 16);
    uint32_t fl;
#if KC_SIMD
    K k;
    k.G = kc::ld4(a->gravity);
    k.D = _mm_loadu_ps(reinterpret_cast<const float *>(a->damping));
    k.H = _mm_set1_ps(cw::f32(a->dt2));
    k.def = _mm_castsi128_ps(_mm_set1_epi32((int)cw::DEFAULT_NAN));
    k.abs_mask = _mm_set1_epi32(0x7fffffff);
    k.one = _mm_set1_epi32(1);
    k.cf_mx = _mm_set1_epi32((int)CF_MX);
    k.cf_mn = _mm_set1_epi32((int)CF_MN);
    k.wd_mx = _mm_set1_epi32((int)WD_MX);
    k.wd_mn = _mm_set1_epi32((int)WD_MN);
    k.pl_mx = _mm_set1_epi32((int)PL_MX);
    k.pl_mn = _mm_set1_epi32((int)PL_MN);
    k.fz_lim = _mm_set1_epi32(0x00800000);
    k.inf_bits = _mm_set1_epi32(0x7f800000);
    k.def_bits = _mm_set1_epi32((int)cw::DEFAULT_NAN);
    const bool flags_full = (a->fpsr_in & cw::FAST_FLAGS) == cw::FAST_FLAGS;
    const int mode = select_mode(k, k.G, k.D, a->dt2, fz, flags_full);
    if (mode == MODE_COMFORT) fl = run_mode<MODE_COMFORT>(a, k, Gb, Db, fz);
    else if (mode == MODE_PLAIN) fl = run_mode<MODE_PLAIN>(a, k, Gb, Db, fz);
    else if (mode == MODE_FASTFZ) fl = run_mode<MODE_FASTFZ>(a, k, Gb, Db, fz);
    else if (mode == MODE_FAST) fl = run_mode<MODE_FAST>(a, k, Gb, Db, fz);
    else fl = run_scalar(a, fz, Gb, Db);
#else
    fl = run_scalar(a, fz, Gb, Db);
#endif
    _mm_setcsr(mxcsr);
    return fl;
}
