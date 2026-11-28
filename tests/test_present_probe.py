"""RX_VK_PRESENT_PROBE's copy timestamps (gpu/vk/vk_replay.cpp's probe block): each probed present's line reads its own
copy's timestamp, never one a later copy wrote over it.

The probe's code is cut from vk_replay.cpp (from `constexpr int kProbeSlots` to the end of its namespace) with the copy's
call to probe_copy_stamp cut from present_image_with_trial, and compiled with stand-ins: the Vulkan calls it makes run on a
made-up GPU in the test (a copy's timestamp written as it is recorded, the probe's own as they are submitted), and the
probe's thread can be held at its fences, as a reader behind the presents is. Each case runs in a process of its own,
which writes the probe's file and prints it; the file's rows are then read here:

- own: a present's line waits while 70 more copies and presents are made (the reader held). Its row, and every row
  after it, has its own copy's timestamp: a copy's query is the image's until its present's line takes it, then the
  line's until the reader has read it, never reset or written again before.
- full: 32 lines wait (the reader held) while 50 more images are copied: no copy's query is taken from a line; the copies
  with no query free go without, and the log says so once. Such a copy's present, once probed, has -1, not a value.
- first: the first set presented ahead, both its copies made before its first present: each row has its copy's timestamp
  (the probe is made at the first copy presented ahead, not at the first present).
- unset, behind: with the switch unset, or with copies not presented ahead (handed to the presenter thread), the probe
  makes no Vulkan call, no file and no log line.
- unread: the probe's own timestamps not given by the driver are -1, not 0.
- reuse: 200 presents, each line read before the next copy: every copy has its timestamp (a query read is free again).
- close: the game closes right after its last presents (the window's close, then the process ends as the game's does,
  no stdio flushed): the file holds its # line, its columns' names and every row, the lines still queued written; the
  log's closing line counts them; the reader's thread object is deleted once joined.
- stuck: a line whose fence never signals, queued at the close: the close still ends, the others written, the log
  counting the one not written.
- stuck_taken: a line whose fence never signals, already taken by the reader when the window closes: the close is not
  held by it (under 1 s; the drain has 500 ms in all), and the log counts it not written.
- close_first: the window closed before the first copy presented ahead: no probe is made after it (no file), and the
  presents are made.
- after: a present after the close is made, not probed, and the log does not say the presents are not probed.
- and the window's close (vk_main.cpp mkwvk_close_asked) closes the probe (present_probe_close) before its first return.
- refused_lost_before, refused_lost_after: a probe's submit around a present comes back VK_ERROR_DEVICE_LOST: the loss is
  reported through device_lost at once (the present not made when the first probe's is lost).
- refused_memory_before, refused_memory_after: a probe's submit is refused otherwise (out of host memory): the present is
  still made once, its own result returned; one log line says the probe's submit was refused and the probe is off: no row
  for that present, and no probe's submit, stamp nor row for the next; the closing line counts the warm-up's 2 lines
  written and none not.
- late: a copy whose timestamp is not yet written when the probe's fences are seen (its present on a queue of its own),
  in a query an earlier copy used and the reader read: the reader waits for the copy to run, and writes its value, never
  0 nor the earlier copy's. The made-up GPU runs such a copy's reset when the copy runs, not when it is recorded: until
  then the query holds the earlier copy's value, available (each query handed to a copy is reset from the CPU first).
- lost_copy: a copy that never runs: its line has -1 once the reader gives up, and its query is set aside, never handed
  to another copy (its copy may still write it); the closing line counts it.
- recopy: an image copied again before its present: its present's line has the second copy's timestamp, and the first
  copy's query is set aside (that copy may not have run).
- nohost_own: the presents on a queue of their own, on a device that cannot reset queries from the CPU
  (hostQueryReset): nothing orders the probe's fences after the copies, so no copy is stamped; every gpu_copy_ns is -1,
  and the log says why, once.
- nohost_replay: the same device, the presents on the replay's queue: the probe's first submit follows the copy on that
  queue, so every copy is stamped and read.
- full, stuck: the closing line also counts the copies without a timestamp (18) and the queries set aside (stuck's line
  never written: its query, 1).

    python -I -B tests/test_present_probe.py --headers <Vulkan-Headers/include> [--cxx g++]
"""
import argparse
import csv
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import between   # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}", "--headers", "{vulkan_headers}"]

ROOT = Path(__file__).resolve().parents[1]
COPY = 1000000   # (a copy's timestamp: COPY + the id the case gives it, its present's set the same id)

