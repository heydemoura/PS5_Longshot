/* The sphere contact pass of the cloth: every record of 16 particles of one call, pushed out of a moving sphere, with
   the friction step on their previous positions (SPEC.md). C interface, C++ implementation (cloth_sphere.cpp). */
#ifndef CLOTH_SPHERE_H
#define CLOTH_SPHERE_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

typedef struct ClothSphereArgs {
    const uint8_t *indices;    /* records x 16 particle numbers (unsigned 16-bit); record r's k-th at indices + 32 r + 2 k */
    const uint8_t *radii;      /* records x 16 floats: record r's k-th particle's contact radius at radii + 64 r + 4 k */
    const uint8_t *frictions;  /* records x 16 floats, the same layout: each particle's friction factor */
    uint8_t *positions;        /* particle i's position: four floats (x, y, z, w) at positions + 16 i */
    uint8_t *previous;         /* particle i's previous position: four floats at previous + 16 i */
    uint32_t records;          /* the number of records, 1 or more */
    uint32_t fpcr, fpsr;       /* the float control and the cumulative float status at the call (Arm bit layout) */
    uint32_t sphere[4];        /* the sphere's centre x, y, z and its radius (float bits) */
    uint32_t eps[4];           /* the small constant added to a squared distance, one per place 0..3 in a group (float bits) */
    uint32_t dt;               /* the time step (float bits) */
    uint32_t body[3];          /* the point of the sphere's body its angular velocity turns about: x, y, z (float bits) */
    uint32_t velocity[4];      /* the body's linear velocity, four lanes (float bits; the fourth lane is used) */
    uint32_t spin[3];          /* the body's angular velocity x, y, z (float bits) */
} ClothSphereArgs;

enum { CLOTH_SPHERE_ALL = 0, CLOTH_SPHERE_FLOAT_MODE = 1, CLOTH_SPHERE_VALUES = 2, CLOTH_SPHERE_REPEATED = 3 };

typedef struct ClothSphereResult {
    uint32_t done;   /* records done, 0..records: records 0 .. done-1 are stored, nothing of record `done` or later */
    uint32_t why;    /* CLOTH_SPHERE_ALL when done == records, else why record `done` was not done */
} ClothSphereResult;

/* Does records 0, 1, ... in order (each sees the stores of the ones before it) and stops at the first one it declines;
   writes only the position and previous-position slots SPEC.md names; fills *r. Thread-safe, no state kept. */
void cloth_sphere_records(const ClothSphereArgs *a, ClothSphereResult *r);

#ifdef __cplusplus
}
#endif
#endif
