#include "cloth_bend.h"
#include "cloth_common.hpp"
#include "cloth_wlane.hpp"
#include <immintrin.h>

namespace {

using Vec = __m128;
template<int i> inline Vec splat(Vec v) {
    return _mm_shuffle_ps(v, v, _MM_SHUFFLE(i, i, i, i));
}
template<int i> inline uint32_t bits(Vec v) {
    return (uint32_t)_mm_cvtsi128_si32(_mm_castps_si128(splat<i>(v)));
}
inline Vec load(const uint8_t *p) { return _mm_loadu_ps((const float *)p); }
inline void save(uint8_t *p, Vec v) { _mm_storeu_ps((float *)p, v); }
inline Vec pair(const uint32_t *p) {
    return _mm_castsi128_ps(_mm_set_epi32(p[1], p[0], p[1], p[0]));
}
#if !defined(__AVX2__)
inline Vec rotate(Vec v) { return _mm_shuffle_ps(v, v, _MM_SHUFFLE(1, 0, 2, 1)); }
inline Vec prime(Vec v) {
    return _mm_and_ps(rotate(v), _mm_castsi128_ps(_mm_set_epi32(0, -1, -1, -1)));
}
#endif
inline Vec select(Vec m, Vec yes, Vec no) {
#if KC_SIMD
    return _mm_blendv_ps(no, yes, m);
#else
    return _mm_or_ps(_mm_and_ps(m, yes), _mm_andnot_ps(m, no));
#endif
}
inline Vec set_w(Vec v, uint32_t w) {
#if KC_SIMD
    return cw::with3(v, w);
#else
    return _mm_or_ps(_mm_and_ps(v, _mm_castsi128_ps(_mm_set_epi32(0,-1,-1,-1))),
                     _mm_castsi128_ps(_mm_set_epi32(w,0,0,0)));
#endif
}

// Most checks use lanes 0..2. A repeated pair or scalar fits those lanes too.
struct Range {
#if KC_SIMD
    kc::Checks checks;
    Range() { checks.reset(); }
    // Per lane, require zero or 2^-power <= |v| < 2^power.
    inline int band(unsigned power) const {
        __m128i upper = _mm_set1_epi32(((127 + power) << 23) - 1);
        __m128i lower = _mm_set1_epi32(((127 - power) << 23) - 1);
        __m128i valid = _mm_and_si128(_mm_cmpeq_epi32(_mm_max_epu32(checks.mx,upper),upper),
                                      _mm_cmpeq_epi32(_mm_min_epu32(checks.mn,lower),lower));
        return _mm_movemask_ps(_mm_castsi128_ps(valid));
    }
    inline void add(Vec v) { checks.add(v); }
    bool good(int mask = 7) const {
        const int wanted = mask == 7 ? 0xfff : 0xffff;
        return (checks.mask() & wanted) == wanted;
    }
#else
    unsigned invalid = 0;
    inline void add(Vec v) {
        float f[4];
        _mm_storeu_ps(f, v);
        for (unsigned j = 0; j < 4; ++j)
            invalid |= (unsigned)!kc::in_r(f[j]) << j;
    }
    bool good(int mask = 7) const { return !(invalid & mask); }
#endif
    inline void merge(const Range &other) {
#if KC_SIMD
        checks.mx = _mm_max_epu32(checks.mx,other.checks.mx);
        checks.mn = _mm_min_epu32(checks.mn,other.checks.mn);
#else
        invalid |= other.invalid;
#endif
    }
    inline Vec operator()(Vec v) { add(v); return v; }
};

// Two independent four-lane quantities share a YMM register where available.
#if defined(__AVX2__)
using Twins = __m256;
inline Twins join(Vec a, Vec b) { return _mm256_insertf128_ps(_mm256_castps128_ps256(a),b,1); }
inline Vec first(Twins v) { return _mm256_castps256_ps128(v); }
inline Vec second(Twins v) { return _mm256_extractf128_ps(v,1); }
inline Twins product(Twins a, Twins b) { return _mm256_mul_ps(a,b); }
inline Twins difference(Twins a, Twins b) { return _mm256_sub_ps(a,b); }
inline Twins rotated(Twins v) { return _mm256_permute_ps(v,_MM_SHUFFLE(1,0,2,1)); }
inline Twins primed(Twins v) { return _mm256_blend_ps(rotated(v),_mm256_setzero_ps(),0x88); }
struct TwinRange {
    __m256i maximum = _mm256_setzero_si256(), minimum = _mm256_set1_epi32(-1);
    inline Twins operator()(Twins v) {
        __m256i mag = _mm256_and_si256(_mm256_castps_si256(v),_mm256_set1_epi32(0x7fffffff));
        maximum = _mm256_max_epu32(maximum,mag);
        minimum = _mm256_min_epu32(minimum,_mm256_sub_epi32(mag,_mm256_set1_epi32(1)));
        return v;
    }
    Range fold() const {
        Range r;
        r.checks.mx = _mm_max_epu32(_mm256_castsi256_si128(maximum),_mm256_extracti128_si256(maximum,1));
        r.checks.mn = _mm_min_epu32(_mm256_castsi256_si128(minimum),_mm256_extracti128_si256(minimum,1));
        return r;
    }
};
#else
struct Twins { Vec a,b; };
inline Twins join(Vec a, Vec b) { return {a,b}; }
inline Vec first(Twins v) { return v.a; }
inline Vec second(Twins v) { return v.b; }
inline Twins product(Twins a, Twins b) { return {_mm_mul_ps(a.a,b.a),_mm_mul_ps(a.b,b.b)}; }
inline Twins difference(Twins a, Twins b) { return {_mm_sub_ps(a.a,b.a),_mm_sub_ps(a.b,b.b)}; }
inline Twins rotated(Twins v) { return {rotate(v.a),rotate(v.b)}; }
inline Twins primed(Twins v) { return {prime(v.a),prime(v.b)}; }
struct TwinRange {
    Range range;
    inline Twins operator()(Twins v) { range.add(v.a); range.add(v.b); return v; }
    Range fold() const { return range; }
};
#endif

// R spans fewer than 128 consecutive exponents. Its seven low exponent
// bits and the top seven fraction bits therefore name the complete estimate.
// Other lengths cannot commit a record. Zero is masked in the refinement.
struct EstimateTable {
    struct Entry { uint32_t half, square; };
    Entry word[16384]{};
    constexpr EstimateTable() {
        for (unsigned i = 0; i < 16384; ++i) {
            unsigned exponent = (i >> 7) + (i < 8192 ? 128 : 0);
            unsigned bias = (380 - exponent) >> 1;
            unsigned fraction = kc::RSQ.t[(i << 1) & 511];
            word[i].half = ((bias - 1) << 23) | (fraction << 15);
            // The estimate has nine significant bits. Its square is exact in
            // binary32 throughout R, so tabulate both inputs to the refinement.
            unsigned square = (256 + fraction) * (256 + fraction);
            unsigned carry = square >> 17;
            word[i].square = ((2*bias - 127 + carry) << 23) |
                             ((square << (7-carry)) & 0x7fffff);
        }
    }
};
constexpr EstimateTable estimate_table{};
inline Vec estimate(float length) {
    const auto &entry = estimate_table.word[(kc::fb(length) >> 16) & 16383];
    return _mm_castsi128_ps(_mm_loadl_epi64((const __m128i *)&entry));
}

// Both roots take the same Newton step. The exact double products serve the
// range check on every target and the fused-step emulation on SSE2/SSE4.1.
template<bool bounded> inline Vec roots(Vec length, Vec half, Vec e2, unsigned &bad) {
    Vec positive = _mm_cmpgt_ps(length,_mm_setzero_ps());
    half = _mm_and_ps(half,positive);
    __m128d le;
    if (!bounded || !KC_FRSQRTS_FMA) {
        le = _mm_mul_pd(_mm_cvtps_pd(length),_mm_cvtps_pd(e2));
        int within = _mm_movemask_pd(_mm_and_pd(_mm_cmpge_pd(le,_mm_set1_pd(0.125)),
                                                _mm_cmple_pd(le,_mm_set1_pd(8))));
        bad |= (unsigned)(_mm_movemask_ps(positive) & ~within & 3);
    }
#if KC_FRSQRTS_FMA
    Vec residual = _mm_fnmadd_ps(length,e2,_mm_set1_ps(3.0f));
    if (bounded) {
        // A rounded |3 - L*e2| <= 2.5 proves the exact L*e2 is in
        // [1/8,8], with room on both sides for the FMA rounding error.
        // The full path tests the exact double product for all other cases.
        Vec mag = _mm_and_ps(residual,_mm_castsi128_ps(_mm_set1_epi32(0x7fffffff)));
        Vec suspect = _mm_and_ps(positive,_mm_cmpnle_ps(mag,_mm_set1_ps(2.5f)));
        bad |= (unsigned)(_mm_movemask_ps(suspect) & 3);
    }
#else
    Vec low = _mm_cvtpd_ps(_mm_sub_pd(_mm_set1_pd(3.0),le));
    Vec residual = _mm_movelh_ps(low,low);
#endif
    // Halving e is exact in the class, and independent of the residual.
    return _mm_mul_ps(half,residual);
}

struct Inputs {
    Vec a, d, b, c;
    Vec coefficients, mass;
    float gain, stiffness, scale;
    uint32_t slot[4];
    __m128i indices;
};
struct Update {
    Vec value[4];
};

struct WordLane {
    unsigned fz, flags;
    uint32_t add(uint32_t a, uint32_t b) { return cw::arm_add(a,b,fz,flags); }
    uint32_t sub(uint32_t a, uint32_t b) { return cw::arm_sub(a,b,fz,flags); }
    uint32_t mul(uint32_t a, uint32_t b) { return cw::arm_mul(a,b,fz,flags); }
};

__attribute__((cold,noinline)) void exact_w(const Inputs &in, Update &out, Vec weights, uint32_t t1,
                                            unsigned fz, unsigned &flags) {
    WordLane w{fz, flags};
    uint32_t initial[] = {bits<3>(in.a), bits<3>(in.d), bits<3>(in.b), bits<3>(in.c)};
    uint32_t e1 = w.sub(initial[0], initial[2]);
    uint32_t e2 = w.sub(initial[3], initial[2]);
    uint32_t e3 = w.sub(initial[1], initial[2]);
    uint32_t y1 = bits<1>(_mm_sub_ps(in.a,in.b)), y2 = bits<1>(_mm_sub_ps(in.c,in.b));
    uint32_t y3 = bits<1>(_mm_sub_ps(in.d,in.b));
    w.sub(w.mul(e2,y1),w.mul(e1,y2));
    w.sub(w.mul(e3,y2),w.mul(e2,y3));
    w.mul(e2,e2);
    uint32_t cv = w.add(w.mul(initial[0],bits<0>(in.coefficients)),
                        w.mul(initial[1],bits<1>(in.coefficients)));
    cv = w.add(cv, w.mul(initial[2],bits<2>(in.coefficients)));
    cv = w.add(cv, w.mul(initial[3],bits<3>(in.coefficients)));
    uint32_t correction = w.add(w.mul(w.mul(0,t1),kc::fb(in.gain)), cv);
    uint32_t updated[4], factors[4];
    _mm_storeu_si128((__m128i *)factors,_mm_castps_si128(weights));
    for (int j = 0; j < 4; ++j) {
        uint32_t prior = initial[j];
        for (int k = 0; k < j; ++k)
            if (in.slot[k] == in.slot[j]) prior = updated[k];
        uint32_t factor = factors[j];
        uint32_t delta = j == 0 ? w.mul(factor, correction) : w.mul(correction, factor);
        updated[j] = j == 3 ? w.add(delta,prior) : w.add(prior,delta);
        out.value[j] = set_w(out.value[j],updated[j]);
    }
    flags = w.flags;
}

inline __m128i w_words(Vec a, Vec b, Vec c, Vec d) {
    _MM_TRANSPOSE4_PS(a,b,c,d);
    return _mm_castps_si128(d);
}
inline bool simple_w(__m128i u) {
    __m128i zero = _mm_cmpeq_epi32(_mm_slli_epi32(u,1),_mm_setzero_si128());
    __m128i nan = _mm_cmpeq_epi32(u,_mm_set1_epi32(cw::DEFAULT_NAN));
    return _mm_movemask_ps(_mm_castsi128_ps(_mm_or_si128(zero,nan))) == 15;
}

inline bool forces_default_w(__m128i words) {
    __m128i canonical = _mm_cmpeq_epi32(words,_mm_set1_epi32(cw::DEFAULT_NAN));
    __m128i mag = _mm_and_si128(words,_mm_set1_epi32(0x7fffffff));
    __m128i nan = _mm_cmpgt_epi32(mag,_mm_set1_epi32(0x7f800000));
    int canonical_lanes = _mm_movemask_epi8(canonical);
    return canonical_lanes != 0 && canonical_lanes == _mm_movemask_epi8(nan);
}

template<bool z_first = false> inline Vec sum_xyz(Vec squares, Range &range) {
    // Nonnegative terms bound the partial sum between a nonzero square and the total.
    Vec partial = _mm_add_ps(splat<0>(squares), splat<1>(squares));
    return range(z_first ? _mm_add_ps(splat<2>(squares),partial) : _mm_add_ps(partial,splat<2>(squares)));
}

template<bool bounded>
__attribute__((always_inline)) inline int solve(const Inputs &in, Update &out, Vec num, Vec threshold, Vec alt,
          unsigned zero_flags, unsigned fz, unsigned &flags, bool uniform) {
    Range geom, terms, moves, inputs;
    inputs.add(in.coefficients);
    inputs.add(in.mass);
    float kq_scalar = in.stiffness * in.scale;
    inputs.add(_mm_setr_ps(in.stiffness,in.scale,in.gain,bounded ? 0.0f : kq_scalar));
    TwinRange paired, bounded_pairs, positions;
    Range bounded_vectors;
    TwinRange &sides_and_normals = bounded ? bounded_pairs : paired;
    Range &edge_and_mean = bounded ? bounded_vectors : geom;
    Vec a = in.a, b = in.b, c = in.c, d = in.d;
    TwinRange &position_checks = bounded ? positions : paired;
    Twins ad_pos = position_checks(join(a,d)), bc_pos = position_checks(join(b,c));
    Twins sides = sides_and_normals(difference(ad_pos,join(b,b)));
    Vec e1 = first(sides), e2 = edge_and_mean(_mm_sub_ps(c,b)), e3 = second(sides);
    Twins left = join(e2,e3), right = join(e1,e2);
    Twins pos = product(left,rotated(right)), neg = product(right,rotated(left));
    if (!bounded) { paired(pos); paired(neg); }
    Twins normal = sides_and_normals(difference(pos,neg));
    Twins squares = product(normal,normal);
    if (!bounded) paired(squares);
    Vec length1, length2;
#if defined(__AVX2__)
    Twins y = _mm256_permute_ps(squares,_MM_SHUFFLE(1,1,1,1));
    Twins z = _mm256_permute_ps(squares,_MM_SHUFFLE(2,2,2,2));
    Twins lengths_xyz = _mm256_add_ps(_mm256_add_ps(y,z),squares);
    // Only the low lanes carry the two squared lengths, all terms nonnegative.
    length1 = splat<0>(first(lengths_xyz));
    length2 = splat<0>(second(lengths_xyz));
#else
    Vec sq1 = first(squares), sq2 = second(squares);
    Vec part1 = _mm_add_ps(splat<1>(sq1),splat<2>(sq1));
    Vec part2 = _mm_add_ps(splat<1>(sq2),splat<2>(sq2));
    length1 = _mm_add_ps(part1,splat<0>(sq1));
    length2 = _mm_add_ps(splat<0>(sq2),part2);
#endif
    if (!bounded) { geom.add(length1); geom.add(length2); }
    float l1 = _mm_cvtss_f32(length1), l2 = _mm_cvtss_f32(length2);
    unsigned bad = 0;
    Vec lengths = _mm_setr_ps(l1,l2,l1,l2);
    Vec estimates = _mm_unpacklo_ps(estimate(l1),estimate(l2));
    Vec ri = roots<bounded>(lengths,_mm_movelh_ps(estimates,estimates),
                            _mm_movehl_ps(estimates,estimates),bad);
    Vec r1 = splat<0>(ri), r2 = splat<1>(ri);
    Twins unit = product(primed(normal),join(r1,r2));
    if (!bounded) paired(unit);
    Vec m = edge_and_mean(_mm_add_ps(first(unit),second(unit)));
    // For L in R, the refined reciprocal root and L times it are both in R.
    Vec len1 = _mm_mul_ps(_mm_set1_ps(l1),r1);
    Vec len2 = _mm_mul_ps(_mm_set1_ps(l2),r2);
    Vec mm = _mm_mul_ps(m,m), ee = _mm_mul_ps(e2,e2);
    if (!bounded) { terms.add(mm); geom.add(ee); }
    Vec lm = _mm_add_ps(_mm_add_ps(splat<0>(mm),splat<1>(mm)),splat<2>(mm));
    Vec le = _mm_add_ps(splat<2>(ee),_mm_add_ps(splat<0>(ee),splat<1>(ee)));
    if (!bounded) { terms.add(lm); geom.add(le); }
    Vec em = estimate(_mm_cvtss_f32(lm));
    Vec rm = roots<bounded>(lm,splat<0>(em),splat<1>(em),bad);
    Vec ll = _mm_mul_ps(len1,len2), nm = _mm_mul_ps(m,rm);
    if (!bounded) { terms.add(ll); terms.add(nm); }
    Vec quo = _mm_div_ps(num,le);
    Vec sel = terms(_mm_and_ps(_mm_cmpneq_ps(le,_mm_setzero_ps()),quo));
    Vec t = terms(_mm_mul_ps(ll,sel));
    Vec kq = _mm_set1_ps(kq_scalar);
    Vec gain = _mm_set1_ps(in.gain);
    Vec u = terms(_mm_mul_ps(t,gain));
    Vec st = select(_mm_cmpgt_ps(_mm_mul_ps(u,u),threshold),alt,kq);
    Vec f0 = splat<0>(in.coefficients), f1 = splat<1>(in.coefficients);
    Vec f2 = splat<2>(in.coefficients), f3 = splat<3>(in.coefficients);
    Twins ad_weighted = product(ad_pos,join(f0,f1));
    Twins bc_weighted = product(bc_pos,join(f2,f3));
    Vec cv = _mm_add_ps(first(ad_weighted),second(ad_weighted));
    if (!bounded) { paired(ad_weighted); paired(bc_weighted); moves.add(cv); }
    cv = _mm_add_ps(cv,first(bc_weighted));
    if (!bounded) moves.add(cv);
    cv = _mm_add_ps(cv,second(bc_weighted));
    if (!bounded) moves.add(cv);
    Vec g = terms(_mm_mul_ps(terms(_mm_mul_ps(nm,t)),gain));
    Vec correction = moves(_mm_add_ps(g,cv));
    // Each lane now holds a different particle, first for st0 and then for st1.
    Vec k0 = _mm_mul_ps(in.coefficients,splat<0>(st));
    Vec weights0 = _mm_mul_ps(k0,in.mass);
    if (!bounded) { inputs.add(k0); inputs.add(weights0); }
    Vec factor[4], weights1 = weights0;
    if (uniform) {
        factor[0] = splat<0>(weights0);
        factor[1] = splat<1>(weights0);
        factor[2] = splat<2>(weights0);
        factor[3] = splat<3>(weights0);
    } else {
        Vec k1 = _mm_mul_ps(in.coefficients,splat<1>(st));
        weights1 = _mm_mul_ps(k1,in.mass);
        if (!bounded) { inputs.add(k1); inputs.add(weights1); }
        Vec ad = _mm_unpacklo_ps(weights0,weights1), bc = _mm_unpackhi_ps(weights0,weights1);
        factor[0] = _mm_movelh_ps(ad,ad);
        factor[1] = _mm_movehl_ps(ad,ad);
        factor[2] = _mm_movelh_ps(bc,bc);
        factor[3] = _mm_movehl_ps(bc,bc);
    }
    Twins correction2 = join(correction,correction);
    Twins ad_delta = paired(product(join(factor[0],correction),join(correction,factor[1])));
    Twins bc_delta = paired(product(correction2,join(factor[2],factor[3])));
    Vec delta[] = {first(ad_delta),second(ad_delta),first(bc_delta),second(bc_delta)};
    Range joint = paired.fold();
    bool bounded_w = true;
#if KC_SIMD
    if (bounded) {
        // For edges, normals and M in [2^-30, 2^30) or zero, their products
        // and squares are zero or in [2^-60, 2^60). Three squares sum to
        // less than 2^62. Nonzero reciprocal roots lie in (2^-32, 2^31),
        // so each normalized component is in R. The product of the two
        // refined lengths is also in R (within the estimate error of sqrt(L1*L2)).
        // With positions in the 24-bit exponent band and coefficients in
        // the 15-bit band, weighted coordinates have magnitude below 2^39
        // and are multiples of 2^-62. All four successive sums stay in R.
        // Each particle factor uses at most four of the bounded inputs.
        Range particle_bounds = positions.fold();
        int particles_ok = particle_bounds.band(24);
        if ((particles_ok & 7) != 7 || inputs.band(15) != 15)
            return CLOTH_BEND_OTHER;
        Range bounds = bounded_pairs.fold();
        bounds.merge(bounded_vectors);
        bounds.add(st);
        int lanes = bounds.band(30);
        if ((lanes & 7) != 7) return CLOTH_BEND_OTHER;
        bounded_w = (lanes & particles_ok & 8) != 0;
    }
#endif
    joint.merge(geom);
    joint.merge(terms);
    joint.merge(moves);
    if ((!bounded && !inputs.good(15)) || !joint.good())
        return CLOTH_BEND_CLASS;
    out.value[0] = _mm_add_ps(a,delta[0]);
    __m128i adjacent = _mm_shufflelo_epi16(in.indices,_MM_SHUFFLE(0,3,2,1));
    __m128i opposite = _mm_shufflelo_epi16(in.indices,_MM_SHUFFLE(1,0,3,2));
    __m128i same = _mm_or_si128(_mm_cmpeq_epi16(in.indices,adjacent),
                                _mm_cmpeq_epi16(in.indices,opposite));
    bool distinct = (_mm_movemask_epi8(same) & 255) == 0;
    bool default_outputs = false;
    if (bounded) {
        // The full path stages repeated slots and handles exceptional w.
        // Keeping it outside the common path lets the four sums stay in registers.
        if (!distinct || bad) return CLOTH_BEND_OTHER;
        if (!(bounded_w && joint.good(15))) {
            __m128i words = w_words(a,d,b,c);
            if (!simple_w(words)) {
                // One default NaN in the weighted sum reaches every slot.
                // Other NaNs must have that same payload. All possible w
                // exceptions are sticky already (IDC follows from the tiny test).
                default_outputs = (flags & cw::FAST_FLAGS) == cw::FAST_FLAGS && forces_default_w(words);
                if (!default_outputs) return CLOTH_BEND_OTHER;
            }
        }
    }
    if (bounded || distinct) {
        out.value[1] = _mm_add_ps(d,delta[1]);
        out.value[2] = _mm_add_ps(b,delta[2]);
        out.value[3] = _mm_add_ps(delta[3],c);
    } else {
        Vec dr = in.slot[1] == in.slot[0] ? out.value[0] : d;
        out.value[1] = _mm_add_ps(dr,delta[1]);
        Vec br = in.slot[2] == in.slot[1] ? out.value[1] :
                 in.slot[2] == in.slot[0] ? out.value[0] : b;
        out.value[2] = _mm_add_ps(br,delta[2]);
        Vec cr = in.slot[3] == in.slot[2] ? out.value[2] :
                 in.slot[3] == in.slot[1] ? out.value[1] :
                 in.slot[3] == in.slot[0] ? out.value[0] : c;
        out.value[3] = _mm_add_ps(delta[3],cr);
        Range reloads;
        reloads.add(dr); reloads.add(br); reloads.add(cr);
        if (!reloads.good()) return CLOTH_BEND_CLASS;
    }
    if (bad) return bounded ? CLOTH_BEND_OTHER : CLOTH_BEND_STEP;
    if (_mm_cvtss_f32(le) == 0) flags |= zero_flags;
    if (bounded && default_outputs)
        for (Vec &v : out.value) v = set_w(v,cw::DEFAULT_NAN);
    __m128i initial_w = w_words(a,d,b,c);
    // If all w intermediates are themselves in R, their host arithmetic also
    // raises no guest exceptions. Signed zeros and canonical NaNs form another
    // closed set here because every multiplier from the other lanes is finite.
    if (!bounded && !joint.good(15) && !simple_w(initial_w)) {
#if KC_SIMD
        // Where the guest already holds the possible w exceptions, the only
        // remaining differences are NaN payload choice and guest flushing.
        if ((flags & cw::FAST_FLAGS) == cw::FAST_FLAGS) {
            __m128i mag = _mm_and_si128(initial_w,_mm_set1_epi32(0x7fffffff));
            __m128i noncanonical = _mm_andnot_si128(_mm_cmpeq_epi32(initial_w,_mm_set1_epi32(cw::DEFAULT_NAN)),
                                        _mm_cmpgt_epi32(mag,_mm_set1_epi32(0x7f800000)));
            bool safe = _mm_testz_si128(noncanonical,noncanonical);
            if (fz && safe) {
                __m128i min_w = joint.checks.mn;
                __m128i last = w_words(out.value[0],out.value[1],out.value[2],out.value[3]);
                __m128i last_mag = _mm_and_si128(last,_mm_set1_epi32(0x7fffffff));
                __m128i last_small = _mm_andnot_si128(_mm_cmpeq_epi32(last_mag,_mm_setzero_si128()),
                                            _mm_cmpgt_epi32(_mm_set1_epi32(0x00800001),last_mag));
                safe = cw::fz_safe_min3(min_w) && _mm_testz_si128(last_small,last_small);
            }
            if (safe) {
                for (Vec &v : out.value) v = cw::w_canon(v);
                return CLOTH_BEND_ALL;
            }
        }
#endif
        exact_w(in,out,weights1,bits<1>(t),fz,flags);
    }
    return CLOTH_BEND_ALL;
}

__attribute__((always_inline)) inline Inputs read_record(const ClothBendPass *p, const uint8_t *rec, uint8_t *dest[4]) {
    Inputs in;
    in.indices = _mm_loadl_epi64((const __m128i *)(rec+24));
    float masses[4];
#pragma GCC unroll 4
    for (int j = 0; j < 4; ++j) {
        in.slot[j] = kc::ld16(rec+24+2*j);
        size_t off = 16 * in.slot[j];
        dest[j] = p->slots + off;
        masses[j] = kc::ld1(p->particles + off + 4);
    }
    in.a = load(dest[0]); in.d = load(dest[1]);
    in.b = load(dest[2]); in.c = load(dest[3]);
    in.mass = _mm_loadu_ps(masses);
    in.coefficients = load(rec);
    in.stiffness = kc::ld1(rec+16);
    in.gain = kc::ld1(rec+20);
    in.scale = kc::ld1(p->scale);
    return in;
}

#if KC_SIMD
// An inconclusive bound has no side effects. Reloading here avoids keeping
// the record's initial values live across the entire common calculation.
__attribute__((noinline)) int full_record(const ClothBendPass *p, const uint8_t *rec, Update &out,
                                          Vec num, Vec threshold, Vec alt, unsigned zero_flags,
                                          unsigned &flags, bool uniform) {
    uint8_t *dest[4];
    Inputs in = read_record(p,rec,dest);
    return solve<false>(in,out,num,threshold,alt,zero_flags,p->fz,flags,uniform);
}
#endif
} // namespace

