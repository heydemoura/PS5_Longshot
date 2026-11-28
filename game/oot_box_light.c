/* A recycled box light starts with the constructor's parameters. A placed light
   without a parameter resource otherwise keeps the previous owner's size and
   dimming at its new position. light.boxAcquired runs while the pool and light
   locks are held, before the new owner can load its own parameters. */
#include "game_host.h"

/* ---- build e0f3ccfa's addresses (the site and object layout belong to this build) */
#define OOT_BOX_LIGHT_VTABLE 0x84baa5f8ULL
/* ---- end of the build's addresses */

int hos_site_light_box_acquired(Ctx *A64_NOESC c) {
    const uint64_t box = c->x[20];
    if (!box || recomp_load64(c, box) != OOT_BOX_LIGHT_VTABLE) return 0;

    /* Rotation, half-extents, the two blend vectors and the dimming factor.
       Keep identity, ownership, position and the cached matrices; dirty bit 0
       makes the game's transform update rebuild those matrices. */
    for (unsigned off = 580; off < 632; off += 4)
        recomp_store32(c, box + off, 0);
    for (unsigned off = 592; off <= 600; off += 4)
        recomp_store32(c, box + off, 0x3f800000u);
    recomp_store32(c, box + 628, 0x3f800000u);
    recomp_store8(c, box + 632, recomp_load8(c, box + 632) | 1);
    return 0;
}
