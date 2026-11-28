/* Object-space skinning by bone weights: one block of 16 cloth vertices, each a 16-bit local position blended by 1 to 4
   bone matrices with 8-bit weights, bit-exact against the game's routine (Havok's object-space skinning operator, its
   "Object Space Deform"). What it computes: cloth-spec/skin/SPEC.md. This header is the interface; a candidate
   cloth_skin.cpp implements it (INTERFACE.md). */
#ifndef CLOTH_SKIN_H
#define CLOTH_SKIN_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* What is the same for every block of a call (SPEC.md 1). Float values are given as their bit patterns. */
typedef struct ClothSkinParams {
    uint16_t pos_low[4];        /* the low 16 bits of each position lane's 32-bit integer (0 in the game) */
    uint32_t weight_scale[4];   /* the factor weight lane q is multiplied by (1/255 = 0x3b808081 in every lane in the game) */
    uint32_t fourth_weight;     /* 3-influence blocks: lane 3 of the weights before the scale, used as it is, not converted
                                   (1.0 = 0x3f800000 in the game) */
    int fz;                     /* 1: flush-to-zero mode is on; 0: off (SPEC.md 2) */
    uint32_t fpsr_in;           /* the float status flags already set when the block starts (bits: CLOTH_SKIN_*) */
} ClothSkinParams;

/* One block of 16 vertices (SPEC.md 1). Every pointer is a host pointer to bytes laid out as the game keeps them
   (little-endian; 16-byte columns of four binary32 floats, lane 0 first); any alignment. The hook has checked that every
   byte the block reads is mapped and that the block's stores meet nothing it reads. */
typedef struct ClothSkinBlock {
    int influences;             /* 4, 3, 2 or 1: the bone influences of every vertex of the block (the block's format) */
    const uint8_t *positions;   /* the 16 vertices' positions, 8 bytes each (four unsigned 16-bit values) */
    const uint8_t *record;      /* the block's record: output numbers, bone indices, weights (SPEC.md 1; readable for
                                   CLOTH_SKIN_READABLE(influences) bytes) */
    const uint8_t *bones;       /* the host address of bone number bone_base: bone k's matrix (64 bytes) is at
                                   bones + 64 * (k - bone_base); every bone the record names is >= bone_base */
    uint32_t bone_base;
    uint8_t *out;               /* the host address of output byte offset out_base: the slot at offset o is at
                                   out + (o - out_base); every slot the block stores is at an offset >= out_base */
    uint32_t out_base;
    uint32_t first;             /* the number added to every vertex's output number */
    uint32_t stride;            /* the bytes from one output slot to the next (0 .. 255) */
} ClothSkinBlock;

/* the bytes of a record of the format, and how many bytes from its start the hook guarantees readable */
#define CLOTH_SKIN_RECORD(n) ((n) == 4 ? 0xe0u : (n) == 3 ? 0xb0u : (n) == 2 ? 0x80u : 0x40u)
#define CLOTH_SKIN_READABLE(n) ((n) == 4 ? 0xe4u : (n) == 3 ? 0xb0u : (n) == 2 ? 0x86u : 0x40u)

/* The values the block's last vertex (vertex 15) hands back (SPEC.md 6): four floats each, lane 0 first, as bit
   patterns. Which fields are filled depends on the block's influence count (SPEC.md 6's table); the others are not
   looked at and may hold anything. */
typedef struct ClothSkinLast {
    uint32_t out[4];            /* the skinned position stored for the vertex */
    uint32_t xy_sum[4];         /* the sum before the z term is added */
    uint32_t x_sum[4];          /* the sum of the x term and the blended translation column */
    uint32_t y_term[4];         /* the blended column 1 times the position's y */
    uint32_t pos[4];            /* the scaled position */
    uint32_t blend2[4];         /* the blended column 2 */
    uint32_t blend3[4];         /* the blended column 3 (the translation) */
    uint32_t blend2_head[4];    /* the blended column 2 before its last influence's term is added */
    uint32_t last_col2[4];      /* column 2 of the last influence's bone matrix, as read */
    uint32_t term[4][4][4];     /* term[k][c]: column c of influence k's bone matrix times influence k's weight */
} ClothSkinLast;

/* float status bits (the Arm's FPSR layout) */
enum {
    CLOTH_SKIN_IOC = 0x01u,     /* invalid operation */
    CLOTH_SKIN_DZC = 0x02u,     /* divide by zero (never raised here) */
    CLOTH_SKIN_OFC = 0x04u,     /* overflow */
    CLOTH_SKIN_UFC = 0x08u,     /* underflow */
    CLOTH_SKIN_IXC = 0x10u,     /* inexact (always already set at a call: need not be reported) */
    CLOTH_SKIN_IDC = 0x80u      /* input denormal (flush-to-zero mode only) */
};

/* Skins the block's 16 vertices in order, stores each one's result in its output slot, fills `last` from vertex 15, and
   returns the status bits the block raises. The return value R is correct when (fpsr_in | R) == (fpsr_in | raised): bits
   already set in fpsr_in, and IXC, may be left out. Writes nothing but the 16 output slots and *last. Never declines. */
uint32_t cloth_skin_81db9a40(const ClothSkinParams *p, const ClothSkinBlock *b, ClothSkinLast *last);

#ifdef __cplusplus
}
#endif
#endif
