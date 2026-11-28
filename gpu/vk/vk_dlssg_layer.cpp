// (longshot) Longshot's own Vulkan layer under NVIDIA Streamline (vk_dlssg_layer.h). The layer is this DLL: its manifest,
// written at the start in a folder beside longshot_vk.dll and given to the loader in VK_ADD_LAYER_PATH, names
// longshot_vk.dll, already loaded, and the loader finds vkNegotiateLoaderLayerInterfaceVersion below.
//
// What it does, on a device with VK_EXT_present_timing (dlssg_layer_device_extensions): a swapchain made through it gets
// VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT where its surface reports the stages (made again as asked when that is
// refused), and every present on such a swapchain asks for its stages' times: a VkPresentTimingsInfoEXT put ahead of the
// present's own chain in a copy of its VkPresentInfoKHR, the plugin's structures behind it untouched. The times are read
// back after each present without the plugin's metering structure (the second of a DLSS-G batch, or any present outside
// metering), never between a batch's two presents, and go to the law's reader (dlssg_law.h), which asks the game for the
// toggle (vk_dlssg.cpp dlssg_hold). Without the extension, or on a swapchain that takes no timing, every call is passed on
// as it came and nothing is read.
//
// RX_VK_DLSSG_LAYER_CSV=<file>, for measurements: everything the layer sees, a line each with QueryPerformanceCounter's
// ticks, by its first field (# qpc_hz=... first):
//   s,qpc,swapchain,flags_in,flag_added,res,stages            a swapchain made through the layer
//   x,qpc,swapchain,waiting                                   one destroyed, with its asks not read back
//   q,qpc,swapchain,size,res                                  its timing queue sized
//   d,qpc,swapchain,counter,domain,id / u,qpc,swapchain,domain,id   its time domains, and the one asked in
//   p,n,call_qpc,ret_qpc,done_qpc,queue,swapchain,image,cfg,frames,fb_before,fb_after,asked,res
//        each present, n its count; cfg 1 when it carries VkSetPresentConfigNV (the plugin's metering structure, on the
//        first present of a batch), frames its numFramesPerBatch, fb_* its presentConfigFeedback before the call and after
//        it; done_qpc after the layer's own work (the read back)
//   t,n,swapchain,present_id,target,domain,domain_id,complete,queue_end,dequeued,first_pixel_out,first_pixel_visible,poll_qpc
//        one present's stages (0: not reported) in the domain's time, n the present's (results in the presents' order)
//   c,qpc_before,swapchain,domain,domain_id,stage,value,qpc_value,max_deviation,qpc_after   the domain against QPC, once a second
//   e,qpc,text,value                                          an error, or an ask not made
//   r,from_qpc,to_qpc,state,values,in_band,spread_us,median_us,l2_run,reading,band   a half second read by the law's
//        reader; state L1, L2, - (not read) or pause; band m (first_pixel_out follows the made image), g (the game's) or -
//   w,qpc,asked                                               the reader's half seconds L2 in a row: the toggle asked (1) or not (0)
//   k,call_qpc,sent_qpc,held                                  the game's answer to an ask: held 0 when it let it go
#include "vk_dlssg_layer.h"

#if defined(LONGSHOT_SL) && defined(VK_EXT_present_timing) && defined(VK_NV_present_metering)

#include <windows.h>

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>

#include <vulkan/vk_layer.h>

#include "dlssg_law.h"
#include "vk_dlssg.h"

