"""Check the shared frame-generation image formats without a game or Vulkan device.

Run framegen_record's setup and ensure_size from vk_framegen.cpp. Two adjacent
10-bit PQ codes must remain distinct in the history and output image formats the
renderer actually allocates. SDR still uses RGBA8, including after an HDR frame
of the same size. A device without HDR storage images presents the original HDR
frame instead of running frame generation with an 8-bit copy.

python -I -B tests/test_framegen_hdr.py [--cxx clang++] [--glslang glslangValidator] [--write]
"""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from srccut import between, block  # noqa: E402


RUN_ALL_ARGS = ["--cxx", "{cxx}", "--glslang", "{glslang}"]
ROOT = Path(__file__).resolve().parents[1]
SHADERS = ROOT / "gpu/vk/shaders"

TYPES = r'''
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <future>
#include <map>
#include <mutex>
using VkImage = int;
using VkImageView = int;
using VkCommandBuffer = int;
using VkDescriptorSetLayout = int;
using VkPipelineLayout = int;
using VkPipeline = int;
using VkSampler = int;
using VmaAllocation = void*;
constexpr int VK_NULL_HANDLE = 0, kMaxMult = 8;
enum VkFormat {
    VK_FORMAT_UNDEFINED,
    VK_FORMAT_R8G8B8A8_UNORM,
    VK_FORMAT_A2B10G10R10_UNORM_PACK32,
    VK_FORMAT_R16_SFLOAT,
    VK_FORMAT_R16G16B16A16_SFLOAT,
};
using VkImageUsageFlags = unsigned;
constexpr unsigned VK_IMAGE_USAGE_STORAGE_BIT = 1, VK_IMAGE_USAGE_SAMPLED_BIT = 2;
constexpr unsigned VK_IMAGE_USAGE_TRANSFER_SRC_BIT = 4, VK_IMAGE_USAGE_TRANSFER_DST_BIT = 8;
'''

SHIM = r'''
struct { std::mutex present_mu; } dev;
struct PresentSourceRect { uint32_t x, y, w, h; };
PresentSourceRect present_source_rect(uint32_t w, uint32_t h, const int*) { return {0, 0, w, h}; }
std::map<VkImage, VkFormat> images;
int allocations = 0;
bool pq_supported = true;
bool init() { return true; }
bool init_pq_framegen() { return pq_supported; }
int multiplier() { return 2; }
void device_wait_idle_locked() {}
void drop_setup() {}
void to_general(VkCommandBuffer, VkImage) {}
void logf(const char*, ...) {}
bool make_img(Img& image, uint32_t, uint32_t, VkFormat format, VkImageUsageFlags) {
    image.image = ++allocations;
    images.emplace(image.image, format);
    return true;
}
void drop_img(Img& image) {
    images.erase(image.image);
    image = {};
}
'''

FIXTURE = r'''
constexpr auto sdr = VK_FORMAT_R8G8B8A8_UNORM;
constexpr auto pq = VK_FORMAT_A2B10G10R10_UNORM_PACK32;
int failures = 0;
void expect(bool value, const char* reason) {
    if (!value) {
        std::printf("FAIL: %s\n", reason);
        ++failures;
    }
}
bool frame_formats_are(VkFormat expected) {
    for (const Img& image : fg.frames)
        if (images.at(image.image) != expected) return false;
    for (const OutSet& set : fg.sets)
        for (int i = 0; i < multiplier(); ++i)
            if (images.at(set.frame[i].image) != expected) return false;
    return true;
}
long stored_code(int pq_code, VkFormat format) {
    return std::lround(pq_code * (format == pq ? 1023.0 : 255.0) / 1023.0);
}
int main() {
    expect(framegen_record(0, 1, 3840, 2160, nullptr, false), "SDR can record");
    expect(frame_formats_are(sdr), "SDR allocates RGBA8 history and output");
    expect(framegen_record(0, 1, 3840, 2160, nullptr, true), "Native HDR can record");
    expect(frame_formats_are(pq), "Native HDR allocates RGB10A2 history and output");
    for (const Img& image : {fg.frames[0], fg.sets[0].frame[0]}) {
        const auto format = images.at(image.image);
        expect(stored_code(400, format) != stored_code(401, format), "adjacent 10-bit PQ codes survive storage");
    }
    int before = allocations;
    framegen_record(0, 1, 3840, 2160, nullptr, true);
    expect(allocations == before, "a second HDR frame reuses its images");
    framegen_record(0, 1, 3840, 2160, nullptr, false);
    expect(frame_formats_are(sdr), "same-sized SDR after HDR returns to RGBA8");
    pq_supported = false;
    before = allocations;
    fg.have_prev = true;
    fg.sets[0].busy = true;
    fg.jobs.push_back({});
    auto fallback = std::async(std::launch::async, [] {
        return framegen_record(0, 1, 3840, 2160, nullptr, true);
    });
    using namespace std::chrono_literals;
    expect(fallback.wait_for(50ms) == std::future_status::timeout, "fallback waits for queued SDR set");
    {
        std::lock_guard lk(fg.mu);
        fg.jobs.clear();   // the display thread has taken the set, but is still presenting it
    }
    fg.cv.notify_all();
    expect(fallback.wait_for(50ms) == std::future_status::timeout, "fallback waits for SDR presentation to finish");
    {
        std::lock_guard lk(fg.mu);
        fg.sets[0].busy = false;
    }
    fg.cv.notify_all();
    expect(!fallback.get(), "unsupported HDR storage bypasses generation");
    expect(!fg.have_prev, "fallback invalidates history before SDR resumes");
    expect(allocations == before, "unsupported HDR storage allocates no fallback images");
    if (!failures) std::puts("PASS: HDR retains adjacent PQ codes, SDR uses RGBA8, and unsupported HDR bypasses FG");
    return failures != 0;
}
'''


