/* Capsule cull: particles inside a capsule's box that can move or share its group.
   SPEC.md sections 2-6. Comparisons only; SSE ordered >= matches the Arm when
   subnormals are flushed to signed zero first (flush-to-zero). */
#include "cloth_cull.h"
#include <string.h>
#include <emmintrin.h>
#if defined(__AVX2__)
#include <immintrin.h>
#endif

#if defined(__GNUC__)
#define CC_UNLIKELY(x) __builtin_expect(!!(x), 0)
#else
#define CC_UNLIKELY(x) (x)
#endif

static inline uint32_t ld32(const uint8_t *p) {
    uint32_t u;
    memcpy(&u, p, 4);
    return u;
}

static inline uint8_t *st_idx(uint8_t *o, uint32_t idx) {
    uint16_t v = (uint16_t)idx;
    memcpy(o, &v, 2);
    return o + 2;
}

/* Taken when Z(m) is "not zero". Flags: IOC for a signalling NaN, IDC for a
   nonzero subnormal under flush-to-zero (that mass then counts as zero). */
static inline int mass_taken(uint32_t bits, int fz, uint32_t *flags) {
    uint32_t a = bits & 0x7fffffffu;
    if (a - 1u >= 0x007fffffu && a <= 0x7f800000u) return a != 0u;
    if (a > 0x7f800000u) {
        if ((bits & 0x00400000u) == 0u) *flags |= CLOTH_CULL_IOC;
        return 1;
    }
    if (fz) {
        *flags |= CLOTH_CULL_IDC;
        return 0;
    }
    return 1;
}

static inline __m128 ge128(__m128 a, __m128 b) {
#if defined(__AVX__)
    return _mm_cmp_ps(a, b, _CMP_GE_OQ);
#else
    return _mm_cmpge_ps(a, b);
#endif
}

static inline __m128 flush128(__m128 p) {
    __m128i b = _mm_castps_si128(p);
    __m128i abs = _mm_and_si128(b, _mm_set1_epi32(0x7fffffff));
    __m128i is_sub = _mm_and_si128(_mm_cmpgt_epi32(abs, _mm_setzero_si128()),
                                   _mm_cmpgt_epi32(_mm_set1_epi32(0x00800000), abs));
    __m128i sign = _mm_and_si128(b, _mm_set1_epi32((int)0x80000000));
    __m128i out = _mm_or_si128(_mm_andnot_si128(is_sub, b), _mm_and_si128(is_sub, sign));
    return _mm_castsi128_ps(out);
}

static inline int has_nz_sub128(__m128i b) {
    __m128i abs = _mm_and_si128(b, _mm_set1_epi32(0x7fffffff));
    __m128i sub = _mm_and_si128(_mm_cmpgt_epi32(abs, _mm_setzero_si128()),
                                _mm_cmpgt_epi32(_mm_set1_epi32(0x00800000), abs));
    return _mm_movemask_ps(_mm_castsi128_ps(sub)) != 0;
}

static inline int zero_exp128(__m128i b) {
    __m128i exp = _mm_and_si128(b, _mm_set1_epi32(0x7f800000));
    return _mm_movemask_ps(_mm_castsi128_ps(_mm_cmpeq_epi32(exp, _mm_setzero_si128()))) != 0;
}

static inline void note_nan128(__m128i b, uint32_t *flags, int *need_ioc) {
    __m128i abs = _mm_and_si128(b, _mm_set1_epi32(0x7fffffff));
    __m128i nan = _mm_cmpgt_epi32(abs, _mm_set1_epi32(0x7f800000));
    if (_mm_movemask_ps(_mm_castsi128_ps(nan))) {
        *flags |= CLOTH_CULL_IOC;
        *need_ioc = 0;
    }
}

/* Eight box-corner lanes, once: the union does not grow when they are compared again. */
static uint32_t corner_flags(const uint8_t *box, int fz) {
    __m128i b0 = _mm_loadu_si128((const __m128i *)(const void *)box);
    __m128i b1 = _mm_loadu_si128((const __m128i *)(const void *)(box + 16));
    const __m128i m = _mm_set1_epi32(0x7fffffff);
    const __m128i lim = _mm_set1_epi32(0x7f800000);
    __m128i nan = _mm_or_si128(_mm_cmpgt_epi32(_mm_and_si128(b0, m), lim),
                               _mm_cmpgt_epi32(_mm_and_si128(b1, m), lim));
    uint32_t fl = _mm_movemask_ps(_mm_castsi128_ps(nan)) ? CLOTH_CULL_IOC : 0u;
    if (fz) {
        const __m128i sl = _mm_set1_epi32(0x00800000);
        const __m128i z = _mm_setzero_si128();
        __m128i a0 = _mm_and_si128(b0, m);
        __m128i a1 = _mm_and_si128(b1, m);
        __m128i s0 = _mm_and_si128(_mm_cmpgt_epi32(a0, z), _mm_cmpgt_epi32(sl, a0));
        __m128i s1 = _mm_and_si128(_mm_cmpgt_epi32(a1, z), _mm_cmpgt_epi32(sl, a1));
        if (_mm_movemask_ps(_mm_castsi128_ps(_mm_or_si128(s0, s1)))) fl |= CLOTH_CULL_IDC;
    }
    return fl;
}