namespace mkwvk {

namespace {

constexpr const char* kLayerName = "VK_LAYER_LONGSHOT_streamline";

int64_t qpc() {
    LARGE_INTEGER q;
    QueryPerformanceCounter(&q);
    return q.QuadPart;
}
int64_t qpc_hz() {
    static const int64_t hz = [] {
        LARGE_INTEGER q;
        QueryPerformanceFrequency(&q);
        return q.QuadPart;
    }();
    return hz;
}

/// The CSV (RX_VK_DLSSG_LAYER_CSV): lines added under its own lock, written by a thread of their own a few times a second
struct Out {
    std::mutex mu;
    std::condition_variable cv;
    std::string pending;   // (mu)
    std::atomic<FILE*> file{nullptr};
    std::once_flag opened;
};
Out& out() {
    static Out& o = *new Out;   // (never destroyed: its thread writes until the process ends)
    return o;
}
void out_main() {
    SetThreadDescription(GetCurrentThread(), L"mkw_vk dlssg layer");
    Out& o = out();
    std::string chunk;
    for (;;) {
        {
            std::unique_lock lk(o.mu);
            o.cv.wait_for(lk, std::chrono::milliseconds(300));
            chunk.swap(o.pending);
        }
        if (chunk.empty()) continue;
        std::fwrite(chunk.data(), 1, chunk.size(), o.file.load());
        std::fflush(o.file.load());
        chunk.clear();
    }
}
void open_out() {
    const char* path = getenv("RX_VK_DLSSG_LAYER_CSV");
    if (!path || !*path) return;
    std::call_once(out().opened, [path] {
        FILE* f = std::fopen(path, "w");
        if (!f) {
            logf("vk: Longshot's layer under Streamline: %s not opened: no file", path);
            return;
        }
        std::fprintf(f, "# qpc_hz=%lld\n", (long long)qpc_hz());
        std::fflush(f);
        out().file.store(f);
        std::thread(out_main).detach();
        logf("vk: Longshot's layer under Streamline: written to %s", path);
    });
}
#if defined(__GNUC__)
__attribute__((format(printf, 1, 2)))
#endif
void line(const char* fmt, ...) {
    Out& o = out();
    if (!o.file.load(std::memory_order_relaxed)) return;
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    const int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n <= 0) return;
    std::lock_guard lk(o.mu);
    o.pending.append(buf, (size_t)n < sizeof buf ? (size_t)n : sizeof buf - 1);
    o.pending.push_back('\n');
}

const char* domain_name(VkTimeDomainKHR d) {
    switch ((int)d) {
    case VK_TIME_DOMAIN_DEVICE_KHR: return "device";
    case VK_TIME_DOMAIN_CLOCK_MONOTONIC_KHR: return "monotonic";
    case VK_TIME_DOMAIN_CLOCK_MONOTONIC_RAW_KHR: return "monotonic_raw";
    case VK_TIME_DOMAIN_QUERY_PERFORMANCE_COUNTER_KHR: return "qpc";
    case VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT: return "present_stage_local";
    case VK_TIME_DOMAIN_SWAPCHAIN_LOCAL_EXT: return "swapchain_local";
    default: return "other";
    }
}

/// The loader's dispatch key of a dispatchable handle: the same for an instance and its physical devices, a device and its queues
void* key(const void* h) { return *(void* const*)h; }

struct Inst {
    VkInstance instance = VK_NULL_HANDLE;
    PFN_vkGetInstanceProcAddr gipa = nullptr;
    PFN_vkDestroyInstance destroy = nullptr;
    PFN_vkGetPhysicalDeviceSurfaceCapabilities2KHR surface_caps = nullptr;
};

constexpr uint32_t kQueueSize = 128;   // (a swapchain's timing queue)
constexpr size_t kMaxWaiting = 48;     // (asked and not read back: past that no asks until some come back)
constexpr uint32_t kReadAtOnce = 16;

struct Swapchain {
    VkPresentStageFlagsEXT stages = 0;
    bool have_domain = false;
    VkTimeDomainKHR domain = VK_TIME_DOMAIN_DEVICE_KHR;
    uint64_t domain_id = 0, domains_counter = 0;
    bool cut = false, said_full = false;
    std::deque<uint64_t> waiting;   // (the asked presents' n, oldest first)
    int64_t last_calibration = 0;
};

struct Dev {
    VkDevice device = VK_NULL_HANDLE;
    VkPhysicalDevice physical = VK_NULL_HANDLE;
    Inst* inst = nullptr;
    PFN_vkGetDeviceProcAddr gdpa = nullptr;
    PFN_vkDestroyDevice destroy = nullptr;
    PFN_vkQueuePresentKHR present = nullptr;
    PFN_vkCreateSwapchainKHR create_swapchain = nullptr;
    PFN_vkDestroySwapchainKHR destroy_swapchain = nullptr;
    PFN_vkSetSwapchainPresentTimingQueueSizeEXT queue_size = nullptr;
    PFN_vkGetSwapchainTimeDomainPropertiesEXT domain_props = nullptr;
    PFN_vkGetPastPresentationTimingEXT past = nullptr;
    PFN_vkGetCalibratedTimestampsKHR calibrated = nullptr;
    bool timing = false;   // (the device has present timing enabled and its functions)
    std::mutex mu;         // (below)
    std::unordered_map<VkSwapchainKHR, Swapchain> swapchains;
};

std::mutex g_mu;   // (the two maps)
std::unordered_map<void*, Inst*> g_inst;
std::unordered_map<void*, Dev*> g_dev;
std::atomic<uint64_t> g_presents{0};
std::atomic<bool> g_in_instance{false};   // (an instance was made through the layer)
std::atomic<bool> g_device_wanted{false};   // (the device's extensions were added: its feature follows)

Inst* inst_of(const void* h) {
    std::lock_guard lk(g_mu);
    auto it = g_inst.find(key(h));
    return it == g_inst.end() ? nullptr : it->second;
}
Dev* dev_of(const void* h) {
    std::lock_guard lk(g_mu);
    auto it = g_dev.find(key(h));
    return it == g_dev.end() ? nullptr : it->second;
}

std::atomic<int64_t> g_want{0};   // (the reader's ask, its QPC time; 0: none)

/// The law's reader (dlssg_law.h), on the last swapchain made through the layer with asks. Its lock is taken after a
/// Dev's mu, never before it
struct Law {
    std::mutex mu;
    VkSwapchainKHR sc = VK_NULL_HANDLE;
    dlssg_law::Reader reader;
    bool reading = false;
    int unread = 0;   // (half seconds in a row not read: the log says so once, at 30 s, until the law is read again)
    char band = 0;    // (the last band read; the log says when it changes, 8 times at most)
    int bands_told = 0;
    int holds_told = 0;   // (the toggle's holds the log said, 32 at most)
};
Law& law() {
    static Law& l = *new Law;
    return l;
}

/// With the reader's lock held, after it took something in: what it did goes to the CSV and the log, and its ask to the game
void law_settle(Law& l) {
    dlssg_law::Reader& r = l.reader;
    for (const dlssg_law::Event& e : r.events) {
        if (e.kind == 'r') {
            line("r,%lld,%lld,%s,%d,%d,%.0f,%.0f,%d,%d,%c", (long long)e.from, (long long)e.to,
                 e.state == '1' ? "L1" : e.state == '2' ? "L2" : e.state == 'p' ? "pause" : "-", e.values, e.in_band,
                 e.spread_ms * 1000, e.median_ms * 1000, e.l2_run, e.reading ? 1 : 0, e.band ? e.band : '-');
            if (e.band && e.band != l.band && l.bands_told < 8) {
                l.band = e.band, l.bands_told++;
                logf("vk: Longshot's layer under Streamline: the driver's present timing follows %s (median %.1f ms)",
                     e.band == 'm' ? "DLSS FG's made frame" : "the game's frame", e.median_ms);
            }
            if (e.reading != l.reading) {
                l.reading = e.reading;
                logf("vk: Longshot's layer under Streamline: the driver's metering law %s", e.reading ? "is read" : "is no longer read: no toggle is asked");
            }
            // (a law never read from the start says nothing above: the log says so once the reader has read nothing for
            // 30 s: no batches of two (DLSS FG off or paused), or present times in neither of the reader's bands)
            l.unread = e.reading ? 0 : l.unread + 1;
            if (l.unread == 60)
                logf("vk: Longshot's layer under Streamline: the driver's metering law has not been read for 30 s: no toggle is asked");
        } else if (e.kind == 'w') {
            line("w,%lld,%d", (long long)e.from, e.asked ? 1 : 0);
        }
    }
    r.events.clear();
    if (r.ask_at) g_want.store(r.ask_at), r.ask_at = 0;
}

/// With the Dev's mu held: a present of the reader's swapchain, after its call (call: its QPC time)
void law_present(VkSwapchainKHR sc, uint64_t n, int64_t call, const VkSetPresentConfigNV* cfg) {
    Law& l = law();
    std::lock_guard lk(l.mu);
    if (sc != l.sc) return;
    if (cfg && cfg->numFramesPerBatch == dlssg_law::kBatch) l.reader.batch_first(n, call);
    else if (!cfg) l.reader.other_present(n, call);
    law_settle(l);
}
/// With the Dev's mu held: a first_pixel_out time come back for present n (its domain's; QPC's when qpc_domain)
void law_time(VkSwapchainKHR sc, uint64_t n, uint64_t first_pixel_out, bool qpc_domain) {
    Law& l = law();
    std::lock_guard lk(l.mu);
    if (sc == l.sc) l.reader.time(n, first_pixel_out, qpc_domain);
}
/// With the Dev's mu held: a calibration of first_pixel_out's domain (its time, QPC's)
void law_calibrated(VkSwapchainKHR sc, int64_t ts, int64_t q) {
    Law& l = law();
    std::lock_guard lk(l.mu);
    if (sc == l.sc) l.reader.calibrated(ts, q);
}
/// With the Dev's mu held: the reader's swapchain asks its times in another domain. What the reader took in the old one
/// is gone, its calibration first: the slope runs from the first point it was given
void law_domain(VkSwapchainKHR sc) {
    Law& l = law();
    std::lock_guard lk(l.mu);
    if (sc == l.sc) l.reader.reset();
}
/// Another swapchain for the reader (null: none)
void law_swapchain(VkSwapchainKHR sc) {
    Law& l = law();
    std::lock_guard lk(l.mu);
    l.sc = sc;
    l.reader.hz = qpc_hz(), l.reader.toggle = dlssg_toggles();
    l.reader.reset();
}

/// With d.mu held: a swapchain's time domains read, the one its asks use chosen (QPC first, then the swapchain's own, then a
/// stage's own, then the first)
void read_domains(Dev& d, VkSwapchainKHR h, Swapchain& s) {
    VkSwapchainTimeDomainPropertiesEXT p{VK_STRUCTURE_TYPE_SWAPCHAIN_TIME_DOMAIN_PROPERTIES_EXT};
    uint64_t counter = 0;
    VkResult r = d.domain_props(d.device, h, &p, &counter);
    std::vector<VkTimeDomainKHR> domains(r < 0 ? 0 : p.timeDomainCount);
    std::vector<uint64_t> ids(domains.size());
    if (!domains.empty()) {
        p.pTimeDomains = domains.data(), p.pTimeDomainIds = ids.data();
        r = d.domain_props(d.device, h, &p, &counter);
    }
    if (r < 0 || !p.timeDomainCount || domains.empty()) {
        line("e,%lld,domains_not_read,%d", (long long)qpc(), (int)r);
        s.have_domain = false;
        return;
    }
    const uint32_t n = std::min<uint32_t>(p.timeDomainCount, (uint32_t)domains.size());
    s.domains_counter = counter;
    int pick = -1;
    const VkTimeDomainKHR order[] = {VK_TIME_DOMAIN_QUERY_PERFORMANCE_COUNTER_KHR, VK_TIME_DOMAIN_SWAPCHAIN_LOCAL_EXT,
                                     VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT};
    for (VkTimeDomainKHR want : order) {
        for (uint32_t i = 0; i < n && pick < 0; i++)
            if (domains[i] == want) pick = (int)i;
        if (pick >= 0) break;
    }
    if (pick < 0) pick = 0;
    for (uint32_t i = 0; i < n; i++)
        line("d,%lld,%p,%llu,%s,%llu", (long long)qpc(), (void*)h, (unsigned long long)counter, domain_name(domains[i]),
             (unsigned long long)ids[i]);
    s.domain = domains[pick], s.domain_id = ids[pick], s.have_domain = true;
    line("u,%lld,%p,%s,%llu", (long long)qpc(), (void*)h, domain_name(s.domain), (unsigned long long)s.domain_id);
}

/// With d.mu held: the asked domain against QPC, once a second (each stage apart when it is a stage's own); nothing when it
/// is QPC already
void calibrate(Dev& d, VkSwapchainKHR h, Swapchain& s, int64_t now) {
    if (now - s.last_calibration < qpc_hz()) return;
    s.last_calibration = now;
    if (!d.calibrated || !s.have_domain || s.domain == VK_TIME_DOMAIN_QUERY_PERFORMANCE_COUNTER_KHR) return;
    const bool per_stage = s.domain == VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT;
    for (uint32_t bit = 1; bit <= (uint32_t)VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT; bit <<= 1) {
        if (per_stage && !(s.stages & bit)) continue;
        VkSwapchainCalibratedTimestampInfoEXT sc{VK_STRUCTURE_TYPE_SWAPCHAIN_CALIBRATED_TIMESTAMP_INFO_EXT};
        sc.swapchain = h, sc.presentStage = per_stage ? bit : 0, sc.timeDomainId = s.domain_id;
        VkCalibratedTimestampInfoKHR ci[2] = {{VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_KHR},
                                              {VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_KHR}};
        ci[0].pNext = &sc, ci[0].timeDomain = s.domain;
        ci[1].timeDomain = VK_TIME_DOMAIN_QUERY_PERFORMANCE_COUNTER_KHR;
        uint64_t ts[2] = {0, 0}, deviation = 0;
        const int64_t before = qpc();
        const VkResult r = d.calibrated(d.device, 2, ci, ts, &deviation);
        const int64_t after = qpc();
        if (r != VK_SUCCESS) {
            line("e,%lld,calibration_failed,%d", (long long)before, (int)r);
            return;
        }
        line("c,%lld,%p,%s,%llu,%u,%llu,%llu,%llu,%lld", (long long)before, (void*)h, domain_name(s.domain),
             (unsigned long long)s.domain_id, per_stage ? bit : 0u, (unsigned long long)ts[0], (unsigned long long)ts[1],
             (unsigned long long)deviation, (long long)after);
        if (!per_stage || bit == (uint32_t)VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT) law_calibrated(h, (int64_t)ts[0], (int64_t)ts[1]);
        if (!per_stage) return;
    }
}

/// With d.mu held: the results the driver has for a swapchain, each with its oldest present waiting
void read_back(Dev& d, VkSwapchainKHR h, Swapchain& s) {
    VkPastPresentationTimingInfoEXT info{VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_INFO_EXT};
    info.swapchain = h;
    for (int round = 0; round < 8 && !s.waiting.empty(); round++) {
        VkPresentStageTimeEXT stages[kReadAtOnce][4];
        VkPastPresentationTimingEXT got[kReadAtOnce];
        for (uint32_t i = 0; i < kReadAtOnce; i++) {
            got[i] = {VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_EXT};
            got[i].presentStageCount = 4;
            got[i].pPresentStages = stages[i];
        }
        VkPastPresentationTimingPropertiesEXT p{VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_PROPERTIES_EXT};
        p.presentationTimingCount = kReadAtOnce;
        p.pPresentationTimings = got;
        const VkResult r = d.past(d.device, &info, &p);
        const int64_t now = qpc();
        if (r < 0) {
            line("e,%lld,read_failed,%d", (long long)now, (int)r);
            logf("vk: Longshot's layer under Streamline: the presents' times were not read (VkResult %d): no more asked on this swapchain", (int)r);
            s.cut = true;
            return;
        }
        for (uint32_t i = 0; i < p.presentationTimingCount && i < kReadAtOnce; i++) {
            const VkPastPresentationTimingEXT& g = got[i];
            uint64_t at[4] = {0, 0, 0, 0};   // (queue end, dequeued, first pixel out, first pixel visible)
            for (uint32_t k = 0; k < g.presentStageCount && k < 4; k++) {
                const VkPresentStageFlagsEXT st = g.pPresentStages[k].stage;
                const int slot = st == VK_PRESENT_STAGE_QUEUE_OPERATIONS_END_BIT_EXT        ? 0
                                 : st == VK_PRESENT_STAGE_REQUEST_DEQUEUED_BIT_EXT          ? 1
                                 : st == VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT     ? 2
                                 : st == VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT ? 3
                                                                                            : -1;
                if (slot >= 0) at[slot] = g.pPresentStages[k].time;
            }
            uint64_t n = 0;
            if (!s.waiting.empty()) n = s.waiting.front(), s.waiting.pop_front();
            line("t,%llu,%p,%llu,%llu,%s,%llu,%u,%llu,%llu,%llu,%llu,%lld", (unsigned long long)n, (void*)h,
                 (unsigned long long)g.presentId, (unsigned long long)g.targetTime, domain_name(g.timeDomain),
                 (unsigned long long)g.timeDomainId, (unsigned)g.reportComplete, (unsigned long long)at[0],
                 (unsigned long long)at[1], (unsigned long long)at[2], (unsigned long long)at[3], (long long)now);
            // (a present asked before the domain changed comes back in the old one, which the reader is not calibrated for.
            // A time in QPC needs no calibration: taken whatever the swapchain asks in now)
            const bool in_qpc = g.timeDomain == VK_TIME_DOMAIN_QUERY_PERFORMANCE_COUNTER_KHR;
            if (in_qpc || (g.timeDomain == s.domain && g.timeDomainId == s.domain_id)) law_time(h, n, at[2], in_qpc);
        }
        if (p.timeDomainsCounter != s.domains_counter) {
            const VkTimeDomainKHR was = s.domain;
            const uint64_t was_id = s.domain_id;
            read_domains(d, h, s);
            if (s.domain != was || s.domain_id != was_id) law_domain(h), s.last_calibration = 0;   // (calibrated at once)
        }
        if (r != VK_INCOMPLETE) return;
    }
}

bool device_has_timing(const VkDeviceCreateInfo* ci) {
    bool ext = false, feature = false;
    for (uint32_t i = 0; i < ci->enabledExtensionCount; i++)
        if (!strcmp(ci->ppEnabledExtensionNames[i], VK_EXT_PRESENT_TIMING_EXTENSION_NAME)) ext = true;
    for (auto* b = (const VkBaseInStructure*)ci->pNext; b; b = b->pNext)
        if (b->sType == VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT)
            feature = ((const VkPhysicalDevicePresentTimingFeaturesEXT*)b)->presentTiming == VK_TRUE;
    return ext && feature;
}

// ---- the layer's functions ----

PFN_vkVoidFunction VKAPI_CALL layer_GetInstanceProcAddr(VkInstance instance, const char* name);
PFN_vkVoidFunction VKAPI_CALL layer_GetDeviceProcAddr(VkDevice device, const char* name);

VkResult VKAPI_CALL layer_CreateInstance(const VkInstanceCreateInfo* ci, const VkAllocationCallbacks* alloc, VkInstance* out) {
    auto* link = (VkLayerInstanceCreateInfo*)ci->pNext;
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO && link->function == VK_LAYER_LINK_INFO))
        link = (VkLayerInstanceCreateInfo*)link->pNext;
    if (!link || !link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    const PFN_vkGetInstanceProcAddr gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    const auto create = (PFN_vkCreateInstance)gipa(VK_NULL_HANDLE, "vkCreateInstance");
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;
    const VkResult r = create(ci, alloc, out);
    if (r != VK_SUCCESS) return r;
    auto* in = new Inst;
    in->instance = *out, in->gipa = gipa;
    in->destroy = (PFN_vkDestroyInstance)gipa(*out, "vkDestroyInstance");
    in->surface_caps = (PFN_vkGetPhysicalDeviceSurfaceCapabilities2KHR)gipa(*out, "vkGetPhysicalDeviceSurfaceCapabilities2KHR");
    {
        std::lock_guard lk(g_mu);
        g_inst[key(*out)] = in;
    }
    g_in_instance.store(true);
    return r;
}

