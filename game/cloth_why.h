/* (longshot) HOS_CLOTH_DECLINES: why a cloth kernel (game/cloth_*.cpp) does not take a record, for game/oot_cloth.c's
   counts and its capture of the calls it declined. Diagnostics only: nothing here decides what runs natively.

   (The three whole-function kernels, the bending pass, the anchor pass and Prepare Matrices, have none: game/oot_cloth.c
   names the reason their interfaces give, CW_KERNEL or a whole call's.)
   Each other kernel has a function cloth_<pass>_why(struct, i, &why) that runs record i (on the memory as it is now: the
   records before it already stored) through the scalar kernel's own checks, in the scalar kernel's order and with the
   same arithmetic, and names the first value that fails: which operand or intermediate (`what`, as the kernel's comments
   name it), its lane, the rule it broke and how (`cls`), and its bits. It writes nothing. It returns 1 when a check fails
   (the kernel stops at that record) and 0 when record i is in the class (the kernel would take it); the entry checks
   (values the kernel checks once a call) come first, so a call the kernel refuses whole is named too. The kernels'
   tests hold every kernel's decision to its why function's (tests/test_cloth_kernels.py).
   Lane 3 (w) of a 4-lane value is not a check: it feeds no other lane, and the kernels do it the Arm's way whatever it
   holds (game/cloth_wlane.hpp), so `rule` passes every value named with lane 3. */
#ifndef CLOTH_WHY_H
#define CLOTH_WHY_H
#include <stdint.h>
#include <string.h>

enum {
    CW_NONE,
    CW_POS0, CW_NEG0,        /* +0 / -0 where the rule takes no zero (no kernel rule today; kept for the table) */
    CW_POS_SUB, CW_NEG_SUB,  /* a nonzero subnormal (FZ would flush it; Arm's tininess differs from IEEE's) */
    CW_TINY,                 /* normal but below the rule's lower bound (2^-63 for the [2^-63, 2^63) class, 2^-125 for the
                                link kernel's [2^-125, FLT_MAX]) */
    CW_HUGE,                 /* finite but at or above the rule's upper bound (2^63 for the [2^-63, 2^63) class) */
    CW_INF, CW_NAN,
    CW_FLOOR,                /* the link kernel's product operands: in [2^-125, 2^-74), under the floor 2^-74 */
    CW_UFLOW,                /* a product of two nonzero values that rounded to zero (Arm raises UFC) */
    CW_RSQRTS,               /* L*e outside [1/8, 8], where the binary64 FRSQRTS is proven (`bits`: L) */
    CW_ORDER,                /* not a value: a record the kernel cannot take for another reason (`what` says which) */
    CW_CALL,                 /* game/oot_cloth.c: the whole call (gate, memory, overlap, count; `what` says which) */
    CW_UNEXPLAINED,          /* game/oot_cloth.c: the kernel stopped but its why function found nothing (a fault) */
    CW_KERNEL,               /* game/oot_cloth.c: a whole-function kernel's own reason for stopping (it names no value;
                                `what` says which reason) */
    CW_COUNT
};

/* the rules a value is held to (the bounds are on the bits without the sign; zero passes every rule) */
enum {
    CR_R,       /* zero or [2^-63, 2^63): 0x20000000 .. 0x5effffff (cloth_common.hpp) */
    CR_F,       /* zero or [2^-125, FLT_MAX]: 0x01000000 .. 0x7f7fffff (cloth_link.cpp okf) */
    CR_P,       /* zero or [2^-74, FLT_MAX]: 0x1a800000 .. 0x7f7fffff (cloth_link.cpp okp) */
    CR_PLAIN    /* not NaN, not a nonzero subnormal (a value only compared, an FMIN operand) */
};

typedef struct ClothWhy {
    const char *what;   /* the value, as the kernel's comments name it ("E2", "w5", "L1 = |N1|^2"); a string literal */
    int lane;           /* 0-3: lane x, y, z, w of a 4-lane value (0-1 of a pair); -1: a scalar */
    int cls;            /* CW_* */
    uint32_t bits;      /* the value's bits */
} ClothWhy;

static inline int cloth_why_rule_ok(uint32_t u, int rule) {
    const uint32_t a = u & 0x7fffffffu;
    switch (rule) {
    case CR_R: return a == 0 || a - 0x20000000u < 0x3f000000u;
    case CR_F: return a == 0 || a - 0x01000000u < 0x7e800000u;
    case CR_P: return a == 0 || a - 0x1a800000u < 0x65000000u;
    default: return a <= 0x7f800000u && (a == 0 || a >= 0x00800000u);
    }
}

/* how a value fails its rule (CW_NONE when it does not) */
static inline int cloth_why_class(uint32_t u, int rule) {
    if (cloth_why_rule_ok(u, rule)) return CW_NONE;
    const uint32_t a = u & 0x7fffffffu, neg = u >> 31;
    if (a > 0x7f800000u) return CW_NAN;
    if (a == 0x7f800000u) return CW_INF;
    if (a == 0) return neg ? CW_NEG0 : CW_POS0;
    if (a < 0x00800000u) return neg ? CW_NEG_SUB : CW_POS_SUB;
    const uint32_t lo = rule == CR_R ? 0x20000000u : rule == CR_P ? 0x1a800000u : 0x01000000u;
    if (a < lo) return rule == CR_P && a >= 0x01000000u ? CW_FLOOR : CW_TINY;
    return CW_HUGE;
}

static inline const char *cloth_why_class_name(int cls) {
    static const char *const names[CW_COUNT] = {"in the class", "+0", "-0", "+subnormal", "-subnormal", "tiny", "huge",
                                                "inf", "NaN", "under 2^-74", "product to 0", "L*e range", "order", "call",
                                                "unexplained", "(kernel)"};
    return cls >= 0 && cls < CW_COUNT ? names[cls] : "?";
}

#ifdef __cplusplus
/* The kernels' why functions: the first failing check is kept, the later ones ignored (the kernel's `bad` is their OR). */
struct ClothWhyFirst {
    ClothWhy *w;
    bool hit;
    explicit ClothWhyFirst(ClothWhy *out) : w(out), hit(false) { w->what = nullptr, w->lane = -1, w->cls = CW_NONE, w->bits = 0; }
    void set(const char *what, int lane, int cls, uint32_t bits) {
        if (hit) return;
        hit = true, w->what = what, w->lane = lane, w->cls = cls, w->bits = bits;
    }
    void rule(float x, int r, const char *what, int lane) {
        if (lane == 3) return;   /* (lane 3: no check, see the top) */
        uint32_t u;
        memcpy(&u, &x, 4);
        if (!hit && !cloth_why_rule_ok(u, r)) set(what, lane, cloth_why_class(u, r), u);
    }
    void r(float x, const char *what, int lane = -1) { rule(x, CR_R, what, lane); }
    void f(float x, const char *what, int lane = -1) { rule(x, CR_F, what, lane); }
    void p(float x, const char *what, int lane = -1) { rule(x, CR_P, what, lane); }
    void plain(float x, const char *what, int lane = -1) { rule(x, CR_PLAIN, what, lane); }
    /* a check that is not a value's range: the product rounded to zero, FRSQRTS's L*e bound */
    void flag(bool bad, int cls, const char *what, int lane, float x) {
        uint32_t u;
        memcpy(&u, &x, 4);
        if (bad) set(what, lane, cls, u);
    }
};
#endif
#endif
