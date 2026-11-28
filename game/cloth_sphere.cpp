/* Sphere contact pass: records of 16 particles pushed out of a moving sphere.
   Groups of four, one SSE vector per coordinate. Pair store as SPEC.md 4.
   Plain SSE2 when the build has no SSE4.1; FMA for the reciprocal step when present. */
#include "cloth_sphere.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"

#include <emmintrin.h>
#include <string.h>
#include <xmmintrin.h>
#if defined(__SSE4_1__)
#include <smmintrin.h>
#endif
#if defined(__FMA__)
#include <immintrin.h>
#endif

namespace {

constexpr uint32_t NAN_BITS = 0x7fc00000u;

inline uint32_t canon_u(uint32_t u) {
    return (u & 0x7fffffffu) > 0x7f800000u ? NAN_BITS : u;
}

inline bool bad_nan_u(uint32_t u) {
    return (u & 0x7fffffffu) > 0x7f800000u && u != NAN_BITS;
}

inline bool tiny_u(uint32_t u) {
    return (u & 0x7fffffffu) - 1u < 0x00800000u;
}

inline int problem_u(uint32_t u, int fz) {
    return bad_nan_u(u) || (fz && tiny_u(u));
}

inline __m128 splat_bits(uint32_t u) {
    return _mm_castsi128_ps(_mm_set1_epi32((int)u));
}

inline __m128 load4(const uint8_t *p) {
    return _mm_loadu_ps((const float *)(const void *)p);
}

inline __m128i bias_bits() { return _mm_set1_epi32((int)0x80000000u); }

/* Unsigned mag > limit, SSE2. */
inline __m128i mag_gt(__m128i mag, uint32_t limit) {
    const __m128i bias = bias_bits();
    return _mm_cmpgt_epi32(_mm_xor_si128(mag, bias), _mm_xor_si128(_mm_set1_epi32((int)limit), bias));
}

struct Bad {
    __m128i nan;
    __m128i ftz;
    int fz;
    int step;
    void reset(int fz_on) {
        nan = _mm_setzero_si128();
        ftz = _mm_setzero_si128();
        fz = fz_on;
        step = 0;
    }
    void note_nan(__m128 v) {
        const __m128i u = _mm_castps_si128(v);
        const __m128i mag = _mm_and_si128(u, _mm_set1_epi32(0x7fffffff));
        const __m128i is_nan = mag_gt(mag, 0x7f800000u);
        const __m128i not_def = _mm_xor_si128(_mm_cmpeq_epi32(u, _mm_set1_epi32((int)NAN_BITS)), _mm_set1_epi32(-1));
        nan = _mm_or_si128(nan, _mm_and_si128(is_nan, not_def));
    }
    void note_ftz(__m128 v) {
        if (!fz) return;
        const __m128i mag = _mm_and_si128(_mm_castps_si128(v), _mm_set1_epi32(0x7fffffff));
        const __m128i eq0 = _mm_cmpeq_epi32(mag, _mm_setzero_si128());
        const __m128i above = mag_gt(mag, 0x00800000u);
        const __m128i safe = _mm_or_si128(eq0, above);
        ftz = _mm_or_si128(ftz, _mm_xor_si128(safe, _mm_set1_epi32(-1)));
    }
    void note_in(__m128 v) {
        note_nan(v);
        note_ftz(v);
    }
    /* Keep contact lanes; non-contact lanes become zero (not a bad NaN, not tiny). */
    void note_in_masked(__m128 v, __m128 m) { note_in(_mm_and_ps(v, m)); }
    void note_ftz_masked(__m128 v, __m128 m) { note_ftz(_mm_and_ps(v, m)); }
    void force() { nan = _mm_set1_epi32(-1); }
    bool hit() const {
        if (_mm_movemask_epi8(nan)) return true;
        if (fz && _mm_movemask_epi8(ftz)) return true;
        if (step) return true;
        return false;
    }
};

inline float step_one(float a, float b, int &step_bad) {
    const uint32_t ua = kc::fb(a), ub = kc::fb(b);
    const uint32_t ma = ua & 0x7fffffffu, mb = ub & 0x7fffffffu;
    if (ma > 0x7f800000u || mb > 0x7f800000u) return kc::bf(NAN_BITS);
    if ((ma == 0x7f800000u && mb == 0u) || (ma == 0u && mb == 0x7f800000u)) return 1.5f;
#if defined(__FMA__)
    (void)step_bad;
    return __builtin_fmaf(-a, b, 3.0f) * 0.5f;
#else
    const double prod = (double)a * (double)b;
    if (!(prod >= 0.125 && prod <= 8.0)) step_bad = 1;
    return (float)(3.0 - prod) * 0.5f;
#endif
}

/* A host result Arm would flush, or the ambiguous min-normal boundary (SPEC.md 6). */
inline int ftz_bad(float x) { return tiny_u(kc::fb(x)); }

/* InvRoot(L), SPEC.md 3.4. Two fused steps. Specials never call Estimate.
   With flush-to-zero, e0*e0 (and y1*y1) for a huge L is subnormal: the Arm step is then
   exactly 1.5, while the host mul keeps the tiny product. Decline the record (step_bad)
   rather than store that wrong scale. Same for any other tiny step result. */
inline float invroot_one(float L, int &step_bad, int fz) {
    const uint32_t u = kc::fb(L);
    const uint32_t mag = u & 0x7fffffffu;
    if (mag > 0x7f800000u) return kc::bf(NAN_BITS);
    if ((u & 0x80000000u) || mag <= 0x007fffffu || mag == 0x7f800000u) return 0.0f;
    const float e0 = kc::bf(kc::frsqrte_bits(u));
    const float ee = e0 * e0;
    const float s1 = step_one(L, ee, step_bad);
    const float y1 = e0 * s1;
    const float yy = y1 * y1;
    const float s2 = step_one(L, yy, step_bad);
    const float y2 = y1 * s2;
    if (fz) {
        step_bad |= ftz_bad(e0);
        step_bad |= ftz_bad(ee);
        step_bad |= ftz_bad(s1);
        step_bad |= ftz_bad(y1);
        step_bad |= ftz_bad(yy);
        step_bad |= ftz_bad(s2);
        step_bad |= ftz_bad(y2);
    }
    return y2;
}

inline __m128 invroot4(__m128 L, int &step_bad, int fz) {
    alignas(16) float in[4], out[4];
    _mm_store_ps(in, L);
    out[0] = invroot_one(in[0], step_bad, fz);
    out[1] = invroot_one(in[1], step_bad, fz);
    out[2] = invroot_one(in[2], step_bad, fz);
    out[3] = invroot_one(in[3], step_bad, fz);
    return _mm_load_ps(out);
}

struct Group {
    uint16_t idx[4];
    int mask;
    float px[4], py[4], pz[4], pw[4];
    float gx[4], gy[4], gz[4], gw[4];
    float qx[4], qy[4], qz[4], qw[4];
};

inline void store4(uint8_t *base, uint32_t index, uint32_t x, uint32_t y, uint32_t z, uint32_t w) {
    uint32_t u[4] = {canon_u(x), canon_u(y), canon_u(z), canon_u(w)};
    memcpy(base + 16u * index, u, 16);
}

inline void store_lanes(uint8_t *base, uint32_t index, const float *x, const float *y, const float *z, const float *w, int lane) {
    store4(base, index, kc::fb(x[lane]), kc::fb(y[lane]), kc::fb(z[lane]), kc::fb(w[lane]));
}

void store_group(const Group &g, uint8_t *positions, uint8_t *previous) {
    int ids[4];
    int n = 0;
    for (int k = 0; k < 4; k++) {
        if (g.mask & (1 << k)) ids[n++] = k;
    }
    for (int t = 0; t < n; t++) {
        store_lanes(positions, g.idx[ids[t]], g.px, g.py, g.pz, g.pw, ids[t]);
    }
    if (n == 1) {
        store_lanes(previous, g.idx[ids[0]], g.qx, g.qy, g.qz, g.qw, ids[0]);
    } else if (n >= 2) {
        const int a = ids[0], b = ids[1];
        /* G_b + Q'_a, G_b first. */
        const float sx = g.gx[b] + g.qx[a];
        const float sy = g.gy[b] + g.qy[a];
        const float sz = g.gz[b] + g.qz[a];
        const float sw = g.gw[b] + g.qw[a];
        store4(previous, g.idx[a], kc::fb(sx), kc::fb(sy), kc::fb(sz), kc::fb(sw));
        for (int t = 2; t < n; t++) {
            store_lanes(previous, g.idx[ids[t]], g.qx, g.qy, g.qz, g.qw, ids[t]);
        }
    }
}

void compute_group(const uint8_t *positions, const uint8_t *previous, const uint8_t *ip, const uint8_t *rp,
                    const uint8_t *fp, int g, __m128 cx, __m128 cy, __m128 cz, __m128 rho, __m128 eps, __m128 bx,
                    __m128 by, __m128 bz, __m128 vx, __m128 vy, __m128 vz, __m128 vw, __m128 wx, __m128 wy, __m128 wz,
                    __m128 dt, Bad &bad, Group &out) {
    const int base = g * 4;
    uint32_t i0 = kc::ld16(ip + (base + 0) * 2);
    uint32_t i1 = kc::ld16(ip + (base + 1) * 2);
    uint32_t i2 = kc::ld16(ip + (base + 2) * 2);
    uint32_t i3 = kc::ld16(ip + (base + 3) * 2);
    out.idx[0] = (uint16_t)i0;
    out.idx[1] = (uint16_t)i1;
    out.idx[2] = (uint16_t)i2;
    out.idx[3] = (uint16_t)i3;

    const __m128 p0 = load4(positions + 16u * i0);
    const __m128 p1 = load4(positions + 16u * i1);
    const __m128 p2 = load4(positions + 16u * i2);
    const __m128 p3 = load4(positions + 16u * i3);
    const __m128 q0 = load4(previous + 16u * i0);
    const __m128 q1 = load4(previous + 16u * i1);
    const __m128 q2 = load4(previous + 16u * i2);
    const __m128 q3 = load4(previous + 16u * i3);

    const __m128 t0 = _mm_unpacklo_ps(p0, p1);
    const __m128 t1 = _mm_unpackhi_ps(p0, p1);
    const __m128 t2 = _mm_unpacklo_ps(p2, p3);
    const __m128 t3 = _mm_unpackhi_ps(p2, p3);
    const __m128 px = _mm_movelh_ps(t0, t2);
    const __m128 py = _mm_movehl_ps(t2, t0);
    const __m128 pz = _mm_movelh_ps(t1, t3);
    const __m128 pw = _mm_movehl_ps(t3, t1);

    const __m128 u0 = _mm_unpacklo_ps(q0, q1);
    const __m128 u1 = _mm_unpackhi_ps(q0, q1);
    const __m128 u2 = _mm_unpacklo_ps(q2, q3);
    const __m128 u3 = _mm_unpackhi_ps(q2, q3);
    const __m128 qx = _mm_movelh_ps(u0, u2);
    const __m128 qy = _mm_movehl_ps(u2, u0);
    const __m128 qz = _mm_movelh_ps(u1, u3);
    const __m128 qw = _mm_movehl_ps(u3, u1);

    const __m128 R = load4(rp + 16 * g);
    const __m128 F = load4(fp + 16 * g);

    bad.note_in(px);
    bad.note_in(py);
    bad.note_in(pz);
    bad.note_in(R);

    /* 3.1 contact */
    const __m128 dx = _mm_sub_ps(px, cx);
    const __m128 dy = _mm_sub_ps(py, cy);
    const __m128 dz = _mm_sub_ps(pz, cz);
    const __m128 dx2 = _mm_mul_ps(dx, dx);
    const __m128 dy2 = _mm_mul_ps(dy, dy);
    const __m128 sxy = _mm_add_ps(dx2, dy2);
    const __m128 dz2 = _mm_mul_ps(dz, dz);
    const __m128 sq = _mm_add_ps(dz2, sxy);
    const __m128 L = _mm_add_ps(eps, sq);
    bad.note_ftz(dx);
    bad.note_ftz(dy);
    bad.note_ftz(dz);
    bad.note_ftz(dx2);
    bad.note_ftz(dy2);
    bad.note_ftz(sxy);
    bad.note_ftz(dz2);
    bad.note_ftz(sq);
    bad.note_ftz(L);

    const __m128 r = invroot4(L, bad.step, bad.fz);
    const __m128 len = _mm_mul_ps(L, r);
    const __m128 len_rho = _mm_sub_ps(len, rho);
    const __m128 pen = _mm_sub_ps(len_rho, R);
    bad.note_ftz(r);
    bad.note_ftz(len);
    bad.note_ftz(len_rho);
    bad.note_ftz(pen);

    const __m128 cmask = _mm_cmplt_ps(pen, _mm_setzero_ps());
    out.mask = _mm_movemask_ps(cmask);
    if (out.mask == 0) {
        return;
    }

    /* 3.2 push. Masked notes: only particles in contact use these. */
    bad.note_in_masked(pw, cmask);
    const __m128 dw = _mm_sub_ps(pw, rho);
    const __m128 nx = _mm_mul_ps(dx, r);
    const __m128 ny = _mm_mul_ps(dy, r);
    const __m128 nz = _mm_mul_ps(dz, r);
    const __m128 nw = _mm_mul_ps(dw, r);
    const __m128 nxr = _mm_mul_ps(nx, rho);
    const __m128 nyr = _mm_mul_ps(ny, rho);
    const __m128 nzr = _mm_mul_ps(nz, rho);
    const __m128 Sx = _mm_add_ps(cx, nxr);
    const __m128 Sy = _mm_add_ps(cy, nyr);
    const __m128 Sz = _mm_add_ps(cz, nzr);
    const __m128 ex = _mm_sub_ps(px, Sx);
    const __m128 ey = _mm_sub_ps(py, Sy);
    const __m128 ez = _mm_sub_ps(pz, Sz);
    const __m128 nxex = _mm_mul_ps(nx, ex);
    const __m128 nyey = _mm_mul_ps(ny, ey);
    const __m128 nzez = _mm_mul_ps(nz, ez);
    const __m128 sum_xy = _mm_add_ps(nxex, nyey);
    const __m128 dot = _mm_add_ps(nzez, sum_xy);
    const __m128 depth = _mm_sub_ps(R, dot);
    const __m128 nxd = _mm_mul_ps(nx, depth);
    const __m128 nyd = _mm_mul_ps(ny, depth);
    const __m128 nzd = _mm_mul_ps(nz, depth);
    const __m128 nwd = _mm_mul_ps(nw, depth);
    const __m128 Ppx = _mm_add_ps(px, nxd);
    const __m128 Ppy = _mm_add_ps(py, nyd);
    const __m128 Ppz = _mm_add_ps(pz, nzd);
    const __m128 Ppw = _mm_add_ps(pw, nwd);

    bad.note_ftz_masked(dw, cmask);
    bad.note_ftz_masked(nx, cmask);
    bad.note_ftz_masked(ny, cmask);
    bad.note_ftz_masked(nz, cmask);
    bad.note_ftz_masked(nw, cmask);
    bad.note_ftz_masked(nxr, cmask);
    bad.note_ftz_masked(nyr, cmask);
    bad.note_ftz_masked(nzr, cmask);
    bad.note_ftz_masked(Sx, cmask);
    bad.note_ftz_masked(Sy, cmask);
    bad.note_ftz_masked(Sz, cmask);
    bad.note_ftz_masked(ex, cmask);
    bad.note_ftz_masked(ey, cmask);
    bad.note_ftz_masked(ez, cmask);
    bad.note_ftz_masked(nxex, cmask);
    bad.note_ftz_masked(nyey, cmask);
    bad.note_ftz_masked(nzez, cmask);
    bad.note_ftz_masked(sum_xy, cmask);
    bad.note_ftz_masked(dot, cmask);
    bad.note_ftz_masked(depth, cmask);
    bad.note_ftz_masked(nxd, cmask);
    bad.note_ftz_masked(nyd, cmask);
    bad.note_ftz_masked(nzd, cmask);
    bad.note_ftz_masked(nwd, cmask);
    bad.note_ftz_masked(Ppx, cmask);
    bad.note_ftz_masked(Ppy, cmask);
    bad.note_ftz_masked(Ppz, cmask);
    bad.note_ftz_masked(Ppw, cmask);

    /* 3.3 friction */
    bad.note_in_masked(qx, cmask);
    bad.note_in_masked(qy, cmask);
    bad.note_in_masked(qz, cmask);
    bad.note_in_masked(qw, cmask);
    bad.note_in_masked(F, cmask);

    const __m128 ax = _mm_sub_ps(Sx, bx);
    const __m128 ay = _mm_sub_ps(Sy, by);
    const __m128 az = _mm_sub_ps(Sz, bz);
    const __m128 wyaz = _mm_mul_ps(wy, az);
    const __m128 aywz = _mm_mul_ps(ay, wz);
    const __m128 wzax = _mm_mul_ps(wz, ax);
    const __m128 azwx = _mm_mul_ps(az, wx);
    const __m128 wxay = _mm_mul_ps(wx, ay);
    const __m128 axwy = _mm_mul_ps(ax, wy);
    const __m128 cxv = _mm_sub_ps(wyaz, aywz);
    const __m128 cyv = _mm_sub_ps(wzax, azwx);
    const __m128 czv = _mm_sub_ps(wxay, axwy);
    const __m128 Vx = _mm_add_ps(vx, cxv);
    const __m128 Vy = _mm_add_ps(vy, cyv);
    const __m128 Vz = _mm_add_ps(vz, czv);
    const __m128 Vw = _mm_add_ps(vw, _mm_setzero_ps());
    const __m128 Vxdt = _mm_mul_ps(Vx, dt);
    const __m128 Vydt = _mm_mul_ps(Vy, dt);
    const __m128 Vzdt = _mm_mul_ps(Vz, dt);
    const __m128 Vwdt = _mm_mul_ps(Vw, dt);
    const __m128 dpx = _mm_sub_ps(Ppx, qx);
    const __m128 dpy = _mm_sub_ps(Ppy, qy);
    const __m128 dpz = _mm_sub_ps(Ppz, qz);
    const __m128 dpw = _mm_sub_ps(Ppw, qw);
    const __m128 relx = _mm_sub_ps(dpx, Vxdt);
    const __m128 rely = _mm_sub_ps(dpy, Vydt);
    const __m128 relz = _mm_sub_ps(dpz, Vzdt);
    const __m128 relw = _mm_sub_ps(dpw, Vwdt);
    const __m128 nxrx = _mm_mul_ps(nx, relx);
    const __m128 nyry = _mm_mul_ps(ny, rely);
    const __m128 nzrz = _mm_mul_ps(nz, relz);
    const __m128 dn_xy = _mm_add_ps(nxrx, nyry);
    const __m128 dn = _mm_add_ps(dn_xy, nzrz);
    const __m128 nxdn = _mm_mul_ps(nx, dn);
    const __m128 nydn = _mm_mul_ps(ny, dn);
    const __m128 nzdn = _mm_mul_ps(nz, dn);
    const __m128 nwdn = _mm_mul_ps(nw, dn);
    const __m128 Tx = _mm_sub_ps(relx, nxdn);
    const __m128 Ty = _mm_sub_ps(rely, nydn);
    const __m128 Tz = _mm_sub_ps(relz, nzdn);
    const __m128 Tw = _mm_sub_ps(relw, nwdn);
    const __m128 Gx = _mm_mul_ps(Tx, F);
    const __m128 Gy = _mm_mul_ps(Ty, F);
    const __m128 Gz = _mm_mul_ps(Tz, F);
    const __m128 Gw = _mm_mul_ps(Tw, F);
    const __m128 Qpx = _mm_add_ps(qx, Gx);
    const __m128 Qpy = _mm_add_ps(qy, Gy);
    const __m128 Qpz = _mm_add_ps(qz, Gz);
    const __m128 Qpw = _mm_add_ps(qw, Gw);

    bad.note_ftz_masked(ax, cmask);
    bad.note_ftz_masked(ay, cmask);
    bad.note_ftz_masked(az, cmask);
    bad.note_ftz_masked(wyaz, cmask);
    bad.note_ftz_masked(aywz, cmask);
    bad.note_ftz_masked(wzax, cmask);
    bad.note_ftz_masked(azwx, cmask);
    bad.note_ftz_masked(wxay, cmask);
    bad.note_ftz_masked(axwy, cmask);
    bad.note_ftz_masked(cxv, cmask);
    bad.note_ftz_masked(cyv, cmask);
    bad.note_ftz_masked(czv, cmask);
    bad.note_ftz_masked(Vx, cmask);
    bad.note_ftz_masked(Vy, cmask);
    bad.note_ftz_masked(Vz, cmask);
    bad.note_ftz_masked(Vw, cmask);
    bad.note_ftz_masked(Vxdt, cmask);
    bad.note_ftz_masked(Vydt, cmask);
    bad.note_ftz_masked(Vzdt, cmask);
    bad.note_ftz_masked(Vwdt, cmask);
    bad.note_ftz_masked(dpx, cmask);
    bad.note_ftz_masked(dpy, cmask);
    bad.note_ftz_masked(dpz, cmask);
    bad.note_ftz_masked(dpw, cmask);
    bad.note_ftz_masked(relx, cmask);
    bad.note_ftz_masked(rely, cmask);
    bad.note_ftz_masked(relz, cmask);
    bad.note_ftz_masked(relw, cmask);
    bad.note_ftz_masked(nxrx, cmask);
    bad.note_ftz_masked(nyry, cmask);
    bad.note_ftz_masked(nzrz, cmask);
    bad.note_ftz_masked(dn_xy, cmask);
    bad.note_ftz_masked(dn, cmask);
    bad.note_ftz_masked(nxdn, cmask);
    bad.note_ftz_masked(nydn, cmask);
    bad.note_ftz_masked(nzdn, cmask);
    bad.note_ftz_masked(nwdn, cmask);
    bad.note_ftz_masked(Tx, cmask);
    bad.note_ftz_masked(Ty, cmask);
    bad.note_ftz_masked(Tz, cmask);
    bad.note_ftz_masked(Tw, cmask);
    bad.note_ftz_masked(Gx, cmask);
    bad.note_ftz_masked(Gy, cmask);
    bad.note_ftz_masked(Gz, cmask);
    bad.note_ftz_masked(Gw, cmask);
    bad.note_ftz_masked(Qpx, cmask);
    bad.note_ftz_masked(Qpy, cmask);
    bad.note_ftz_masked(Qpz, cmask);
    bad.note_ftz_masked(Qpw, cmask);

    _mm_storeu_ps(out.px, Ppx);
    _mm_storeu_ps(out.py, Ppy);
    _mm_storeu_ps(out.pz, Ppz);
    _mm_storeu_ps(out.pw, Ppw);
    _mm_storeu_ps(out.gx, Gx);
    _mm_storeu_ps(out.gy, Gy);
    _mm_storeu_ps(out.gz, Gz);
    _mm_storeu_ps(out.gw, Gw);
    _mm_storeu_ps(out.qx, Qpx);
    _mm_storeu_ps(out.qy, Qpy);
    _mm_storeu_ps(out.qz, Qpz);
    _mm_storeu_ps(out.qw, Qpw);

    /* Pair sum is a stored value when two or more in the group contact. */
    if (bad.fz) {
        int ids[4];
        int n = 0;
        for (int k = 0; k < 4; k++) {
            if (out.mask & (1 << k)) ids[n++] = k;
        }
        if (n >= 2) {
            const int a = ids[0], b = ids[1];
            const float ps[4] = {out.gx[b] + out.qx[a], out.gy[b] + out.qy[a], out.gz[b] + out.qz[a],
                                 out.gw[b] + out.qw[a]};
            for (int c = 0; c < 4; c++) {
                if (tiny_u(kc::fb(ps[c]))) bad.force();
            }
        }
    }
}

bool repeated16(const uint16_t *id) {
    for (int i = 0; i < 16; i++) {
        for (int j = i + 1; j < 16; j++) {
            if (id[i] == id[j]) return true;
        }
    }
    return false;
}

}  // namespace