void VKAPI_CALL layer_DestroyInstance(VkInstance instance, const VkAllocationCallbacks* alloc) {
    if (!instance) return;
    Inst* in = nullptr;
    {
        std::lock_guard lk(g_mu);
        auto it = g_inst.find(key(instance));
        if (it != g_inst.end()) in = it->second, g_inst.erase(it);
        if (g_inst.empty()) g_in_instance.store(false);
    }
    if (in && in->destroy) in->destroy(instance, alloc);
    delete in;
}

VkResult VKAPI_CALL layer_CreateDevice(VkPhysicalDevice physical, const VkDeviceCreateInfo* ci, const VkAllocationCallbacks* alloc,
                                       VkDevice* out) {
    auto* link = (VkLayerDeviceCreateInfo*)ci->pNext;
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && link->function == VK_LAYER_LINK_INFO))
        link = (VkLayerDeviceCreateInfo*)link->pNext;
    if (!link || !link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    const PFN_vkGetInstanceProcAddr gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    const PFN_vkGetDeviceProcAddr gdpa = link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    Inst* in = inst_of(physical);
    const auto create = (PFN_vkCreateDevice)gipa(in ? in->instance : VK_NULL_HANDLE, "vkCreateDevice");
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;
    const VkResult r = create(physical, ci, alloc, out);
    if (r != VK_SUCCESS) return r;
    auto* d = new Dev;
    d->device = *out, d->physical = physical, d->inst = in, d->gdpa = gdpa;
#define LOAD(f, name) d->f = (decltype(d->f))gdpa(*out, name)
    LOAD(destroy, "vkDestroyDevice");
    LOAD(present, "vkQueuePresentKHR");
    LOAD(create_swapchain, "vkCreateSwapchainKHR");
    LOAD(destroy_swapchain, "vkDestroySwapchainKHR");
    LOAD(queue_size, "vkSetSwapchainPresentTimingQueueSizeEXT");
    LOAD(domain_props, "vkGetSwapchainTimeDomainPropertiesEXT");
    LOAD(past, "vkGetPastPresentationTimingEXT");
    LOAD(calibrated, "vkGetCalibratedTimestampsKHR");
    if (!d->calibrated) LOAD(calibrated, "vkGetCalibratedTimestampsEXT");
#undef LOAD
    d->timing = device_has_timing(ci) && d->queue_size && d->domain_props && d->past && in && in->surface_caps;
    {
        std::lock_guard lk(g_mu);
        g_dev[key(*out)] = d;
    }
    if (d->timing) open_out();
    logf("vk: Longshot's layer under Streamline: %s", d->timing ? "reads when DLSS FG's presents reach the screen"
                                                               : "no present timing on this device: every call passed on, no toggle");
    return r;
}

