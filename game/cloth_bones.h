/* Mesh Bone Deform: bone frames from triangles of cloth particles, bit-exact against the game's routine (Havok's
   bone-space deformer). What it computes: cloth-spec/bones/SPEC.md. This header is the interface; a candidate
   cloth_bones.cpp implements it (INTERFACE.md). */
#ifndef CLOTH_BONES_H
#define CLOTH_BONES_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* What the loop leaves behind after its last bone (bone count - 1), handed back for the rest of the game's routine
   (SPEC.md 6). Every field is the bit pattern of a binary32 value (or a 16-bit index, zero-extended). The vectors are
   four lanes, lane 0 first; every lane counts. */
typedef struct ClothBonesLast {
    uint32_t origin[4];       /* the stored fourth column: (t.x, t.y, t.z, T) (SPEC.md 3, step 9) */
    uint32_t unit_z[4];       /* Z * inv_len_z, all four lanes (step 10) */
    uint32_t unit_y[4];       /* Y * inv_len_y, all four lanes */
    uint32_t unit_x[4];       /* X * inv_len_x, all four lanes */
    uint32_t axis_z[4];       /* Z, all four lanes (step 5) */
    uint32_t axis_y[4];       /* Y, all four lanes (lane 3 is the +0 put there) (step 6) */
    uint32_t axis_x[4];       /* X, all four lanes (lane 3 is the +0 put there) (step 7) */
    uint32_t inv_len_z;       /* the three scale factors (step 8) */
    uint32_t inv_len_y;
    uint32_t inv_len_x;
    uint32_t origin_coords[4];/* the last bone's record column 3, its 16 bytes as read */
    uint32_t chain_a;         /* the two estimate-chain values of SPEC.md 6 */
    uint32_t chain_b;
    uint32_t tri_second;      /* the last bone's triangle: its second particle index (u16, zero-extended) */
    uint32_t tri_third;       /* and its third */
} ClothBonesLast;

/* One run of the loop. Every pointer is a host pointer to bytes laid out as the game keeps them (little-endian; floats
   binary32; any alignment). The hook has checked that every range the loop reads or writes is mapped and that the
   output meets nothing the loop reads. */
typedef struct ClothBonesArgs {
    const uint8_t *positions;  /* particle k's position (four floats, 16 bytes) at positions + 16 * (k - pos_first) */
    uint32_t pos_first;        /* the lowest particle index any triangle the pairs name holds */
    const uint8_t *triangles;  /* the triangle at byte offset o (three u16 particle indices, 6 bytes) is at
                                  triangles + (o - tri_first) */
    uint32_t tri_first;        /* the lowest triangle byte offset any pair holds */
    const uint8_t *pairs;      /* count pairs of two u16: (output byte offset, triangle byte offset), 4 bytes each */
    const uint8_t *records;    /* count records of 64 bytes: four columns of four floats; column 1 is never read */
    uint8_t *out;              /* bone i's 64 bytes go to out + (its output byte offset - out_first) */
    uint32_t out_first;        /* the lowest output byte offset any pair holds */
    uint32_t count;            /* the number of bones, at least 1 */
    uint32_t centre_weight[4]; /* C: four floats (bits) multiplying the corner sum (SPEC.md 3, step 1) */
    uint32_t t;                /* T: one float (bits): the fourth lane of the frame's centre column and of the stored origin */
    int fz;                    /* 1: flush-to-zero mode is on; 0: off (SPEC.md 2) */
    uint32_t fpsr_in;          /* the float status flags already set when the loop starts (bits: CLOTH_BN_*) */
    ClothBonesLast *last;      /* out: what the loop leaves after its last bone (SPEC.md 6) */
} ClothBonesArgs;

/* float status bits (the Arm's FPSR layout) */
enum {
    CLOTH_BN_IOC = 0x01u,   /* invalid operation */
    CLOTH_BN_DZC = 0x02u,   /* divide by zero (the reciprocal square root estimate of a zero) */
    CLOTH_BN_OFC = 0x04u,   /* overflow */
    CLOTH_BN_UFC = 0x08u,   /* underflow */
    CLOTH_BN_IXC = 0x10u,   /* inexact (always already set at a call: need not be reported) */
    CLOTH_BN_IDC = 0x80u    /* input denormal (flush-to-zero mode only) */
};

/* Computes every bone in order, writes each bone's 64 bytes and *last, and returns the status bits the loop raises.
   The hook ORs the return value into the status register: the return value R is correct when
   (fpsr_in | R) == (fpsr_in | raised), so bits already set in fpsr_in, and IXC, may be left out. Writes nothing but the
   bones' 64-byte outputs and *last. Called only with count >= 1; never declines. */
uint32_t cloth_bones_81ce6500(const ClothBonesArgs *a);

#ifdef __cplusplus
}
#endif
#endif
