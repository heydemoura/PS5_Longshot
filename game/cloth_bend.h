/* The cloth bending pass: one pass of four-particle bending constraints over the cloth's particles, record by record
   (Gauss-Seidel: each record reads the particle positions that earlier records of the same pass may have just moved).
   SPEC.md says what is computed; INTERFACE.md how this entry point is called and what it may read and write.

   The kernel does the records from the first, in order, while it can prove that the host's arithmetic gives the game's
   exact bits and float status (SPEC.md section 4), and stops before the first record it cannot: that record and the
   ones after it are then done by the game's own routine. A record is either done whole (its four positions stored) or
   not touched at all. */
#ifndef CLOTH_BEND_PASS_H
#define CLOTH_BEND_PASS_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* why the kernel stopped (ClothBendPass.why when done < n) */
enum {
    CLOTH_BEND_ALL = 0,      /* every record done (done == n) */
    CLOTH_BEND_ENTRY = 1,    /* a value read once for the whole call is outside what the kernel takes (SPEC.md 4.1): done = 0 */
    CLOTH_BEND_CLASS = 2,    /* record `done` has a value outside the exactness class (SPEC.md 4.2) */
    CLOTH_BEND_STEP = 3,     /* record `done`: a reciprocal square root step outside its proven range (SPEC.md 4.3) */
    CLOTH_BEND_OTHER = 4     /* any other reason the kernel cannot prove record `done` (say which in a comment) */
};

typedef struct ClothBendPass {
    /* ---- in (host pointers: resolved and range-checked by the hook) ---- */
    uint8_t *slots;              /* particle positions: 16-byte slots of four floats (x, y, z, w), indexed by the records'
                                    16-bit slot numbers (slot s at slots + 16 * s); read and written */
    const uint8_t *particles;    /* particle data, 16 bytes a particle, the same indexing; only the float at +4 (the
                                    particle's inverse mass) is read. May be the same memory as `slots`. */
    const uint8_t *records;      /* n records of 32 bytes (SPEC.md 1.2); may lie inside the slots' memory */
    const uint8_t *scale;        /* one float, the time-step scale k: read anew for every record (a slot store of an
                                    earlier record may have changed it) */
    uint32_t n;                  /* the number of records, >= 1 */
    uint32_t threshold[2];       /* the bend threshold pair (float bits), read by the routine once at entry */
    uint32_t numerator[2];       /* the numerator pair (float bits; normally 1.0, 1.0), read once at entry */
    uint32_t alt_stiffness[2];   /* the stiffness used past the threshold (float bits; normally 0, 0), read once at entry */
    int fz;                      /* 1 when the call runs with flush to zero (the Arm FPCR.FZ bit), else 0 */
    uint32_t fpsr_in;            /* the float status flags already set when the call starts (Arm FPSR bit layout:
                                    IOC 0x01, DZC 0x02, OFC 0x04, UFC 0x08, IXC 0x10, IDC 0x80); IXC is always set */
    /* ---- out ---- */
    uint32_t done;               /* records done and stored, 0..n; record `done` (when < n) was not touched */
    uint32_t fpsr;               /* the float status flags the routine raises over those records, IXC aside (it is
                                    set already); the hook ORs them into the guest's. May include flags already in
                                    fpsr_in. Ignored when done == 0. */
    uint32_t slot_lo, slot_hi;   /* the lowest and the highest slot number stored (slot_lo > slot_hi: none) */
    int why;                     /* CLOTH_BEND_* : CLOTH_BEND_ALL when done == n, else why it stopped */
} ClothBendPass;

/* Do the pass (as many records as can be proven, from the first). Writes guest memory only through `slots`, and only
   the four slots of each record it does. */
void cloth_bend_pass(ClothBendPass *p);

#ifdef __cplusplus
}
#endif
#endif