void VKAPI_CALL layer_DestroyDevice(VkDevice device, const VkAllocationCallbacks* alloc) {
    if (!device) return;
    Dev* d = nullptr;
    {
        std::lock_guard lk(g_mu);
        auto it = g_dev.find(key(device));
        if (it != g_dev.end()) d = it->second, g_dev.erase(it);
    }
    if (d && d->destroy) d->destroy(device, alloc);
    delete d;
}

VkResult VKAPI_CALL layer_CreateSwapchainKHR(VkDevice device, const VkSwapchainCreateInfoKHR* ci, const VkAllocationCallbacks* alloc,
                                             VkSwapchainKHR* out) {
    Dev* d = dev_of(device);
    if (!d) return VK_ERROR_INITIALIZATION_FAILED;
    if (!d->timing) return d->create_swapchain(device, ci, alloc, out);
    VkSwapchainCreateInfoKHR c = *ci;
    VkPresentStageFlagsEXT stages = 0;
    bool added = false;
    {
        VkPresentTimingSurfaceCapabilitiesEXT pt{VK_STRUCTURE_TYPE_PRESENT_TIMING_SURFACE_CAPABILITIES_EXT};
        VkSurfaceCapabilities2KHR caps{VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_2_KHR};
        caps.pNext = &pt;
        VkPhysicalDeviceSurfaceInfo2KHR si{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SURFACE_INFO_2_KHR};
        si.surface = ci->surface;
        const VkResult cr = d->inst->surface_caps(d->physical, &si, &caps);
        if (cr == VK_SUCCESS && pt.presentTimingSupported && pt.presentStageQueries) {
            stages = pt.presentStageQueries;
            if (!(c.flags & VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT)) c.flags |= VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT, added = true;
        } else {
            line("e,%lld,surface_no_timing,%d", (long long)qpc(), (int)cr);
        }
    }
    VkResult r = d->create_swapchain(device, &c, alloc, out);
    if (r != VK_SUCCESS && added) {   // (the flag refused: made as asked, without asks)
        line("e,%lld,flag_refused,%d", (long long)qpc(), (int)r);
        logf("vk: Longshot's layer under Streamline: a swapchain refused present timing's flag (VkResult %d): made without it", (int)r);
        added = false, stages = 0;
        r = d->create_swapchain(device, ci, alloc, out);
    }
    line("s,%lld,%p,%#x,%u,%d,%#x", (long long)qpc(), r == VK_SUCCESS ? (void*)*out : nullptr, (unsigned)ci->flags, added ? 1u : 0u,
         (int)r, (unsigned)stages);
    if (r != VK_SUCCESS || !stages) return r;
    std::lock_guard lk(d->mu);
    Swapchain& s = d->swapchains[*out];
    s = Swapchain{};
    s.stages = stages;
    const VkResult qr = d->queue_size(device, *out, kQueueSize);
    line("q,%lld,%p,%u,%d", (long long)qpc(), (void*)*out, kQueueSize, (int)qr);
    if (qr != VK_SUCCESS) s.cut = true;
    read_domains(*d, *out, s);
    law_swapchain(*out);   // (the reader on the last swapchain made with asks)
    return r;
}

