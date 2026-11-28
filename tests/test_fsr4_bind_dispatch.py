"""AMD FSR 4's pipeline-creation, unroll-worker, destroy, bind, and dispatch hooks (gpu/vk/vk_fsr4.cpp), cut from the
file and built against recording stand-ins for the driver's calls:
- bind/dispatch: a dispatch goes by what its bind really bound, also when a background job publishes the unrolled
  pipeline between the two (tail written when library's pipeline was bound; none when unrolled was bound; AMD's own
  when the tail cannot be read);
- create_compute_pipelines -> build_unrolled: a wave64 layer arrives at the pipeline-creation callback with 64 lanes,
  the postpass is queued and arrives with its requested 32 lanes, and a pass with no subgroup-size requirement keeps
  a null pNext and preserves ALLOW_VARYING_SUBGROUP_SIZE_BIT;
- two-tier headroom: compact folded shaders (<= kCompactUnrollWords) request kCompactHeadroom (1 GiB), while larger
  shaders still require kBuildHeadroom (8 GiB);
- worker dispatch isolation: build_unrolled calls worker_fn.* exclusively (never real_csm/real_dsm/real_dp);
- handle reuse cancellation: a stale job whose pipeline was destroyed (even after the driver reuses the same handle
  for a newer job) destroys its stale pipeline and never overwrites the newer job's token or better[] entry.
No game or GPU required.

python -I -B tests/test_fsr4_bind_dispatch.py [--cxx g++] --headers <folder holding vulkan/vulkan.h>
"""
RUN_ALL_ARGS = ["--cxx", "{cxx}", "--headers", "{vulkan_headers}"]

