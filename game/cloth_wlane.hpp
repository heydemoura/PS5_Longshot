/* Single Arm float operations, bit for bit, for values the host's own arithmetic cannot vouch for: NaNs, infinities,
   subnormals, flush-to-zero, and the status flags they raise (C++, header-only, inline). From the Arm Architecture
   Reference Manual's pseudocode (FPAdd, FPSub, FPMul, FPMin, FPMax, FPCompare, FPCompareGE, FPUnpack, FPProcessNaNs,
   FPRound); nothing here is taken from the game.

   Used for the fourth lane ("w") of the cloth's 4-float vectors: in the cloth data that lane holds anything (most
   often the default NaN 0x7fc00000, else values from 2^-20 to 2^127) and feeds no other lane, so a kernel can prove
   lanes 0-2 with the exactness class (cloth_common.hpp) and give lane 3 the Arm's exact value and flags. Two ways, both
   exact:

   1. arm_add / arm_sub / arm_mul below: one Arm single-precision addition / subtraction / multiplication (FPAdd, FPSub,
      FPMul with FPRound) for the float control the kernels run under (round to nearest, default-NaN off, no alternate
      handling, no traps; flush-to-zero either way), its result bits and the flags it raises ORed into `fl`:
      - FPUnpack: under flush-to-zero a nonzero subnormal operand is a zero of its sign and raises IDC (both operands,
        before NaNs are looked at);
      - FPProcessNaNs (default-NaN off): a signalling NaN operand, first operand first, is returned quieted (bit 22 set)
        and raises IOC; else a quiet NaN operand, first operand first, is returned as it is;
      - invalid: inf - inf (an addition with opposite signs, a subtraction with equal signs), 0 * inf: the default NaN
        0x7fc00000, IOC;
      - infinities and zeros as IEEE (a sum of zeros is +0 unless both are -0; a product's sign is the XOR);
      - otherwise the exact result rounded once (FPRound): below 2^-126 in magnitude (tiny BEFORE rounding) it is, under
        flush-to-zero, a zero of its sign with UFC (no rounding), else the correctly rounded subnormal with UFC when
        inexact; past FLT_MAX after rounding, infinity with OFC. The exact product of two binary32 values fits binary64
        (48 bits), so (float) of it rounds once; a binary64 sum rounded again to binary32 is the correctly rounded sum
        (53 >= 2*24 + 2), and a tiny sum is a multiple of 2^-149 below 2^-126, exact in both. IXC is not tracked: the
        caller guarantees it is already set.
      Relies on the host float environment the kernels run in (round to nearest, the host's denormals-are-zero and
      flush-to-zero modes clear: the conversions see and give subnormals).

   2. The host vector's own lane 3, when it is provably the Arm's (the "fast way"): the guest float status already holds
      IOC, OFC and UFC (the flags lane 3 can raise; IDC needs a subnormal operand under flush-to-zero, excluded below),
      every NaN the record reads into lane 3 is exactly the default NaN 0x7fc00000, and under flush-to-zero no lane 3
      value of the record (operands and results) is a nonzero of magnitude at most 2^-126. Then each lane 3 operation's
      value is IEEE's on both machines (no flush; overflow gives infinity in both; zeros and infinities as IEEE), except
      a NaN's bits: the Arm's NaNs are all 0x7fc00000 (propagated inputs, or the default NaN of an invalid operation),
      the host's are 0x7fc00000 or its own default 0xffc00000 (and SSE's choice between two NaN operands is the first,
      which a compiler may commute). A NaN stays a NaN through addition, subtraction and multiplication on both, and
      NaN-ness follows the same operands, so the kernel puts 0x7fc00000 in every NaN lane it stores (w_canon). The flags
      are already set, so none are added. (Without flush-to-zero a subnormal is IEEE's on both; under flush-to-zero a
      result that rounds up to exactly 2^-126 is the one ambiguous case and is excluded with the subnormals.) That is the
      game's usual case: flush-to-zero on, guest float status 0x9f, w = 0x7fc00000 or normal or huge. Otherwise lane 3
      goes through (1). */
#ifndef CLOTH_WLANE_HPP
#define CLOTH_WLANE_HPP
#include <stdint.h>
#include <string.h>
#if defined(__SSE4_1__)
#include <smmintrin.h>
#endif