void VKAPI_CALL layer_DestroySwapchainKHR(VkDevice device, VkSwapchainKHR swapchain, const VkAllocationCallbacks* alloc) {
    Dev* d = dev_of(device);
    if (!d) return;
    {
        std::lock_guard lk(d->mu);
        auto it = d->swapchains.find(swapchain);
        if (it != d->swapchains.end()) {
            line("x,%lld,%p,%zu", (long long)qpc(), (void*)swapchain, it->second.waiting.size());
            d->swapchains.erase(it);
            bool readers = false;
            {
                std::lock_guard lr(law().mu);
                readers = law().sc == swapchain;
            }
            if (readers) law_swapchain(VK_NULL_HANDLE);
        }
    }
    d->destroy_swapchain(device, swapchain, alloc);
}

VkResult VKAPI_CALL layer_QueuePresentKHR(VkQueue queue, const VkPresentInfoKHR* pi) {
    Dev* d = dev_of(queue);
    if (!d) return VK_ERROR_DEVICE_LOST;
    if (!d->timing) return d->present(queue, pi);
    const uint64_t n = ++g_presents;
    // (the plugin's metering structure, read, never changed: the driver writes its feedback into it)
    const VkSetPresentConfigNV* cfg = nullptr;
    bool has_timing = false;
    for (auto* b = (const VkBaseInStructure*)pi->pNext; b; b = b->pNext) {
        if (b->sType == VK_STRUCTURE_TYPE_SET_PRESENT_CONFIG_NV) cfg = (const VkSetPresentConfigNV*)b;
        if (b->sType == VK_STRUCTURE_TYPE_PRESENT_TIMINGS_INFO_EXT) has_timing = true;
    }
    const uint32_t fb_before = cfg ? cfg->presentConfigFeedback : 0;
    const VkSwapchainKHR sc = pi->swapchainCount ? pi->pSwapchains[0] : VK_NULL_HANDLE;
    const uint32_t image = pi->swapchainCount && pi->pImageIndices ? pi->pImageIndices[0] : ~0u;
    VkPresentInfoKHR copy;
    VkPresentTimingsInfoEXT all{VK_STRUCTURE_TYPE_PRESENT_TIMINGS_INFO_EXT};
    VkPresentTimingInfoEXT one{VK_STRUCTURE_TYPE_PRESENT_TIMING_INFO_EXT};
    bool asked = false;
    if (pi->swapchainCount == 1 && !has_timing) {
        std::lock_guard lk(d->mu);
        auto it = d->swapchains.find(sc);
        if (it != d->swapchains.end() && !it->second.cut && it->second.have_domain) {
            Swapchain& s = it->second;
            if (s.waiting.size() >= kMaxWaiting) {
                if (!s.said_full)
                    logf("vk: Longshot's layer under Streamline: %zu presents wait for their times: none asked until some come back (said once)", s.waiting.size());
                s.said_full = true;
                line("e,%lld,ask_dropped_waiting,%zu", (long long)qpc(), s.waiting.size());
            } else {
                one.timeDomainId = s.domain_id;
                one.presentStageQueries = s.stages;
                all.pNext = pi->pNext;   // (the plugin's chain behind ours, untouched)
                all.swapchainCount = 1;
                all.pTimingInfos = &one;
                copy = *pi;
                copy.pNext = &all;
                asked = true;
            }
        }
    }
    const int64_t call = qpc();
    const VkResult r = d->present(queue, asked ? &copy : pi);
    const int64_t ret = qpc();
    const uint32_t fb_after = cfg ? cfg->presentConfigFeedback : 0;
    {
        std::lock_guard lk(d->mu);
        auto it = d->swapchains.find(sc);
        if (it != d->swapchains.end()) {
            Swapchain& s = it->second;
            if (asked) {
                if (r == VK_SUCCESS || r == VK_SUBOPTIMAL_KHR) {
                    s.waiting.push_back(n);
                } else {
                    line("e,%lld,present_result,%d", (long long)ret, (int)r);
                    if (r == VK_ERROR_PRESENT_TIMING_QUEUE_FULL_EXT) {
                        logf("vk: Longshot's layer under Streamline: the timing queue is full: no more asked on this swapchain");
                        s.cut = true;
                    }
                }
            }
            law_present(sc, n, call, cfg);
            // (read back between batches, not between a batch's two presents)
            if (!cfg && !s.cut) {
                read_back(*d, sc, s);
                calibrate(*d, sc, s, ret);
            }
        }
    }
    line("p,%llu,%lld,%lld,%lld,%p,%p,%u,%u,%u,%u,%u,%u,%d", (unsigned long long)n, (long long)call, (long long)ret, (long long)qpc(),
         (void*)queue, (void*)sc, image, cfg ? 1u : 0u, cfg ? cfg->numFramesPerBatch : 0u, fb_before, fb_after, asked ? 1u : 0u,
         (int)r);
    return r;
}

