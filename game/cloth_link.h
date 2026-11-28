/* The cloth link (stretch) pass: one pass of two-particle distance constraints over the cloth's particles, link by link
   (Gauss-Seidel: each link reads the particle positions that earlier links of the same pass may have just moved).
   SPEC.md says what is computed; INTERFACE.md how this entry point is called and what it may read and write.

   The kernel does the links from the first, in order, while it can give the game's exact bits and float status
   (SPEC.md section 4), and stops before the first link it cannot: that link and the ones after it are then done by the
   game's own loop. A link is either done whole (its two positions stored) or not touched at all. When every link is
   done, the kernel also hands back what the last link leaves behind (SPEC.md section 5), which the rest of the game's
   function may read after the pass. */
#ifndef CLOTH_LINK_PASS_H
#define CLOTH_LINK_PASS_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* why the kernel stopped (ClothLinkPass.why) */
enum {
    CLOTH_LINK_ALL = 0,      /* every link done (done == n) */
    CLOTH_LINK_ENTRY = 1,    /* the call's scale is outside what the kernel takes (SPEC.md 4.1): done = 0 */
    CLOTH_LINK_CLASS = 2,    /* link `done` has a value outside the exactness class (SPEC.md 4.2) */
    CLOTH_LINK_STEP = 3,     /* link `done`: its reciprocal square root step is outside its proven range (SPEC.md 4.3) */
    CLOTH_LINK_OTHER = 4     /* any other reason the kernel cannot give link `done` exactly (say which in a comment) */
};

/* What the last link of the pass leaves behind (SPEC.md section 5). Float values as their bits. */
typedef struct ClothLinkEnd {
    uint32_t a, b;               /* the last link's two particle numbers (its first and second 16-bit field) */
    uint32_t inv_len;            /* the last link's reciprocal length r (SPEC.md 2.2 step 3): +0 when its tiny test failed */
    uint32_t inv_mass_a;         /* the inverse mass of particle a as the last link read it */
    uint32_t inv_mass_b;         /* the inverse mass of particle b as the last link read it */
    uint32_t stiffness;          /* the last link's stiffness k, as read from its record (SPEC.md 1.2) */
    uint32_t pos_a[4];           /* slot a as the last link read it, before its stores (lanes x, y, z, w) */
    uint32_t pos_b_again[4];     /* slot b as the last link read it AGAIN, after storing slot a (SPEC.md 2.1) */
    uint32_t pos_b_new[4];       /* slot b as the last link stored it */
} ClothLinkEnd;

typedef struct ClothLinkPass {
    /* ---- in (host pointers: resolved and range-checked by the hook) ---- */
    uint8_t *slots;              /* particle positions: 16-byte slots of four floats (x, y, z, w), indexed by the links'
                                    16-bit particle numbers (slot s at slots + 16 * s); read and written */
    const uint8_t *particles;    /* particle data, 16 bytes a particle, the same numbering; only the float at +4 (the
                                    particle's inverse mass) is read. May be the same memory as `slots` or lie inside it. */
    const uint8_t *links;        /* n link records of 12 bytes (SPEC.md 1.2); may lie inside the slots' memory */
    uint32_t n;                  /* the number of links, 1 .. 2^31 - 1 */
    uint32_t scale;              /* the solve's scale s (float bits), the same for every link of the call */
    int fz;                      /* 1 when the call runs with flush to zero (the Arm FPCR.FZ bit), else 0 */
    uint32_t fpsr_in;            /* the float status flags already set when the pass starts (Arm FPSR bit layout:
                                    IOC 0x01, DZC 0x02, OFC 0x04, UFC 0x08, IXC 0x10, IDC 0x80); IXC is always set */
    /* ---- out ---- */
    uint32_t done;               /* links done and stored, 0..n; link `done` (when < n) was not touched */
    uint32_t fpsr;               /* the float status flags the pass raises over those links, IXC aside (it is set
                                    already); the hook ORs them into the guest's. May include flags already in fpsr_in.
                                    Ignored when done == 0. */
    uint32_t slot_lo, slot_hi;   /* the lowest and the highest slot number stored (slot_lo > slot_hi: none) */
    int why;                     /* CLOTH_LINK_* : CLOTH_LINK_ALL when done == n, else why it stopped */
    ClothLinkEnd end;            /* written when done == n (SPEC.md 5); not read otherwise */
} ClothLinkPass;

/* Do the pass (as many links as can be given exactly, from the first). Writes guest memory only through `slots`, and
   only the two slots of each link it does. */
void cloth_link_pass(ClothLinkPass *p);

#ifdef __cplusplus
}
#endif
#endif