FIXTURE = r"""
#define VK_NO_PROTOTYPES
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif
#include <vulkan/vulkan.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// (stand-ins for the renderer's)
std::mutex out_mu;
void logf(const char* fmt, ...) {
    std::lock_guard lk(out_mu);
    va_list ap;
    va_start(ap, fmt);
    std::printf("LOG ");
    std::vprintf(fmt, ap);
    std::printf("\n");
    std::fflush(stdout);
    va_end(ap);
}
int64_t gtl_now() { return (int64_t)std::chrono::steady_clock::now().time_since_epoch().count(); }
std::atomic<int> vk_calls{0}, presents{0}, submits{0};
[[noreturn]] void device_lost(const char* = nullptr) {
    std::printf("LOST\nCALLS %d PRESENTS %d SUBMITS %d\n", vk_calls.load(), presents.load(), submits.load());
    std::fflush(stdout);
    std::_Exit(3);
}
template <class H> H handle(uintptr_t v) { return reinterpret_cast<H>(v); }
struct Dev {
    VkDevice device = handle<VkDevice>(0x10);
    VkPhysicalDevice physical = handle<VkPhysicalDevice>(0x20);
    uint32_t queue_family = 0, present_family = 0;
    bool present_queue_supported = false, host_query_reset = true;
    VkQueue queue = handle<VkQueue>(0x30), present_queue = VK_NULL_HANDLE;
    VkPhysicalDeviceProperties props{};
} dev;
struct PendingPresent { int unused = 0; };

// (the made-up GPU: a copy's commands run as they are recorded, the probe's own when they are submitted)
struct Query { uint64_t value = 0; bool avail = false; };
std::mutex gpu_mu;
std::condition_variable gate_cv;
bool gate_open = true;       // (the probe's thread held at its fences while false)
Query queries[512];
std::map<VkCommandBuffer, std::vector<std::pair<int, uint32_t>>> recorded;   // (0 a reset, 1 a timestamp; the query)
std::map<VkFence, bool> signalled;
uint64_t tick = 5000000, next_copy = 0;
int late_ms = 0;             // (a copy run this long after it is recorded, its reset with it; -1 never)
std::atomic<int> host_resets{0};
bool probes_unread = false;  // (the probe's own two queries' results not given)
int unsignalled = -1;        // (the submit, counted from 1, whose fence is never signalled)
int refused = -1;            // (the submit, counted from 1, the driver refuses with refusal: nothing run, nothing signalled)
VkResult refusal = VK_SUCCESS, present_result = VK_SUCCESS;
const VkCommandBuffer COPY_CMD = handle<VkCommandBuffer>(0x7000);
void run_op(int kind, uint32_t q, uint64_t value) {
    if (kind == 0) queries[q].avail = false;
    else queries[q].value = value, queries[q].avail = true;
}
void op(VkCommandBuffer cb, int kind, uint32_t q) {
    std::unique_lock lk(gpu_mu);
    if (cb != COPY_CMD) { recorded[cb].push_back({kind, q}); return; }
    if (late_ms) {   // (the copy run later: its reset and its timestamp both then)
        if (kind == 0 || late_ms < 0) return;
        const uint64_t value = next_copy;
        const int ms = late_ms;
        std::thread([q, value, ms] {
            std::this_thread::sleep_for(std::chrono::milliseconds(ms));
            std::lock_guard lk(gpu_mu);
            run_op(0, q, value);
            run_op(1, q, value);
        }).detach();
        return;
    }
    run_op(kind, q, next_copy);
}
namespace vkx {
void vkGetPhysicalDeviceQueueFamilyProperties(VkPhysicalDevice, uint32_t* n, VkQueueFamilyProperties* p) {
    ++vk_calls;
    if (p) {
        p[0] = {};
        p[0].queueFlags = VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT;
        p[0].queueCount = 1;
        p[0].timestampValidBits = 64;
    }
    *n = 1;
}
VkResult vkCreateQueryPool(VkDevice, const VkQueryPoolCreateInfo* ci, const VkAllocationCallbacks*, VkQueryPool* out) {
    ++vk_calls;
    if (ci->queryCount > 512) return VK_ERROR_OUT_OF_HOST_MEMORY;
    *out = handle<VkQueryPool>(0x100);
    return VK_SUCCESS;
}
VkResult vkCreateCommandPool(VkDevice, const VkCommandPoolCreateInfo*, const VkAllocationCallbacks*, VkCommandPool* out) {
    ++vk_calls;
    *out = handle<VkCommandPool>(0x200);
    return VK_SUCCESS;
}
VkResult vkAllocateCommandBuffers(VkDevice, const VkCommandBufferAllocateInfo*, VkCommandBuffer* out) {
    static uintptr_t n = 0x1000;
    ++vk_calls;
    *out = handle<VkCommandBuffer>(n += 16);
    return VK_SUCCESS;
}
VkResult vkBeginCommandBuffer(VkCommandBuffer, const VkCommandBufferBeginInfo*) { ++vk_calls; return VK_SUCCESS; }
VkResult vkEndCommandBuffer(VkCommandBuffer) { ++vk_calls; return VK_SUCCESS; }
void vkCmdResetQueryPool(VkCommandBuffer cb, VkQueryPool, uint32_t first, uint32_t count) {
    ++vk_calls;
    for (uint32_t i = 0; i < count; i++) op(cb, 0, first + i);
}
void vkCmdWriteTimestamp2(VkCommandBuffer cb, VkPipelineStageFlags2, VkQueryPool, uint32_t q) { ++vk_calls; op(cb, 1, q); }
void vkResetQueryPool(VkDevice, VkQueryPool, uint32_t first, uint32_t count) {   // (from the CPU: at once)
    ++vk_calls, ++host_resets;
    std::lock_guard lk(gpu_mu);
    for (uint32_t i = 0; i < count; i++) run_op(0, first + i, 0);
}
VkResult vkCreateFence(VkDevice, const VkFenceCreateInfo*, const VkAllocationCallbacks*, VkFence* out) {
    static uintptr_t n = 0x3000;
    ++vk_calls;
    *out = handle<VkFence>(n += 16);
    return VK_SUCCESS;
}
VkResult vkQueueSubmit2(VkQueue, uint32_t, const VkSubmitInfo2* si, VkFence f) {
    ++vk_calls;
    const int n = ++submits;
    if (n == refused) return refusal;
    std::lock_guard lk(gpu_mu);
    for (const auto& [kind, q] : recorded[si->pCommandBufferInfos[0].commandBuffer]) run_op(kind, q, ++tick);
    if (n != unsignalled) signalled[f] = true;
    return VK_SUCCESS;
}
VkResult vkQueuePresentKHR(VkQueue, const VkPresentInfoKHR*) { ++vk_calls, ++presents; return present_result; }
VkResult vkWaitForFences(VkDevice, uint32_t, const VkFence* f, VkBool32, uint64_t timeout) {
    ++vk_calls;
    if (*f == handle<VkFence>(0x9000)) return VK_SUCCESS;   // (the copy's fence, read at the present's call and return: done)
    std::unique_lock lk(gpu_mu);
    gate_cv.wait(lk, [] { return gate_open; });
    if (signalled[*f]) return VK_SUCCESS;
    lk.unlock();   // (not signalled: the whole timeout waited, as a driver does)
    std::this_thread::sleep_for(std::chrono::nanoseconds(timeout));
    lk.lock();
    return signalled[*f] ? VK_SUCCESS : VK_TIMEOUT;
}
VkResult vkResetFences(VkDevice, uint32_t n, const VkFence* f) {
    ++vk_calls;
    std::lock_guard lk(gpu_mu);
    for (uint32_t i = 0; i < n; i++) signalled[f[i]] = false;
    return VK_SUCCESS;
}
VkResult vkGetQueryPoolResults(VkDevice, VkQueryPool, uint32_t first, uint32_t count, size_t, void* data, VkDeviceSize stride,
                               VkQueryResultFlags flags) {
    ++vk_calls;
    std::lock_guard lk(gpu_mu);
    if (probes_unread && first < 64) return VK_NOT_READY;
    bool all = true;
    for (uint32_t i = 0; i < count; i++) {
        const Query& q = queries[first + i];
        uint64_t* at = (uint64_t*)((char*)data + i * stride);
        if (q.avail) at[0] = q.value;
        if (flags & VK_QUERY_RESULT_WITH_AVAILABILITY_BIT) at[1] = q.avail;
        all = all && q.avail;
    }
    return all ? VK_SUCCESS : VK_NOT_READY;
}
}  // namespace vkx
using namespace vkx;

// (the probe, cut from vk_replay.cpp)
@@PROBE@@

// (the copy's end, as present_image_with_trial stamps it)
void copy(uint32_t index, bool ahead, uint64_t id) {
    next_copy = @@COPY@@ + id;
    PendingPresent pending;
    PendingPresent* present_defer_into = ahead ? &pending : nullptr;
    (void)present_defer_into;
    struct { VkCommandBuffer cmd; } f{COPY_CMD};
@@STAMP@@
}
// (a present of present_commit's, as present_out makes it: on the replay's queue, or the presents' own; family 0)
VkResult present(uint32_t index, uint64_t set, int game = 0) {
    probe.copy_fence = handle<VkFence>(0x9000);
    probe.game = game, probe.set = set;
    VkPresentInfoKHR pi{VK_STRUCTURE_TYPE_PRESENT_INFO_KHR};
    const VkQueue queue = dev.present_queue_supported ? dev.present_queue : dev.queue;
    const bool probed = probe_ready(0);
    probe_note(probed);
    const VkResult res = probed ? probe_present(queue, pi, index, 0) : vkQueuePresentKHR(queue, &pi);
    probe.copy_fence = VK_NULL_HANDLE;
    probe.game = -1, probe.set = 0;
    return res;
}
void hold(bool held) {
    std::lock_guard lk(gpu_mu);
    gate_open = !held;
    gate_cv.notify_all();
}
// (the lines queued so far written: none left, every slot back)
bool idle() {
    const auto until = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (std::chrono::steady_clock::now() < until) {
        {
            std::lock_guard lk(probe.mu);
            if (probe.lines.empty() && probe.free.size() == (size_t)kProbeSlots) return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    std::printf("STUCK the probe's lines not written in 5 s\n");
    return false;
}
// (the run's end: what is in the file then, printed. game_end: as the game ends, through the window's close alone)
void finish(const char* path, bool game_end = false) {
    if (game_end) {
@@CLOSE@@
        std::printf("READER %d\n", probe.reader ? 1 : 0);   // (its thread object still held after the close)
    } else {
@@FINISH@@
    }
    if (FILE* f = std::fopen(path, "r")) {
        char line[1024];
        while (std::fgets(line, sizeof line, f)) std::printf("ROW %s", line);
        std::fclose(f);
    } else {
        std::printf("NOFILE\n");
    }
    std::printf("CALLS %d PRESENTS %d SUBMITS %d\n", vk_calls.load(), presents.load(), submits.load());
    std::fflush(stdout);
    std::_Exit(0);   // (as the game's end: ExitProcess, _exit on Linux; no stdio flushed for the probe)
}
void finish_close_only() {
@@CLOSE@@
}
void warm_up() {   // (the probe made, and its first set's lines written)
    copy(0, true, 1);
    present(0, 1, 0);
    copy(1, true, 2);
    present(1, 2, 1);
    idle();
}

int main(int argc, char** argv) {
    if (argc < 3) return 2;
    const std::string c = argv[1];
    const char* path = argv[2];
    if (c != "unset") {
#ifdef _WIN32
        _putenv_s("RX_VK_PRESENT_PROBE", path);
#else
        setenv("RX_VK_PRESENT_PROBE", path, 1);
#endif
    }
    dev.props.limits.timestampPeriod = 1.0f;
    if (c == "own") {
        warm_up();
        hold(true);
        copy(0, true, 100);
        present(0, 100);
        for (int k = 0; k < 70; k++) {
            copy(k % 3, true, 200 + k);
            present(k % 3, 200 + k);
        }
        hold(false);
        idle();
    } else if (c == "full") {
        warm_up();
        hold(true);
        for (int k = 0; k < 32; k++) {
            copy(k % 3, true, 100 + k);
            present(k % 3, 100 + k);
        }
        for (int i = 10; i < 60; i++) copy(i, true, 400 + i);
        hold(false);
        idle();
        present(59, 459);   // (its copy had no query free)
        idle();
    } else if (c == "first") {   // (the first set presented ahead: both copies made before its first present)
        copy(0, true, 1);
        copy(1, true, 2);
        present(0, 1, 0);
        present(1, 2, 1);
        idle();
    } else if (c == "unset" || c == "behind") {   // (no switch; or copies not presented ahead, handed to the presenter thread)
        copy(0, c == "unset", 1);
        copy(1, c == "unset", 2);
        if (c == "unset") present(0, 1), present(1, 2);
    } else if (c == "unread") {
        warm_up();
        probes_unread = true;
        copy(0, true, 100);
        present(0, 100);
        idle();
    } else if (c == "reuse") {
        warm_up();
        for (int k = 0; k < 200; k++) {
            copy(k % 3, true, 100 + k);
            present(k % 3, 100 + k);
            idle();
        }
    } else if (c == "close") {   // (the game closed right after its last presents: their lines maybe still queued)
        warm_up();
        for (int k = 0; k < 3; k++) {
            copy(k % 3, true, 100 + k);
            present(k % 3, 100 + k);
        }
        finish(path, true);
    } else if (c == "stuck") {   // (a line whose fence is never signalled, queued at the close)
        warm_up();
        unsignalled = submits + 2;   // (the next present's second probe)
        copy(0, true, 100);
        present(0, 100);
        copy(1, true, 101);
        present(1, 101);
        finish(path, true);
    } else if (c == "stuck_taken") {   // (a line whose fence never signals, the reader already waiting on it at the close)
        warm_up();
        unsignalled = submits + 2;
        copy(0, true, 100);
        present(0, 100);
        std::this_thread::sleep_for(std::chrono::milliseconds(150));
        const auto t0 = std::chrono::steady_clock::now();
        finish_close_only();
        std::printf("CLOSE_MS %d\n", (int)std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count());
        finish(path, true);
    } else if (c == "close_first") {   // (the window closed before the first copy presented ahead)
        finish_close_only();
        copy(0, true, 1);
        present(0, 1);
        finish(path, true);
    } else if (c == "after") {   // (a present after the close: made, not probed)
        warm_up();
        finish_close_only();
        std::this_thread::sleep_for(std::chrono::milliseconds(5100));   // (past the 5 s between two of probe_note's lines)
        copy(0, true, 100);
        if (present(0, 100) != VK_SUCCESS) std::printf("PRESENT failed\n");
        finish(path, true);
    } else if (c.rfind("refused_", 0) == 0) {   // (refused_<lost|memory>_<before|after>: one of a present's two probes refused)
        warm_up();
        refused = submits + (c.find("_before") != std::string::npos ? 1 : 2);
        refusal = c.find("_lost_") != std::string::npos ? VK_ERROR_DEVICE_LOST : VK_ERROR_OUT_OF_HOST_MEMORY;
        present_result = VK_SUBOPTIMAL_KHR;   // (the present's own result, returned as it came)
        copy(0, true, 100);
        std::printf("RESULT %d\n", (int)present(0, 100));
        present_result = VK_SUCCESS;
        const int submitted = submits;
        copy(1, true, 101);   // (the probe off: no stamp, no probe's submit)
        std::printf("RESULT %d\n", (int)present(1, 101));
        std::printf("SUBMITS AFTER %d\n", submits - submitted);
        finish(path, true);
    } else if (c == "late") {
        warm_up();
        late_ms = 50;
        copy(0, true, 100);
        present(0, 100);
        idle();
    } else if (c == "lost_copy") {   // (a copy that never runs; then 70 presents, each read before the next copy)
        warm_up();
        late_ms = -1;
        copy(0, true, 100);
        present(0, 100);
        idle();
        late_ms = 0;
        for (int k = 0; k < 70; k++) {
            copy(k % 3, true, 200 + k);
            present(k % 3, 200 + k);
            idle();
        }
    } else if (c == "recopy") {   // (image 0 copied twice before its present)
        warm_up();
        copy(0, true, 100);
        copy(0, true, 101);
        present(0, 101);
        idle();
    } else if (c == "nohost_own" || c == "nohost_replay") {
        dev.host_query_reset = false;
        if (c == "nohost_own") dev.present_queue_supported = true, dev.present_queue = handle<VkQueue>(0x31);
        warm_up();
        copy(0, true, 100);
        present(0, 100);
        idle();
    } else {
        return 2;
    }
    finish(path);
}
"""