PFN_vkVoidFunction own_device_function(const char* name) {
    if (!strcmp(name, "vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)layer_GetDeviceProcAddr;
    if (!strcmp(name, "vkDestroyDevice")) return (PFN_vkVoidFunction)layer_DestroyDevice;
    if (!strcmp(name, "vkQueuePresentKHR")) return (PFN_vkVoidFunction)layer_QueuePresentKHR;
    if (!strcmp(name, "vkCreateSwapchainKHR")) return (PFN_vkVoidFunction)layer_CreateSwapchainKHR;
    if (!strcmp(name, "vkDestroySwapchainKHR")) return (PFN_vkVoidFunction)layer_DestroySwapchainKHR;
    return nullptr;
}

PFN_vkVoidFunction VKAPI_CALL layer_GetDeviceProcAddr(VkDevice device, const char* name) {
    if (PFN_vkVoidFunction f = own_device_function(name)) return f;
    Dev* d = device ? dev_of(device) : nullptr;
    return d ? d->gdpa(device, name) : nullptr;
}

PFN_vkVoidFunction VKAPI_CALL layer_GetInstanceProcAddr(VkInstance instance, const char* name) {
    if (!strcmp(name, "vkGetInstanceProcAddr")) return (PFN_vkVoidFunction)layer_GetInstanceProcAddr;
    if (!strcmp(name, "vkCreateInstance")) return (PFN_vkVoidFunction)layer_CreateInstance;
    if (!strcmp(name, "vkDestroyInstance")) return (PFN_vkVoidFunction)layer_DestroyInstance;
    if (!strcmp(name, "vkCreateDevice")) return (PFN_vkVoidFunction)layer_CreateDevice;
    if (PFN_vkVoidFunction f = own_device_function(name)) return f;
    Inst* in = instance ? inst_of(instance) : nullptr;
    return in ? in->gipa(instance, name) : nullptr;
}

std::string utf8(const std::wstring& w) {
    const int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s((size_t)(n > 0 ? n : 0), '\0');
    if (n > 0) WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}

bool listed(const std::vector<VkExtensionProperties>& all, const char* name) {
    for (const auto& e : all)
        if (!strcmp(e.extensionName, name)) return true;
    return false;
}
void add(std::vector<const char*>& want, const char* name) {
    for (const char* w : want)
        if (!strcmp(w, name)) return;
    want.push_back(name);
}

}  // namespace

const char* dlssg_layer_prepare() {
    // (this DLL's own path: the manifest names it, and its folder holds the manifest's)
    HMODULE self = nullptr;
    std::wstring dll(32768, L'\0');
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            (LPCWSTR)(void*)&dlssg_layer_prepare, &self))
        return nullptr;
    const DWORD len = GetModuleFileNameW(self, dll.data(), (DWORD)dll.size());
    if (!len || len >= dll.size()) return nullptr;
    dll.resize(len);
    const size_t slash = dll.find_last_of(L"\\/");
    if (slash == std::wstring::npos) return nullptr;
    const std::wstring dir = dll.substr(0, slash) + L"\\longshot_vk_layer";
    CreateDirectoryW(dir.c_str(), nullptr);
    const std::wstring manifest = dir + L"\\VkLayer_longshot_streamline.json";
    std::string path;   // (JSON: the backslashes doubled)
    for (char ch : utf8(dll)) {
        if (ch == '\\' || ch == '"') path.push_back('\\');
        path.push_back(ch);
    }
    FILE* f = _wfopen(manifest.c_str(), L"wb");
    if (!f) {
        logf("vk: Longshot's layer under Streamline: its manifest was not written in %s: the game starts without it", utf8(dir).c_str());
        return nullptr;
    }
    std::fprintf(f,
                 "{\n  \"file_format_version\": \"1.2.0\",\n  \"layer\": {\n    \"name\": \"%s\",\n    \"type\": \"GLOBAL\",\n"
                 "    \"library_path\": \"%s\",\n    \"api_version\": \"1.4.%u\",\n    \"implementation_version\": \"1\",\n"
                 "    \"description\": \"Longshot: reads when NVIDIA DLSS Frame Generation's presents reach the screen\"\n  }\n}\n",
                 kLayerName, path.c_str(), (unsigned)VK_HEADER_VERSION);
    std::fclose(f);
    // (the loader reads VK_ADD_LAYER_PATH, from 1.3.234, each time it looks for layers: before the instance is made is
    // enough. A restarted game inherits the variable: the folder is not added twice)
    std::wstring paths = dir;
    if (const DWORD had = GetEnvironmentVariableW(L"VK_ADD_LAYER_PATH", nullptr, 0)) {
        std::wstring old(had, L'\0');
        old.resize(GetEnvironmentVariableW(L"VK_ADD_LAYER_PATH", old.data(), had));
        if (old.find(dir) != std::wstring::npos) paths = old;
        else if (!old.empty()) paths += L";" + old;
    }
    SetEnvironmentVariableW(L"VK_ADD_LAYER_PATH", paths.c_str());
    // (listed? naming a layer the loader does not list fails the instance)
    const PFN_vkGetInstanceProcAddr gipa = instance_proc();
    const auto enumerate = gipa ? (PFN_vkEnumerateInstanceLayerProperties)gipa(nullptr, "vkEnumerateInstanceLayerProperties") : nullptr;
    uint32_t n = 0;
    if (enumerate) enumerate(&n, nullptr);
    std::vector<VkLayerProperties> all(n);
    if (n) enumerate(&n, all.data());
    for (uint32_t i = 0; i < n && i < all.size(); i++)
        if (!strcmp(all[i].layerName, kLayerName)) return kLayerName;
    logf("vk: Longshot's layer under Streamline: the Vulkan loader does not list it (%s): the game starts without it", utf8(manifest).c_str());
    return nullptr;
}