#if defined(__FAST_MATH__)
#error "the cloth kernels must not be built with -ffast-math: they rely on IEEE arithmetic"
#endif
#if defined(__clang__)
#pragma clang fp contract(off)
#elif defined(__GNUC__)
#pragma GCC optimize("fp-contract=off")
#endif

namespace cw {

enum : uint32_t { IOC = 0x01u, DZC = 0x02u, OFC = 0x04u, UFC = 0x08u, IXC = 0x10u, IDC = 0x80u };
constexpr uint32_t DEFAULT_NAN = 0x7fc00000u;
/* the status bits lane 3 can raise in the fast way (2): already set in the guest's float status, or the record's
   lane 3 goes through (1) */
constexpr uint32_t FAST_FLAGS = IOC | OFC | UFC;

inline float f32(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
inline uint32_t u32(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }

/* FPProcessNaNs with default-NaN off (at least one of a, b is a NaN) */
inline uint32_t nans(uint32_t a, uint32_t b, uint32_t &fl) {
    const bool na = (a & 0x7fffffffu) > 0x7f800000u, nb = (b & 0x7fffffffu) > 0x7f800000u;
    if (na && !(a & 0x00400000u)) return fl |= IOC, a | 0x00400000u;
    if (nb && !(b & 0x00400000u)) return fl |= IOC, b | 0x00400000u;
    return na ? a : b;
}
/* FPUnpack under flush-to-zero: a nonzero subnormal reads as a zero of its sign, IDC */
inline void flush_in(uint32_t &a, uint32_t fz, uint32_t &fl) {
    if (fz && (a & 0x7fffffffu) - 1u < 0x007fffffu) fl |= IDC, a &= 0x80000000u;
}
/* FPRound of an exact nonzero value p (a product; or a sum, see the top), sign s */
inline uint32_t round_from(double p, uint32_t s, uint32_t fz, uint32_t &fl) {
    const double ap = p < 0 ? -p : p;
    if (ap < 0x1p-126) {
        if (fz) return fl |= UFC, s;
        const float r = (float)p;
        if ((double)r != p) fl |= UFC;
        return u32(r);
    }
    const uint32_t r = u32((float)p);
    if ((r & 0x7fffffffu) == 0x7f800000u) fl |= OFC;
    return r;
}
/* FPAdd of two flushed, non-NaN operands (a subtraction: b's sign flipped first) */
inline uint32_t add_core(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    const uint32_t ma = a & 0x7fffffffu, mb = b & 0x7fffffffu;
    if (ma == 0x7f800000u || mb == 0x7f800000u) {
        if (ma == mb && ((a ^ b) & 0x80000000u)) return fl |= IOC, DEFAULT_NAN;
        return ma == 0x7f800000u ? a : b;
    }
    if (!mb) return ma ? a : a & b;   /* (two zeros: +0 unless both are -0; x + 0 = x exactly, a subnormal x too) */
    if (!ma) return b;
    const double s = (double)f32(a) + (double)f32(b);
    if (s == 0.0) return 0u;          /* (an exact cancellation: +0 when rounding to nearest) */
    return round_from(s, s < 0 ? 0x80000000u : 0u, fz, fl);
}

/* a + b, a - b, a * b as the Arm computes them (bits), flags into fl. The operand order matters: of two NaN operands
   the first one's is kept */
/* one Arm single-precision addition a + b */
inline uint32_t arm_add(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    flush_in(a, fz, fl), flush_in(b, fz, fl);
    if ((a & 0x7fffffffu) > 0x7f800000u || (b & 0x7fffffffu) > 0x7f800000u) return nans(a, b, fl);
    return add_core(a, b, fz, fl);
}
/* one Arm single-precision subtraction a - b */
inline uint32_t arm_sub(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    flush_in(a, fz, fl), flush_in(b, fz, fl);
    if ((a & 0x7fffffffu) > 0x7f800000u || (b & 0x7fffffffu) > 0x7f800000u) return nans(a, b, fl);
    return add_core(a, b ^ 0x80000000u, fz, fl);
}
/* one Arm single-precision multiplication a * b */
inline uint32_t arm_mul(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    flush_in(a, fz, fl), flush_in(b, fz, fl);
    const uint32_t ma = a & 0x7fffffffu, mb = b & 0x7fffffffu;
    if (ma > 0x7f800000u || mb > 0x7f800000u) return nans(a, b, fl);
    const uint32_t s = (a ^ b) & 0x80000000u;
    if (ma == 0x7f800000u || mb == 0x7f800000u) return (!ma || !mb) ? (fl |= IOC, DEFAULT_NAN) : s | 0x7f800000u;
    if (!ma || !mb) return s;
    return round_from((double)f32(a) * (double)f32(b), s, fz, fl);
}

/* FPMin / FPMax (default-NaN off, no alternate handling): both operands flushed under flush-to-zero (IDC), then
   FPProcessNaNs (a signalling NaN quieted with IOC, first operand first; else a quiet NaN, first operand first); else
   the smaller (larger) value, and of two equal ones the OR (AND) of their bits: zeros are the only equal values with
   different bits, and FPMin gives a zero the sign sign1 OR sign2 (FPMax: AND). The result of two non-NaN operands is
   one of them (after the flush) exactly: FPRound of an exact value raises nothing, a subnormal one with flush-to-zero
   clear included (UFC needs an inexact result). */
inline uint32_t arm_fmin(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    flush_in(a, fz, fl), flush_in(b, fz, fl);
    if ((a & 0x7fffffffu) > 0x7f800000u || (b & 0x7fffffffu) > 0x7f800000u) return nans(a, b, fl);
    const float x = f32(a), y = f32(b);
    return x == y ? (a | b) : x < y ? a : b;
}
inline uint32_t arm_fmax(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    flush_in(a, fz, fl), flush_in(b, fz, fl);
    if ((a & 0x7fffffffu) > 0x7f800000u || (b & 0x7fffffffu) > 0x7f800000u) return nans(a, b, fl);
    const float x = f32(a), y = f32(b);
    return x == y ? (a & b) : x > y ? a : b;
}
/* FPCompareGE (a signalling comparison): both operands flushed under flush-to-zero (IDC); any NaN operand, quiet or
   signalling, gives false and IOC; else a >= b on the values (+0 = -0). The vector lane's result: all ones or zero */
inline uint32_t arm_fcmge(uint32_t a, uint32_t b, uint32_t fz, uint32_t &fl) {
    flush_in(a, fz, fl), flush_in(b, fz, fl);
    if ((a & 0x7fffffffu) > 0x7f800000u || (b & 0x7fffffffu) > 0x7f800000u) return fl |= IOC, 0u;
    return f32(a) >= f32(b) ? 0xffffffffu : 0u;
}
/* FPCompare of a with zero (a quiet comparison): a flushed under flush-to-zero (IDC); a signalling NaN raises IOC, a
   quiet one nothing (both unordered: not equal); else whether a == 0. Returns that equality */
inline bool arm_fcmp_zero_z(uint32_t a, uint32_t fz, uint32_t &fl) {
    flush_in(a, fz, fl);
    if ((a & 0x7fffffffu) > 0x7f800000u) {
        if (!(a & 0x00400000u)) fl |= IOC;
        return false;
    }
    return (a & 0x7fffffffu) == 0;
}

/* (2)'s conditions on single values: a lane 3 input (read from memory) whose NaN, if it is one, is the default NaN; a
   lane 3 value that flush-to-zero cannot touch (zero, or above 2^-126 in magnitude; NaN and infinity included) */
inline bool canon_in(uint32_t u) { return (u & 0x7fffffffu) <= 0x7f800000u || u == DEFAULT_NAN; }
inline bool fz_safe(uint32_t u) { return (u & 0x7fffffffu) - 1u >= 0x00800000u; }

#if defined(__SSE4_1__)
/* lane 3 of an unsigned min accumulator of |bits| - 1 (a zero wraps to all ones): every value in it fz_safe */
inline bool fz_safe_min3(__m128i mn) { return (uint32_t)_mm_extract_epi32(mn, 3) >= 0x00800000u; }
/* every NaN lane of v replaced by the default NaN (lanes 0-2 hold no NaN where this is used) */
inline __m128 w_canon(__m128 v) {
    return _mm_blendv_ps(v, _mm_castsi128_ps(_mm_set1_epi32((int)DEFAULT_NAN)), _mm_cmpunord_ps(v, v));
}
inline uint32_t lane3(__m128 v) { return (uint32_t)_mm_extract_ps(v, 3); }
inline __m128 with3(__m128 v, uint32_t w) { return _mm_insert_ps(v, _mm_castsi128_ps(_mm_cvtsi32_si128((int)w)), 0x30); }
#endif

}  // namespace cw
#endif
