/* The capsule cull: which of a cloth's particles a capsule collider can touch (the particles inside the collider's
   box, and either moving or in the collider's collision group), bit-exact against the game's routine (Havok cloth's
   capsule collidable). What it computes: cloth-spec/cull/SPEC.md. This header is the interface; a candidate
   cloth_cull.cpp implements it (INTERFACE.md). */
#ifndef CLOTH_CULL_H
#define CLOTH_CULL_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* One pass over the particles. Every pointer is a host pointer to bytes laid out as the game keeps them (little-endian
   binary32 floats and integers), any alignment. The hook has checked that every range below is mapped and that `list`
   meets none of the ranges read; the kernel never has to decline. */
typedef struct ClothCullArgs {
    const uint8_t *positions;   /* n positions, 16 bytes each (x, y, z, w floats); particle i's at positions + 16 i */
    const uint8_t *masses;      /* mass form: particle i's inverse mass, a float at masses + 16 i (only those 4 bytes of
                                   each 16 are the particle's); NULL in the mask form */
    const uint8_t *masks;       /* mask form: particle i's collision mask, a 32-bit integer at masks + 4 i; NULL in the
                                   mass form */
    const uint8_t *box;         /* box test on: the box's low corner (4 floats: x, y, z, w), then its high corner (4 floats),
                                   32 bytes; NULL when the box test is off */
    uint8_t *list;              /* out: the indices of the particles taken, unsigned 16-bit each, from list + 0 */
    uint32_t n;                 /* the number of particles, 1 .. 65535 */
    uint32_t group;             /* mask form: the collider's group bits (a particle is in the group when mask & group != 0) */
    int mass_form;              /* 1: the mass form (a particle is taken when its inverse mass is not zero); 0: the mask form */
    int box_test;               /* 1: a particle must also be inside the box; 0: no box test */
    int fz;                     /* 1: flush-to-zero mode is on; 0: off (SPEC.md 2) */
} ClothCullArgs;

/* What the pass hands back (SPEC.md 5, 6). A field marked "valid when" may hold anything when its condition is false. */
typedef struct ClothCullResult {
    uint32_t count;             /* the number of particles taken (= the entries written to the list) */
    uint32_t status;            /* the float status flags the pass raises: CLOTH_CULL_IOC, CLOTH_CULL_IDC, nothing else */
    /* the values handed back at the loop's end (SPEC.md 6) */
    uint32_t inside_all[4];     /* valid when box_test and !mass_examined: the last particle's box verdict, lane by lane */
    uint32_t inside_yz[4];      /* valid when box_test: the last particle's "y and z inside" verdict, in all four lanes */
    uint32_t inside_z[4];       /* valid when box_test: the last particle's "z inside" verdict, in all four lanes */
    int mass_examined;          /* 1 when the pass ended by examining the last particle's inverse mass, else 0 */
    uint32_t last_mass;         /* valid when mass_examined: that inverse mass, its 32 bits as read */
    int word_set;               /* 1 when the pass's last-decision word was set at all, else 0 */
    uint32_t word;              /* valid when word_set: the last-decision word (SPEC.md 6) */
} ClothCullResult;

/* float status bits (the Arm's FPSR layout) */
enum {
    CLOTH_CULL_IOC = 0x01u,     /* invalid operation */
    CLOTH_CULL_IDC = 0x80u      /* input denormal (flush-to-zero mode only) */
};

/* Takes particles 0 .. n-1 in order, writes the index of each one taken to the list, and fills *r. Writes nothing but
   list[0 .. 2 * r->count) and *r. Called only with 1 <= n <= 65535. */
void cloth_cull_81b5a3c0(const ClothCullArgs *a, ClothCullResult *r);

#ifdef __cplusplus
}
#endif
#endif
