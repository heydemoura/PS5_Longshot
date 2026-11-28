"""Game-free checks for 32-bit SDR FSR 3.1 temporal targets, cropped fg_scale UVs, and SDR RGBA8 HUD layer.

Exercises the real C++ functions cut out of gpu/vk/vk_framegen.cpp, gpu/vk/vk_temporal.cpp, and gpu/vk/vk_fsr3.cpp,
plus the real GLSL helpers cut out of gpu/vk/shaders/fg_scale.comp, fg_hud_layer.comp, fg_hud_composite.comp, and
dlss_prep.comp:
  1. SDR hud.layer allocates VK_FORMAT_R8G8B8A8_UNORM (preserving VK_FORMAT_R16G16B16A16_SFLOAT in HDR and keeping
     hud.fit at RGBA16F), releases fit_pipe if over_pipe setup fails, and pack_layer/unpack_layer preserve unknown,
     no-HUD, opaque, translucent, and additive HUD values through 8-bit UNORM quantization.
  2. Cropped framegen_scale scaling passes sample src directly with push-constant crop UVs (zero scale.cropped
     blit/copy or full_barrier for both SDR and Native HDR PQ), while CRT-only cropped present still crops for CRT,
     SPIR-V PushConstant offsets match the 36-byte scalar C++ struct, and crop_uv() clamps strictly inside crop texels.
  3. Temporal format selection picks VK_FORMAT_B10G11R11_UFLOAT_PACK32 for SDR FSR 3.1 when
     shaderStorageImageExtendedFormats + storage/sampled features are supported and preserves RGBA16F for HDR,
     options.hdr != 0, PsychoV35, pre-bloom, FSR 4, XeSS, DLSS, or unsupported GPUs; dlss_prep_packed_comp_spv.h
     matches dlss_prep.comp with R11fG11fB10f storage image; FSR 3.1 accepts B10G11R11 on its direct-output path.

    python -I -B tests/test_temporal_present_bandwidth.py --headers <Vulkan-Headers/include> [--cxx g++] [--glslang glslangValidator] [--write]
"""
import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from srccut import between, block  # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}", "--headers", "{vulkan_headers}", "--glslang", "{glslang}"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"
SHADERS = VK / "shaders"
ENV_ON_H = str(VK / "compat" / "longshot_env.h")