void dlssg_layer_instance_extensions(std::vector<const char*>& want, const std::vector<VkExtensionProperties>& all) {
    if (listed(all, VK_KHR_GET_SURFACE_CAPABILITIES_2_EXTENSION_NAME)) add(want, VK_KHR_GET_SURFACE_CAPABILITIES_2_EXTENSION_NAME);
}

void dlssg_layer_device_extensions(std::vector<const char*>& want, const std::vector<VkExtensionProperties>& all) {
    g_device_wanted.store(false);
    if (!g_in_instance.load() || !dlssg_active()) return;
    const char* stamps = listed(all, VK_KHR_CALIBRATED_TIMESTAMPS_EXTENSION_NAME)   ? VK_KHR_CALIBRATED_TIMESTAMPS_EXTENSION_NAME
                         : listed(all, VK_EXT_CALIBRATED_TIMESTAMPS_EXTENSION_NAME) ? VK_EXT_CALIBRATED_TIMESTAMPS_EXTENSION_NAME
                                                                                    : nullptr;
    if (!stamps || !listed(all, VK_EXT_PRESENT_TIMING_EXTENSION_NAME) || !listed(all, VK_KHR_PRESENT_ID_2_EXTENSION_NAME)) return;
    add(want, VK_EXT_PRESENT_TIMING_EXTENSION_NAME), add(want, VK_KHR_PRESENT_ID_2_EXTENSION_NAME), add(want, stamps);
    g_device_wanted.store(true);
}

