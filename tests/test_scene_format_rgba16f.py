"""The scene's light in RGBA16F with the PsychoV35 tone mapper (host/scene_format.h, host/nvn_native.c, gpu/vk/vk_texture.h).

OoT draws its scene, and the copies and bloom made of it, into R11G11B10F render targets: 6 bits of mantissa for red and green,
5 for blue. The sky's gradients came out in bands in every output (a frame dump of Hyrule Field: 28 levels of blue over the top
half of a 1440p sky before the tone map). With PsychoV35 (hos_scene_rgba16f) the game reads and writes those targets only as
colour, so the host makes them RGBX16F: the storage size and alignment the game's per-frame heap asks, and the texture made in it,
agree. Checked here, with host/scene_format.h and host/nvn_layout.c compiled:
- an R11G11B10F target (2D, one level, no sample count, compressible alone) is RGBX16F while on, and its storage twice the size
  (2560x1440: 0xf00000 to 0x1e00000, the size the game's own RGBA16F target at that address has);
- off (the Vanilla tone map, or HOS_SCENE_RGBA16F=0), a packaged texture, another format, target, flag, level count, sample count
  or depth: as it is;
- nvn_native.c asks the same description for the storage size, the alignment and the texture made (made_desc), from
  hos_scene_rgba16f and the switch, and a packaged texture is never changed;
- the answer is the builder's for its description (made_desc, cut out of nvn_native.c and run): the tone mapper turned on or
  off between the size the game asks and the texture it makes gives the texture the size it was answered (a texture made
  larger than its storage would run into the next target); the texture made, the next one is answered again, and so is
  another description;
- the renderer takes an R11G11B10F view over an RGBX16F texture as the texture's format (vk_texture.h scene_view_format) for its
  views and its pipelines' targets, and the temporal upscalers take an RGBA16F scene under PsychoV35 (vk_temporal.cpp).
No game, GPU or Vulkan.

python -I -B tests/test_scene_format_rgba16f.py [--cc gcc]
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cc", "{cc}"]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))   # (python -I leaves the check's own folder off the path)
from compiler_command import parse_compiler  # noqa: E402
from srccut import block  # noqa: E402

FIXTURE = r'''#include <stdio.h>
#include <stdint.h>
#include "../gpu/render/rx_nvn.h"
#include "scene_format.h"
#include <string.h>
#include <stdlib.h>
static int fails;
static volatile int hos_scene_rgba16f;
static int env_on_or(const char *name, int otherwise) { (void)name; return otherwise; }
@MADE@
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); fails++; } } while (0)
static uint64_t size_of(int format, uint32_t w, uint32_t h) {
    nvn_texdesc d = {0};
    d.target = 1, d.format = format, d.flags = 0x8, d.width = w, d.height = h, d.depth = 1, d.levels = 1;
    nvn_texlayout L;
    nvn_tex_layout(&d, &L);
    return L.size;
}
int main(void) {
    const int R11 = 0x3F, RGBX16F = 0x71;
    CHECK(hos_scene_format(1, R11, 1, 0x8, 0, 1, 1, 0) == RGBX16F);
    CHECK(hos_scene_format(1, R11, 1, 0x8, 1, 1, 0, 0) == RGBX16F);   /* (one sample, depth 0: as the game sets them) */
    CHECK(hos_scene_format(0, R11, 1, 0x8, 0, 1, 1, 0) == R11);       /* (off: Vanilla, or the switch) */
    CHECK(hos_scene_format(1, R11, 1, 0x8, 0, 1, 1, 1) == R11);       /* (packaged: its data is R11G11B10F's) */
    CHECK(hos_scene_format(1, 0x29, 1, 0x8, 0, 1, 1, 0) == 0x29);     /* (another format) */
    CHECK(hos_scene_format(1, 0x40, 1, 0x8, 0, 1, 1, 0) == 0x40);     /* (RGB9E5: the Vanilla picture's alias) */
    CHECK(hos_scene_format(1, R11, 2, 0x8, 0, 1, 1, 0) == R11);       /* (not 2D) */
    CHECK(hos_scene_format(1, R11, 1, 0x0, 0, 1, 1, 0) == R11);       /* (not compressible) */
    CHECK(hos_scene_format(1, R11, 1, 0x18, 0, 1, 1, 0) == R11);      /* (another flag too) */
    CHECK(hos_scene_format(1, R11, 1, 0x8, 4, 1, 1, 0) == R11);       /* (multisampled) */
    CHECK(hos_scene_format(1, R11, 1, 0x8, 0, 3, 1, 0) == R11);       /* (mipmapped) */
    CHECK(hos_scene_format(1, R11, 1, 0x8, 0, 1, 4, 0) == R11);       /* (more than one slice) */
    const uint64_t r11 = size_of(R11, 2560, 1440), rgbx = size_of(RGBX16F, 2560, 1440), rgba = size_of(0x29, 2560, 1440);
    printf("2560x1440: R11G11B10F %#llx, RGBX16F %#llx, RGBA16F %#llx\n", (unsigned long long)r11, (unsigned long long)rgbx,
           (unsigned long long)rgba);
    CHECK(r11 == 0xf00000 && rgbx == 0x1e00000 && rgbx == rgba);
    /* (made_desc, nvn_native.c's: a builder's answer kept from the size asked to the texture made) */
    nvn_tex_builder b;
    memset(&b, 0, sizeof b);
    b.d.target = 1, b.d.format = R11, b.d.flags = 0x8, b.d.width = 2560, b.d.height = 1440, b.d.depth = 1, b.d.levels = 1;
    hos_scene_rgba16f = 0;
    CHECK(made_desc(&b).format == R11);              /* (the size asked: R11G11B10F's) */
    hos_scene_rgba16f = 1;                           /* (the tone mapper changed before the texture is made) */
    CHECK(made_desc(&b).format == R11);              /* (made as answered) */
    b.scene_set = 0;                                 /* (made: nvnTextureInitialize forgets it) */
    CHECK(made_desc(&b).format == RGBX16F);          /* (the next one: answered again) */
    hos_scene_rgba16f = 0;
    CHECK(made_desc(&b).format == RGBX16F);          /* (still that answer, until made) */
    b.d.width = 1280;                                /* (another description: answered again) */
    CHECK(made_desc(&b).format == R11);
    b.has_layout = 1, b.scene_set = 0, hos_scene_rgba16f = 1;
    CHECK(made_desc(&b).format == R11);              /* (packaged: never) */
    if (!fails) printf("PASS: the scene's targets in RGBX16F with PsychoV35\n");
    return fails != 0;
}
'''


def check_wiring():
    nvn = (ROOT / "host" / "nvn_native.c").read_text(encoding="utf-8")
    assert '#include "scene_format.h"' in nvn
    made = block(nvn, "static nvn_texdesc made_desc(nvn_tex_builder *b)")
    for part in ('env_on_or("HOS_SCENE_RGBA16F", 1)', "on && hos_scene_rgba16f", "b->has_layout || b->pkg_data",
                 "nvn_texdesc d = b->d;", "b->scene_on"):
        assert part in made, part
    for fn in ("NV(nvnTextureBuilderGetStorageSize)", "NV(nvnTextureBuilderGetStorageAlignment)"):
        body = block(nvn, fn)
        assert "made_desc(TB(P(0)))" in body and "nvn_tex_layout(&made, &L);" in body, fn
    init = block(nvn, "NV(nvnTextureInitialize)")
    assert "o->d = made_desc(b);" in init and "o->d = b->d;" not in init and "b->scene_set = 0;" in init
    assert init.index("o->d = made_desc(b);") < init.index("b->scene_set = 0;")
    rx = (ROOT / "gpu" / "render" / "rx_nvn.h").read_text(encoding="utf-8")
    assert "NVN_FITS(nvn_tex_builder, NVNtextureBuilder);" in rx
    assert "NV(nvnTextureBuilderSetFormat) { TB(P(0))->d.format = I(1); }" in nvn, "the builder keeps the game's own format"
    vi = (ROOT / "host" / "nn_vi.c").read_text(encoding="utf-8")
    assert "hos_scene_rgba16f = rx_option(HOS_OPT_HDR_TONEMAP, 0) != 0;" in vi
    tex_h = (ROOT / "gpu" / "vk" / "vk_texture.h").read_text(encoding="utf-8")
    view = block(tex_h, "inline int scene_view_format(")
    assert "view_nvn == NVN_FORMAT_R11G11B10F && tex_nvn == NVN_FORMAT_RGBX16F ? tex_nvn : view_nvn" in view
    tex = (ROOT / "gpu" / "vk" / "vk_texture.cpp").read_text(encoding="utf-8")
    assert "scene_view_format(t->d.format," in block(tex, "int view_nvn_format(const nvn_tex* t, const nvn_view* v)")
    assert "view_nvn = scene_view_format(tex_nvn, view_nvn);" in block(tex, "VkFormat attachment_format(int tex_nvn, int view_nvn)")
    temporal = (ROOT / "gpu" / "vk" / "vk_temporal.cpp").read_text(encoding="utf-8")
    scene = block(temporal, "bool temporal_scene_format(VkFormat format)")
    assert "VK_FORMAT_R16G16B16A16_SFLOAT && scene_tonemap_on()" in scene
    formats = (ROOT / "gpu" / "vk" / "vk_formats.cpp").read_text(encoding="utf-8")
    assert re.search(r"case NVN_FORMAT_RGBX16F: return \{VK_FORMAT_R16G16B16A16_SFLOAT,", formats)
    switches = (ROOT / "gpu" / "vk" / "switches.def").read_text(encoding="utf-8")
    assert re.search(r"^SW\(HOS_SCENE_RGBA16F, kill, bool,", switches, re.M)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cc", default="gcc")
    a = p.parse_args()
    check_wiring()
    with tempfile.TemporaryDirectory(prefix="scene-format-") as tmp:
        src = Path(tmp) / "t.c"
        exe = Path(tmp) / "t.exe"
        nvn = (ROOT / "host" / "nvn_native.c").read_text(encoding="utf-8")
        made = block(nvn, "static uint32_t desc_key(const nvn_texdesc *d)") + "\n" + block(nvn, "static nvn_texdesc made_desc(nvn_tex_builder *b)")
        src.write_text(FIXTURE.replace("@MADE@", made).replace('"../gpu/render/rx_nvn.h"', '"%s"' % (ROOT / "gpu" / "render" / "rx_nvn.h").as_posix()),
                       encoding="utf-8")
        subprocess.run([*parse_compiler(a.cc), "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "host"), str(src),
                        str(ROOT / "host" / "nvn_layout.c"), "-o", str(exe)], check=True)
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)
        print(run.stdout + run.stderr, end="")
        return run.returncode


if __name__ == "__main__":
    sys.exit(main())