def check_allocations(cxx):
    framegen = (ROOT / "gpu/vk/vk_framegen.cpp").read_text(encoding="utf-8")
    types = "\n".join(block(framegen, f"struct {name} {{") + ";" for name in ("Img", "OutSet", "Job", "Pipe"))
    state = block(framegen, "struct State {") + " fg;"
    allocation = "\n".join(block(framegen, name) for name in
                           ("void drop_size()", "void drain_framegen_sets()", "bool ensure_size("))
    setup = between(framegen, "bool framegen_record(", "    if (!fg.thread_started)") + "    return true;\n}\n"
    with tempfile.TemporaryDirectory(prefix="framegen-hdr-") as temp:
        cpp, exe = Path(temp) / "fixture.cpp", Path(temp) / "fixture.exe"
        cpp.write_text("\n".join((TYPES, types, state, SHIM, allocation, setup, FIXTURE)), encoding="utf-8")
        subprocess.run([cxx, "-std=c++20", "-O1", str(cpp), "-o", str(exe)], check=True)
        return subprocess.run([str(exe)]).returncode


def storage_formats(shader):
    words = [int(value, 16) for value in re.findall(rb"0x([0-9a-fA-F]{8})\b", shader)]
    if not words or words[0] != 0x07230203:
        raise SystemExit("Missing SPIR-V header")
    offset = 5
    formats = set()
    while offset < len(words):
        opcode, count = words[offset] & 0xffff, words[offset] >> 16
        if not count or offset + count > len(words):
            raise SystemExit("Malformed SPIR-V instruction")
        if opcode == 25 and words[offset + 7] == 2:  # OpTypeImage, Sampled = 2 (storage)
            formats.add(words[offset + 8])
        offset += count
    return formats


def check_shaders(glslang, write):
    with tempfile.TemporaryDirectory(prefix="framegen-spv-") as temp:
        out = Path(temp) / "shader.h"
        for name, target in (("fg_copy", "vulkan1.3"), ("fg_synth", "vulkan1.2")):
            source = SHADERS / f"{name}.comp"
            digest = hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for hdr, image_format in ((False, 4), (True, 11)):  # SPIR-V Rgba8 and Rgb10A2
                stem = name + ("_pq" if hdr else "") + "_comp_spv"
                header = SHADERS / f"{stem}.h"
                defines = ["-DFG_NATIVE_PQ"] if hdr else []
                options = ["-V", "--target-env", target, *defines, "--vn", stem.upper()]
                notice = (f"// {source.name} sha256 {digest} "
                          f"(tests/test_framegen_hdr.py --write: glslangValidator {' '.join(options)})\n").encode()
                if glslang:
                    subprocess.run([glslang, *options, "-o", str(out), str(source)], check=True, capture_output=True)
                    compiled = out.read_bytes().replace(b"\r\n", b"\n")
                    if write:
                        header.write_bytes(notice + compiled)
                first, _, body = header.read_bytes().replace(b"\r\n", b"\n").partition(b"\n")
                if first + b"\n" != notice or storage_formats(body) != {image_format}:
                    raise SystemExit(f"{header.name}: wrong source hash or storage format")
                if glslang and body.partition(b"\n")[2] != compiled.partition(b"\n")[2]:
                    raise SystemExit(f"{header.name}: embedded shader differs from glslang output")
        hud = (SHADERS / "fg_hud_pq_comp_spv.h").read_bytes()
        if storage_formats(hud) != {11}:
            raise SystemExit("fg_hud_pq_comp_spv.h: HDR HUD storage must use Rgb10A2")
    print("PASS: SDR and Native HDR shaders use their matching storage formats")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="clang++")
    parser.add_argument("--glslang", default="glslangValidator")
    parser.add_argument("--write", action="store_true", help="regenerate copy and synthesis SPIR-V headers")
    args = parser.parse_args()
    glslang = shutil.which(args.glslang)
    if args.write and not glslang:
        raise SystemExit("--write needs glslangValidator")
    if not args.write and check_allocations(args.cxx):
        return 1
    check_shaders(glslang, args.write)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