def cut(source):
    text = (source / "gpu" / "vk" / "vk_replay.cpp").read_text(encoding="utf-8")
    probe = between(text, "constexpr int kProbeSlots = 32",
                    "}  // namespace\n// doc: docs/dev/reference/architecture.md#frame-generations-presents", "vk_replay.cpp")
    present = between(text, "bool source_srgb, bool allow_scale, OutputTrialSource trial, VkFormat framegen_format) {\n",
                      "\nbool present_commit(const PendingPresent& p) {", "vk_replay.cpp")
    stamps = re.findall(r"^\s*(probe_copy_stamp\([^;]*\);)", present, re.M)
    if len(stamps) != 1:
        raise SystemExit("FAIL: present_image_with_trial stamps its copy for RX_VK_PRESENT_PROBE %d times, not once" % len(stamps))
    return probe, "    " + stamps[0]


def close_code(probe):
    """The window's close, as mkwvk_close_asked makes it: the probe's close where there is one, else nothing for the probe"""
    if re.search(r"^void probe_close\(\) \{", probe, re.M):
        return "        probe_close();"
    return "        // (no probe_close: the window's close does nothing for the probe)"


def check_close_wiring(source):
    """The window's close (vk_main.cpp mkwvk_close_asked) closes the probe before its first way out, and
    present_probe_close (vk_replay.cpp) is the probe's close"""
    main = (source / "gpu" / "vk" / "vk_main.cpp").read_text(encoding="utf-8")
    close = between(main, "void mkwvk_close_asked(void) {", "\n}\n", "vk_main.cpp")
    at, out = close.find("present_probe_close();"), close.find("return;")
    if at < 0 or (0 <= out < at):
        return ["the window's close (mkwvk_close_asked) does not close RX_VK_PRESENT_PROBE's file before its first return"]
    replay = (source / "gpu" / "vk" / "vk_replay.cpp").read_text(encoding="utf-8")
    if not re.search(r"^void present_probe_close\(\) \{\s*probe_close\(\);\s*\}", replay, re.M):
        return ["vk_replay.cpp's present_probe_close is not the probe's close (probe_close)"]
    return []


