/* (longshot) The scene's light in RGBA16F with the PsychoV35 tone mapper (nvn_native.c made_desc: nvnTextureBuilderGetStorageSize,
   ...Alignment and nvnTextureInitialize; tests/test_scene_format_rgba16f.py)

   OoT draws its scene (the sky, the world, the clouds, the sun and the fog over it), and the copies and the bloom made of it,
   into R11G11B10F render targets: 6 bits of mantissa for red and green, 5 for blue, steps of 1.6 % and 3.1 % of the light. The
   sky's gradients and the sun's halo came out in bands in every output: a frame dump of Hyrule Field had 28 levels of blue over
   the top half of a 1440p sky, in bands of 24 rows, before the tone map. PsychoV35 (HDR Native, the HDR tone mapper row:
   host/nn_vi.c hos_scene_rgba16f) already has the game tone map into RGBA16F; the light it maps was still R11G11B10F.

   The game makes a frame's render targets one after another in a first-fit heap, asking the storage size and alignment of each
   (shadow_slot.h). With PsychoV35 on, an R11G11B10F one (two-dimensional, one level, no sample count, compressible and nothing
   else: what the game's targets are; not a packaged texture, whose data is R11G11B10F's) is answered and made as RGBX16F: 10 bits
   of mantissa a channel, the alpha read as one, as the R11G11B10F one had it. The heap sets aside its 8 bytes a texel, so the
   targets after it move past it. The game reads and writes these targets only as colour while PsychoV35 is on: with the Vanilla
   tone map it packs its square-root picture into the scene's storage as RGB9E5 through an R32UI view (a renderer
   log of such views showed it with DLSS, the game's own upscaler and Off alike), so there they stay R11G11B10F. The
   renderer takes the scene in
   RGBA16F under PsychoV35 already (vk_temporal.cpp temporal_scene_format). HOS_SCENE_RGBA16F=0: R11G11B10F, as before. */
#ifndef HOS_SCENE_FORMAT_H
#define HOS_SCENE_FORMAT_H

#include <stdint.h>

enum {
    HOS_SCENE_FORMAT_R11G11B10F = 0x3F,   /* (NVN_FORMAT_R11G11B10F) */
    HOS_SCENE_FORMAT_RGBX16F = 0x71,      /* (NVN_FORMAT_RGBX16F) */
    HOS_SCENE_FORMAT_TARGET_2D = 1,       /* (NVN_TEXTURE_TARGET_2D) */
    HOS_SCENE_FORMAT_FLAGS = 0x8,         /* (NVN_TEXTURE_FLAGS_COMPRESSIBLE, and no other flag) */
};

/* The format a texture of this description is made in: RGBX16F for one of the game's R11G11B10F render targets while on (PsychoV35
   and the switch), else its own */
static inline int hos_scene_format(int on, int format, int target, int flags, int samples, uint32_t levels, uint32_t depth,
                                   int packaged) {
    if (!on || format != HOS_SCENE_FORMAT_R11G11B10F || target != HOS_SCENE_FORMAT_TARGET_2D || flags != HOS_SCENE_FORMAT_FLAGS ||
        samples > 1 || levels != 1 || depth > 1 || packaged)
        return format;
    return HOS_SCENE_FORMAT_RGBX16F;
}

#endif
