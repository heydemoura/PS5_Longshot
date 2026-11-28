/* The Verlet integration step of a cloth's particles, bit-exact against the game's routine (Havok cloth's simulate
   operator, its integration task). What it computes: cloth-spec/verlet/SPEC.md. This header is the interface; a
   candidate cloth_verlet.cpp implements it (INTERFACE.md). */
#ifndef CLOTH_VERLET_H
#define CLOTH_VERLET_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* The values handed back at the loop's end (SPEC.md 5): each a column of four lanes as bit patterns (lane 0 first),
   except `single_scale_b`, one value. The "last group" is particles 4 (groups - 1) + k, k = 0..3; its fields are
   written only when groups >= 1. The "last single" is particle 4 groups + singles - 1; its fields are written only when
   singles >= 1. Fields the call does not write keep whatever they held. */
typedef struct ClothVerletLast {
    uint32_t group_old[4][4];       /* [k]: the position particle k of the last group had before the step (P) */
    uint32_t group_moved[4][4];     /* [k]: its moved position M = P + E */
    uint32_t group_new[2][4];       /* [k - 2] for k = 2, 3: the new position P' */
    uint32_t group_push[2][4];      /* [k - 2] for k = 2, 3: the scaled force F = (A + G * s_a) * s_b */
    uint32_t group_damped[2][4];    /* [k - 2] for k = 2, 3: the damped velocity E = D * (P - Q) */
    uint32_t single_new[4];         /* the last single's new position P' */
    uint32_t single_moved[4];       /* its moved position M */
    uint32_t single_old[4];         /* its position before the step P */
    uint32_t single_force[4];       /* its force A as read */
    uint32_t single_scale_b;        /* its second scale factor s_b as read */
} ClothVerletLast;

/* One call: the whole loop. Every pointer is a host pointer to bytes laid out as the game keeps them (little-endian
   binary32 floats, 16-byte columns, any alignment). The hook has checked that every range below is mapped and that the
   two written arrays meet neither each other nor anything read; the kernel never declines. */
typedef struct ClothVerletArgs {
    uint8_t *pos;               /* the positions, 16 bytes a particle (four floats): read, then the new position written */
    uint8_t *prev;              /* the previous positions, 16 bytes a particle: overwritten with the position before the step */
    const uint8_t *force;       /* the force gathered for each particle, 16 bytes a particle (four floats) */
    const uint8_t *scale;       /* each particle's two scale factors, 16 bytes a particle: s_a at +0, s_b at +4 (floats);
                                   the other 8 bytes are not used, and for the last particle they need not be readable */
    const uint8_t *gravity;     /* G: one column of four floats, the same for every particle */
    uint32_t groups;            /* the particles are done as `groups` groups of four, then ... */
    uint32_t singles;           /* ... `singles` (0..3) one at a time: N = 4 groups + singles particles, in order */
    uint32_t dt2;               /* h: the squared time step, binary32 bits (one value for every lane) */
    uint32_t damping[4];        /* D: the damping factor of each lane, binary32 bits */
    int fz;                     /* 1: flush-to-zero mode is on; 0: off (SPEC.md 2) */
    uint32_t fpsr_in;           /* the float status flags already set when the loop starts (bits: CLOTH_VL_*) */
    ClothVerletLast *last;      /* out: the values handed back (SPEC.md 5); meets nothing else */
} ClothVerletArgs;

/* float status bits (the Arm's FPSR layout) */
enum {
    CLOTH_VL_IOC = 0x01u,   /* invalid operation */
    CLOTH_VL_DZC = 0x02u,   /* divide by zero (never raised here) */
    CLOTH_VL_OFC = 0x04u,   /* overflow */
    CLOTH_VL_UFC = 0x08u,   /* underflow */
    CLOTH_VL_IXC = 0x10u,   /* inexact (always already set at a call: need not be reported) */
    CLOTH_VL_IDC = 0x80u    /* input denormal (flush-to-zero mode only) */
};

/* Steps every particle, writes `pos` and `prev` (16 N bytes each) and the hand-back at `last`, and returns the status
   bits the loop raises. The hook ORs the return value into the status register: the return value R is correct when
   (fpsr_in | R) == (fpsr_in | raised), so bits already set in fpsr_in, and IXC, may be left out. Writes nothing else.
   Called only with N >= 1. */
uint32_t cloth_verlet_81c66a60(const ClothVerletArgs *a);

#ifdef __cplusplus
}
#endif
#endif