import argparse
import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DRIVER = r'''#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include "spirv_unroll.h"
#include "spirv_unroll_legacy.h"

static int fails;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); fails++; } } while (0)
static struct {
    VkDevice device = VK_NULL_HANDLE;
    struct { VkBool32 pipelineCreationCacheControl = VK_TRUE; } vk13;
} dev;
static void logf(const char*, ...) {}
template <class T> static T handle(uintptr_t v) { return reinterpret_cast<T>(v); }

static void VKAPI_CALL vkDestroyPipelineLayout(VkDevice, VkPipelineLayout, const VkAllocationCallbacks*) {}
static void VKAPI_CALL vkDestroyDescriptorSetLayout(VkDevice, VkDescriptorSetLayout, const VkAllocationCallbacks*) {}

static const uint32_t kPostpassSpv[] = { /* POSTPASS_SPV */ };

namespace tune {
bool expanded_unroll = true;
bool compact_builds = true;
static const VkPipeline kOwn = handle<VkPipeline>(0x77);   // (AMD's own, as make_plain makes it)
static int plains_made;
std::mutex mu;
/* WORKER_DISPATCH */
/* BIG */
/* SET_LAYOUT_INFO */
/* LAYOUT_INFO */
/* LAYOUT_COPY */
/* MODULE */
/* PLAIN */
/* TAILPIPE */
std::unordered_map<uint64_t, Big> bigs;
std::unordered_set<uint64_t> told;
std::unordered_map<VkDescriptorSetLayout, SetLayoutInfo> set_layouts;
std::unordered_map<VkPipelineLayout, LayoutInfo> layouts;
std::unordered_map<VkShaderModule, Module> modules;
std::unordered_map<VkPipeline, TailPipe> tail_pipes;
std::atomic<int> tail_count{0};
/* BOUND */
int layers64 = 0;
std::unordered_map<VkPipeline, VkPipeline> better;
std::unordered_map<VkPipeline, uint64_t> pending_token;
uint64_t next_job_token = 1;
std::atomic<int> better_count{0};
/* JOBS */
/* JOBS_FN */
std::atomic<bool> closing{false};
std::atomic<bool> suppressed{false};
/* CONSTS */

static VkPipeline make_plain(const Plain&) { return plains_made++, kOwn; }

// (the driver's calls, recorded)
static std::mutex rec_mu;
static int updates, barriers, dispatches;
static VkPipeline last_bound;
static int worker_csm_calls, worker_dsm_calls, worker_dp_calls;
static int real_csm_calls, real_dsm_calls, real_dp_calls;
static uint64_t next_pipe_id = 0x1000, next_own_pipe_id = 0x2000;
static std::vector<uint64_t> headroom_wants;

struct RecordedPipe {
    std::string entry;
    bool has_req = false;
    uint32_t req_lanes = 0;
    VkPipelineShaderStageCreateFlags stage_flags = 0;
    VkPipelineCreateFlags flags = 0;
};
static std::vector<RecordedPipe> sync_pipes, own_pipes;

static RecordedPipe capture_ci(const VkComputePipelineCreateInfo& ci) {
    RecordedPipe rp;
    rp.entry = ci.stage.pName ? ci.stage.pName : "";
    rp.stage_flags = ci.stage.flags;
    rp.flags = ci.flags;
    for (const VkBaseInStructure* s = (const VkBaseInStructure*)ci.stage.pNext; s; s = s->pNext) {
        if (s->sType == VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_REQUIRED_SUBGROUP_SIZE_CREATE_INFO) {
            rp.has_req = true;
            rp.req_lanes = ((const VkPipelineShaderStageRequiredSubgroupSizeCreateInfo*)s)->requiredSubgroupSize;
        }
    }
    return rp;
}

static VkResult VKAPI_CALL rec_csm_real(VkDevice, const VkShaderModuleCreateInfo*, const VkAllocationCallbacks*, VkShaderModule* m) {
    std::lock_guard lk(rec_mu);
    real_csm_calls++;
    *m = handle<VkShaderModule>(0x900 + real_csm_calls);
    return VK_SUCCESS;
}
static void VKAPI_CALL rec_dsm_real(VkDevice, VkShaderModule, const VkAllocationCallbacks*) {
    std::lock_guard lk(rec_mu);
    real_dsm_calls++;
}
static VkResult VKAPI_CALL rec_csm_worker(VkDevice, const VkShaderModuleCreateInfo*, const VkAllocationCallbacks*, VkShaderModule* m) {
    std::lock_guard lk(rec_mu);
    worker_csm_calls++;
    *m = handle<VkShaderModule>(0xa00 + worker_csm_calls);
    return VK_SUCCESS;
}
static void VKAPI_CALL rec_dsm_worker(VkDevice, VkShaderModule, const VkAllocationCallbacks*) {
    std::lock_guard lk(rec_mu);
    worker_dsm_calls++;
}
static void VKAPI_CALL rec_dp_worker(VkDevice, VkPipeline, const VkAllocationCallbacks*) {
    std::lock_guard lk(rec_mu);
    worker_dp_calls++;
}
static void VKAPI_CALL rec_dp_real(VkDevice, VkPipeline, const VkAllocationCallbacks*) {
    std::lock_guard lk(rec_mu);
    real_dp_calls++;
}
static VkResult VKAPI_CALL rec_cdsl(VkDevice, const VkDescriptorSetLayoutCreateInfo*, const VkAllocationCallbacks*, VkDescriptorSetLayout* out) {
    *out = handle<VkDescriptorSetLayout>(0x501);
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL rec_cpl(VkDevice, const VkPipelineLayoutCreateInfo*, const VkAllocationCallbacks*, VkPipelineLayout* out) {
    *out = handle<VkPipelineLayout>(0x601);
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL rec_ccp(VkDevice, VkPipelineCache, uint32_t n, const VkComputePipelineCreateInfo* ci,
                                   const VkAllocationCallbacks*, VkPipeline* p) {
    std::lock_guard lk(rec_mu);
    for (uint32_t i = 0; i < n; i++) {
        sync_pipes.push_back(capture_ci(ci[i]));
        p[i] = handle<VkPipeline>(next_pipe_id++);
    }
    return VK_SUCCESS;
}
static void VKAPI_CALL rec_bind(VkCommandBuffer, VkPipelineBindPoint, VkPipeline p) { last_bound = p; }
static void VKAPI_CALL rec_dispatch(VkCommandBuffer, uint32_t, uint32_t, uint32_t) { dispatches++; }
static void VKAPI_CALL rec_update(VkCommandBuffer, VkBuffer, VkDeviceSize, VkDeviceSize, const void*) { updates++; }
static void VKAPI_CALL rec_barrier(VkCommandBuffer, VkPipelineStageFlags, VkPipelineStageFlags, VkDependencyFlags, uint32_t,
                                   const VkMemoryBarrier*, uint32_t, const VkBufferMemoryBarrier*, uint32_t,
                                   const VkImageMemoryBarrier*) { barriers++; }

PFN_vkCreateShaderModule real_csm = rec_csm_real;
PFN_vkDestroyShaderModule real_dsm = rec_dsm_real;
PFN_vkCreateComputePipelines real_ccp = rec_ccp;
PFN_vkCreateDescriptorSetLayout real_cdsl = rec_cdsl;
PFN_vkCreatePipelineLayout real_cpl = rec_cpl;
PFN_vkCmdBindPipeline real_bind = rec_bind;
PFN_vkCmdDispatch real_dispatch = rec_dispatch;
PFN_vkCmdUpdateBuffer real_update = rec_update;
PFN_vkCmdPipelineBarrier real_barrier = rec_barrier;
PFN_vkDestroyPipeline real_dp = rec_dp_real;

size_t own_cache_saved = 0;
static VkPipelineCache own_cache() { return handle<VkPipelineCache>(0x88); }
static bool own_pipeline_cache_save(VkPipelineCache, const char*, size_t*) { return true; }
static bool save_own_cache() { return own_pipeline_cache_save(own_cache(), kOwnCacheFile, &own_cache_saved); }
static VkResult own_cache_sdk_pipelines(PFN_vkCreateComputePipelines create, VkDevice device, VkPipelineCache cache, uint32_t count,
                                        const VkComputePipelineCreateInfo* infos, const VkAllocationCallbacks* allocator, VkPipeline* pipelines) {
    return create(device, cache, count, infos, allocator, pipelines);
}
static void device_lost(const char*) {}
static VkResult own_cache_compute_pipeline(VkPipelineCache, const VkComputePipelineCreateInfo* ci, VkPipeline* out) {
    if (ci->flags & VK_PIPELINE_CREATE_FAIL_ON_PIPELINE_COMPILE_REQUIRED_BIT) return VK_PIPELINE_COMPILE_REQUIRED;
    std::lock_guard lk(rec_mu);
    own_pipes.push_back(capture_ci(*ci));
    *out = handle<VkPipeline>(next_own_pipe_id++);
    return VK_SUCCESS;
}
static bool build_headroom(uint64_t* avail, uint64_t want = kBuildHeadroom) {
    std::lock_guard lk(rec_mu);
    headroom_wants.push_back(want);
    *avail = 16ull << 30;
    return *avail >= want;
}

/* LAYOUT_HELPERS */
/* UNROLLED_LANES */
/* BUILD_UNROLLED */
/* CREATE_PIPES */
/* DESTROY_PIPE */
/* HOOKS */
}  // namespace tune
using namespace tune;

// (one library pipeline that reads the tail of generation 1, its Big as `bound_state` says, and its unrolled one)
static const VkPipeline kLib = handle<VkPipeline>(0x10), kUnrolled = handle<VkPipeline>(0x11);
static void reset(int bound_state) {
    bigs.clear(), told.clear(), tail_pipes.clear(), better.clear(), bound.clear(), pending_token.clear();
    Big& big = bigs[1];
    big.buffer = handle<VkBuffer>(0x20), big.size = 1 << 20, big.tail.assign(1000, 7), big.due = true, big.bound = bound_state;
    tail_pipes[kLib].gen = 1;
    tail_pipes[kLib].plain = std::make_shared<Plain>();
    tail_count = 1, better_count = 0;
    updates = barriers = dispatches = plains_made = 0, last_bound = VK_NULL_HANDLE;
}
static void publish() { better[kLib] = kUnrolled, better_count = 1; }   // (build_unrolled, on its own thread)

static void wait_jobs() {
    Jobs& j = jobs();
    std::unique_lock lk(j.mu);
    j.cv.wait(lk, [&j] { return j.running == 0; });
}

int main() {
    dev.device = handle<VkDevice>(0x1);
    worker_fn = {rec_csm_worker, rec_dsm_worker, rec_dp_worker};

    const VkCommandBuffer cb = handle<VkCommandBuffer>(0x30);
    // 1. bound before the unrolled one arrived, dispatched after: the library's pipeline runs, so its tail first
    reset(1);
    cmd_bind_pipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, kLib);
    CHECK(last_bound == kLib);
    publish();
    cmd_dispatch(cb, 1, 1, 1);
    CHECK(updates > 0 && barriers == 1 && !bigs[1].due && dispatches == 1);
    CHECK(last_bound == kLib);

    // 2. the unrolled one bound: it reads no tail, so none is written and the next reader still has it due
    reset(1);
    publish();
    cmd_bind_pipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, kLib);
    CHECK(last_bound == kUnrolled);
    cmd_dispatch(cb, 1, 1, 1);
    CHECK(updates == 0 && bigs[1].due && dispatches == 1 && last_bound == kUnrolled);

    // 3. the tail cannot be read (its buffer bound without it), the unrolled one arrives between: AMD's own in its place
    reset(-1);
    cmd_bind_pipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, kLib);
    publish();
    cmd_dispatch(cb, 1, 1, 1);
    CHECK(plains_made == 1 && last_bound == kOwn && updates == 0 && dispatches == 1);

    // 4. create_compute_pipelines -> build_unrolled: wave64 layer gets 64 lanes, postpass gets queued with its
    //    requested 32 lanes, and a pass with no subgroup-size requirement keeps pNext == nullptr and ALLOW_VARYING
    reset(1);
    auto spv = std::make_shared<const std::vector<uint32_t>>(kPostpassSpv, kPostpassSpv + sizeof(kPostpassSpv) / 4);
    const VkDescriptorSetLayout dsl = handle<VkDescriptorSetLayout>(0x401);
    const VkPipelineLayout pl_lay = handle<VkPipelineLayout>(0x402);
    set_layouts[dsl] = SetLayoutInfo{};
    layouts[pl_lay] = LayoutInfo{{dsl}, {}};

    const VkShaderModule m_layer = handle<VkShaderModule>(0x701);
    const VkShaderModule m_post32 = handle<VkShaderModule>(0x702);
    const VkShaderModule m_post0 = handle<VkShaderModule>(0x703);
    modules[m_layer] = Module{3, 1, spv, "fsr4_model_test_pass1"};
    modules[m_post32] = Module{2, 1, spv, "fsr4_model_test_postpass"};
    modules[m_post0] = Module{2, 1, spv, "fsr4_model_test_postpass_nosg"};

    VkPipelineShaderStageRequiredSubgroupSizeCreateInfo sg32_a{VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_REQUIRED_SUBGROUP_SIZE_CREATE_INFO};
    sg32_a.requiredSubgroupSize = 32;
    VkPipelineShaderStageRequiredSubgroupSizeCreateInfo sg32_b{VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_REQUIRED_SUBGROUP_SIZE_CREATE_INFO};
    sg32_b.requiredSubgroupSize = 32;

    VkComputePipelineCreateInfo cis[3]{};
    for (int i = 0; i < 3; i++) {
        cis[i].sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
        cis[i].stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        cis[i].stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
        cis[i].stage.flags = VK_PIPELINE_SHADER_STAGE_CREATE_ALLOW_VARYING_SUBGROUP_SIZE_BIT;
        cis[i].layout = pl_lay;
    }
    cis[0].stage.module = m_layer;  cis[0].stage.pName = "fsr4_model_test_pass1";        cis[0].stage.pNext = &sg32_a;
    cis[1].stage.module = m_post32; cis[1].stage.pName = "fsr4_model_test_postpass";     cis[1].stage.pNext = &sg32_b;
    cis[2].stage.module = m_post0;  cis[2].stage.pName = "fsr4_model_test_postpass_nosg"; cis[2].stage.pNext = nullptr;

    // Another FSR 4 version keeps the postpass on the original path.
    expanded_unroll = false;
    VkPipeline fallback{};
    CHECK(create_compute_pipelines(dev.device, VK_NULL_HANDLE, 1, &cis[1], nullptr, &fallback) == VK_SUCCESS);
    wait_jobs();
    CHECK(worker_csm_calls == 0 && pending_token.count(fallback) == 0);
    destroy_pipeline(dev.device, fallback, nullptr);
    sync_pipes.clear();
    real_dp_calls = 0;
    expanded_unroll = true;

    // A suppressed context passes through without queueing unroll jobs.
    suppressed = true;
    VkPipeline untuned{};
    CHECK(create_compute_pipelines(dev.device, VK_NULL_HANDLE, 1, &cis[0], nullptr, &untuned) == VK_SUCCESS);
    wait_jobs();
    CHECK(worker_csm_calls == 0 && pending_token.count(untuned) == 0 && sync_pipes.size() == 1 && sync_pipes[0].req_lanes == 32);
    sync_pipes.clear();
    suppressed = false;

    VkPipeline created[3]{};
    CHECK(create_compute_pipelines(dev.device, VK_NULL_HANDLE, 3, cis, nullptr, created) == VK_SUCCESS);
    wait_jobs();

    CHECK(sync_pipes.size() == 3);
    CHECK(sync_pipes[0].has_req && sync_pipes[0].req_lanes == 64);
    CHECK((sync_pipes[0].stage_flags & VK_PIPELINE_SHADER_STAGE_CREATE_ALLOW_VARYING_SUBGROUP_SIZE_BIT) == 0);
    CHECK(sync_pipes[1].has_req && sync_pipes[1].req_lanes == 32);
    CHECK(!sync_pipes[2].has_req);

    CHECK(own_pipes.size() == 3);
    auto find_own = [](const char* name) -> const RecordedPipe* {
        for (const auto& r : own_pipes) if (r.entry == name) return &r;
        return nullptr;
    };
    const RecordedPipe* rp_layer = find_own("fsr4_model_test_pass1");
    const RecordedPipe* rp_post32 = find_own("fsr4_model_test_postpass");
    const RecordedPipe* rp_post0 = find_own("fsr4_model_test_postpass_nosg");
    CHECK(rp_layer && rp_layer->has_req && rp_layer->req_lanes == 64);
    CHECK(rp_layer && (rp_layer->stage_flags & VK_PIPELINE_SHADER_STAGE_CREATE_ALLOW_VARYING_SUBGROUP_SIZE_BIT) == 0);
    CHECK(rp_post32 && rp_post32->has_req && rp_post32->req_lanes == 32);
    CHECK(rp_post32 && (rp_post32->stage_flags & VK_PIPELINE_SHADER_STAGE_CREATE_ALLOW_VARYING_SUBGROUP_SIZE_BIT) == 0);
    CHECK(rp_post0 && !rp_post0->has_req);
    CHECK(rp_post0 && (rp_post0->stage_flags & VK_PIPELINE_SHADER_STAGE_CREATE_ALLOW_VARYING_SUBGROUP_SIZE_BIT) != 0);

    // 5. worker dispatch isolation (L14) & two-tier headroom (L11)
    CHECK(worker_csm_calls == 3 && worker_dsm_calls == 3);
    CHECK(real_csm_calls == 0 && real_dsm_calls == 0 && real_dp_calls == 0);
    CHECK(kBuildHeadroom == (8ull << 30) && kCompactHeadroom == (1ull << 30) && kCompactUnrollWords == 60000);
    CHECK(headroom_wants.size() == 3);
    for (uint64_t w : headroom_wants) CHECK(w == kCompactHeadroom);
    // (1 GiB only for a compact build by the expanded unroller on the compiler that was measured)
    CHECK(build_want(true, kCompactUnrollWords) == kCompactHeadroom);
    CHECK(build_want(true, kCompactUnrollWords + 1) == kBuildHeadroom);
    CHECK(build_want(false, kCompactUnrollWords) == kBuildHeadroom);
    compact_builds = false;
    CHECK(build_want(true, kCompactUnrollWords) == kBuildHeadroom);
    compact_builds = true;

    // 6. handle reuse & stale token cancellation (L13)
    const VkPipeline kReuse = handle<VkPipeline>(0x9999);
    {
        std::lock_guard lk(mu);
        pending_token[kReuse] = 500;
    }
    destroy_pipeline(dev.device, kReuse, nullptr);
    {
        std::lock_guard lk(mu);
        CHECK(pending_token.count(kReuse) == 0);
        // (driver reuses the same handle for a brand-new context with token 501)
        pending_token[kReuse] = 501;
    }
    const int dp_before = worker_dp_calls;
    VkComputePipelineCreateInfo job_ci = cis[1];
    job_ci.pNext = nullptr, job_ci.stage.pNext = nullptr;
    { std::lock_guard lk(jobs().mu); jobs().running++; }
    build_unrolled(kReuse, 500, spv, "fsr4_model_test_postpass", job_ci, 32, handle<VkPipelineLayout>(0x601), {}, true);
    {
        std::lock_guard lk(mu);
        CHECK(worker_dp_calls == dp_before + 1);
        CHECK(better.count(kReuse) == 0);
        CHECK(pending_token.count(kReuse) == 1 && pending_token[kReuse] == 501);
    }
    { std::lock_guard lk(jobs().mu); jobs().running++; }
    build_unrolled(kReuse, 501, spv, "fsr4_model_test_postpass", job_ci, 32, handle<VkPipelineLayout>(0x601), {}, true);
    {
        std::lock_guard lk(mu);
        CHECK(better.count(kReuse) == 1);
        CHECK(pending_token.count(kReuse) == 0);
    }

    return fails ? 1 : 0;
}
'''