static void verdicts(ClothCullResult *r, int bits) {
    uint32_t in0 = (bits & 1) ? 0xffffffffu : 0u;
    uint32_t in1 = (bits & 2) ? 0xffffffffu : 0u;
    uint32_t in2 = (bits & 4) ? 0xffffffffu : 0u;
    uint32_t in3 = (bits & 8) ? 0xffffffffu : 0u;
    uint32_t yz = in1 & in2;
    r->inside_yz[0] = r->inside_yz[1] = r->inside_yz[2] = r->inside_yz[3] = yz;
    r->inside_z[0] = r->inside_z[1] = r->inside_z[2] = r->inside_z[3] = in2;
    r->inside_all[0] = in0 & yz;
    r->inside_all[1] = yz;
    r->inside_all[2] = yz;
    r->inside_all[3] = in3 & yz;
}

static void finish(ClothCullResult *r, uint32_t count, uint32_t flags, int mass_form, int box_test,
                   int last_taken, int last_inside, int last_bits, uint32_t last_mass) {
    r->count = count;
    r->status = flags & (CLOTH_CULL_IOC | CLOTH_CULL_IDC);
    r->mass_examined = 0;
    r->word_set = 0;
    r->word = 0;
    if (box_test) verdicts(r, last_bits);
    if (mass_form && (!box_test || last_inside)) {
        r->mass_examined = 1;
        r->last_mass = last_mass;
    }
    if (last_taken) {
        r->word_set = 1;
        r->word = count;
    } else if (!mass_form) {
        r->word_set = 1;
        r->word = 0;
    } else if (box_test) {
        r->word_set = 1;
        r->word = last_inside ? 0xffffffffu : 0u;
    } else if (count >= 1u) {
        r->word_set = 1;
        r->word = count;
    }
}

static inline uint8_t *commit(uint8_t *o, int inside, uint32_t idx, const uint8_t *extra, int mass,
                              uint32_t group, int fz, uint32_t *flags) {
    if (!inside) return o;
    if (mass) {
        if (!mass_taken(ld32(extra + (idx << 4)), fz, flags)) return o;
    } else if ((ld32(extra + (idx << 2)) & group) == 0u) {
        return o;
    }
    return st_idx(o, idx);
}

template <int Fz>
static inline int box_bits128(__m128 p, __m128 L, __m128 H, uint32_t *flags, int *need_ioc, int *need_idc) {
    __m128i b = _mm_castps_si128(p);
    if (*need_ioc) note_nan128(b, flags, need_ioc);
    if (Fz && zero_exp128(b)) {
        if (*need_idc && has_nz_sub128(b)) {
            *flags |= CLOTH_CULL_IDC;
            *need_idc = 0;
        }
        p = flush128(p);
    }
    __m128 in = _mm_and_ps(ge128(p, L), ge128(H, p));
    return _mm_movemask_ps(in);
}

template <int Fz>
static void loop128(const ClothCullArgs *a, __m128 L, __m128 H, uint8_t **op, uint32_t *flags,
                    int *last_inside, int *last_bits) {
    const uint32_t n = a->n;
    const int mass = a->mass_form;
    const uint32_t group = a->group;
    const uint8_t *pos = a->positions;
    const uint8_t *extra = mass ? a->masses : a->masks;
    uint8_t *o = *op;
    int need_ioc = (*flags & CLOTH_CULL_IOC) == 0;
    int need_idc = Fz && (*flags & CLOTH_CULL_IDC) == 0;
    for (uint32_t i = 0; i < n; i++) {
        __m128 p = _mm_loadu_ps((const float *)(const void *)(pos + (i << 4)));
        int bits = box_bits128<Fz>(p, L, H, flags, &need_ioc, &need_idc);
        int inside = (bits & 7) == 7;
        if (i + 1u == n) {
            *last_bits = bits;
            *last_inside = inside;
        }
        uint32_t before = *flags;
        o = commit(o, inside, i, extra, mass, group, Fz, flags);
        if (*flags != before) {
            need_ioc = (*flags & CLOTH_CULL_IOC) == 0;
            need_idc = Fz && (*flags & CLOTH_CULL_IDC) == 0;
        }
    }
    *op = o;
}

