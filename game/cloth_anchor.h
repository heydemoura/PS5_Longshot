/* The anchor pass: the C interface of its host kernel (see INTERFACE.md; what it computes is SPEC.md).

   One call answers one whole call of the game's routine, or declines it whole. A done call has written guest memory
   exactly as the routine writes it (the particle slots) and reports the float status bits the routine raises; a
   declined call has written nothing, and the caller then runs the game's own routine. */
#ifndef CLOTH_ANCHOR_H
#define CLOTH_ANCHOR_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* The guest cell (8 bytes) that holds the address of the solver's constant table (SPEC.md 1.5): a data cell of the game */
#define CLOTH_ANCHOR_CONSTANTS_CELL 0x84ca4680ull

/* The host pointer of guest bytes [addr, addr + len), or NULL when they are not all mapped. Asking reads nothing. Guest
   memory is one flat mapping: for ranges inside one mapped range the host pointers differ as the guest addresses do. */
typedef void *(*ClothAnchorRange)(uint64_t addr, uint64_t len);

/* Why a call was declined (ClothAnchorResult.why) */
enum {
    CLOTH_ANCHOR_DONE = 0,    /* (not declined) */
    CLOTH_ANCHOR_RANGE = 1,   /* a range the routine reads or writes is not mapped */
    CLOTH_ANCHOR_COUNT = 2,   /* the record count taken is negative */
    CLOTH_ANCHOR_VALUE = 3,   /* a value the kernel cannot answer with the game's exact bits and status flags */
    CLOTH_ANCHOR_ALIAS = 4,   /* a store could reach data the kernel holds from an earlier read */
    CLOTH_ANCHOR_LIMIT = 5,   /* more records than the kernel's own storage holds */
    CLOTH_ANCHOR_OTHER = 6
};

typedef struct ClothAnchorCall {
    uint64_t constraint;      /* the routine's 1st argument: the anchor constraint object (guest address) */
    uint64_t slots;           /* 2nd: particle slot 0 (guest address; slot i is 16 bytes at slots + 16 i) */
    uint64_t skin;            /* 4th: the skin object (guest address) */
    uint64_t factor;          /* 5th: the time factor (guest address of a float) */
    uint64_t scale;           /* 6th: the scale object (guest address), or 0 for none */
    int fz;                   /* 1 when the call runs with flush-to-zero on, else 0 (the only float control bit allowed) */
    uint32_t fpsr;            /* the guest's float status at the call (Inexact, bit 4, is always set) */
    ClothAnchorRange range;   /* resolves guest ranges to host pointers */
} ClothAnchorCall;

typedef struct ClothAnchorResult {
    uint32_t fpsr;            /* status bits the call raised, ORed into the guest's by the caller (a bit already set
                                 in call->fpsr may be left out: ORing it again changes nothing) */
    uint32_t records;         /* records done (the count of the list taken; 0 when both lists are empty) */
    uint32_t slot_lo, slot_hi;/* the lowest and highest slot index stored; slot_lo > slot_hi when nothing was stored */
    int why;                  /* CLOTH_ANCHOR_DONE, or the reason of a decline */
} ClothAnchorResult;

/* Returns 1 when the call is done (guest memory written, *res filled), 0 when it is declined (nothing written
   anywhere, res->why set). Leaves the host's float environment (MXCSR) as it found it. */
int cloth_anchor_81c7bd00(const ClothAnchorCall *call, ClothAnchorResult *res);

#ifdef __cplusplus
}
#endif
#endif