def block(source, marker):
    start = source.index(marker)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default=os.environ.get("CXX") or "g++")
    p.add_argument("--headers", required=True)
    a = p.parse_args()
    src = (ROOT / "gpu/vk/vk_fsr4.cpp").read_text(encoding="utf-8")
    tune = block(src, "namespace tune {")
    # (the per-command-buffer record of binds: the map, and the type of its entries where it has one of its own)
    bound = re.search(r"(?:struct Bind \{.*?\n\};\n)?std::unordered_map<VkCommandBuffer, \w+> bound;", tune, re.S)
    if not bound:
        raise SystemExit("FAIL: tune::bound not found in gpu/vk/vk_fsr4.cpp")
    bound = bound.group(0)
    consts = "\n".join(line for line in re.findall(r"^constexpr [^;]+;.*$", tune, re.M) if "kTail" not in line)
    layout_helpers = "\n".join(block(tune, sig) for sig in ("bool layout_copy_locked(", "bool make_layout("))
    hooks = "\n".join(block(tune, "VKAPI_ATTR void VKAPI_CALL " + name + "(") for name in ("cmd_bind_pipeline", "cmd_dispatch"))
    postpass_bytes = (ROOT / "tests/fsr4_tune/postpass.spv").read_bytes()
    postpass_words = struct.unpack("<%dI" % (len(postpass_bytes) // 4), postpass_bytes)
    postpass_init = ", ".join(str(w) for w in postpass_words)

    code = (DRIVER
            .replace("/* POSTPASS_SPV */", postpass_init)
            .replace("/* WORKER_DISPATCH */", block(tune, "struct WorkerDispatch {") + " worker_fn;")
            .replace("/* BIG */", block(tune, "struct Big {") + ";")
            .replace("/* SET_LAYOUT_INFO */", block(tune, "struct SetLayoutInfo {") + ";")
            .replace("/* LAYOUT_INFO */", block(tune, "struct LayoutInfo {") + ";")
            .replace("/* LAYOUT_COPY */", block(tune, "struct LayoutCopy {") + ";")
            .replace("/* MODULE */", block(tune, "struct Module {") + ";")
            .replace("/* PLAIN */", block(tune, "struct Plain {") + ";")
            .replace("/* TAILPIPE */", block(tune, "struct TailPipe {") + ";")
            .replace("/* BOUND */", bound)
            .replace("/* JOBS */", block(tune, "struct Jobs {") + ";")
            .replace("/* JOBS_FN */", block(tune, "Jobs& jobs()"))
            .replace("/* CONSTS */", consts)
            .replace("/* LAYOUT_HELPERS */", layout_helpers)
            .replace("/* UNROLLED_LANES */", block(tune, "uint32_t unrolled_lanes("))
            .replace("/* BUILD_UNROLLED */", block(tune, "uint64_t build_want(") + "\n" + block(tune, "void build_unrolled("))
            .replace("/* CREATE_PIPES */", block(tune, "VKAPI_ATTR VkResult VKAPI_CALL create_compute_pipelines("))
            .replace("/* DESTROY_PIPE */", block(tune, "VKAPI_ATTR void VKAPI_CALL destroy_pipeline("))
            .replace("/* HOOKS */", hooks))
    with tempfile.TemporaryDirectory(prefix="fsr4-bind-") as tmp:
        cpp, exe = Path(tmp) / "bind.cpp", Path(tmp) / "bind.exe"
        cpp.write_text(code, encoding="utf-8")
        extra = ["-static"] if os.name == "nt" else ["-pthread"]
        subprocess.run([a.cxx, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                        "-I" + a.headers, "-I" + str(ROOT / "gpu/vk"), str(cpp), "-o", str(exe)] + extra, check=True)
        r = subprocess.run([str(exe)], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("FAIL: FSR 4's hooks:\n" + r.stdout + r.stderr)
    print("PASS: FSR 4's bind/dispatch, subgroup-size preservation (wave64 layer -> 64, postpass -> 32 / none), "
          "two-tier headroom, worker dispatch isolation, and handle-reuse token cancellation all hold")


if __name__ == "__main__":
    main()