void cloth_sphere_records(const ClothSphereArgs *a, ClothSphereResult *r) {
    if ((a->fpcr & 0x06C8FF07u) != 0u || (a->fpsr & 0x9fu) != 0x9fu) {
        r->done = 0;
        r->why = CLOTH_SPHERE_FLOAT_MODE;
        return;
    }
    const int fz = (int)((a->fpcr >> 24) & 1u);

    const __m128 cx = splat_bits(a->sphere[0]);
    const __m128 cy = splat_bits(a->sphere[1]);
    const __m128 cz = splat_bits(a->sphere[2]);
    const __m128 rho = splat_bits(a->sphere[3]);
    alignas(16) uint32_t eps_u[4];
    memcpy(eps_u, a->eps, 16);
    const __m128 eps = _mm_castsi128_ps(_mm_load_si128((const __m128i *)eps_u));
    const __m128 bx = splat_bits(a->body[0]);
    const __m128 by = splat_bits(a->body[1]);
    const __m128 bz = splat_bits(a->body[2]);
    const __m128 vx = splat_bits(a->velocity[0]);
    const __m128 vy = splat_bits(a->velocity[1]);
    const __m128 vz = splat_bits(a->velocity[2]);
    const __m128 vw = splat_bits(a->velocity[3]);
    const __m128 wx = splat_bits(a->spin[0]);
    const __m128 wy = splat_bits(a->spin[1]);
    const __m128 wz = splat_bits(a->spin[2]);
    const __m128 dt = splat_bits(a->dt);

    int sphere_bad = 0;
    for (int i = 0; i < 4; i++) sphere_bad |= problem_u(a->sphere[i], fz);
    for (int i = 0; i < 4; i++) sphere_bad |= problem_u(a->eps[i], fz);

    int fr_bad = problem_u(a->dt, fz);
    for (int i = 0; i < 3; i++) fr_bad |= problem_u(a->body[i], fz);
    for (int i = 0; i < 4; i++) fr_bad |= problem_u(a->velocity[i], fz);
    for (int i = 0; i < 3; i++) fr_bad |= problem_u(a->spin[i], fz);

    const uint32_t nrec = a->records;
    for (uint32_t ri = 0; ri < nrec; ri++) {
        const uint8_t *ip = a->indices + 32u * ri;
        const uint8_t *rp = a->radii + 64u * ri;
        const uint8_t *fp = a->frictions + 64u * ri;

        uint16_t ids[16];
        for (int k = 0; k < 16; k++) ids[k] = (uint16_t)kc::ld16(ip + 2 * k);
        if (repeated16(ids)) {
            r->done = ri;
            r->why = CLOTH_SPHERE_REPEATED;
            return;
        }
        if (sphere_bad) {
            r->done = ri;
            r->why = CLOTH_SPHERE_VALUES;
            return;
        }

        Bad bad;
        bad.reset(fz);
        Group groups[4];
        int any = 0;
        for (int g = 0; g < 4; g++) {
            compute_group(a->positions, a->previous, ip, rp, fp, g, cx, cy, cz, rho, eps, bx, by, bz, vx, vy, vz, vw,
                           wx, wy, wz, dt, bad, groups[g]);
            any |= groups[g].mask;
        }
        if ((any && fr_bad) || bad.hit()) {
            r->done = ri;
            r->why = CLOTH_SPHERE_VALUES;
            return;
        }
        for (int g = 0; g < 4; g++) {
            if (groups[g].mask) store_group(groups[g], a->positions, a->previous);
        }
    }
    r->done = nrec;
    r->why = CLOTH_SPHERE_ALL;
}
