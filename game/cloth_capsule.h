/* The capsule collider's particle pass of the game's Havok cloth, on host code: the kernel's interface (C).
   SPEC.md says what it computes, INTERFACE.md how it is called. Two entry points: the particles in batches of 16, and the
   particles left over one at a time. Each writes only the particles' positions and previous positions. */
#ifndef CLOTH_CAPSULE_H
#define CLOTH_CAPSULE_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* The call's constants, as float bits (lanes x, y, z, w of a vector). */
typedef struct ClothCapsule {
    uint32_t a[4], b[4];       /* the capsule's two end points A and B, four lanes each (lane w is used too) */
    uint32_t radius;           /* the capsule's radius */
    uint32_t inv_len2;         /* the segment's 1 / |B - A|^2, as given (never computed by the kernel) */
    uint32_t eps[4];           /* batches only: the small value added to a squared distance, by a particle's place in its
                                  group of four (place 0..3) */
    uint32_t one[4];           /* batches only: the upper bound of the segment parameter, by place in the group of four */
    uint32_t dt;               /* the time step */
    uint32_t centre[4];        /* the collider body's centre (lanes x, y, z read) */
    uint32_t lin_vel[4];       /* the collider body's linear velocity (all four lanes read) */
    uint32_t ang_vel[4];       /* the collider body's angular velocity (lanes x, y, z read) */
    int fz;                    /* 1: the float mode flushes subnormals to zero; 0: it does not */
} ClothCapsule;

/* why a pass stopped before its end (`why`) */
enum { CLOTH_CAP_ALL = 0, CLOTH_CAP_VALUES = 1, CLOTH_CAP_INDEX = 2 };

/* The particles in batches of 16 (SPEC.md 3 and 4). */
typedef struct ClothCapBatches {
    const ClothCapsule *k;
    const uint8_t *indices;    /* batch 0's 16 particle indices, u16 each (32 bytes a batch, batch i at indices + 32 i) */
    const uint8_t *radii;      /* batch 0's 16 particle radii, float each (64 bytes a batch) */
    const uint8_t *frictions;  /* batch 0's 16 friction factors, float each (64 bytes a batch) */
    uint8_t *pos;              /* the positions: particle p's 16 bytes (x, y, z, w floats) at pos + 16 p */
    uint8_t *prev;             /* the previous positions, the same layout */
    uint32_t n;                /* batches to do (at least 1) */
    uint32_t done;             /* out: batches done, each whole, from batch 0 on */
    uint32_t why;              /* out: CLOTH_CAP_ALL when done == n, else why batch `done` was left */
    uint16_t *contacts;        /* in: NULL, or room for n entries; out (when not NULL): for each batch done, bit j set
                                  when the batch's particle j was in contact */
} ClothCapBatches;

/* The particles left over, one at a time (SPEC.md 3 and 5). */
typedef struct ClothCapSingles {
    const ClothCapsule *k;
    const uint8_t *indices;    /* the particle indices, u16 each */
    uint8_t *pos;              /* as above */
    uint8_t *prev;             /* as above */
    const uint8_t *data;       /* the particles' own data: particle p's 16 bytes at data + 16 p, its radius (float) at
                                  +8 and its friction factor (float) at +12 */
    uint32_t n;                /* particles to do (at least 1) */
    uint32_t done;             /* out: particles done, from the first on */
    uint32_t why;              /* out: CLOTH_CAP_ALL when done == n, else why particle `done` was left */
    uint32_t contacts;         /* out: how many of the particles done were in contact */
} ClothCapSingles;

void cloth_capsule_batches(ClothCapBatches *b);
void cloth_capsule_singles(ClothCapSingles *s);

#ifdef __cplusplus
}
#endif
#endif
