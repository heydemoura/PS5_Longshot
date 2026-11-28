/* The cloth bend link pass: one pass of minimum / maximum distance constraints ("bend links") over the cloth's particles,
   link by link (Gauss-Seidel: each link reads the particle positions that earlier links of the same pass may have just
   moved). SPEC.md says what is computed; INTERFACE.md how this entry point is called and what it may read and write.

   The kernel does the links from the first, in order, while it can prove that the host's arithmetic gives the game's
   exact bits and float status (SPEC.md section 4), and stops before the first link it cannot: that link and the ones
   after it are then done by the game's own code. A link is either done whole (its two positions stored) or not touched
   at all. When it does every link, it also hands back the values of SPEC.md section 5. */
#ifndef CLOTH_BEND_LINK_PASS_H
#define CLOTH_BEND_LINK_PASS_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* why the kernel stopped (ClothBendLinkPass.why) */
enum {
    CLOTH_BEND_LINK_ALL = 0,     /* every link done (done == n) */
    CLOTH_BEND_LINK_ENTRY = 1,   /* a value given once for the whole pass is outside what the kernel takes (SPEC.md 4.1):
                                    done = 0 */
    CLOTH_BEND_LINK_CLASS = 2,   /* link `done` has a value outside the exactness class (SPEC.md 4.2) */
    CLOTH_BEND_LINK_STEP = 3,    /* link `done`: the reciprocal square root step outside its proven range (SPEC.md 4.3) */
    CLOTH_BEND_LINK_OTHER = 4    /* any other reason the kernel cannot prove link `done` (say which in a comment) */
};

/* The values handed back at the loop's end (SPEC.md 5): all of the LAST link (link n - 1), float bits as computed or
   read. Written only when done == n. */
typedef struct ClothBendLinkEnd {
    uint32_t p1, p2;             /* the last link's two slot numbers (its first and its second particle) */
    uint32_t p2_new[4];          /* P2': the last link's second particle's new position as stored (x, y, z, w) */
    uint32_t p2_step[4];         /* dv2: the move subtracted from the second particle's position (x, y, z, w) */
    uint32_t m2;                 /* the second particle's inverse mass, as read */
    uint32_t r;                  /* the reciprocal length r (+0 when the link's squared length failed the tiny test) */
    uint32_t push[2];            /* the push term pa = MAX(c, lo - len) * k_lo, both lanes */
    uint32_t k_lo, k_hi;         /* the last link's two stiffnesses, as read */
} ClothBendLinkEnd;

typedef struct ClothBendLinkPass {
    /* ---- in (host pointers: resolved and range-checked by the hook) ---- */
    uint8_t *slots;              /* particle positions: 16-byte slots of four floats (x, y, z, w), indexed by the links'
                                    16-bit slot numbers (slot s at slots + 16 * s); read and written */
    const uint8_t *particles;    /* particle data, 16 bytes a particle, the same indexing; only the float at +4 (the
                                    particle's inverse mass) is read. May be the same memory as `slots`. */
    const uint8_t *links;        /* n links of 20 bytes (SPEC.md 1.2); may lie inside the slots' memory */
    uint32_t n;                  /* the number of links, >= 1 */
    uint32_t clamp[2];           /* c: the clamp pair (float bits), given once for the pass (normally +0, +0) */
    uint32_t scale;              /* s: the pass's scale (float bits; > 0 or a NaN: the pass is not run otherwise) */
    int fz;                      /* 1 when the pass runs with flush to zero (the Arm FPCR.FZ bit), else 0 */
    uint32_t fpsr_in;            /* the float status flags already set when the pass starts (Arm FPSR bit layout:
                                    IOC 0x01, DZC 0x02, OFC 0x04, UFC 0x08, IXC 0x10, IDC 0x80); IXC is always set */
    /* ---- out ---- */
    uint32_t done;               /* links done and stored, 0..n; link `done` (when < n) was not touched */
    uint32_t fpsr;               /* the float status flags the pass raises over those links, IXC aside (it is set
                                    already); the hook ORs them into the guest's. May include flags already in
                                    fpsr_in. Ignored when done == 0. */
    uint32_t slot_lo, slot_hi;   /* the lowest and the highest slot number stored (slot_lo > slot_hi: none) */
    int why;                     /* CLOTH_BEND_LINK_* : CLOTH_BEND_LINK_ALL when done == n, else why it stopped */
    ClothBendLinkEnd end;        /* SPEC.md 5; written when done == n (and only then looked at) */
} ClothBendLinkPass;

/* Do the pass (as many links as can be proven, from the first). Writes guest memory only through `slots`, and only the
   two slots of each link it does. */
void cloth_bend_link_pass(ClothBendLinkPass *p);

#ifdef __cplusplus
}
#endif
#endif
