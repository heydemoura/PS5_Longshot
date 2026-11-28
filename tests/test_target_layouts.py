"""Depth/stencil optimal layouts and fast Z-clear (gpu/vk/vk_texture.cpp, vk_draw.cpp, vk_xfer.cpp). No game, no GPU.

Verifies PR 1a:
 - Depth/stencil images initialize via UNDEFINED -> TRANSFER_DST_OPTIMAL -> clear -> DEPTH_STENCIL_ATTACHMENT_OPTIMAL
   while colour images stay in VK_IMAGE_LAYOUT_GENERAL.
 - texture_transition_layout transitions depth/stencil images with layout-accurate stages/accesses, no-ops for colour
   textures or same-layout requests, and honours discard (oldLayout = UNDEFINED while still waiting on prior stages).
 - begin_pass attaches depth/stencil targets in DEPTH_STENCIL_ATTACHMENT_OPTIMAL (or DEPTH_STENCIL_READ_ONLY_OPTIMAL
   with STORE_OP_NONE when read-only depth test + sampled per Rule 5, or GENERAL for self-feedback).
 - clear_depth_stencil uses LOAD_OP_CLEAR on a closed pass with a full rect (discarding only single-mip, single-layer,
   full-aspect, full-extent targets), preserves stencil on depth-only clears, reopens read-only passes, and keeps
   vkCmdClearAttachments for partial rects or already-open writable passes.
 - vk_xfer.cpp transitions depth/stencil images to TRANSFER_SRC_OPTIMAL / TRANSFER_DST_OPTIMAL (or GENERAL for
   same-image subresource copies, DEPTH_STENCIL_ATTACHMENT_OPTIMAL for partial clears) while colour transfers stay GENERAL.
 - Depth-sampling sites (vk_draw.cpp descriptors, vk_texture.cpp uploads/compaction, vk_fsr3_inputs.cpp, vk_temporal.cpp,
   vk_dump.cpp, reshade/fx_runtime.cpp) transition depth textures and never leave them as VK_IMAGE_LAYOUT_GENERAL.

python -I -B tests/test_target_layouts.py --cxx clang++ --headers <Vulkan-Headers/include>
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves tests/ off sys.path)
from srccut import block   # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}", "--headers", "{vulkan_headers}"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"


def extract_create_init(tex_src: str) -> str:
    create_at = tex_src.index("Texture* create(")
    after_total = tex_src.index("total_bytes += x.bytes + x.pad;", create_at)
    start = tex_src.index("\n", after_total) + 1
    end = tex_src.index("    Texture* p = tex.get();", start)
    return tex_src[start:end]


def build_and_run(cxx: str, headers: Path, name: str, code: str, tmp: Path) -> tuple[int, str, str]:
    cpp = tmp / f"{name}.cpp"
    exe = tmp / (f"{name}.exe" if os.name == "nt" else name)
    cpp.write_text(code, encoding="utf-8")
    cmd = [
        cxx, "-std=c++20", "-O1", "-Wall", "-Wextra", "-Werror",
        "-Wno-missing-field-initializers", "-Wno-unused-parameter", "-Wno-unused-function",
        "-Wno-unused-variable", "-Wno-unused-but-set-variable",
        "-I" + str(headers), str(cpp), "-o", str(exe),
    ]
    if os.name == "nt":
        cmd.append("-static")
    b = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if b.returncode:
        raise SystemExit(f"FAIL: {name} did not compile:\n{b.stdout}{b.stderr}")
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


def make_init_fixture(init_fragment: str) -> str:
    return r'''
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan_core.h>
#include <cstdio>
#include <cstdlib>
#include <vector>

static int fails = 0;
#define EXPECT(cond, msg) do { if (!(cond)) { std::fprintf(stderr, "FAIL: %s (line %d)\n", (msg), __LINE__); fails++; } } while (0)

struct BarrierRec {
    VkImageLayout old_l, new_l;
    VkPipelineStageFlags2 src_s, dst_s;
    VkAccessFlags2 src_a, dst_a;
    VkImageAspectFlags aspect;
};
struct ClearRec {
    bool is_depth;
    VkImageLayout layout;
};
static std::vector<BarrierRec> barriers;
static std::vector<ClearRec> clears;
static VkImage fake_image = reinterpret_cast<VkImage>(0x101);
static VkCommandBuffer upload_cmd() { return reinterpret_cast<VkCommandBuffer>(0x201); }
static bool is_bc(VkFormat f) { return f == VK_FORMAT_BC3_UNORM_BLOCK; }
static bool is_astc(VkFormat f) { return f == VK_FORMAT_ASTC_4x4_UNORM_BLOCK; }

static void vkCmdPipelineBarrier2(VkCommandBuffer, const VkDependencyInfo* di) {
    if (di->imageMemoryBarrierCount != 1) { fails++; return; }
    const auto& b = di->pImageMemoryBarriers[0];
    barriers.push_back({b.oldLayout, b.newLayout, b.srcStageMask, b.dstStageMask,
                        b.srcAccessMask, b.dstAccessMask, b.subresourceRange.aspectMask});
}
static void vkCmdClearDepthStencilImage(VkCommandBuffer, VkImage, VkImageLayout l, const VkClearDepthStencilValue*,
                                        uint32_t, const VkImageSubresourceRange*) {
    clears.push_back({true, l});
}
static void vkCmdClearColorImage(VkCommandBuffer, VkImage, VkImageLayout l, const VkClearColorValue*,
                                 uint32_t, const VkImageSubresourceRange*) {
    clears.push_back({false, l});
}

struct FakeTex {
    VkImage image = fake_image;
    VkFormat format = VK_FORMAT_UNDEFINED;
    VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT;
    VkImageLayout layout = VK_IMAGE_LAYOUT_GENERAL;
};

static void run_case(VkFormat format, bool depth, VkImageAspectFlags aspect) {
    barriers.clear();
    clears.clear();
    FakeTex x{fake_image, format, aspect, VK_IMAGE_LAYOUT_GENERAL};
''' + init_fragment + r'''
    if (depth) {
        EXPECT(barriers.size() == 2, "depth create must emit init + post-clear barriers");
        EXPECT(clears.size() == 1 && clears[0].is_depth, "depth create must clear depth/stencil");
        if (barriers.size() >= 2 && clears.size() >= 1) {
            EXPECT(barriers[0].old_l == VK_IMAGE_LAYOUT_UNDEFINED &&
                       barriers[0].new_l == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                   "depth create barrier 1 must transition UNDEFINED -> TRANSFER_DST_OPTIMAL");
            EXPECT(clears[0].layout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                   "depth create clear must use TRANSFER_DST_OPTIMAL");
            EXPECT(barriers[1].old_l == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL &&
                       barriers[1].new_l == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
                   "depth create barrier 2 must transition TRANSFER_DST_OPTIMAL -> DEPTH_STENCIL_ATTACHMENT_OPTIMAL");
            EXPECT(x.layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
                   "depth Texture::layout must be DEPTH_STENCIL_ATTACHMENT_OPTIMAL after create");
        }
    } else if (!is_bc(format) && !is_astc(format)) {
        EXPECT(barriers.size() == 2 && barriers[0].new_l == VK_IMAGE_LAYOUT_GENERAL &&
                   barriers[1].new_l == VK_IMAGE_LAYOUT_GENERAL,
               "colour create must remain VK_IMAGE_LAYOUT_GENERAL in PR 1a");
        EXPECT(clears.size() == 1 && !clears[0].is_depth && clears[0].layout == VK_IMAGE_LAYOUT_GENERAL,
               "colour create clear must remain VK_IMAGE_LAYOUT_GENERAL in PR 1a");
        EXPECT(x.layout == VK_IMAGE_LAYOUT_GENERAL, "colour Texture::layout must stay GENERAL");
    } else {
        EXPECT(barriers.size() == 1 && barriers[0].new_l == VK_IMAGE_LAYOUT_GENERAL && clears.empty(),
               "compressed colour create must stay GENERAL with no clear");
    }
}

int main() {
    run_case(VK_FORMAT_D32_SFLOAT, true, VK_IMAGE_ASPECT_DEPTH_BIT);
    run_case(VK_FORMAT_D24_UNORM_S8_UINT, true, VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT);
    run_case(VK_FORMAT_R8G8B8A8_UNORM, false, VK_IMAGE_ASPECT_COLOR_BIT);
    run_case(VK_FORMAT_BC3_UNORM_BLOCK, false, VK_IMAGE_ASPECT_COLOR_BIT);
    if (fails) return 1;
    std::puts("PASS: create() depth/stencil init uses TRANSFER_DST_OPTIMAL -> DEPTH_STENCIL_ATTACHMENT_OPTIMAL");
    return 0;
}
'''


def make_transition_fixture(emit_src: str, guard_src: str, fn_src: str) -> str:
    return r'''
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan_core.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

static int fails = 0;
#define EXPECT(cond, msg) do { if (!(cond)) { std::fprintf(stderr, "FAIL: %s (line %d)\n", (msg), __LINE__); fails++; } } while (0)

struct Texture {
    VkImage image = VK_NULL_HANDLE;
    VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT;
    VkImageLayout layout = VK_IMAGE_LAYOUT_GENERAL;
    uint64_t layout_cmd_rec = 0;
    VkImageLayout layout_before_cmd = VK_IMAGE_LAYOUT_GENERAL;
};
static inline bool is_depth_texture(const Texture* t) {
    return t && (t->aspect & (VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT)) != 0;
}

static VkCommandBuffer up_cmd = reinterpret_cast<VkCommandBuffer>(0x99);
static uint64_t up_rec_id = 1;

static std::vector<VkImageMemoryBarrier2> barriers;
static void vkCmdPipelineBarrier2(VkCommandBuffer, const VkDependencyInfo* di) {
    for (uint32_t i = 0; i < di->imageMemoryBarrierCount; i++)
        barriers.push_back(di->pImageMemoryBarriers[i]);
}
void texture_transition_layout(VkCommandBuffer cmd, Texture* tex, VkImageLayout target, bool discard = false);

''' + emit_src + "\n" + guard_src + ";\n" + fn_src + r'''

int main() {
    const VkCommandBuffer cmd = reinterpret_cast<VkCommandBuffer>(0x10);
    Texture color{reinterpret_cast<VkImage>(0x20), VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_LAYOUT_GENERAL};
    Texture depth{reinterpret_cast<VkImage>(0x30), VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT,
                  VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL};

    // Colour texture is a no-op in PR 1a.
    texture_transition_layout(cmd, &color, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    EXPECT(barriers.empty() && color.layout == VK_IMAGE_LAYOUT_GENERAL, "colour transition must be a no-op");

    // Same layout is a no-op.
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL);
    EXPECT(barriers.empty(), "same-layout depth transition must be a no-op");

    // ATTACHMENT -> READ_ONLY: flushes DS writes to graphics+compute read.
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL);
    EXPECT(barriers.size() == 1, "ATTACHMENT -> READ_ONLY emits one barrier");
    if (barriers.size() == 1) {
        const auto& b = barriers.back();
        EXPECT(b.oldLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL &&
                   b.newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL,
               "ATTACHMENT -> READ_ONLY layouts");
        EXPECT((b.srcAccessMask & VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT) != 0,
               "ATTACHMENT -> READ_ONLY flushes depth/stencil writes");
        EXPECT((b.dstStageMask & (VK_PIPELINE_STAGE_2_ALL_GRAPHICS_BIT | VK_PIPELINE_STAGE_2_COMPUTE_SHADER_BIT)) ==
                   (VK_PIPELINE_STAGE_2_ALL_GRAPHICS_BIT | VK_PIPELINE_STAGE_2_COMPUTE_SHADER_BIT),
               "READ_ONLY dstStage covers graphics + compute");
        EXPECT((b.dstAccessMask & (VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_READ_BIT | VK_ACCESS_2_SHADER_SAMPLED_READ_BIT)) ==
                   (VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_READ_BIT | VK_ACCESS_2_SHADER_SAMPLED_READ_BIT),
               "READ_ONLY dstAccess covers DS read + shader sampled read");
        EXPECT(b.subresourceRange.aspectMask == (VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT),
               "subresource aspect covers both depth and stencil");
    }
    EXPECT(depth.layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL, "tracked layout updated to READ_ONLY");

    // READ_ONLY -> ATTACHMENT (with discard): oldLayout UNDEFINED, srcStage still waits on prior reads, dstStage does not stall geometry.
    barriers.clear();
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL, true);
    EXPECT(barriers.size() == 1, "READ_ONLY -> ATTACHMENT emits one barrier");
    if (barriers.size() == 1) {
        const auto& b = barriers.back();
        EXPECT(b.oldLayout == VK_IMAGE_LAYOUT_UNDEFINED &&
                   b.newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
               "discard uses oldLayout UNDEFINED");
        EXPECT(b.srcStageMask != VK_PIPELINE_STAGE_2_NONE && b.srcAccessMask == 0,
               "discard from READ_ONLY still waits on prior stages with 0 srcAccess");
        EXPECT(b.dstStageMask == (VK_PIPELINE_STAGE_2_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT),
               "ATTACHMENT dstStage only holds early/late fragment tests (no geometry stall)");
    }

    // ATTACHMENT -> TRANSFER_SRC -> TRANSFER_DST -> GENERAL -> ATTACHMENT
    barriers.clear();
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_GENERAL);
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL);
    EXPECT(barriers.size() == 4, "4 distinct transitions emit 4 barriers");
    if (barriers.size() == 4) {
        EXPECT(barriers[0].newLayout == VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL &&
                   barriers[0].dstAccessMask == VK_ACCESS_2_TRANSFER_READ_BIT, "TRANSFER_SRC barrier");
        EXPECT(barriers[1].newLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL &&
                   barriers[1].srcAccessMask == 0 && barriers[1].dstAccessMask == VK_ACCESS_2_TRANSFER_WRITE_BIT,
               "TRANSFER_SRC -> TRANSFER_DST barrier");
        EXPECT(barriers[2].newLayout == VK_IMAGE_LAYOUT_GENERAL &&
                   (barriers[2].srcAccessMask & VK_ACCESS_2_TRANSFER_WRITE_BIT) != 0,
               "TRANSFER_DST -> GENERAL barrier");
        EXPECT(barriers[3].oldLayout == VK_IMAGE_LAYOUT_GENERAL &&
                   barriers[3].newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
               "GENERAL -> ATTACHMENT barrier");
    }

    // r.upload vs r.cmd ordering (Blocking #1):
    up_rec_id = 7;
    depth.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    depth.layout_cmd_rec = 0;
    barriers.clear();
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL);
    EXPECT(depth.layout_cmd_rec == 7 && depth.layout_before_cmd == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
           "r.cmd snapshots layout_before_cmd on first transition in recording");
    barriers.clear();
    {
        UploadLayoutGuard lg;
        lg.begin(up_cmd, &depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, false);
        lg.begin(up_cmd, &depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, false);
        EXPECT(barriers.size() == 1 && barriers[0].oldLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL &&
                   barriers[0].newLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
               "r.upload partial upload uses layout_before_cmd and dedupes multi-arena begin()");
    }
    EXPECT(barriers.size() == 2 && barriers[1].oldLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL &&
               barriers[1].newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL &&
               depth.layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL,
           "r.upload restores layout_before_cmd and preserves r.cmd tail layout");

    barriers.clear();
    {
        UploadLayoutGuard lg(up_cmd, &depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, true);
        EXPECT(barriers.size() == 1 && barriers[0].oldLayout == VK_IMAGE_LAYOUT_UNDEFINED &&
                   barriers[0].newLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
               "r.upload discard uses UNDEFINED -> TRANSFER_DST");
    }
    EXPECT(barriers.size() == 2 && barriers[1].oldLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL &&
               barriers[1].newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL &&
               depth.layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL,
           "r.upload discard restores layout_before_cmd and preserves r.cmd tail layout");

    up_rec_id = 8;
    barriers.clear();
    {
        UploadLayoutGuard lg(up_cmd, &depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, true);
    }
    EXPECT(barriers.size() == 1 && barriers[0].oldLayout == VK_IMAGE_LAYOUT_UNDEFINED &&
               barriers[0].newLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL &&
               depth.layout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
           "r.upload on untouched texture updates layout directly without restore barrier");

    // Same-layout r.cmd use must still stamp layout_cmd_rec so a later r.upload restores it.
    up_rec_id = 9;
    depth.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;
    depth.layout_cmd_rec = 0;
    barriers.clear();
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL);
    EXPECT(barriers.empty() && depth.layout_cmd_rec == 9 &&
               depth.layout_before_cmd == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL,
           "same-layout r.cmd call emits no barrier yet stamps layout_cmd_rec/layout_before_cmd");
    {
        UploadLayoutGuard lg(up_cmd, &depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, false);
        EXPECT(barriers.size() == 1 && barriers[0].oldLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL &&
                   barriers[0].newLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
               "r.upload after same-layout r.cmd transitions from READ_ONLY -> TRANSFER_DST");
    }
    EXPECT(barriers.size() == 2 && barriers[1].oldLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL &&
               barriers[1].newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL &&
               depth.layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL,
           "r.upload after same-layout r.cmd restores READ_ONLY and preserves depth.layout");

    // Same-layout transition with discard == true must still emit UNDEFINED -> ATTACHMENT HTILE discard.
    depth.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    barriers.clear();
    texture_transition_layout(cmd, &depth, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL, true);
    EXPECT(barriers.size() == 1 && barriers[0].oldLayout == VK_IMAGE_LAYOUT_UNDEFINED &&
               barriers[0].newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL &&
               barriers[0].srcAccessMask == 0,
           "same-layout discard emits UNDEFINED -> ATTACHMENT with 0 srcAccess");

    if (fails) return 1;
    std::puts("PASS: texture_transition_layout + UploadLayoutGuard state machine");
    return 0;
}
'''


def make_draw_fixture(begin_src: str, clear_src: str, helper_src: str) -> str:
    has_helper = bool(helper_src)
    return r'''
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan_core.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

static int fails = 0;
#define EXPECT(cond, msg) do { if (!(cond)) { std::fprintf(stderr, "FAIL: %s (line %d)\n", (msg), __LINE__); fails++; } } while (0)

struct Texture {
    VkImage image = VK_NULL_HANDLE;
    VkFormat format = VK_FORMAT_UNDEFINED;
    VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT;
    VkImageLayout layout = VK_IMAGE_LAYOUT_GENERAL;
    uint32_t width = 1280, height = 720, levels = 1, layers = 1;
};
static inline bool is_depth_texture(const Texture* t) {
    return t && (t->aspect & (VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT)) != 0;
}
struct Targets {
    Texture* color[8]{};
    VkImageView color_view[8]{};
    VkImage block_image[8]{};
    VkFormat color_format[8]{};
    uint32_t color_level[8]{}, color_layer[8]{}, depth_level = 0, depth_layer = 0;
    Texture* depth = nullptr;
    VkImageView depth_view = VK_NULL_HANDLE;
    VkFormat depth_format = VK_FORMAT_UNDEFINED;
    VkImageLayout depth_layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    int n = 0;
    bool has_block_images = false;
    uint32_t width = 1280, height = 720;
};
struct Pass {
    bool open = false;
    VkImageView color[8]{};
    VkImage block_image[8]{};
    bool has_block_images = false;
    Texture* tex[8]{};
    uint32_t level[8]{}, layer[8]{};
    Texture* depth_tex = nullptr;
    uint32_t depth_level = 0, depth_layer = 0;
    VkImageView depth = VK_NULL_HANDLE;
    VkImageLayout depth_layout = VK_IMAGE_LAYOUT_UNDEFINED;
    int n = 0;
    uint32_t width = 0, height = 0;
    int draws = 0;
    uint32_t wm_or = 0, wm_n = 0;
    int clears = 0;
    uint64_t first_prog = 0;
    VkFormat format0 = VK_FORMAT_UNDEFINED;
    std::vector<const Texture*> sampled;
    uint16_t sampled_targets = 0;
    uint16_t written = 0;
};
struct NvnState {
    VkRect2D scissor{{0, 0}, {1280, 720}};
};

static Pass g_pass;
static Targets g_targets;
static Pass& pass() { return g_pass; }
static Targets targets(NvnState&) { return g_targets; }
static VkRect2D clear_rect(const NvnState& s, uint32_t, uint32_t) { return s.scissor; }
static bool work_since_barrier = false;
static int64_t st_passes = 0, st_clears = 0;

struct TransRec { Texture* tex; VkImageLayout from, to; bool discard; };
struct BeginRec {
    bool has_depth, has_stencil;
    VkImageLayout color0_layout;
    VkImageLayout depth_layout, stencil_layout;
    VkAttachmentLoadOp depth_load, stencil_load;
    VkAttachmentStoreOp depth_store, stencil_store;
    VkClearDepthStencilValue clear_val;
};
struct ClearAttRec { VkImageAspectFlags aspect; VkClearDepthStencilValue val; VkRect2D rect; };

static std::vector<TransRec> transitions;
static std::vector<BeginRec> begins;
static std::vector<ClearAttRec> clear_atts;
static int end_count = 0, tex_written_count = 0;

static void end_pass(VkCommandBuffer) { if (g_pass.open) { g_pass.open = false; end_count++; } }
static void clear_after_draws(VkCommandBuffer cmd) { if (g_pass.open && g_pass.draws > 0) end_pass(cmd); }
static void full_barrier(VkCommandBuffer, const char* = "") {}
static void pass_start_barrier(VkCommandBuffer) {}
static void copy_block_attachment(VkCommandBuffer, const Texture&, VkImage, uint32_t, uint32_t, bool) {}
static void texture_written(Texture*, const char*) { tex_written_count++; }
static void texture_rendered(Texture*) {}
static void gprof_begin(VkCommandBuffer) {}
static void logf(const char*, ...) {}
static bool depth_aspect(VkFormat f) { return f == VK_FORMAT_D32_SFLOAT || f == VK_FORMAT_D24_UNORM_S8_UINT; }
static bool has_stencil(VkFormat f) { return f == VK_FORMAT_D24_UNORM_S8_UINT || f == VK_FORMAT_S8_UINT; }

static void texture_transition_layout(VkCommandBuffer, Texture* tex, VkImageLayout target, bool discard = false) {
    if (!is_depth_texture(tex) || !tex->image || (tex->layout == target && !discard)) return;
    transitions.push_back({tex, tex->layout, target, discard});
    tex->layout = target;
}

static void vkCmdBeginRendering(VkCommandBuffer, const VkRenderingInfo* ri) {
    BeginRec r{};
    r.color0_layout = ri->colorAttachmentCount ? ri->pColorAttachments[0].imageLayout : VK_IMAGE_LAYOUT_UNDEFINED;
    if (ri->pDepthAttachment) {
        r.has_depth = true;
        r.depth_layout = ri->pDepthAttachment->imageLayout;
        r.depth_load = ri->pDepthAttachment->loadOp;
        r.depth_store = ri->pDepthAttachment->storeOp;
        r.clear_val = ri->pDepthAttachment->clearValue.depthStencil;
    }
    if (ri->pStencilAttachment) {
        r.has_stencil = true;
        r.stencil_layout = ri->pStencilAttachment->imageLayout;
        r.stencil_load = ri->pStencilAttachment->loadOp;
        r.stencil_store = ri->pStencilAttachment->storeOp;
    }
    begins.push_back(r);
}
static void vkCmdClearAttachments(VkCommandBuffer, uint32_t n, const VkClearAttachment* a, uint32_t, const VkClearRect* cr) {
    for (uint32_t i = 0; i < n; i++)
        clear_atts.push_back({a[i].aspectMask, a[i].clearValue.depthStencil, cr->rect});
}

''' + helper_src + "\n" + begin_src + "\n" + clear_src + r'''

static void reset_log() {
    transitions.clear();
    begins.clear();
    clear_atts.clear();
    end_count = 0;
    tex_written_count = 0;
}

int main() {
    const VkCommandBuffer cmd = reinterpret_cast<VkCommandBuffer>(0x1);
    Texture col{reinterpret_cast<VkImage>(0x10), VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_LAYOUT_GENERAL, 1280, 720, 1, 1};
    Texture d32{reinterpret_cast<VkImage>(0x20), VK_FORMAT_D32_SFLOAT, VK_IMAGE_ASPECT_DEPTH_BIT, VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL, 1280, 720, 1, 1};
    Texture d24s8{reinterpret_cast<VkImage>(0x30), VK_FORMAT_D24_UNORM_S8_UINT, VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT, VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL, 1280, 720, 1, 1};
    Texture darr{reinterpret_cast<VkImage>(0x40), VK_FORMAT_D32_SFLOAT, VK_IMAGE_ASPECT_DEPTH_BIT, VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL, 1280, 720, 1, 4};

    // 1. Normal depth pass uses DEPTH_STENCIL_ATTACHMENT_OPTIMAL (colour stays GENERAL).
    g_pass = {};
    g_targets = {};
    g_targets.n = 1;
    g_targets.color[0] = &col;
    g_targets.color_view[0] = reinterpret_cast<VkImageView>(0x100);
    g_targets.color_format[0] = col.format;
    g_targets.depth = &d32;
    g_targets.depth_view = reinterpret_cast<VkImageView>(0x200);
    g_targets.depth_format = d32.format;
    g_targets.depth_layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    reset_log();
    begin_pass(cmd, g_targets);
    EXPECT(begins.size() == 1, "normal pass began");
    if (!begins.empty()) {
        EXPECT(begins[0].color0_layout == VK_IMAGE_LAYOUT_GENERAL, "colour attachment stays GENERAL in PR 1a");
        EXPECT(begins[0].depth_layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
               "depth attachment must use DEPTH_STENCIL_ATTACHMENT_OPTIMAL");
        EXPECT(begins[0].depth_load == VK_ATTACHMENT_LOAD_OP_LOAD && begins[0].depth_store == VK_ATTACHMENT_STORE_OP_STORE,
               "normal depth pass loads and stores");
    }
    EXPECT(d32.layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL, "d32 layout transitioned to ATTACHMENT");

    // 2. Read-only depth + sampled pass (Rule 5) uses DEPTH_STENCIL_READ_ONLY_OPTIMAL + STORE_OP_NONE.
    reset_log();
    g_targets.depth_layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;
    begin_pass(cmd, g_targets);
    EXPECT(end_count == 1 && begins.size() == 1, "layout change from ATTACHMENT to READ_ONLY must reopen pass");
    if (!begins.empty()) {
        EXPECT(begins[0].depth_layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL,
               "read-only depth pass must use DEPTH_STENCIL_READ_ONLY_OPTIMAL");
        EXPECT(begins[0].depth_store == VK_ATTACHMENT_STORE_OP_NONE,
               "read-only depth pass must use STORE_OP_NONE");
    }
    EXPECT(d32.layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL, "d32 transitioned to READ_ONLY");

    // 3. Full-rect clear_depth_stencil while pass is open in READ_ONLY reopens in ATTACHMENT with LOAD_OP_CLEAR + discard.
    reset_log();
    NvnState s{};
    clear_depth_stencil(cmd, s, 1.0f, true, 0, 0);
    EXPECT(end_count == 1, "clear on open READ_ONLY pass must close it first");
    EXPECT(begins.size() == 1, "clear opens a new pass");
    EXPECT(clear_atts.empty(), "full-rect depth clear on closed/reopened pass must use LOAD_OP_CLEAR, not vkCmdClearAttachments");
    if (!begins.empty()) {
        EXPECT(begins[0].depth_layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
               "fast Z-clear opens in DEPTH_STENCIL_ATTACHMENT_OPTIMAL");
        EXPECT(begins[0].depth_load == VK_ATTACHMENT_LOAD_OP_CLEAR, "fast Z-clear uses LOAD_OP_CLEAR");
        EXPECT(begins[0].clear_val.depth == 1.0f, "fast Z-clear carries depth clear value");
    }
    EXPECT(transitions.size() == 1 && transitions[0].discard,
           "single-mip single-layer full-aspect depth clear discards prior contents (oldLayout=UNDEFINED)");

    // 4. Already-open writable pass (e.g. after clear_color_target): clear_depth_stencil uses vkCmdClearAttachments in ATTACHMENT.
    reset_log();
    clear_depth_stencil(cmd, s, 0.5f, true, 0, 0);
    EXPECT(begins.empty() && clear_atts.size() == 1, "clear inside already-open writable pass uses vkCmdClearAttachments");

    // 5. Partial-rect depth clear on closed pass uses LOAD_OP_LOAD + vkCmdClearAttachments.
    end_pass(cmd);
    reset_log();
    s.scissor = {{10, 10}, {200, 100}};
    clear_depth_stencil(cmd, s, 1.0f, true, 0, 0);
    EXPECT(begins.size() == 1 && begins[0].depth_load == VK_ATTACHMENT_LOAD_OP_LOAD,
           "partial-rect depth clear must use LOAD_OP_LOAD");
    EXPECT(clear_atts.size() == 1, "partial-rect depth clear must call vkCmdClearAttachments");

    // 6. Depth-only clear on D24S8 preserves stencil (LOAD_OP_LOAD on stencil, discard=false).
    end_pass(cmd);
    reset_log();
    s.scissor = {{0, 0}, {1280, 720}};
    g_targets.depth = &d24s8;
    g_targets.depth_view = reinterpret_cast<VkImageView>(0x300);
    g_targets.depth_format = d24s8.format;
    clear_depth_stencil(cmd, s, 1.0f, true, 0, 0);
    EXPECT(begins.size() == 1 && clear_atts.empty(), "depth-only full clear on D24S8 uses LOAD_OP_CLEAR");
    if (!begins.empty()) {
        EXPECT(begins[0].depth_load == VK_ATTACHMENT_LOAD_OP_CLEAR &&
                   begins[0].stencil_load == VK_ATTACHMENT_LOAD_OP_LOAD,
               "depth-only clear on D24S8 must clear depth and load stencil");
    }
    EXPECT(transitions.size() == 1 && !transitions[0].discard,
           "depth-only clear on D24S8 must NOT discard (stencil must be preserved)");

    // 7. Multi-layer depth array clear must NOT discard (preserves other layers).
    end_pass(cmd);
    reset_log();
    g_targets.depth = &darr;
    g_targets.depth_view = reinterpret_cast<VkImageView>(0x400);
    g_targets.depth_format = darr.format;
    clear_depth_stencil(cmd, s, 1.0f, true, 0, 0);
    EXPECT(begins.size() == 1 && begins[0].depth_load == VK_ATTACHMENT_LOAD_OP_CLEAR && clear_atts.empty(),
           "multi-layer depth array full clear uses LOAD_OP_CLEAR");
    EXPECT(transitions.size() == 1 && !transitions[0].discard,
           "multi-layer depth array clear must NOT discard other layers");
''' + (r'''
    // 8. Rule 5 depth_attachment_layout decision table.
    EXPECT(depth_attachment_layout(false, true, false) == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
           "writable depth -> ATTACHMENT_OPTIMAL");
    EXPECT(depth_attachment_layout(true, false, false) == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL,
           "Rule 5 read-only depth + sampled -> READ_ONLY_OPTIMAL");
    EXPECT(depth_attachment_layout(false, false, true) == VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL,
           "non-writing draw in open READ_ONLY pass stays READ_ONLY_OPTIMAL");
    EXPECT(depth_attachment_layout(true, true, false) == VK_IMAGE_LAYOUT_GENERAL,
           "Rule 5 self-feedback (samples & writes depth) -> GENERAL");
''' if has_helper else r'''
    EXPECT(false, "depth_attachment_layout helper missing in vk_draw.cpp");
''') + r'''
    if (fails) return 1;
    std::puts("PASS: begin_pass + clear_depth_stencil + Rule 5 depth layouts");
    return 0;
}
'''


def make_xfer_fixture(copy_b2t_src: str, copy_t2t_src: str, clear_tex_src: str) -> str:
    return r'''
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan_core.h>
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

static int fails = 0;
#define EXPECT(cond, msg) do { if (!(cond)) { std::fprintf(stderr, "FAIL: %s (line %d)\n", (msg), __LINE__); fails++; } } while (0)

struct NVNcopyRegion { int32_t xoffset, yoffset, zoffset, width, height, depth; };
struct nvn_texdesc { uint32_t format = 0; };
struct nvn_tex { uint64_t gpu = 0; nvn_texdesc d{}; };
struct nvn_view {
    uint8_t set_levels : 1, set_layers : 1, set_format : 1;
    uint8_t base_level, nlevels;
    uint16_t min_layer, nlayers;
};
struct nvn_p_copy_bt { uint64_t buf; const nvn_tex* tex; nvn_view view; uint8_t has_view; NVNcopyRegion r; int32_t flags; };
struct nvn_p_copy_tt { const nvn_tex* src; const nvn_tex* dst; nvn_view sv, dv; uint8_t has_sv, has_dv; NVNcopyRegion sr, dr; int32_t flags; };
struct nvn_p_clear_tex { const nvn_tex* tex; nvn_view view; uint8_t has_view, is_uint; NVNcopyRegion r; int32_t mask; union { float f[4]; uint32_t u[4]; } c; };
constexpr int NVN_COPY_FLAGS_LINEAR_FILTER_BIT = 1;

struct Texture {
    VkImage image = VK_NULL_HANDLE;
    VkFormat format = VK_FORMAT_UNDEFINED;
    VkImageType type = VK_IMAGE_TYPE_2D;
    VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT;
    VkImageLayout layout = VK_IMAGE_LAYOUT_GENERAL;
    VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT;
    uint32_t width = 64, height = 64, depth = 1, levels = 2, layers = 2, skip = 0;
    uint64_t gpu = 0;
    int nvn_format = 0;
    uint32_t id = 1;
    bool astc = false, bc = false, bc_resized = false, gpu_written = false, uploaded = false, released = false, only_cleared = false;
};
static inline bool is_depth_texture(const Texture* t) {
    return t && (t->aspect & (VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT)) != 0;
}
struct TextureAttachment { VkImageView view = VK_NULL_HANDLE; VkFormat format = VK_FORMAT_UNDEFINED; VkImage blocks = VK_NULL_HANDLE; };
struct Sub {
    Texture* tex = nullptr;
    VkImageAspectFlags aspect = 0;
    uint32_t level = 0, layer = 0, layers = 1;
    int32_t x = 0, y = 0, z = 0;
    uint32_t w = 0, h = 0, d = 1;
    uint32_t bpb = 4;
    int bw = 1, bh = 1, nvn_format = 0;
    uint32_t down = 0;
};
struct BufferRef { VkBuffer buffer = reinterpret_cast<VkBuffer>(0x99); uint64_t offset = 0; explicit operator bool() const { return buffer != VK_NULL_HANDLE; } };
struct NvnState { int64_t copy_stride = 0; };
struct HostStub {
    const uint8_t* gpu_ptr(uint64_t, uint64_t*) { return nullptr; }
    uint64_t gpu_guest(uint64_t) { return 0; }
};
static HostStub host_obj;
static HostStub* host = &host_obj;
struct DevStub { VkPhysicalDevice physical = VK_NULL_HANDLE; };
static DevStub dev;
struct GprofSpan { uint64_t bytes = 0; GprofSpan(VkCommandBuffer, const char*) {} };

static bool env_on(const char*) { return false; }
static void logf(const char*, ...) {}
static void warn(int, const char*) {}
static void log_big_copy(const char*, const Texture*, uint32_t, uint32_t, int, int, uint32_t, uint32_t, uint64_t) {}
static void end_pass(VkCommandBuffer) {}
static void full_barrier(VkCommandBuffer, const char* = "") {}
static void texture_written(Texture*, const char* = "?") {}
static void memory_stamp_gpu(uint64_t, uint64_t) {}
static void texture_gpu_wrote(uint64_t, uint64_t) {}
static void copy_block_attachment(VkCommandBuffer, const Texture&, VkImage, uint32_t, uint32_t, bool) {}
static bool texture_copy_into_bc(VkCommandBuffer, Texture*, uint32_t, uint32_t, int, int, Texture*, uint32_t, uint32_t, int, int, uint32_t, uint32_t, uint32_t) { return false; }
static bool texels_fit(const nvn_tex*, const NVNcopyRegion*, uint64_t, uint64_t) { return false; }
static void texture_read_texels(const nvn_tex*, unsigned, unsigned, const NVNcopyRegion*, uint8_t*, uint64_t, uint64_t, bool) {}
struct VkFmtInfo { bool astc = false; };
static VkFmtInfo vk_format(int) { return {}; }
static int view_nvn_format(const nvn_tex*, const nvn_view*) { return 0; }
static unsigned view_level(const nvn_view* v) { return v && v->set_levels ? v->base_level : 0; }
static unsigned view_layer(const nvn_view* v) { return v && v->set_layers ? v->min_layer : 0; }
static VkImageSubresourceLayers layers(const Sub& s) { return {s.aspect, s.level, s.layer, s.layers}; }
static VkOffset3D offset(const Sub& s) { return {s.x, s.y, s.z}; }
static VkExtent3D extent(const Sub& s) { return {s.w, s.h, s.d}; }
static bool size_compatible(const Sub& a, const Sub& b) { return a.bpb == b.bpb && a.bw == b.bw && a.bh == b.bh; }
static VkFormatFeatureFlags tiling_features(VkFormat) { return VK_FORMAT_FEATURE_BLIT_SRC_BIT | VK_FORMAT_FEATURE_BLIT_DST_BIT; }
static int format_class(VkFormat) { return 0; }
static void vkGetPhysicalDeviceFormatProperties(VkPhysicalDevice, VkFormat, VkFormatProperties* fp) {
    fp->optimalTilingFeatures = VK_FORMAT_FEATURE_BLIT_SRC_BIT | VK_FORMAT_FEATURE_BLIT_DST_BIT;
}
static bool buffer_side(const NvnState&, const Sub&, uint64_t, uint64_t& size, VkBufferImageCopy&, BufferRef& span, bool) {
    size = 64 * 64 * 4;
    span = BufferRef{};
    return true;
}

static Texture* lookup_tex(const nvn_tex* t) { return reinterpret_cast<Texture*>(static_cast<uintptr_t>(t->gpu)); }
static Texture* texture_get(const nvn_tex* t, bool) { return lookup_tex(t); }
static bool resolve(const nvn_tex* t, const nvn_view* v, bool has_v, const NVNcopyRegion& r, Sub& s, bool = false) {
    s.tex = lookup_tex(t);
    s.aspect = s.tex->aspect == (VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT)
                   ? (VkImageAspectFlags)VK_IMAGE_ASPECT_DEPTH_BIT : s.tex->aspect;
    s.level = has_v && v->set_levels ? v->base_level : 0;
    s.layer = has_v && v->set_layers ? v->min_layer : 0;
    s.layers = 1;
    s.x = r.xoffset, s.y = r.yoffset, s.z = 0;
    s.w = (uint32_t)r.width, s.h = (uint32_t)r.height, s.d = 1;
    return true;
}
static TextureAttachment texture_attachment(Texture* t, const nvn_tex*, const nvn_view*) {
    return {reinterpret_cast<VkImageView>(0x500), t->format, VK_NULL_HANDLE};
}

static VkImageLayout last_b2t_layout = VK_IMAGE_LAYOUT_UNDEFINED;
static VkImageLayout last_t2b_layout = VK_IMAGE_LAYOUT_UNDEFINED;
static VkImageLayout last_copy_src = VK_IMAGE_LAYOUT_UNDEFINED, last_copy_dst = VK_IMAGE_LAYOUT_UNDEFINED;
static VkImageLayout last_blit_src = VK_IMAGE_LAYOUT_UNDEFINED, last_blit_dst = VK_IMAGE_LAYOUT_UNDEFINED;
static VkImageLayout last_clear_ds = VK_IMAGE_LAYOUT_UNDEFINED, last_clear_col = VK_IMAGE_LAYOUT_UNDEFINED;
static VkImageLayout last_rend_ds = VK_IMAGE_LAYOUT_UNDEFINED;

static void texture_transition_layout(VkCommandBuffer, Texture* tex, VkImageLayout target, bool = false) {
    if (is_depth_texture(tex) && tex->image) tex->layout = target;
}
static void vkCmdCopyBufferToImage(VkCommandBuffer, VkBuffer, VkImage, VkImageLayout l, uint32_t, const VkBufferImageCopy*) { last_b2t_layout = l; }
static void vkCmdCopyImageToBuffer(VkCommandBuffer, VkImage, VkImageLayout l, VkBuffer, uint32_t, const VkBufferImageCopy*) { last_t2b_layout = l; }
static void vkCmdCopyImage(VkCommandBuffer, VkImage, VkImageLayout sl, VkImage, VkImageLayout dl, uint32_t, const VkImageCopy*) { last_copy_src = sl; last_copy_dst = dl; }
static void vkCmdBlitImage(VkCommandBuffer, VkImage, VkImageLayout sl, VkImage, VkImageLayout dl, uint32_t, const VkImageBlit*, VkFilter) { last_blit_src = sl; last_blit_dst = dl; }
static void vkCmdResolveImage(VkCommandBuffer, VkImage, VkImageLayout sl, VkImage, VkImageLayout dl, uint32_t, const VkImageResolve*) { last_copy_src = sl; last_copy_dst = dl; }
static void vkCmdClearDepthStencilImage(VkCommandBuffer, VkImage, VkImageLayout l, const VkClearDepthStencilValue*, uint32_t, const VkImageSubresourceRange*) { last_clear_ds = l; }
static void vkCmdClearColorImage(VkCommandBuffer, VkImage, VkImageLayout l, const VkClearColorValue*, uint32_t, const VkImageSubresourceRange*) { last_clear_col = l; }
static void vkCmdBeginRendering(VkCommandBuffer, const VkRenderingInfo* ri) {
    if (ri->pDepthAttachment) last_rend_ds = ri->pDepthAttachment->imageLayout;
}
static void vkCmdClearAttachments(VkCommandBuffer, uint32_t, const VkClearAttachment*, uint32_t, const VkClearRect*) {}
static void vkCmdEndRendering(VkCommandBuffer) {}

''' + copy_b2t_src + "\n" + copy_t2t_src + "\n" + clear_tex_src + r'''

int main() {
    const VkCommandBuffer cmd = reinterpret_cast<VkCommandBuffer>(0x1);
    NvnState st{};
    Texture d1{reinterpret_cast<VkImage>(0x11), VK_FORMAT_D32_SFLOAT, VK_IMAGE_TYPE_2D, VK_IMAGE_ASPECT_DEPTH_BIT, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL};
    Texture d2{reinterpret_cast<VkImage>(0x12), VK_FORMAT_D32_SFLOAT, VK_IMAGE_TYPE_2D, VK_IMAGE_ASPECT_DEPTH_BIT, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL};
    Texture c1{reinterpret_cast<VkImage>(0x13), VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_TYPE_2D, VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_LAYOUT_GENERAL};
    nvn_tex nt_d1{reinterpret_cast<uintptr_t>(&d1), {}};
    nvn_tex nt_d2{reinterpret_cast<uintptr_t>(&d2), {}};
    nvn_tex nt_c1{reinterpret_cast<uintptr_t>(&c1), {}};

    // 1. copy_b2t to depth -> TRANSFER_DST_OPTIMAL; from depth -> TRANSFER_SRC_OPTIMAL.
    nvn_p_copy_bt bt{0x1000, &nt_d1, {}, 0, {0, 0, 0, 64, 64, 1}, 0};
    copy_b2t(cmd, st, bt, true);
    EXPECT(last_b2t_layout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL && d1.layout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
           "copy_b2t to depth must use TRANSFER_DST_OPTIMAL");
    copy_b2t(cmd, st, bt, false);
    EXPECT(last_t2b_layout == VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL && d1.layout == VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
           "copy_b2t from depth must use TRANSFER_SRC_OPTIMAL");

    // 2. copy_b2t colour stays GENERAL.
    nvn_p_copy_bt btc{0x1000, &nt_c1, {}, 0, {0, 0, 0, 64, 64, 1}, 0};
    copy_b2t(cmd, st, btc, true);
    EXPECT(last_b2t_layout == VK_IMAGE_LAYOUT_GENERAL, "copy_b2t to colour stays GENERAL");

    // 3. copy_t2t between distinct depth images -> TRANSFER_SRC_OPTIMAL -> TRANSFER_DST_OPTIMAL.
    nvn_p_copy_tt tt{&nt_d1, &nt_d2, {}, {}, 0, 0, {0, 0, 0, 64, 64, 1}, {0, 0, 0, 64, 64, 1}, 0};
    copy_t2t(cmd, st, tt);
    EXPECT(last_copy_src == VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL && last_copy_dst == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
           "copy_t2t distinct depth images must use TRANSFER_SRC -> TRANSFER_DST");

    // 4. copy_t2t scaled blit between distinct depth images -> TRANSFER_SRC_OPTIMAL -> TRANSFER_DST_OPTIMAL.
    nvn_p_copy_tt ttb{&nt_d1, &nt_d2, {}, {}, 0, 0, {0, 0, 0, 64, 64, 1}, {0, 0, 0, 32, 32, 1}, 0};
    copy_t2t(cmd, st, ttb);
    EXPECT(last_blit_src == VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL && last_blit_dst == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
           "copy_t2t depth blit must use TRANSFER_SRC -> TRANSFER_DST");

    // 5. copy_t2t between different subresources of the SAME depth image -> GENERAL / GENERAL (whole-image tracking safe).
    nvn_view v1{}; v1.set_layers = 1; v1.min_layer = 1;
    nvn_p_copy_tt tts{&nt_d1, &nt_d1, {}, v1, 0, 1, {0, 0, 0, 64, 64, 1}, {0, 0, 0, 64, 64, 1}, 0};
    copy_t2t(cmd, st, tts);
    EXPECT(last_copy_src == VK_IMAGE_LAYOUT_GENERAL && last_copy_dst == VK_IMAGE_LAYOUT_GENERAL &&
               d1.layout == VK_IMAGE_LAYOUT_GENERAL,
           "same-image depth subresource copy must use GENERAL on both sides");

    // 6. clear_tex whole depth -> TRANSFER_DST_OPTIMAL; partial depth -> DEPTH_STENCIL_ATTACHMENT_OPTIMAL; colour -> GENERAL.
    nvn_p_clear_tex cd{&nt_d1, {}, 0, 0, {0, 0, 0, 64, 64, 1}, 0xf, {{1.0f, 0, 0, 0}}};
    clear_tex(cmd, st, cd);
    EXPECT(last_clear_ds == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL && d1.layout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
           "clear_tex whole depth must use TRANSFER_DST_OPTIMAL");
    nvn_p_clear_tex cdp{&nt_d1, {}, 0, 0, {0, 0, 0, 32, 32, 1}, 0xf, {{1.0f, 0, 0, 0}}};
    clear_tex(cmd, st, cdp);
    EXPECT(last_rend_ds == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL &&
               d1.layout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
           "clear_tex partial depth must use DEPTH_STENCIL_ATTACHMENT_OPTIMAL");
    nvn_p_clear_tex cc{&nt_c1, {}, 0, 0, {0, 0, 0, 64, 64, 1}, 0xf, {{0, 0, 0, 0}}};
    clear_tex(cmd, st, cc);
    EXPECT(last_clear_col == VK_IMAGE_LAYOUT_GENERAL, "clear_tex colour stays GENERAL");

    if (fails) return 1;
    std::puts("PASS: vk_xfer depth transfer/clear layouts");
    return 0;
}
'''


def check_auxiliary_depth_sites() -> list[str]:
    bad = []
    draw_src = (VK / "vk_draw.cpp").read_text(encoding="utf-8")
    tex_src = (VK / "vk_texture.cpp").read_text(encoding="utf-8")
    fsr3_src = (VK / "vk_fsr3_inputs.cpp").read_text(encoding="utf-8")
    temp_src = (VK / "vk_temporal.cpp").read_text(encoding="utf-8")
    dump_src = (VK / "vk_dump.cpp").read_text(encoding="utf-8")
    fx_src = (VK / "reshade" / "fx_runtime.cpp").read_text(encoding="utf-8")

    # vk_draw.cpp: build_descriptors must track all sampled depth textures (not only gpu_written) and use READ_ONLY_OPTIMAL.
    build_desc = block(draw_src, "void build_descriptors(")
    if "VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL" not in build_desc:
        bad.append("vk_draw.cpp build_descriptors does not set VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL for depth samples")
    if "depth_samples" not in build_desc:
        bad.append("vk_draw.cpp build_descriptors does not record sampled depth textures independently of gpu_written")

    # vk_draw.cpp: draw() and dispatch() must transition sampled depth textures before rendering/dispatch.
    draw_fn = block(draw_src, "void draw(VkCommandBuffer cmd, NvnState& s, const DrawArgs& d)")
    if "gfx_depth_sampled" not in draw_fn:
        bad.append("vk_draw.cpp draw() does not keep sampled depth textures across descriptor reuse")
    if "if (t->layout != want) end_pass(cmd);\n        texture_transition_layout(cmd, t, want);" not in draw_fn:
        bad.append("vk_draw.cpp draw() must call texture_transition_layout(cmd, t, want) unconditionally so same-layout r.cmd uses stamp layout_cmd_rec")
    if "cb.desc.valid = false;" not in draw_fn:
        bad.append("vk_draw.cpp draw() does not invalidate cb.desc before SKIP_HERE() when !same_desc")
    fb_dist = draw_fn.find("if (depth_fb || prev_depth_fb) descriptors_disturbed();")
    fb_rec = draw_fn.find("fallback_record(cmd, p, d)")
    if fb_dist < 0 or fb_rec < 0 or fb_dist > fb_rec:
        bad.append("vk_draw.cpp draw() must call descriptors_disturbed() on depth_fb toggle before fallback_record()")

    disp_fn = block(draw_src, "void dispatch(VkCommandBuffer cmd, NvnState& s,")
    if "depth_samples" not in disp_fn or "texture_transition_layout" not in disp_fn:
        bad.append("vk_draw.cpp dispatch() does not transition sampled depth textures to DEPTH_STENCIL_READ_ONLY_OPTIMAL")

    # vk_texture.cpp: upload_all_zero, upload_inner, compact_step must transition depth textures and guard r.upload ordering.
    for fn_sig in ("bool upload_all_zero(", "bool upload_inner(Texture& tex, const Ranges* dirty) {"):
        fn = block(tex_src, fn_sig)
        if "vkCmdClearDepthStencilImage(upload_cmd(), tex.image, VK_IMAGE_LAYOUT_GENERAL" in fn:
            bad.append(f"vk_texture.cpp {fn_sig} still clears depth image in VK_IMAGE_LAYOUT_GENERAL")
    up_inner = block(tex_src, "bool upload_inner(Texture& tex, const Ranges* dirty) {")
    if "VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL" not in up_inner or "UploadLayoutGuard" not in up_inner:
        bad.append("vk_texture.cpp upload_inner does not guard depth uploads with UploadLayoutGuard")
    compact_ctx = block(tex_src, "void compact_step(")
    if "is_depth_texture(t)" not in compact_ctx or "VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL" not in compact_ctx:
        bad.append("vk_texture.cpp compaction copy does not handle depth texture optimal layouts")
    if "t->layout_cmd_rec != up_rec_id" not in compact_ctx:
        bad.append("vk_texture.cpp compact_step does not skip textures already transitioned in r.cmd")

    # vk_fsr3_inputs.cpp: prepare() must transition cam.depth to DEPTH_STENCIL_READ_ONLY_OPTIMAL.
    fsr_prep = block(fsr3_src, "bool prepare(")
    if "texture_transition_layout(cmd, cam.depth, VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL)" not in fsr_prep:
        bad.append("vk_fsr3_inputs.cpp prepare() does not transition cam.depth to DEPTH_STENCIL_READ_ONLY_OPTIMAL")
    if "{s.point, dview, VK_IMAGE_LAYOUT_GENERAL}" in fsr_prep:
        bad.append("vk_fsr3_inputs.cpp prepare() still binds dview with VK_IMAGE_LAYOUT_GENERAL")

    # vk_temporal.cpp: actor_motion, temporal_fg_motion, temporal_upscale, temporal_reshade_depth.
    if "{s.point, dview, VK_IMAGE_LAYOUT_GENERAL}" in temp_src:
        bad.append("vk_temporal.cpp still binds scene depth dview with VK_IMAGE_LAYOUT_GENERAL")
    if "texture_transition_layout(" not in temp_src:
        bad.append("vk_temporal.cpp does not transition scene depth to DEPTH_STENCIL_READ_ONLY_OPTIMAL")

    # vk_dump.cpp: depth dump must transition to TRANSFER_SRC_OPTIMAL.
    if "texture_transition_layout(" not in dump_src:
        bad.append("vk_dump.cpp does not transition depth textures to TRANSFER_SRC_OPTIMAL before dump copy")

    # reshade/fx_runtime.cpp: gameplay depth binding must use DEPTH_STENCIL_READ_ONLY_OPTIMAL.
    if "b.depth ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL" not in fx_src:
        bad.append("reshade/fx_runtime.cpp still binds gameplay depth with VK_IMAGE_LAYOUT_GENERAL")

    return bad


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default="clang++")
    p.add_argument("--headers", required=True, type=Path)
    a = p.parse_args()

    tex_src = (VK / "vk_texture.cpp").read_text(encoding="utf-8")
    draw_src = (VK / "vk_draw.cpp").read_text(encoding="utf-8")
    xfer_src = (VK / "vk_xfer.cpp").read_text(encoding="utf-8")

    failures = []
    with tempfile.TemporaryDirectory(prefix="target-layouts-") as tmp_s:
        tmp = Path(tmp_s)

        # 1. create() init block
        rc, out, err = build_and_run(a.cxx, a.headers, "depth_init", make_init_fixture(extract_create_init(tex_src)), tmp)
        if out:
            print(out, end="")
        if rc:
            failures.append("create() depth init:\n" + err.strip())

        # 2. texture_transition_layout + UploadLayoutGuard (if present)
        if "void texture_transition_layout(" in tex_src and "struct UploadLayoutGuard {" in tex_src:
            emit_fn = block(tex_src, "void emit_layout_barrier(")
            guard_st = block(tex_src, "struct UploadLayoutGuard {")
            fn = block(tex_src, "void texture_transition_layout(")
            rc, out, err = build_and_run(a.cxx, a.headers, "depth_trans", make_transition_fixture(emit_fn, guard_st, fn), tmp)
            if out:
                print(out, end="")
            if rc:
                failures.append("texture_transition_layout:\n" + err.strip())
        else:
            failures.append("texture_transition_layout / UploadLayoutGuard not defined in gpu/vk/vk_texture.cpp")

        # 3. begin_pass + clear_depth_stencil + depth_attachment_layout
        begin_src = block(draw_src, "void begin_pass(")
        clear_src = block(draw_src, "void clear_depth_stencil(")
        helper_src = block(draw_src, "VkImageLayout depth_attachment_layout(") if "VkImageLayout depth_attachment_layout(" in draw_src else ""
        rc, out, err = build_and_run(a.cxx, a.headers, "depth_draw", make_draw_fixture(begin_src, clear_src, helper_src), tmp)
        if out:
            print(out, end="")
        if rc:
            failures.append("begin_pass / clear_depth_stencil:\n" + err.strip())

        # 4. vk_xfer.cpp copy_b2t / copy_t2t / clear_tex
        rc, out, err = build_and_run(
            a.cxx, a.headers, "depth_xfer",
            make_xfer_fixture(
                block(xfer_src, "void copy_b2t("),
                block(xfer_src, "void copy_t2t("),
                block(xfer_src, "void clear_tex("),
            ),
            tmp,
        )
        if out:
            print(out, end="")
        if rc:
            failures.append("vk_xfer:\n" + err.strip())

    # 5. Auxiliary depth sampling/upload/compaction/dump/upscaler sites
    aux_bad = check_auxiliary_depth_sites()
    if aux_bad:
        failures.append("auxiliary depth sites:\n" + "\n".join("FAIL: " + x for x in aux_bad))

    if failures:
        raise SystemExit("\n\n".join(failures))
    print("PASS: depth/stencil optimal layouts and fast Z-clear verified across vk_texture, vk_draw, vk_xfer, and consumers")


if __name__ == "__main__":
    main()