#if defined(__AVX2__)
static inline __m256 dup256(__m128 v) {
    return _mm256_insertf128_ps(_mm256_castps128_ps256(v), v, 1);
}

static inline __m256 ge256(__m256 a, __m256 b) {
    return _mm256_cmp_ps(a, b, _CMP_GE_OQ);
}

static inline int zero_exp256(__m256i b) {
    __m256i exp = _mm256_and_si256(b, _mm256_set1_epi32(0x7f800000));
    return _mm256_movemask_ps(_mm256_castsi256_ps(_mm256_cmpeq_epi32(exp, _mm256_setzero_si256()))) != 0;
}

static inline void note_nan256(__m256i b0, __m256i b1, uint32_t *flags, int *need_ioc) {
    __m256i m = _mm256_set1_epi32(0x7fffffff);
    __m256i lim = _mm256_set1_epi32(0x7f800000);
    __m256i nan = _mm256_or_si256(_mm256_cmpgt_epi32(_mm256_and_si256(b0, m), lim),
                                  _mm256_cmpgt_epi32(_mm256_and_si256(b1, m), lim));
    if (_mm256_movemask_ps(_mm256_castsi256_ps(nan))) {
        *flags |= CLOTH_CULL_IOC;
        *need_ioc = 0;
    }
}

static inline int has_nz_sub256(__m256i b) {
    __m256i abs = _mm256_and_si256(b, _mm256_set1_epi32(0x7fffffff));
    __m256i sub = _mm256_and_si256(_mm256_cmpgt_epi32(abs, _mm256_setzero_si256()),
                                   _mm256_cmpgt_epi32(_mm256_set1_epi32(0x00800000), abs));
    return _mm256_movemask_ps(_mm256_castsi256_ps(sub)) != 0;
}

static inline __m256 flush256(__m256 p) {
    __m256i b = _mm256_castps_si256(p);
    __m256i abs = _mm256_and_si256(b, _mm256_set1_epi32(0x7fffffff));
    __m256i is_sub = _mm256_and_si256(_mm256_cmpgt_epi32(abs, _mm256_setzero_si256()),
                                      _mm256_cmpgt_epi32(_mm256_set1_epi32(0x00800000), abs));
    __m256i sign = _mm256_and_si256(b, _mm256_set1_epi32((int)0x80000000));
    __m256i out = _mm256_or_si256(_mm256_andnot_si256(is_sub, b), _mm256_and_si256(is_sub, sign));
    return _mm256_castsi256_ps(out);
}

static inline uint8_t *mask_chunk(uint8_t *o, uint32_t i, const float *fp, const uint8_t *masks, __m256 L, __m256 H,
                                  __m256i absm, __m256i nanlim, __m128i g, __m128i zz, __m256i *nacc, int *m_hi) {
    __m256 p0 = _mm256_loadu_ps(fp);
    __m256 p1 = _mm256_loadu_ps(fp + 8);
    __m256i b0 = _mm256_castps_si256(p0);
    __m256i b1 = _mm256_castps_si256(p1);
    __m256i nan = _mm256_or_si256(_mm256_cmpgt_epi32(_mm256_and_si256(b0, absm), nanlim),
                                  _mm256_cmpgt_epi32(_mm256_and_si256(b1, absm), nanlim));
    *nacc = _mm256_or_si256(*nacc, nan);
    int m0 = _mm256_movemask_ps(_mm256_and_ps(ge256(p0, L), ge256(H, p0)));
    int m1 = _mm256_movemask_ps(_mm256_and_ps(ge256(p1, L), ge256(H, p1)));
    *m_hi = m1;
    __m128i km = _mm_loadu_si128((const __m128i *)(const void *)(masks + (i << 2)));
    int zm = _mm_movemask_ps(_mm_castsi128_ps(_mm_cmpeq_epi32(_mm_and_si128(km, g), zz)));
    if (((m0 & 0x7) == 0x7) && (zm & 1) == 0) o = st_idx(o, i);
    if (((m0 & 0x70) == 0x70) && (zm & 2) == 0) o = st_idx(o, i + 1u);
    if (((m1 & 0x7) == 0x7) && (zm & 4) == 0) o = st_idx(o, i + 2u);
    if (((m1 & 0x70) == 0x70) && (zm & 8) == 0) o = st_idx(o, i + 3u);
    return o;
}