extern "C" void cloth_bend_pass(ClothBendPass *p) {
    uint32_t done = 0, lo = UINT32_MAX, hi = 0, flags = p->fpsr_in;
    int why = CLOTH_BEND_ALL;
#if KC_SIMD
    __m128i lows = _mm_set1_epi16(-1), highs = _mm_setzero_si128();
#endif
    unsigned saved_mxcsr = _mm_getcsr();
    if ((int32_t)p->n > 0) {
        unsigned zero_flags = 0;
        for (int i = 0; i < 2; ++i) {
            if (!kc::plain_bits(p->threshold[i]) || !kc::in_r_bits(p->numerator[i]) ||
                !kc::in_r_bits(p->alt_stiffness[i])) why = CLOTH_BEND_ENTRY;
            zero_flags |= (p->numerator[i] & 0x7fffffffu) ? cw::DZC : cw::IOC;
        }
        Vec num = pair(p->numerator), threshold = pair(p->threshold), alt = pair(p->alt_stiffness);
        bool uniform = p->threshold[0] == p->threshold[1] && p->numerator[0] == p->numerator[1] &&
                       p->alt_stiffness[0] == p->alt_stiffness[1];
        for (; why == CLOTH_BEND_ALL && done < p->n; ++done) {
            const uint8_t *rec = p->records + (size_t)32 * done;
            uint8_t *dest[4];
            Inputs in = read_record(p,rec,dest);
            Update out;
#if KC_SIMD
            why = solve<true>(in,out,num,threshold,alt,zero_flags,p->fz,flags,uniform);
            if (why == CLOTH_BEND_OTHER)
                why = full_record(p,rec,out,num,threshold,alt,zero_flags,flags,uniform);
#else
                why = solve<false>(in,out,num,threshold,alt,zero_flags,p->fz,flags,uniform);
#endif
            if (why != CLOTH_BEND_ALL) break;
            #pragma GCC unroll 4
            for (int j = 0; j < 4; ++j) {
                save(dest[j],out.value[j]);
#if !KC_SIMD
                kc::note_slot(lo,hi,in.slot[j]);
#endif
            }
#if KC_SIMD
            lows = _mm_min_epu16(lows,in.indices);
            highs = _mm_max_epu16(highs,in.indices);
#endif
        }
    }
#if KC_SIMD
    if (done) {
        __m128i low4 = _mm_unpacklo_epi64(lows,lows);
        lo = _mm_extract_epi16(_mm_minpos_epu16(low4),0);
        hi = 65535u - _mm_extract_epi16(_mm_minpos_epu16(_mm_xor_si128(highs,_mm_set1_epi16(-1))),0);
    }
#endif
    p->done = done; p->slot_lo = lo; p->slot_hi = hi; p->why = why;
    p->fpsr = flags | ((done && p->fz) ? cw::IDC : 0);
    _mm_setcsr(saved_mxcsr);
}
