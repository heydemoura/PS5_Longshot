/* Particle bounding boxes: three boxes over a cloth instance's particles, bit-exact against the game's routine (Havok's
   hclSimClothInstance::updateAabbs, its loop over the particles). What it computes: cloth-spec/aabb/SPEC.md. This
   header is the interface; a candidate cloth_aabb.cpp implements it (INTERFACE.md). */
#ifndef CLOTH_AABB_H
#define CLOTH_AABB_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* One pass over the particles. Every pointer is a host pointer to bytes laid out as the game keeps them (little-endian
   binary32 floats, 16-byte points, four lanes, lane 0 at the lowest address). The hook has checked that every range
   below is mapped; nothing is written to memory, so the kernel never has to decline. */
typedef struct ClothAabbArgs {
    const uint8_t *pos;       /* the particles' positions P: 16 bytes a particle, particle i's at pos + 16 i */
    const uint8_t *prev;      /* their previous positions Q: the same layout */
    const uint8_t *mask;      /* their collision masks: one 32-bit word a particle, particle i's at mask + 4 i */
    const uint8_t *inv_mass;  /* their inverse masses: one float a particle, 16 bytes apart, particle i's at
                                 inv_mass + 16 i (only the first 4 bytes of each 16 belong to it) */
    uint32_t n;               /* the number of particles, at least 1 */
    uint32_t start[4];        /* S: the boxes' start value, four lanes, as bit patterns (SPEC.md 3) */
    int fz;                   /* 1: flush-to-zero mode is on; 0: off (SPEC.md 2) */
    uint32_t fpsr_in;         /* the float status flags already set when the pass starts (bits: CLOTH_AABB_*) */
} ClothAabbArgs;

/* What the pass hands back (SPEC.md 6). Every four-lane value is four bit patterns, lane 0 first. */
typedef struct ClothAabbResult {
    uint32_t pos_min[4], pos_max[4];         /* the box of every particle's position */
    uint32_t moving_min[4], moving_max[4];   /* the box of the tracked particles' two predicted points */
    uint32_t marked_min[4], marked_max[4];   /* the same over the tracked particles whose mask has bit 31 set */
    uint32_t last_pos[4];                    /* the last particle's position, as read */
    uint32_t last_mask;                      /* the last particle's mask word */
    uint32_t last_mid[4];                    /* the last particle's midpoint, or its inverse mass if it was skipped */
    uint32_t moved;                          /* 1 when at least one particle was tracked, else 0 */
    uint32_t ahead[4];                       /* the last tracked particle's point ahead (looked at only when moved) */
    uint32_t ahead_step[4];                  /* the last tracked particle's step to it (looked at only when moved) */
} ClothAabbResult;

/* float status bits (the Arm's FPSR layout) */
enum {
    CLOTH_AABB_IOC = 0x01u,   /* invalid operation */
    CLOTH_AABB_DZC = 0x02u,   /* divide by zero (never raised here) */
    CLOTH_AABB_OFC = 0x04u,   /* overflow */
    CLOTH_AABB_UFC = 0x08u,   /* underflow */
    CLOTH_AABB_IXC = 0x10u,   /* inexact (always already set when the pass starts: need not be reported) */
    CLOTH_AABB_IDC = 0x80u    /* input denormal (flush-to-zero mode only) */
};

/* Does the whole pass over particles 0 .. n-1, fills *r as SPEC.md 6 says and returns the status bits the pass raises.
   The hook ORs the return value into the status register: the return value R is correct when
   (fpsr_in | R) == (fpsr_in | raised), so bits already set in fpsr_in, and IXC, may be left out. Writes nothing but *r.
   Called only with n >= 1. */
uint32_t cloth_aabb_81b38800(const ClothAabbArgs *a, ClothAabbResult *r);

#ifdef __cplusplus
}
#endif
#endif