void dlssg_layer_device_features(void**& tail) {
    if (!g_device_wanted.load()) return;
    static VkPhysicalDevicePresentTimingFeaturesEXT timing;
    timing = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT};
    *tail = &timing, tail = &timing.pNext;
}

bool dlssg_layer_toggle_wanted() {
    const int64_t at = g_want.exchange(0);
    if (!at) return false;
    if (qpc() - at < qpc_hz() / 10) return true;
    Law& l = law();   // (too old to follow: as an ask let go)
    std::lock_guard lk(l.mu);
    l.reader.ask_dropped();
    return false;
}

void dlssg_layer_held(int64_t call, int64_t sent, bool held) {
    Law& l = law();
    std::lock_guard lk(l.mu);
    if (held) l.reader.held(call);
    else l.reader.ask_dropped();
    line("k,%lld,%lld,%d", (long long)call, (long long)sent, held ? 1 : 0);
    // (said where a present is held, not where the reader asks: an ask the game lets go, at a frame rate the toggle does not
    // fit, holds nothing)
    if (held && l.holds_told < 32) {
        l.holds_told++;
        logf("vk: Longshot's layer under Streamline: the driver showed DLSS FG's frames unevenly: one present held %.1f ms%s",
             (sent - call) * 1000.0 / qpc_hz(), l.holds_told == 32 ? " (the next ones are not said)" : "");
    }
}

}  // namespace mkwvk

/// The loader's entry into the layer (interface version 2): the layer's two lookups
extern "C" __declspec(dllexport) VkResult VKAPI_CALL vkNegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface* p) {
    if (!p || p->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT || p->loaderLayerInterfaceVersion < 2)
        return VK_ERROR_INITIALIZATION_FAILED;
    p->loaderLayerInterfaceVersion = 2;
    p->pfnGetInstanceProcAddr = mkwvk::layer_GetInstanceProcAddr;
    p->pfnGetDeviceProcAddr = mkwvk::layer_GetDeviceProcAddr;
    p->pfnGetPhysicalDeviceProcAddr = nullptr;
    return VK_SUCCESS;
}

#else  // (no Streamline in this build, or Vulkan headers without present timing or metering: no layer)

namespace mkwvk {
const char* dlssg_layer_prepare() { return nullptr; }
void dlssg_layer_instance_extensions(std::vector<const char*>&, const std::vector<VkExtensionProperties>&) {}
void dlssg_layer_device_extensions(std::vector<const char*>&, const std::vector<VkExtensionProperties>&) {}
void dlssg_layer_device_features(void**&) {}
bool dlssg_layer_toggle_wanted() { return false; }
void dlssg_layer_held(int64_t, int64_t, bool) {}
}  // namespace mkwvk

#endif