template <int Fz, int Mass>
static void loop256(const ClothCullArgs *a, __m128 Ls, __m128 Hs, uint8_t **op, uint32_t *flags,
                    int *last_inside, int *last_bits) {
    const uint32_t n = a->n;
    const uint32_t group = a->group;
    const uint8_t *pos = a->positions;
    const uint8_t *extra = Mass ? a->masses : a->masks;
    uint8_t *o = *op;
    __m256 L = dup256(Ls);
    __m256 H = dup256(Hs);
    int need_ioc = (*flags & CLOTH_CULL_IOC) == 0;
    int need_idc = Fz && (*flags & CLOTH_CULL_IDC) == 0;
    const uint32_t nvec = n & ~3u;
    int m1 = 0;
    for (uint32_t i = 0; i < nvec; i += 4) {
        const float *fp = (const float *)(const void *)(pos + (i << 4));
        __m256 p0 = _mm256_loadu_ps(fp);
        __m256 p1 = _mm256_loadu_ps(fp + 8);
        __m256i b0 = _mm256_castps_si256(p0);
        __m256i b1 = _mm256_castps_si256(p1);
        if (need_ioc) note_nan256(b0, b1, flags, &need_ioc);
        if (Fz && (zero_exp256(b0) || zero_exp256(b1))) {
            if (need_idc && (has_nz_sub256(b0) || has_nz_sub256(b1))) {
                *flags |= CLOTH_CULL_IDC;
                need_idc = 0;
            }
            p0 = flush256(p0);
            p1 = flush256(p1);
        }
        __m256 in0 = _mm256_and_ps(ge256(p0, L), ge256(H, p0));
        __m256 in1 = _mm256_and_ps(ge256(p1, L), ge256(H, p1));
        int m0 = _mm256_movemask_ps(in0);
        m1 = _mm256_movemask_ps(in1);
        int i0 = (m0 & 0x7) == 0x7;
        int i1 = (m0 & 0x70) == 0x70;
        int i2 = (m1 & 0x7) == 0x7;
        int i3 = (m1 & 0x70) == 0x70;
        if (i0 | i1 | i2 | i3) {
            uint32_t before = *flags;
            if (i0) o = commit(o, 1, i, extra, Mass, group, Fz, flags);
            if (i1) o = commit(o, 1, i + 1, extra, Mass, group, Fz, flags);
            if (i2) o = commit(o, 1, i + 2, extra, Mass, group, Fz, flags);
            if (i3) o = commit(o, 1, i + 3, extra, Mass, group, Fz, flags);
            if (*flags != before) {
                need_ioc = (*flags & CLOTH_CULL_IOC) == 0;
                need_idc = Fz && (*flags & CLOTH_CULL_IDC) == 0;
            }
        }
    }
    if (n >= 4u && (n & 3u) == 0u) {
        *last_bits = (m1 >> 4) & 15;
        *last_inside = (*last_bits & 7) == 7;
    }
    for (uint32_t i = nvec; i < n; i++) {
        __m128 p = _mm_loadu_ps((const float *)(const void *)(pos + (i << 4)));
        int bits = box_bits128<Fz>(p, Ls, Hs, flags, &need_ioc, &need_idc);
        int inside = (bits & 7) == 7;
        if (i + 1u == n) {
            *last_bits = bits;
            *last_inside = inside;
        }
        o = commit(o, inside, i, extra, Mass, group, Fz, flags);
    }
    *op = o;
}

/* Flush-to-zero off, mask form: no subnormal flush, and a NaN anywhere is only a
   sticky flag, so the scan is an integer OR with one test after the loop. */