def finish_code(probe):
    """The run's end: the probe closed where it can be; on the code before that, its lines waited for and its file flushed
    by hand, so the rows the reader wrote are all in the file"""
    if re.search(r"^void probe_close\(\) \{", probe, re.M):
        return "        probe_close();"
    return "        if (probe.file && idle()) std::fflush(probe.file);"


def build(args, tmp):
    probe, stamp = cut(args.source)
    source = FIXTURE.replace("@@PROBE@@", probe).replace("@@STAMP@@", stamp).replace("@@COPY@@", str(COPY))
    source = source.replace("@@FINISH@@", finish_code(probe)).replace("@@CLOSE@@", close_code(probe))
    cpp, exe = tmp / "fixture.cpp", tmp / ("fixture.exe" if sys.platform == "win32" else "fixture")
    cpp.write_text(source, encoding="utf-8")
    r = subprocess.run([args.cxx, "-std=c++20", "-O1", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Wno-unused-function",
                        "-pthread", "-I" + str(args.headers), str(cpp), "-o", str(exe)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        print(r.stdout + r.stderr)
        raise SystemExit("the fixture did not build")
    return exe


def run(exe, tmp, case):
    path = tmp / (case + ".csv")
    r = subprocess.run([str(exe), case, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout.splitlines()
    rows = [line[4:] for line in out if line.startswith("ROW ")]
    logs = [line[4:] for line in out if line.startswith("LOG ")]
    data = [row for row in rows if not row.startswith("#")]
    table = list(csv.DictReader(data)) if data else []
    return r.returncode, out, rows, logs, table


def by_set(table):
    return {int(row["set"]): row for row in table if (row.get("set") or "").isdigit()}   # (a row cut short: none)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headers", type=Path, required=True)
    parser.add_argument("--cxx", default="g++")
    parser.add_argument("--source", type=Path, default=ROOT)
    args = parser.parse_args()
    failures = []
    with tempfile.TemporaryDirectory(prefix="present-probe-") as tmp:
        tmp = Path(tmp)
        exe = build(args, tmp)

        code, out, rows, logs, table = run(exe, tmp, "own")
        sets = by_set(table)
        wrong = ["set %d: %s" % (s, row["gpu_copy_ns"]) for s, row in sorted(sets.items())
                 if s >= 100 and row["gpu_copy_ns"] != str(COPY + s)]
        if code or 100 not in sets or wrong:
            failures.append("own: a line held by the reader while 70 more copies were made has another copy's timestamp, "
                            "or none (expected each row's own, %d + its set): %s; exit %d, rows for %s"
                            % (COPY, ", ".join(wrong[:6]) or "-", code, sorted(sets)[:6]))

        code, out, rows, logs, table = run(exe, tmp, "full")
        sets = by_set(table)
        wrong = ["set %d: %s" % (s, row["gpu_copy_ns"]) for s, row in sorted(sets.items())
                 if 100 <= s < 132 and row["gpu_copy_ns"] != str(COPY + s)]
        full = [line for line in logs if "no copy timestamp free" in line]
        if code or len([s for s in sets if 100 <= s < 132]) != 32 or wrong:
            failures.append("full: 32 lines held by the reader while 50 more images were copied lost their copies' "
                            "timestamps to them: %s; exit %d" % (", ".join(wrong[:6]) or "rows missing", code))
        if len(full) != 1:
            failures.append("full: the log says %d times that a copy had no timestamp free (expected once): %s"
                            % (len(full), full[:2]))
        if sets.get(459, {}).get("gpu_copy_ns") != "-1":
            failures.append("full: the present of a copy that had no query free has gpu_copy_ns %s, expected -1 (none)"
                            % sets.get(459, {}).get("gpu_copy_ns", "(no row)"))
        closed = [line for line in logs if line.startswith("vk: present probe: closed")]
        if len(closed) != 1 or "; 18 copies without their timestamp (no query free), 0 queries set aside" not in closed[0]:
            failures.append("full: the closing line is %s, expected it to count 18 copies without their timestamp and 0 "
                            "queries set aside" % (closed or "none"))

        code, out, rows, logs, table = run(exe, tmp, "first")
        sets = by_set(table)
        wrong = ["set %d: %s" % (s, sets.get(s, {}).get("gpu_copy_ns", "(no row)")) for s in (1, 2)
                 if sets.get(s, {}).get("gpu_copy_ns") != str(COPY + s)]
        if code or wrong:
            failures.append("first: the first set presented ahead, its copies made before its first present: %s, expected "
                            "each its own copy's timestamp (the probe made at the first copy presented ahead); exit %d"
                            % (", ".join(wrong), code))

        for case in ("unset", "behind"):
            code, out, rows, logs, table = run(exe, tmp, case)
            calls = [line for line in out if line.startswith("CALLS ")]
            want = "CALLS 2 PRESENTS 2 SUBMITS 0" if case == "unset" else "CALLS 0 PRESENTS 0 SUBMITS 0"
            if code or "NOFILE" not in out or calls != [want] or logs:
                failures.append("%s: %s: expected no file, no log line and %s (the present's own calls alone); got "
                                "%s, %s, %s" % (case, "the switch unset" if case == "unset" else "copies not presented ahead",
                                                want, "a file" if "NOFILE" not in out else "no file", calls, logs[:2]))

        code, out, rows, logs, table = run(exe, tmp, "unread")
        row = by_set(table).get(100, {})
        if code or (row.get("gpu_before_ns"), row.get("gpu_after_ns")) != ("-1", "-1"):
            failures.append("unread: the probe's own timestamps not given by the driver are written as %s and %s, expected "
                            "-1 (none), not a value" % (row.get("gpu_before_ns", "(no row)"), row.get("gpu_after_ns", "(no row)")))

        code, out, rows, logs, table = run(exe, tmp, "reuse")
        sets = by_set(table)
        wrong = ["set %d: %s" % (s, row["gpu_copy_ns"]) for s, row in sorted(sets.items())
                 if s >= 100 and row["gpu_copy_ns"] != str(COPY + s)]
        if code or len([s for s in sets if s >= 100]) != 200 or wrong:
            failures.append("reuse: 200 presents, each line read before the next copy: a copy's query not given back once "
                            "read leaves the later copies without (expected each row's own): %s; exit %d"
                            % (", ".join(wrong[:6]) or "rows missing", code))

        code, out, rows, logs, table = run(exe, tmp, "close")
        sets = by_set(table)
        closed = [line for line in logs if line.startswith("vk: present probe: closed")]
        if code or not rows or not rows[0].startswith("#") or sorted(sets) != [1, 2, 100, 101, 102]:
            failures.append("close: the game closed right after its last presents: its file holds %d lines (%s), expected "
                            "its # line, the columns' names and 5 rows, sets 1, 2, 100 to 102: the lines queued written, the "
                            "file flushed and closed; exit %d" % (len(rows), sorted(sets), code))
        if len(closed) != 1 or "5 lines written, 0 not" not in closed[0]:
            failures.append("close: the log's closing line is %s, expected one saying 5 lines written, 0 not" % (closed or "none"))
        if "READER 0" not in out:
            failures.append("close: the reader's thread object is still held after the window's close (%s), expected "
                            "it deleted once joined" % ([line for line in out if line.startswith("READER")] or "no line"))

        code, out, rows, logs, table = run(exe, tmp, "stuck")
        sets = by_set(table)
        closed = [line for line in logs if line.startswith("vk: present probe: closed")]
        if code or sorted(sets) != [1, 2, 101] or len(closed) != 1 or "3 lines written, 1 not" not in closed[0] \
                or "0 copies without their timestamp (no query free), 1 queries set aside" not in closed[0]:
            failures.append("stuck: a line whose fence never signals, queued at the close: the rows are %s and the log %s, "
                            "expected sets 1, 2 and 101 written and the closing line saying 3 written, 1 not, and its "
                            "copy's query set aside (1); exit %d"
                            % (sorted(sets), closed or "none", code))

        code, out, rows, logs, table = run(exe, tmp, "stuck_taken")
        took = [int(line.split()[1]) for line in out if line.startswith("CLOSE_MS ")]
        closed = [line for line in logs if line.startswith("vk: present probe: closed")]
        if code or not took or took[0] >= 1000 or len(closed) != 1 or "2 lines written, 1 not" not in closed[0]:
            failures.append("stuck_taken: a line whose fence never signals, the reader waiting on it at the window's close: "
                            "the close took %s ms, log %s; expected under 1000 ms (the drain bounded) and the line counted "
                            "not written (2 written, 1 not); exit %d" % (took[0] if took else "?", closed or "none", code))

        code, out, rows, logs, table = run(exe, tmp, "close_first")
        calls = [line for line in out if line.startswith("CALLS ")]
        if code or "NOFILE" not in out or not calls or " PRESENTS 1 " not in calls[0] + " " or logs:
            failures.append("close_first: the window closed before the first copy presented ahead: %s, %s, logs %s; "
                            "expected no probe made after it (no file, no log line) and the present made"
                            % ("no file" if "NOFILE" in out else "a file", calls, logs[:2]))

        code, out, rows, logs, table = run(exe, tmp, "after")
        sets = by_set(table)
        calls = [line for line in out if line.startswith("CALLS ")]
        if code or sorted(sets) != [1, 2] or "PRESENT failed" in out or not calls or not calls[0].endswith(" PRESENTS 3 SUBMITS 4") \
                or [line for line in logs if "not probed" in line]:
            failures.append("after: a present after the close: rows %s, %s, logs %s; expected it made (3 presents), not "
                            "probed (4 submits, the first set's), no row and no line saying the presents are not probed" % (sorted(sets), calls, logs[-2:]))

        failures += check_close_wiring(args.source)

        for which in ("before", "after"):
            code, out, rows, logs, table = run(exe, tmp, "refused_lost_" + which)
            calls = [line for line in out if line.startswith("CALLS ")]
            want = 2 if which == "before" else 3   # (the warm-up's two presents, and the present itself once its first probe went)
            if code != 3 or "LOST" not in out or "PRESENTS %d " % want not in (calls or [""])[0] + " ":
                failures.append("refused_lost_%s: the probe's submit %s the present came back VK_ERROR_DEVICE_LOST: exit %d, %s, "
                                "%s; expected the device's loss reported (device_lost: exit 3, LOST) at once, %d presents made"
                                % (which, which, code, "LOST" if "LOST" in out else "no loss reported", calls, want))
            code, out, rows, logs, table = run(exe, tmp, "refused_memory_" + which)
            sets = by_set(table)
            said = [line for line in logs if "not submitted" in line]
            results = [line for line in out if line.startswith("RESULT ")]
            after = [line for line in out if line.startswith("SUBMITS AFTER ")]
            calls = [line for line in out if line.startswith("CALLS ")]
            if code or results != ["RESULT %d" % 1000001003, "RESULT 0"] or not calls or " PRESENTS 4 " not in calls[0] + " ":
                failures.append("refused_memory_%s: the probe's submit %s the present refused (out of host memory): exit %d, "
                                "%s, %s; expected both presents made once, each its own result (VK_SUBOPTIMAL_KHR, then "
                                "VK_SUCCESS)" % (which, which, code, results, calls))
            if len(said) != 1 or "(VkResult -1): off" not in said[0]:
                failures.append("refused_memory_%s: the log says %s; expected one line that the probe's submit was refused "
                                "(VkResult -1), the probe off" % (which, said or "nothing"))
            closed = [line for line in logs if line.startswith("vk: present probe: closed")]
            if len(closed) != 1 or "closed: 2 lines written, 0 not" not in closed[0]:
                failures.append("refused_memory_%s: the closing line is %s; expected the warm-up's 2 lines written and none "
                                "not (the refused present's line never queued)" % (which, closed or "none"))
            if after != ["SUBMITS AFTER 0"] or sorted(sets) != [1, 2] \
                    or [line for line in logs if "not probed" in line]:
                failures.append("refused_memory_%s: after the refusal %s, rows %s; expected the probe off: no probe's submit, "
                                "no row for the refused present nor the next, and no line saying the presents are not probed"
                                % (which, after, sorted(sets)))

        code, out, rows, logs, table = run(exe, tmp, "late")
        sets = by_set(table)
        if code or sets.get(100, {}).get("gpu_copy_ns") != str(COPY + 100):
            failures.append("late: a copy run after the probe's fences were seen, in a query an earlier copy used: its line "
                            "has %s, expected %d (its own, waited for; not the earlier copy's)"
                            % (sets.get(100, {}).get("gpu_copy_ns", "(no row)"), COPY + 100))

        code, out, rows, logs, table = run(exe, tmp, "lost_copy")
        sets = by_set(table)
        closed = [line for line in logs if line.startswith("vk: present probe: closed")]
        wrong = ["set %d: %s" % (s, row["gpu_copy_ns"]) for s, row in sorted(sets.items())
                 if 200 <= s and row["gpu_copy_ns"] != str(COPY + s)]
        if code or sets.get(100, {}).get("gpu_copy_ns") != "-1":
            failures.append("lost_copy: a copy that never runs: its line has %s, expected -1"
                            % sets.get(100, {}).get("gpu_copy_ns", "(no row)"))
        if len([s for s in sets if s >= 200]) != 70 or wrong or len(closed) != 1 or "1 queries set aside" not in closed[0]:
            failures.append("lost_copy: after a copy that never ran, 70 presents: %s, closing line %s; expected each its own "
                            "copy's timestamp, the lost copy's query set aside (1), never handed out again"
                            % (", ".join(wrong[:4]) or "%d rows" % len(sets), closed or "none"))

        code, out, rows, logs, table = run(exe, tmp, "recopy")
        sets = by_set(table)
        closed = [line for line in logs if line.startswith("vk: present probe: closed")]
        if code or sets.get(101, {}).get("gpu_copy_ns") != str(COPY + 101) or len(closed) != 1 \
                or "1 queries set aside" not in closed[0]:
            failures.append("recopy: an image copied again before its present: its line has %s, closing line %s; expected "
                            "%d (the second copy's) and the first copy's query set aside (1)"
                            % (sets.get(101, {}).get("gpu_copy_ns", "(no row)"), closed or "none", COPY + 101))

        code, out, rows, logs, table = run(exe, tmp, "nohost_own")
        sets = by_set(table)
        said = [line for line in logs if "hostQueryReset" in line]
        stamped = ["set %d: %s" % (s, row["gpu_copy_ns"]) for s, row in sorted(sets.items()) if row["gpu_copy_ns"] != "-1"]
        if code or sorted(sets) != [1, 2, 100] or stamped or len(said) != 1:
            failures.append("nohost_own: the presents on their own queue, no reset of queries from the CPU: rows %s, %s, log "
                            "%s; expected every gpu_copy_ns -1 (no copy stamped: nothing orders the probe after the copies) "
                            "and one line saying why" % (sorted(sets), ", ".join(stamped[:3]) or "none stamped", said))

        code, out, rows, logs, table = run(exe, tmp, "nohost_replay")
        sets = by_set(table)
        wrong = ["set %d: %s" % (s, row["gpu_copy_ns"]) for s, row in sorted(sets.items()) if row["gpu_copy_ns"] != str(COPY + s)]
        if code or sorted(sets) != [1, 2, 100] or wrong or [line for line in logs if "hostQueryReset" in line]:
            failures.append("nohost_replay: the presents on the replay's queue, no reset of queries from the CPU: rows %s, "
                            "%s; expected each its own copy's timestamp, and no line about hostQueryReset"
                            % (sorted(sets), ", ".join(wrong[:3]) or "-"))
    if failures:
        raise SystemExit("FAIL:\n  " + "\n  ".join(failures))
    print("ok: the probe's lines read their own copies' timestamps: held, full, reused, late, lost and copied again")


if __name__ == "__main__":
    main()