def packed_prep_source_line(comp):
    digest = hashlib.sha256(comp.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return ("// dlss_prep.comp sha256 %s (tests/test_temporal_present_bandwidth.py --write: "
            "glslangValidator -V --target-env vulkan1.3 -DDLSS_PREP_PACKED=1 --vn DLSS_PREP_PACKED_COMP_SPV)\n"
            % digest).encode()


def compile_packed_prep(glslang, comp, out):
    r = subprocess.run([glslang, "-V", "--target-env", "vulkan1.3", "-DDLSS_PREP_PACKED=1",
                        "--vn", "DLSS_PREP_PACKED_COMP_SPV", "-o", str(out), str(comp)],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("dlss_prep.comp (-DDLSS_PREP_PACKED=1) does not compile:\n" + r.stdout + r.stderr)
    return out.read_bytes()


def without_version(header):
    return header.partition(b"\n")[2]


def run_cpp(cxx, code, prefix, extra_flags=()):
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        cpp = Path(tmp) / "fixture.cpp"
        exe = Path(tmp) / "fixture.exe"
        cpp.write_text(code, encoding="utf-8")
        subprocess.run([cxx, "-std=c++20", "-O1", "-Wall", "-Wextra", "-Werror",
                        "-Wno-missing-field-initializers", "-Wno-unused-function", "-Wno-unused-parameter",
                        *extra_flags, str(cpp), "-o", str(exe)], check=True)
        r = subprocess.run([str(exe)], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit((r.stdout + r.stderr).strip() or f"{prefix} exited {r.returncode}")
        return r.stdout.strip()


# ---- 1. SDR hud.layer format (RGBA8 in SDR vs RGBA16F in HDR) & pack/unpack parity ----

HUD_PREFIX = r'''
#include <algorithm>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <initializer_list>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#define VK_NO_PROTOTYPES
#define VK_USE_64_BIT_PTR_DEFINES 0
#include <vulkan/vulkan.h>
using VmaAllocation = void*;
constexpr int kMaxMult = 8;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL hud line %d: %s\n", __LINE__, #x); std::exit(1); } } while (0)
'''

HUD_STUBS = r'''
struct { int framegen = 2, hdr_peak = 1000, hdr = 0; } opts;
struct { bool hdr = false; } dev;
constexpr uint32_t FG_HUD_COMP_SPV[] = {0};
[[maybe_unused]] constexpr uint32_t FG_HUD_LAYER_COMP_SPV[] = {1};
[[maybe_unused]] constexpr uint32_t FG_HUD_COMPOSITE_COMP_SPV[] = {2};
[[maybe_unused]] constexpr uint32_t FG_HUD_LAYER_RGBA8_COMP_SPV[] = {3};
[[maybe_unused]] constexpr uint32_t FG_HUD_COMPOSITE_RGBA8_COMP_SPV[] = {4};
#define BUILTIN_SPV_ARGS(words) words, sizeof words
struct Texture {
    VkImage image = VK_NULL_HANDLE;
    VkFormat format = VK_FORMAT_UNDEFINED;
    VkImageType type = VK_IMAGE_TYPE_2D;
    uint32_t width = 1, height = 1, levels = 1, layers = 1;
    VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT;
    uint64_t gpu = 0;
};
void logf(const char*, ...) {}
uint64_t next_handle = 0x1000;
template <class T> T handle() { return (T)(uintptr_t)(next_handle += 0x10); }
std::vector<std::pair<void*, VkFormat>> img_allocs;
std::vector<uint32_t> pipe_codes;
uint32_t fail_pipe_code = 0;
int dropped_pipes = 0;
void drop_pipe(Pipe& p) {
    if (p.pipe || p.layout || p.set) ++dropped_pipes;
    p = {};
}
bool make_pipe(Pipe& p, const uint32_t* code, size_t, std::initializer_list<VkDescriptorType>, uint32_t) {
    pipe_codes.push_back(code[0]);
    if (fail_pipe_code && code[0] == fail_pipe_code) return false;
    p.pipe = handle<VkPipeline>(), p.layout = handle<VkPipelineLayout>(), p.set = handle<VkDescriptorSetLayout>();
    return true;
}
bool make_img(Img& i, uint32_t, uint32_t, VkFormat f, VkImageUsageFlags) {
    i.image = handle<VkImage>(), i.view = handle<VkImageView>();
    img_allocs.push_back({&i, f});
    return true;
}
void drop_img(Img& i) { i = {}; }
void to_general(VkCommandBuffer, VkImage) {}
void full_barrier(VkCommandBuffer) {}
void vkCmdCopyImage(VkCommandBuffer, VkImage, VkImageLayout, VkImage, VkImageLayout, uint32_t, const VkImageCopy*) {}
std::vector<VkPipeline> dispatched_pipes;
void pass(VkCommandBuffer, const Pipe& p, std::initializer_list<std::pair<VkImageView, VkSampler>>,
          uint32_t, uint32_t, const void* = nullptr, uint32_t = 0) {
    dispatched_pipes.push_back(p.pipe);
}
uint64_t frame_no = 100;
uint64_t texture_frame_no() { return frame_no; }
std::vector<std::pair<uint64_t, const Texture*>> alive;
bool texture_alive(uint64_t gpu, const Texture* t, VkImage image) {
    for (const auto& [g, a] : alive)
        if (g == gpu && a == t) return t->image == image;
    return false;
}
uint64_t vk_retirement_value() { return 1; }
uint64_t vk_timeline_completed() { return 1; }
bool framegen_on() { return true; }
bool fsr_given = false;
namespace fsr3_math { struct Camera { float near_plane = 0, far_plane = 0, device_to_view_depth[4]{}, tan_half_fov_x = 0; }; }
namespace fsr3_inputs {
struct Frame {
    uint64_t frame = 0;
    VkImageView depth = handle<VkImageView>(), motion = handle<VkImageView>();
    uint32_t width = 1280, height = 720;
    float jitter[2]{};
    fsr3_math::Camera camera;
    float frame_ms = 16.667f;
    bool reset = false;
};
bool take(Frame& f) { f = Frame{}; f.frame = frame_no; return true; }
uint64_t motions_taken() { return 0; }
}
namespace fsr3fi {
uint32_t frames_wanted() { return 1u; }
struct Inputs {
    VkImageView color = VK_NULL_HANDLE;
    VkImage color_image = VK_NULL_HANDLE;
    VkFormat color_format = VK_FORMAT_UNDEFINED;
    bool native_pq = false;
    bool hud_mask = false;
    VkImageView present = VK_NULL_HANDLE;
    float max_luminance = 1.0f;
    uint32_t width = 0, height = 0;
    VkImageView depth = VK_NULL_HANDLE, motion = VK_NULL_HANDLE;
    uint32_t render_width = 0, render_height = 0;
    float jitter[2]{};
    fsr3_math::Camera camera;
    float frame_time_ms = 0;
    bool reset = false;
    uint64_t sequence = 0;
};
uint32_t generate(VkCommandBuffer, const Inputs&, const float*, uint32_t count) { return count; }
VkImage made_image(uint32_t) { return (VkImage)(uintptr_t)0x77; }
}
'''

HUD_MAIN = r'''
static VkFormat last_format_for(void* ptr) {
    for (auto it = img_allocs.rbegin(); it != img_allocs.rend(); ++it)
        if (it->first == ptr) return it->second;
    return VK_FORMAT_UNDEFINED;
}
int main() {
    const VkCommandBuffer cmd = (VkCommandBuffer)(uintptr_t)1;
    fg.frames[0] = {handle<VkImage>(), nullptr, handle<VkImageView>()};
    fg.frames[1] = {handle<VkImage>(), nullptr, handle<VkImageView>()};
    OutSet set{};
    for (Img& f : set.frame) f.image = handle<VkImage>(), f.view = handle<VkImageView>();
    const float t[1] = {0.5f};
    const int slot[1] = {0};
    Texture picture;
    picture.image = handle<VkImage>(), picture.format = VK_FORMAT_B10G11R11_UFLOAT_PACK32;
    picture.width = 2880, picture.height = 1800, picture.gpu = 0x80000000;
    alive.push_back({picture.gpu, &picture});

    // SDR: hud.layer must allocate VK_FORMAT_R8G8B8A8_UNORM while hud.fit stays RGBA16F.
    opts.hdr = 0; dev.hdr = false;
    framegen_hud_before(cmd, &picture, 0);
    CHECK(hud_source(cmd, 0, 2880, 1800, 0, 0, 2880, 1800, false));
    CHECK(fsr3fi_frames(cmd, set, 0, 2880, 1800, 1, false, t, slot, 1) == 1);
    if (last_format_for(&hud.layer) != VK_FORMAT_R8G8B8A8_UNORM) {
        std::fprintf(stderr, "FAIL: SDR hud.layer format was %d, expected VK_FORMAT_R8G8B8A8_UNORM (%d)\n",
                     (int)last_format_for(&hud.layer), (int)VK_FORMAT_R8G8B8A8_UNORM);
        return 1;
    }
    CHECK(last_format_for(&hud.fit) == VK_FORMAT_R16G16B16A16_SFLOAT);

    // HDR composition active: hud.layer must switch to VK_FORMAT_R16G16B16A16_SFLOAT.
    opts.hdr = 1; dev.hdr = true;
    framegen_hud_before(cmd, &picture, 0);
    CHECK(hud_source(cmd, 1, 2880, 1800, 0, 0, 2880, 1800, false));
    CHECK(fsr3fi_frames(cmd, set, 1, 2880, 1800, 2, false, t, slot, 1) == 1);
    CHECK(last_format_for(&hud.layer) == VK_FORMAT_R16G16B16A16_SFLOAT);
    CHECK(last_format_for(&hud.fit) == VK_FORMAT_R16G16B16A16_SFLOAT);

    // Back to SDR: hud.layer returns to VK_FORMAT_R8G8B8A8_UNORM.
    opts.hdr = 0; dev.hdr = false;
    framegen_hud_before(cmd, &picture, 0);
    CHECK(hud_source(cmd, 0, 2880, 1800, 0, 0, 2880, 1800, false));
    CHECK(last_format_for(&hud.layer) == VK_FORMAT_R8G8B8A8_UNORM);

    // Partial pipeline setup failure: if over_pipe fails after fit_pipe succeeds, fit_pipe is released (no leak).
    hud.fit_pipe_rgba8 = {};
    hud.over_pipe_rgba8 = {};
    hud.layer_failed = false;
    dropped_pipes = 0;
    fail_pipe_code = FG_HUD_COMPOSITE_RGBA8_COMP_SPV[0];
    CHECK(!hud_layer(cmd, 0, 0, 2880, 1800, false));
    CHECK(hud.layer_failed && !hud.fit_pipe_rgba8.pipe && !hud.over_pipe_rgba8.pipe && dropped_pipes == 1);
    fail_pipe_code = 0;
    std::puts("PASS: SDR hud.layer uses RGBA8, HDR preserves RGBA16F, and partial pipe failure drops fit_pipe");
    return 0;
}
'''

HUD_PACK_SHIM = r'''
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
struct vec3 {
    float r, g, b;
    vec3() : r(0), g(0), b(0) {}
    explicit vec3(float v) : r(v), g(v), b(v) {}
    vec3(float x, float y, float z) : r(x), g(y), b(z) {}
};
struct vec4 {
    float x, y, z, w;
    vec4() : x(0), y(0), z(0), w(0) {}
    vec4(float a, float b, float c, float d) : x(a), y(b), z(c), w(d) {}
    vec4(vec3 v, float d) : x(v.r), y(v.g), z(v.b), w(d) {}
    vec3 rgb() const { return {x, y, z}; }
};
static float clamp(float v, float lo, float hi) { return std::min(std::max(v, lo), hi); }
static vec3 clamp(vec3 v, float lo, float hi) { return {clamp(v.r, lo, hi), clamp(v.g, lo, hi), clamp(v.b, lo, hi)}; }
static vec3 sqrt(vec3 v) { return {std::sqrt(v.r), std::sqrt(v.g), std::sqrt(v.b)}; }
static vec3 operator*(vec3 a, vec3 b) { return {a.r * b.r, a.g * b.g, a.b * b.b}; }
static float q8(float v) { return std::round(clamp(v, 0.0f, 1.0f) * 255.0f) / 255.0f; }
static vec4 quantize_rgba8(vec4 v) { return {q8(v.x), q8(v.y), q8(v.z), q8(v.w)}; }
static float srgb1(float v) {
    v = clamp(v, 0.0f, 1.0f);
    return v <= 0.0031308f ? 12.92f * v : 1.055f * std::pow(v, 1.0f / 2.4f) - 0.055f;
}
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL pack line %d: %s\n", __LINE__, #x); std::exit(1); } } while (0)
'''

HUD_PACK_MAIN = r'''
int main() {
    // Unknown sentinel (T < 0) round-trips to T < 0 after RGBA8 quantization.
    const vec4 unk = unpack_layer(quantize_rgba8(pack_layer(vec4(0.0f, 0.0f, 0.0f, -1.0f))));
    CHECK(unk.w < 0.0f);
    // No HUD sentinel (0, 0, 0, 1) stays exact (0, 0, 0, 1) in packed form for early-out.
    const vec4 no_hud_packed = quantize_rgba8(pack_layer(vec4(0.0f, 0.0f, 0.0f, 1.0f)));
    CHECK(no_hud_packed.x == 0.0f && no_hud_packed.y == 0.0f && no_hud_packed.z == 0.0f && no_hud_packed.w == 1.0f);
    // Opaque (T=0), translucent (T=0.55), and additive (T=1) round-trip within <= 2/255 sRGB.
    for (float t : {0.0f, 0.15f, 0.55f, 0.9f, 1.0f}) {
        for (float b : {0.0f, 0.005f, 0.05f, 0.30f, 0.80f, 1.0f}) {
            const vec4 raw(b, b * 0.5f, b * 0.25f, t);
            const vec4 dec = unpack_layer(quantize_rgba8(pack_layer(raw)));
            CHECK(dec.w >= 0.0f && std::fabs(dec.w - t) <= 0.5f / 254.0f + 1e-5f);
            CHECK(std::fabs(srgb1(dec.x) - srgb1(raw.x)) <= 2.0f / 255.0f);
            CHECK(std::fabs(srgb1(dec.y) - srgb1(raw.y)) <= 2.0f / 255.0f);
            CHECK(std::fabs(srgb1(dec.z) - srgb1(raw.z)) <= 2.0f / 255.0f);
        }
    }
    std::puts("PASS: RGBA8 hud.layer pack/unpack round-trip");
    return 0;
}
'''


def check_hud_layer(cxx, headers):
    fg = (VK / "vk_framegen.cpp").read_text(encoding="utf-8")
    structs = between(fg, "struct Img {", "int64_t now_ns()")
    hud = between(fg, "struct HudState {", "void hud_retire(")
    markers = ("void hud_retire(", "void hud_free_dead(", "bool hud_image(",
               "bool hud_layer_wanted(", "bool hudless_wanted(", "VkFormat hud_layer_format(",
               "bool hud_layer(", "bool hud_source(", "void framegen_hud_before(", "int fsr3fi_frames(")
    fns = "\n".join(block(fg, m) for m in markers if m in fg)
    print(run_cpp(cxx, HUD_PREFIX + structs + HUD_STUBS + hud + fns + HUD_MAIN, "hud-layer-fmt-",
                  ["-pthread", "-I" + str(headers)]))

    layer_glsl = (SHADERS / "fg_hud_layer.comp").read_text(encoding="utf-8")
    comp_glsl = (SHADERS / "fg_hud_composite.comp").read_text(encoding="utf-8")
    if "vec4 pack_layer(" not in layer_glsl or "vec4 unpack_layer(" not in comp_glsl:
        raise SystemExit("FAIL: fg_hud_layer.comp / fg_hud_composite.comp lack RGBA8 pack_layer / unpack_layer")
    pack_fn = block(layer_glsl, "vec4 pack_layer(").replace(".rgb", ".rgb()").replace("l.a", "l.w")
    unpack_fn = block(comp_glsl, "vec4 unpack_layer(").replace(".rgb", ".rgb()").replace("p.a", "p.w")
    print(run_cpp(cxx, HUD_PACK_SHIM + "static " + pack_fn + "\nstatic " + unpack_fn + "\n" + HUD_PACK_MAIN,
                  "hud-layer-pack-"))


# ---- 2. Cropped fg_scale direct UVs & zero-blit dispatch ----

SCALE_TEMPLATE = r'''
#ifdef _WIN32
#define _CRT_SECURE_NO_WARNINGS
#endif
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <mutex>
#include <utility>
#include "vk_present_rect.h"
#include "crt_policy.h"
namespace crt_policy = mkwvk::crt_policy;
using mkwvk::PresentSourceRect;
using mkwvk::present_source_rect;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL scale line %d: %s\n", __LINE__, #x); std::exit(1); } } while (0)
using VkImage = uint64_t;
using VkImageView = uint64_t;
using VkSampler = uint64_t;
using VkCommandBuffer = int;
using VkDescriptorType = int;
using VkImageUsageFlags = int;
using VkFormat = int;
constexpr uint64_t VK_NULL_HANDLE = 0;
constexpr int VK_SUCCESS = 0, VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO = 1;
constexpr int VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER = 2, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE = 3;
constexpr int VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE = 4, VK_FILTER_LINEAR = 5, VK_FILTER_NEAREST = 6;
constexpr int VK_FORMAT_R8G8B8A8_UNORM = 7, VK_IMAGE_LAYOUT_GENERAL = 8, VK_IMAGE_ASPECT_COLOR_BIT = 9;
constexpr int VK_FORMAT_A2B10G10R10_UNORM_PACK32 = 10;
constexpr int VK_IMAGE_USAGE_SAMPLED_BIT = 1, VK_IMAGE_USAGE_TRANSFER_DST_BIT = 2;
constexpr int VK_IMAGE_USAGE_STORAGE_BIT = 4, VK_IMAGE_USAGE_TRANSFER_SRC_BIT = 8;
const uint32_t FG_SCALE_COMP_SPV[] = {123}, FG_SCALE_PQ_COMP_SPV[] = {456};
#define BUILTIN_SPV_ARGS(words) words, sizeof words
struct Img { VkImage image = 0; VkImageView view = 0; };
struct Pipe { uint64_t set = 0, layout = 0, pipe = 0; };
struct VkSamplerCreateInfo {
    int sType, addressModeU = 0, addressModeV = 0, addressModeW = 0;
    float maxLod = 0; int magFilter = 0, minFilter = 0;
};
struct Offset { int x = 0, y = 0, z = 0; };
struct Layers { int aspect = 0, level = 0, first = 0, count = 0; };
struct VkImageBlit { Layers srcSubresource, dstSubresource; Offset srcOffsets[2], dstOffsets[2]; };
struct Extent { uint32_t width = 0, height = 0, depth = 0; };
struct VkImageCopy { Layers srcSubresource; Offset srcOffset; Layers dstSubresource; Offset dstOffset; Extent extent; };
struct { int upscale = 0, sharpness = 0, framegen = 0, crt_filter = 0; } opts;
struct SpyMutex { void lock() {} void unlock() {} };
struct { bool push_descriptor = true, offscreen = false; int device = 1; SpyMutex present_mu; } dev;
struct { bool failed = false; } fg;
bool vkCmdPushDescriptorSetKHR = true;
int blits = 0, copies = 0, dispatches = 0, crt_calls = 0;
uint32_t last_push_bytes = 0, last_pipe_push = 0;
int last_crop[4] = {-1, -1, -1, -1};
VkImageView last_sampled_view = 0, crt_source = 0;
uint64_t next_handle = 100;
bool make_pipe(Pipe& p, const uint32_t* code, size_t, std::initializer_list<int>, uint32_t push) {
    last_pipe_push = push;
    p.set = 1; p.layout = 2; p.pipe = code == FG_SCALE_PQ_COMP_SPV ? 5 : 3;
    return true;
}
int vkCreateSampler(int, const VkSamplerCreateInfo*, void*, VkSampler* out) { *out = 4; return VK_SUCCESS; }
void vkDestroyPipeline(int, uint64_t, void*) {}
void vkDestroyPipelineLayout(int, uint64_t, void*) {}
void vkDestroyDescriptorSetLayout(int, uint64_t, void*) {}
void device_wait_idle_locked() {}
void drop_img(Img& i) { i = {}; }
bool make_img(Img& i, uint32_t, uint32_t, int, int) { i.image = ++next_handle; i.view = ++next_handle; return true; }
void to_general(int, VkImage) {}
void full_barrier(int) {}
void vkCmdBlitImage(int, VkImage, int, VkImage, int, int, const VkImageBlit*, int) { ++blits; }
void vkCmdCopyImage(int, VkImage, int, VkImage, int, int, const VkImageCopy*) { ++copies; }
void pass(int, const Pipe&, std::initializer_list<std::pair<VkImageView, VkSampler>> b,
          uint32_t, uint32_t, const void* data, uint32_t bytes) {
    ++dispatches;
    last_push_bytes = bytes;
    last_sampled_view = b.begin()->first;
    if (bytes >= 36) std::memcpy(last_crop, (const char*)data + 20, sizeof last_crop);
}
void logf(const char*, ...) {}
VkImage record_crt(int, VkImageView src, uint32_t, uint32_t, uint32_t, uint32_t, VkImageView& view) {
    ++crt_calls; crt_source = src; view = 99; return 98;
}
// PRODUCTION_FUNCTIONS
static void reset() {
    scale = {}; opts = {}; wide_now = WideSides{};
    blits = copies = dispatches = crt_calls = 0;
    last_push_bytes = last_pipe_push = 0;
    last_sampled_view = crt_source = 0;
    for (int& v : last_crop) v = -1;
}
int main() {
    // 16:9 crop inside 16:10 texture with Lanczos2 + RCAS: must sample src directly with zero crop blit/copy.
    reset(); opts.upscale = 2; opts.sharpness = 50;
    const int crop169[] = {0, 80, 2560, 1440};
    CHECK(framegen_scale(1, 12, 11, 2560, 1600, crop169, 2880, 1620, false));
    if (blits != 0 || copies != 0 || last_sampled_view != 12) {
        std::fprintf(stderr, "FAIL: cropped SDR scaling still copied/blitted (blits=%d copies=%d view=%llu)\n",
                     blits, copies, (unsigned long long)last_sampled_view);
        return 1;
    }
    CHECK(last_push_bytes == 36 && last_pipe_push == 36 && !scale.cropped.image);
    CHECK(last_crop[0] == 0 && last_crop[1] == 80 && last_crop[2] == 2560 && last_crop[3] == 1440);

    // Native HDR PQ cropped scaling: zero vkCmdCopyImage into scale.cropped.
    CHECK(framegen_scale(1, 12, 11, 2560, 1600, crop169, 2880, 1620, true));
    CHECK(blits == 0 && copies == 0 && last_sampled_view == 12 && !scale.cropped.image);
    CHECK(last_crop[0] == 0 && last_crop[1] == 80 && last_crop[2] == 2560 && last_crop[3] == 1440);

    // Clipped crop passes validated extent to shader.
    const int clipped[] = {2500, 1400, 100, 100};
    CHECK(framegen_scale(1, 12, 11, 2560, 1440, clipped, 1920, 1080, false));
    CHECK(blits == 0 && copies == 0 && last_sampled_view == 12);
    CHECK(last_crop[0] == 2500 && last_crop[1] == 1400 && last_crop[2] == 60 && last_crop[3] == 40);

    // Cropped ultrawide blurred menu sides: both side=3 downsample and main pass read src directly with crop.
    reset(); wide_now = WideSides{0.25f, 1};
    CHECK(framegen_scale(1, 12, 11, 2560, 1600, crop169, 2880, 1620, false));
    CHECK(blits == 0 && copies == 0 && dispatches == 2 && last_sampled_view == 12 && !scale.cropped.image);
    CHECK(last_crop[0] == 0 && last_crop[1] == 80 && last_crop[2] == 2560 && last_crop[3] == 1440);

    // CRT-only cropped present (no scaling) still crops into scale.cropped for record_crt.
    reset(); opts.crt_filter = 1;
    CHECK(framegen_scale(1, 12, 11, 2560, 1600, crop169, 2880, 1620, false) == 98);
    CHECK(blits == 1 && crt_calls == 1 && crt_source == scale.cropped.view);
    std::puts("PASS: cropped fg_scale samples src directly with zero blit/copy; CRT-only preserves crop");
    return 0;
}
'''

CROP_UV_SHIM = r'''
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
struct ivec2 {
    int x, y;
    ivec2(int a = 0, int b = 0) : x(a), y(b) {}
};
struct vec2 {
    float x, y;
    vec2(float a = 0, float b = 0) : x(a), y(b) {}
    vec2(int a, int b) : x((float)a), y((float)b) {}
    explicit vec2(ivec2 v) : x((float)v.x), y((float)v.y) {}
};
static vec2 operator+(vec2 a, vec2 b) { return {a.x + b.x, a.y + b.y}; }
static vec2 operator+(vec2 a, float s) { return {a.x + s, a.y + s}; }
static vec2 operator-(vec2 a, float s) { return {a.x - s, a.y - s}; }
static vec2 operator*(vec2 a, vec2 b) { return {a.x * b.x, a.y * b.y}; }
static vec2 operator/(vec2 a, vec2 b) { return {a.x / b.x, a.y / b.y}; }
static vec2 clamp(vec2 v, vec2 lo, vec2 hi) { return {std::clamp(v.x, lo.x, hi.x), std::clamp(v.y, lo.y, hi.y)}; }
static int src = 0;
static ivec2 g_tex_size{2560, 1600};
static ivec2 textureSize(int, int) { return g_tex_size; }
static struct { int crop_x, crop_y, crop_w, crop_h; } pc;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL crop_uv line %d: %s\n", __LINE__, #x); std::exit(1); } } while (0)
'''

CROP_UV_MAIN = r'''
int main() {
    g_tex_size = {2560, 1600};
    pc = {0, 80, 2560, 1440};
    const float lo_x = 0.5f / 2560.0f, hi_x = 2559.5f / 2560.0f;
    const float lo_y = 80.5f / 1600.0f, hi_y = 1519.5f / 1600.0f;
    // Edges & out-of-bounds clamp strictly inside crop texel centers (zero bleed into y < 80 or y >= 1520).
    for (float u : {-0.5f, 0.0f, 0.5f / 2560.0f, 0.5f, 1.0f, 1.5f}) {
        for (float v : {-0.5f, 0.0f, 0.5f / 1440.0f, 0.5f, 1.0f, 1.5f}) {
            const vec2 r = crop_uv({u, v});
            CHECK(r.x >= lo_x - 1e-6f && r.x <= hi_x + 1e-6f);
            CHECK(r.y >= lo_y - 1e-6f && r.y <= hi_y + 1e-6f);
        }
    }
    const vec2 mid = crop_uv({0.5f, 0.5f});
    CHECK(std::fabs(mid.x - 0.5f) < 1e-6f && std::fabs(mid.y - 0.5f) < 1e-6f);
    // Asymmetric crop (160, 90, 2240, 1260) inside 2560x1440.
    g_tex_size = {2560, 1440};
    pc = {160, 90, 2240, 1260};
    const vec2 tl = crop_uv({0.0f, 0.0f}), br = crop_uv({1.0f, 1.0f});
    CHECK(std::fabs(tl.x - 160.5f / 2560.0f) < 1e-6f && std::fabs(tl.y - 90.5f / 1440.0f) < 1e-6f);
    CHECK(std::fabs(br.x - 2399.5f / 2560.0f) < 1e-6f && std::fabs(br.y - 1349.5f / 1440.0f) < 1e-6f);
    std::puts("PASS: fg_scale crop_uv maps and clamps strictly inside crop texel centers");
    return 0;
}
'''


def spv_push_constant_offsets(header_path):
    """Parse SPIR-V OpMemberDecorate Offset entries for the PushConstant struct in a *_spv.h header."""
    words = [int(w, 16) for w in re.findall(r"0x[0-9a-fA-F]{8}", header_path.read_text(encoding="utf-8"))]
    if len(words) < 5 or words[0] != 0x07230203:
        raise SystemExit("FAIL: %s does not contain valid SPIR-V words" % header_path.name)
    ptr_target = {}
    pc_ptr_types = set()
    member_offsets = {}
    i = 5
    while i < len(words):
        wc = words[i] >> 16
        op = words[i] & 0xFFFF
        if wc == 0 or i + wc > len(words):
            raise SystemExit("FAIL: truncated SPIR-V instruction in %s" % header_path.name)
        if op == 32 and wc >= 4 and words[i + 2] == 9:       # OpTypePointer PushConstant
            ptr_target[words[i + 1]] = words[i + 3]
        elif op == 59 and wc >= 4 and words[i + 3] == 9:     # OpVariable PushConstant
            pc_ptr_types.add(words[i + 1])
        elif op == 72 and wc >= 5 and words[i + 3] == 35:    # OpMemberDecorate Offset
            member_offsets.setdefault(words[i + 1], {})[words[i + 2]] = words[i + 4]
        i += wc
    pc_structs = [ptr_target[t] for t in pc_ptr_types if t in ptr_target]
    if len(pc_structs) != 1:
        raise SystemExit("FAIL: %s expected 1 PushConstant struct, found %d" % (header_path.name, len(pc_structs)))
    offs = member_offsets.get(pc_structs[0], {})
    return [offs[k] for k in sorted(offs)]


def check_fg_scale_crop(cxx):
    fg = (VK / "vk_framegen.cpp").read_text(encoding="utf-8")
    fns = (block(fg, "void drop_pipe(") + "\n" +
           block(fg, "struct ScaleState") + " scale;\n" +
           block(fg, "struct WideSides") + ";\nstd::atomic<WideSides> wide_now;\n" +
           block(fg, "bool init_scale(") + "\n" +
           block(fg, "bool framegen_wanted(") + "\n" +
           block(fg, "bool framegen_on(") + "\n" +
           block(fg, "VkImage framegen_scale("))
    print(run_cpp(cxx, SCALE_TEMPLATE.replace("// PRODUCTION_FUNCTIONS", fns), "fg-scale-crop-",
                  ["-include", ENV_ON_H, "-I" + str(VK)]))

    comp = (SHADERS / "fg_scale.comp").read_text(encoding="utf-8")
    if "vec2 crop_uv(" not in comp:
        raise SystemExit("FAIL: fg_scale.comp does not define crop_uv() for direct cropped sampling")
    # Guard against GLSL std430 vector alignment drift (e.g. ivec4 after 5 scalars pads to offset 32, 48 B total)
    # against vk_framegen.cpp's 36-byte scalar push-constant struct.
    want_offsets = [0, 4, 8, 12, 16, 20, 24, 28, 32]
    for spv_name in ("fg_scale_comp_spv.h", "fg_scale_pq_comp_spv.h"):
        got = spv_push_constant_offsets(SHADERS / spv_name)
        if got != want_offsets:
            raise SystemExit("FAIL: %s PushConstant member offsets %s != expected 36-byte scalar layout %s" %
                             (spv_name, got, want_offsets))
    print("PASS: fg_scale SPIR-V PushConstant member offsets match 36-byte C++ layout (0..32)")
    crop_fn = block(comp, "vec2 crop_uv(")
    print(run_cpp(cxx, CROP_UV_SHIM + "static " + crop_fn + "\n" + CROP_UV_MAIN, "fg-scale-uv-"))


# ---- 3. Temporal 32-bit format selection and SPIR-V / FSR 3.1 parity ----

TEMPORAL_SHIM = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include "gpu/vk/upscaler_rows.h"
namespace ur = mkwvk::upscaler_rows;
using VkFormat = int;
constexpr int VK_FORMAT_B10G11R11_UFLOAT_PACK32 = 122;
constexpr int VK_FORMAT_R16G16B16A16_SFLOAT = 97;
/* HELPERS */
int main() {
    for (int mode = 0; mode < ur::kUpscalers; ++mode) {
        for (bool hdr : {false, true})
            for (bool pre : {false, true})
                for (bool rgba : {false, true})
                    for (int opt_hdr : {0, 1, 2})
                        for (bool storage : {false, true}) {
                            const bool want_packed = mode == ur::FSR3 && !hdr && !pre && !rgba && opt_hdr == 0 && storage;
                            const VkFormat got = temporal_target_format(mode, hdr, pre, rgba, opt_hdr, storage);
                            const VkFormat want = want_packed ? VK_FORMAT_B10G11R11_UFLOAT_PACK32
                                                              : VK_FORMAT_R16G16B16A16_SFLOAT;
                            assert(got == want);
                        }
    }
    std::puts("PASS: temporal target format selects B10G11R11 for SDR FSR 3.1 only and preserves RGBA16F elsewhere");
    return 0;
}
'''


def check_temporal_formats(cxx, glslang):
    temporal = (VK / "vk_temporal.cpp").read_text(encoding="utf-8")
    if "VkFormat temporal_target_format(" not in temporal or "bool packed_color_supported(" not in temporal:
        raise SystemExit("FAIL: vk_temporal.cpp hardcodes RGBA16F for SDR s.color/s.output and lacks packed_color_supported / temporal_target_format")
    if "dev.features.shaderStorageImageExtendedFormats" not in block(temporal, "bool packed_color_supported("):
        raise SystemExit("FAIL: packed_color_supported() does not guard on dev.features.shaderStorageImageExtendedFormats")
    helpers = block(temporal, "VkFormat temporal_target_format(")
    print(run_cpp(cxx, TEMPORAL_SHIM.replace("/* HELPERS */", helpers), "temporal-fmt-", ["-I" + str(ROOT)]))

    prep = SHADERS / "dlss_prep.comp"
    prep_text = prep.read_text(encoding="utf-8")
    if "#ifdef DLSS_PREP_PACKED" not in prep_text or "r11f_g11f_b10f" not in prep_text:
        raise SystemExit("FAIL: dlss_prep.comp lacks DLSS_PREP_PACKED r11f_g11f_b10f binding")
    header = SHADERS / "dlss_prep_packed_comp_spv.h"
    if not header.exists():
        raise SystemExit("FAIL: dlss_prep_packed_comp_spv.h is missing (run --write)")
    first, _, body = header.read_bytes().partition(b"\n")
    if first + b"\n" != packed_prep_source_line(prep):
        raise SystemExit("FAIL: dlss_prep_packed_comp_spv.h hash does not match dlss_prep.comp (run --write)")
    words = [int(w, 16) for w in re.findall(r"0x[0-9a-fA-F]{8}", body.decode("utf-8"))]
    assert words and words[0] == 0x07230203
    i, formats = 5, []
    while i < len(words):
        count, opcode = words[i] >> 16, words[i] & 0xFFFF
        assert count > 0 and i + count <= len(words)
        if opcode == 25:  # OpTypeImage
            formats.append(words[i + 8])
        i += count
    if 8 not in formats or 2 in formats:  # 8 = R11fG11fB10f, 2 = Rgba16f
        raise SystemExit(f"FAIL: dlss_prep_packed_comp_spv.h storage image formats {formats} expected R11fG11fB10f (8), no Rgba16f (2)")
    if glslang:
        with tempfile.TemporaryDirectory(prefix="dlss-prep-packed-") as tmp:
            made = compile_packed_prep(glslang, prep, Path(tmp) / "out.h")
            if without_version(made) != without_version(body):
                raise SystemExit("FAIL: dlss_prep_packed_comp_spv.h differs from glslang output (run --write)")

    fsr3 = (VK / "vk_fsr3.cpp").read_text(encoding="utf-8")
    eval_fn = block(fsr3, "bool chain_evaluate(")
    if "VK_FORMAT_B10G11R11_UFLOAT_PACK32" not in eval_fn:
        raise SystemExit("FAIL: vk_fsr3.cpp chain_evaluate rejects VK_FORMAT_B10G11R11_UFLOAT_PACK32 direct output")
    print("PASS: dlss_prep_packed SPIR-V and FSR 3.1 packed direct-output gate")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default="g++")
    p.add_argument("--headers", type=Path)
    p.add_argument("--glslang", default="glslangValidator")
    p.add_argument("--write", action="store_true", help="regenerate dlss_prep_packed_comp_spv.h from dlss_prep.comp")
    a = p.parse_args()
    glslang = shutil.which(a.glslang) or shutil.which("glslang")
    if a.write:
        if not glslang:
            raise SystemExit(f"--write needs {a.glslang}")
        comp = SHADERS / "dlss_prep.comp"
        with tempfile.TemporaryDirectory(prefix="dlss-prep-packed-") as tmp:
            out = packed_prep_source_line(comp) + compile_packed_prep(glslang, comp, Path(tmp) / "out.h")
            (SHADERS / "dlss_prep_packed_comp_spv.h").write_bytes(out)
            print("wrote", SHADERS / "dlss_prep_packed_comp_spv.h")
        return
    if not a.headers:
        raise SystemExit("--headers <Vulkan-Headers/include> is required")
    failures = []
    for fn, args in ((check_hud_layer, (a.cxx, a.headers)),
                     (check_fg_scale_crop, (a.cxx,)),
                     (check_temporal_formats, (a.cxx, glslang))):
        try:
            fn(*args)
        except SystemExit as e:
            failures.append(str(e))
    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
