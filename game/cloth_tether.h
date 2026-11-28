/* The tether pass: the C interface of its host kernel (see INTERFACE.md; what it computes is SPEC.md).

   One call does the records of one tether pass in order, from the first, for as many records as it can give the game's
   exact bits and status bits: all of them (the usual case), or the first `done` of them, stopping at a record boundary.
   The caller runs the game's own loop for the records from `done` on, on the memory the kernel left. When every record
   is done, the kernel also hands back the values of the last record that the rest of the game's routine can use
   (ClothTetherLast, SPEC.md section 5). */
#ifndef CLOTH_TETHER_H
#define CLOTH_TETHER_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* Why the kernel stopped before the last record (ClothTetherResult.why) */
enum {
    CLOTH_TETHER_DONE = 0,    /* (every record done) */
    CLOTH_TETHER_VALUE = 1,   /* a value for which the kernel cannot give the routine's exact bits or status bits */
    CLOTH_TETHER_OTHER = 2    /* anything else */
};

typedef struct ClothTetherCall {
    uint8_t *slots;           /* host pointer of particle slot 0: slot k is the vector (16 bytes) at slots + 16 k, for any
                                 k from 0 to 65535 (the whole 1 MB is mapped); the only bytes the kernel writes */
    const uint8_t *records;   /* host pointer of record 0: record k is the 12 bytes at records + 12 k, k < count (mapped).
                                 May lie inside the slots: one flat mapping, so it overlaps them as in the guest */
    uint32_t count;           /* the number of records, at least 1 and below 2^31 */
    uint32_t scale;           /* the pass's scale factor `s` (the bits of a binary32; SPEC.md 1.3) */
    int fz;                   /* 1 when flush-to-zero is on for this call, else 0 (the only float control bit allowed) */
    uint32_t fpsr;            /* the guest's float status at the call (Inexact, bit 4, is always set) */
} ClothTetherCall;

/* The last record's values (SPEC.md section 5); valid only when done == count. Bits of binary32 values; vectors lane x
   first. */
typedef struct ClothTetherLast {
    uint32_t i;               /* its anchor particle (the record's first index) */
    uint32_t j;               /* its pulled particle (the record's second index) */
    uint32_t moved[4];        /* P': the vector it stored into slot j */
    uint32_t delta[4];        /* DELTA: the move it added to slot j's vector */
    uint32_t pull;            /* g: the factor the scaled direction was multiplied by */
    uint32_t len;             /* len: the distance as the pass computes it */
    uint32_t rlen;            /* r: the reciprocal length (+0 when the squared length was not above the threshold) */
    uint32_t stiffness;       /* its stiffness, as read */
} ClothTetherLast;

typedef struct ClothTetherResult {
    uint32_t done;            /* records done, 0 .. count: records 0 .. done - 1 are stored, nothing of the others */
    uint32_t fpsr;            /* status bits raised by the records done, ORed into the guest's by the caller (a bit
                                 already set in call->fpsr may be left out) */
    uint32_t slot_lo, slot_hi;/* the lowest and highest slot index stored; slot_lo > slot_hi when nothing was stored */
    int why;                  /* CLOTH_TETHER_DONE when done == count, else why the kernel stopped at record `done` */
    ClothTetherLast last;     /* when done == count */
} ClothTetherResult;

/* Does the records from the first on (see above) and fills *res. Leaves the host's float environment (MXCSR) as it
   found it. */
void cloth_tether_81c88940(const ClothTetherCall *call, ClothTetherResult *res);

#ifdef __cplusplus
}
#endif
#endif