static void loop_mask_clean(const ClothCullArgs *a, __m128 Ls, __m128 Hs, uint8_t **op, uint32_t *flags,
                            int *last_inside, int *last_bits) {
    const uint32_t n = a->n;
    const uint8_t *pos = a->positions;
    const uint8_t *masks = a->masks;
    uint8_t *o = *op;
    const __m256 L = dup256(Ls);
    const __m256 H = dup256(Hs);
    const __m256i absm = _mm256_set1_epi32(0x7fffffff);
    const __m256i nanlim = _mm256_set1_epi32(0x7f800000);
    const __m128i g = _mm_set1_epi32((int)a->group);
    const __m128i zz = _mm_setzero_si128();
    __m256i nacc = _mm256_setzero_si256();
    const uint32_t nvec = n & ~3u;
    int m_hi = 0;
    uint32_t i = 0;
    for (; i + 8u <= nvec; i += 8u) {
        const float *fp = (const float *)(const void *)(pos + (i << 4));
        o = mask_chunk(o, i, fp, masks, L, H, absm, nanlim, g, zz, &nacc, &m_hi);
        o = mask_chunk(o, i + 4u, fp + 16, masks, L, H, absm, nanlim, g, zz, &nacc, &m_hi);
    }
    if (i < nvec) {
        const float *fp = (const float *)(const void *)(pos + (i << 4));
        o = mask_chunk(o, i, fp, masks, L, H, absm, nanlim, g, zz, &nacc, &m_hi);
        i += 4u;
    }
    int need_ioc = (*flags & CLOTH_CULL_IOC) == 0;
    if (need_ioc && _mm256_movemask_ps(_mm256_castsi256_ps(nacc))) {
        *flags |= CLOTH_CULL_IOC;
        need_ioc = 0;
    }
    if (n >= 4u && (n & 3u) == 0u) {
        *last_bits = (m_hi >> 4) & 15;
        *last_inside = (*last_bits & 7) == 7;
    }
    int need_idc = 0;
    for (; i < n; i++) {
        __m128 p = _mm_loadu_ps((const float *)(const void *)(pos + (i << 4)));
        int bits = box_bits128<0>(p, Ls, Hs, flags, &need_ioc, &need_idc);
        int inside = (bits & 7) == 7;
        if (i + 1u == n) {
            *last_bits = bits;
            *last_inside = inside;
        }
        o = commit(o, inside, i, masks, 0, a->group, 0, flags);
    }
    *op = o;
}
#endif

static void run_box(const ClothCullArgs *a, ClothCullResult *r) {
    uint32_t flags = corner_flags(a->box, a->fz);
    __m128 L = _mm_loadu_ps((const float *)(const void *)a->box);
    __m128 H = _mm_loadu_ps((const float *)(const void *)(a->box + 16));
    if (a->fz) {
        L = flush128(L);
        H = flush128(H);
    }
    uint8_t *o = a->list;
    int last_inside = 0, last_bits = 0;
#if defined(__AVX2__)
    if (a->mass_form) {
        if (a->fz) loop256<1, 1>(a, L, H, &o, &flags, &last_inside, &last_bits);
        else loop256<0, 1>(a, L, H, &o, &flags, &last_inside, &last_bits);
    } else if (a->fz) {
        loop256<1, 0>(a, L, H, &o, &flags, &last_inside, &last_bits);
    } else {
        loop_mask_clean(a, L, H, &o, &flags, &last_inside, &last_bits);
    }
#else
    if (a->fz) loop128<1>(a, L, H, &o, &flags, &last_inside, &last_bits);
    else loop128<0>(a, L, H, &o, &flags, &last_inside, &last_bits);
#endif
    uint32_t count = (uint32_t)((o - a->list) >> 1);
    int last_taken = 0;
    if (count) {
        uint16_t last;
        memcpy(&last, a->list + ((count - 1u) << 1), 2);
        last_taken = last == (uint16_t)(a->n - 1u);
    }
    uint32_t last_mass = 0;
    if (a->mass_form && last_inside) last_mass = ld32(a->masses + ((a->n - 1u) << 4));
    finish(r, count, flags, a->mass_form, 1, last_taken, last_inside, last_bits, last_mass);
}

static void run_nobox(const ClothCullArgs *a, ClothCullResult *r) {
    const uint32_t n = a->n;
    uint8_t *o = a->list;
    uint32_t flags = 0;
    if (a->mass_form) {
        const uint8_t *m = a->masses;
        for (uint32_t i = 0; i < n; i++) {
            if (mass_taken(ld32(m + (i << 4)), a->fz, &flags)) o = st_idx(o, i);
        }
    } else {
        const uint8_t *k = a->masks;
        uint32_t g = a->group;
        for (uint32_t i = 0; i < n; i++) {
            if (ld32(k + (i << 2)) & g) o = st_idx(o, i);
        }
    }
    uint32_t count = (uint32_t)((o - a->list) >> 1);
    int last_taken = 0;
    uint32_t last_mass = 0;
    if (count) {
        uint16_t last;
        memcpy(&last, a->list + ((count - 1u) << 1), 2);
        last_taken = last == (uint16_t)(n - 1u);
    }
    if (a->mass_form) last_mass = ld32(a->masses + ((n - 1u) << 4));
    finish(r, count, flags, a->mass_form, 0, last_taken, 0, 0, last_mass);
}

void cloth_cull_81b5a3c0(const ClothCullArgs *a, ClothCullResult *r) {
    if (a->box_test) run_box(a, r);
    else run_nobox(a, r);
}
