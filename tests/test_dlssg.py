"""NVIDIA DLSS Frame Generation through NVIDIA Streamline (gpu/vk/vk_dlssg.cpp, vk_dlssg_layer.cpp) without a GPU, the game
or NVIDIA's SDK: Reflex's frame limit for the frames shown; the stubs of a build without Streamline; what is checked before
sl.interposer.dll is loaded; the places the renderer hands over to Streamline (its loader, Longshot's layer under it, its
Reflex, the frame chain's inputs, the present and its hold) and stands its own frame generation, NGX's and its Reflex aside;
the Frame generation by row's restart; the folders NGX is told of through Streamline; what the log says of the NVIDIA app's
DLSS override; and, run with a stand-in SDK whose Reflex sleep blocks, the exit (Streamline shut down when the window
closes) and Streamline giving up when the Upscaler's engine does not run. When it starts, the pacing and the law reader:
tests/test_dlssg_pace.py.

python -I -B tests/test_dlssg.py [--cxx g++]
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import between  # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"


def block(text, marker):
    start = text.index(marker)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def read(name):
    return (VK / name).read_text(encoding="utf-8")


def ordered(text, *markers, what):
    at = [text.find(m) for m in markers]
    if -1 in at or at != sorted(at):
        raise SystemExit("%s (%s)" % (what, " before ".join(markers)))


# sl_log, Streamline's log callback (vk_dlssg.cpp), with a stand-in sl::LogType and logf, handed Streamline's lines of a
# run with the NVIDIA app's DLSS override on (shortened):
#   kept:      Streamline looks for NVIDIA's version of its plugins, does not find one, and keeps its own;
#   searching: only the search, its outcome not known yet (or a run whose plugins all came from NVIDIA).
# Each line the game logs is printed
SL_LOG_FIXTURE = r"""
#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <string>
namespace sl { enum class LogType : unsigned { eInfo, eWarn, eError }; }
void logf(const char* fmt, ...) {
    char b[4096];
    va_list a;
    va_start(a, fmt);
    std::vsnprintf(b, sizeof b, fmt, a);
    va_end(a);
    std::printf("%s\n", b);
}
/* SL_LOG */
int main(int argc, char** argv) {
    const std::string mode = argc > 1 ? argv[1] : "";
    const char* search = "[info] OTA override DRS key is set, searching for override OTA'd plugins...\n";
    const char* none = "[info] Not all override plugins available, OTA'd plugins will not be loaded\n";
    sl_log(sl::LogType::eInfo, search);
    if (mode == "kept") {
        sl_log(sl::LogType::eWarn, "[warn] Could not find version matching for plugin: dlss_g\n");
        sl_log(sl::LogType::eInfo, none);
    }
    sl_log(sl::LogType::eInfo, search);   /* (Streamline searches again when it loads again) */
    if (mode == "kept") sl_log(sl::LogType::eInfo, none);
    return 0;
}
"""

FIXTURE = r"""
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); std::exit(1); } } while (0)
int mhz = 0;
int display_refresh_mhz() { return mhz; }
int vsync = 0;   /* the menu's V-Sync: 0 Auto, 1 On, 2 Off */
int menu_vsync() { return vsync; }
/* FRAME_LIMIT */
int main() {
    /* the frames shown held below the refresh as NVIDIA's driver holds them with G-SYNC: refresh - refresh^2 / 3600 */
    vsync = 0;
    mhz = 120000;
    CHECK(frame_limit_us(true) == 8621);   /* 116 fps */
    mhz = 144000;
    CHECK(frame_limit_us(true) == 7234);   /* 138.24 */
    mhz = 240000;
    CHECK(frame_limit_us(true) == 4464);   /* 224 */
    mhz = 60000;
    CHECK(frame_limit_us(true) == 16949);  /* 59 */
    CHECK(frame_limit_us(false) == 0);     /* DLSS-G off: no limit */
    vsync = 1;
    CHECK(frame_limit_us(true) == 16949);  /* V-Sync On: held as with Auto */
    /* V-Sync Off: no limit, as NVIDIA's driver sets none without V-Sync (at 60 Hz and 2x the game rendered 29.5 fps
       where main renders 60) */
    vsync = 2;
    CHECK(frame_limit_us(true) == 0);
    mhz = 240000;
    CHECK(frame_limit_us(true) == 0);
    vsync = 0;
    mhz = 0;
    CHECK(frame_limit_us(true) == 0);      /* the refresh not known */
    std::printf("PASS: Reflex holds DLSS Frame Generation's frames below the display's refresh, but for V-Sync Off\n");
}
"""

# The exit (dlssg_shutdown) with vk_dlssg.cpp's own State, runs(), token(), set_dlssg, dlssg_marker, dlssg_sleep and
# dlssg_shutdown, and a stand-in for Streamline whose Reflex sleep blocks until it is let go. Each run is one process: the
# exit happens once.
#   order:         a sleep under way when the window closes: the exit waits for it with the present lock free, lets nothing
#                  in meanwhile, then the recorder's parts, idle, DLSS-G off, slShutdown, in that order; the locks stay
#                  held; a second close does nothing
#   stuck_present: a present that does not end: the exit gives up after its wait, Streamline left as it is, nothing held
#   stuck_sleep:   a sleep that does not end: likewise, and the present lock never taken
#   gave_up:       Streamline loaded and given up on this GPU: no call goes to its features, and the exit still shuts it down
#   not_loaded:    without Streamline's loader the exit does nothing, takes no lock and leaves the close to vk_main.cpp
SHUTDOWN_FIXTURE = r"""
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include "dlssg_rules.h"
#include "upscaler_rows.h"
#include "xess_rules.h"
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); std::exit(1); } } while (0)
using HMODULE = void*;
namespace fx {
void note(const std::string& e);
}
namespace mkwvk::rec {   // (vk_recorder.h's: the parts handed to the recorder, submitted before the queue's lock is tried)
enum Bound { B_QUEUE_LOCK };
inline bool settle_parts(Bound, uint64_t) {
    fx::note("settle");
    return true;
}
}  // namespace mkwvk::rec
namespace sl {
enum class Result { eOk, eErrorNotInitialized };
struct FrameToken { uint32_t index = 0; operator uint32_t() const { return index; } };
enum class PCLMarker { eSimulationStart, eSimulationEnd, eRenderSubmitStart, eRenderSubmitEnd, ePresentStart, ePresentEnd };
enum class DLSSGMode { eOff, eOn };
enum class DLSSGFlags : uint32_t { eRetainResourcesWhenOff = 1 };
struct DLSSGOptions { DLSSGMode mode = DLSSGMode::eOff; uint32_t numFramesToGenerate = 1; DLSSGFlags flags{}; };
struct ViewportHandle { ViewportHandle(int) {} };
struct DLSSGState { uint64_t numFramesActuallyPresented = 0; uint32_t status = 0; };
}  // namespace sl
#define SL_FAILED(r, f) sl::Result r = f; r != sl::Result::eOk
#define SL_SUCCEEDED(r, f) sl::Result r = f; r == sl::Result::eOk
using PFun_slInit = sl::Result();
using PFun_slIsFeatureSupported = sl::Result();
using PFun_slSetTagForFrame = sl::Result();
using PFun_slSetConstants = sl::Result();
using PFun_slGetFeatureFunction = sl::Result();
using PFun_slGetNewFrameToken = sl::Result(sl::FrameToken*& token, const uint32_t* frameIndex);
using PFun_slDLSSGSetOptions = sl::Result(const sl::ViewportHandle& viewport, const sl::DLSSGOptions& options);
using PFun_slDLSSGGetState = sl::Result(const sl::ViewportHandle& viewport, sl::DLSSGState& state, const void* options);
using PFun_slReflexSetOptions = sl::Result();
using PFun_slReflexSleep = sl::Result(const sl::FrameToken& frame);
using PFun_slPCLSetMarker = sl::Result(sl::PCLMarker marker, const sl::FrameToken& frame);
using PFun_slShutdown = sl::Result();
namespace fx {
namespace dlssg_rules = ::mkwvk::dlssg_rules;
/* HEADER_ENUMS */
std::chrono::milliseconds kExitWait{5000};
// (what happened, in order: Streamline's calls, the device's idle, a present that does not end)
std::mutex ev_mu;
std::condition_variable ev_cv;
std::vector<std::string> events, logs;
bool sleep_go = false, let_go = false;
void note(const std::string& e) {
    {
        std::lock_guard l(ev_mu);
        events.push_back(e);
    }
    ev_cv.notify_all();
}
int at(const std::string& e) {   // (its first place, -1 when it did not happen)
    std::lock_guard l(ev_mu);
    for (size_t i = 0; i < events.size(); i++)
        if (events[i] == e) return (int)i;
    return -1;
}
int count(const std::string& e) {
    std::lock_guard l(ev_mu);
    int n = 0;
    for (const auto& x : events) n += x == e;
    return n;
}
size_t size() {
    std::lock_guard l(ev_mu);
    return events.size();
}
std::string event(size_t i) {
    std::lock_guard l(ev_mu);
    return events.at(i);
}
bool seen(const std::string& e) {   // (waited for, 5 s at most)
    std::unique_lock l(ev_mu);
    return ev_cv.wait_for(l, std::chrono::seconds(5), [&] {
        for (const auto& x : events)
            if (x == e) return true;
        return false;
    });
}
void release(bool& flag) {
    {
        std::lock_guard l(ev_mu);
        flag = true;
    }
    ev_cv.notify_all();
}
void logf(const char* f, ...) {
    char b[512];
    va_list a;
    va_start(a, f);
    std::vsnprintf(b, sizeof b, f, a);
    va_end(a);
    std::lock_guard l(ev_mu);
    logs.push_back(b);
}
bool logged(const char* part) {
    std::lock_guard l(ev_mu);
    for (const auto& x : logs)
        if (x.find(part) != std::string::npos) return true;
    return false;
}
std::mutex present_image_mu;
struct {
    void* device = &present_image_mu;
    std::mutex present_mu;
    std::atomic<bool> closing{false};   // (vk_internal.h: the window is closing, no swapchain is made again)
} dev;
void device_wait_idle_locked() { note("idle"); }
// (the rows, the Insert menu, the FPS counter and the temporal chain's engine: vk_menu.h, vk_temporal.h)
struct { int framegen = 0, framegen_by = 0, dlss = 0, reflex = 0; } opts;
bool menu_open = false, engine_runs = true;
bool menu_is_open() { return menu_open; }
void menu_note_frame(bool, int) {}
// (the temporal chain's engines on an NVIDIA RTX GPU: DLSS and DLAA while engine_runs, AMD FSR 3.1 and Intel XeSS, not FSR 4.
// vk_temporal.cpp's temporal_mode and vk_fsr3.cpp's choice of FSR 3.1 are their own, cut below)
namespace upscaler_rows = ::mkwvk::upscaler_rows;
namespace ur = upscaler_rows;
namespace xess_rules = ::mkwvk::xess_rules;
enum DlssWhy { DLSS_OK, DLSS_NO_RUNTIME };
enum FsrWhy { FSR_OK, FSR_NO_MODEL };
enum XessWhy { XESS_OK };
DlssWhy dlss_why() { return engine_runs ? DLSS_OK : DLSS_NO_RUNTIME; }
FsrWhy fsr4_why() { return FSR_NO_MODEL; }
XessWhy xess_why() { return XESS_OK; }
namespace fsr3 {
bool supported() { return true; }
}  // namespace fsr3
int env_switch(const char* name) {
    const char* e = getenv(name);
    return e && *e ? atoi(e) : -1;
}
int dlssg_upscaler(int row);
bool fsr3_chosen();
bool fsr3_on_chain();
bool fsr3_wanted();
/* TEMPORAL */
/* FSR3 */
/* STATE */
/* LOADED */
int limits_off = 0;   // (Reflex's frame limit taken off)
void apply_reflex(bool on, uint32_t) { limits_off += !on; }
/* CUT */
// (the stand-in SDK)
sl::FrameToken tokens[64];
uint32_t next_token = 0;
sl::Result fake_token(sl::FrameToken*& t, const uint32_t*) {
    tokens[next_token % 64].index = next_token;
    t = &tokens[next_token++ % 64];
    return sl::Result::eOk;
}
sl::Result fake_sleep(const sl::FrameToken&) {
    note("sleep in");
    {
        std::unique_lock l(ev_mu);
        ev_cv.wait(l, [] { return sleep_go; });
    }
    note("sleep out");
    return sl::Result::eOk;
}
sl::Result fake_marker(sl::PCLMarker, const sl::FrameToken&) {
    note("marker");
    return sl::Result::eOk;
}
sl::Result fake_options(const sl::ViewportHandle&, const sl::DLSSGOptions& o) {
    note(o.mode == sl::DLSSGMode::eOff ? "off" : "on");
    return sl::Result::eOk;
}
sl::Result fake_state(const sl::ViewportHandle&, sl::DLSSGState&, const void*) { return sl::Result::eOk; }
sl::Result fake_shutdown() {
    note("slShutdown");
    return sl::Result::eOk;
}
bool closing() {
    std::lock_guard l(st().mu);
    return st().closing;
}
bool its_close = false;     // (the last dlssg_shutdown's answer: the close is Streamline's, not vk_main.cpp's)
double timed_shutdown() {   // (seconds)
    const auto t0 = std::chrono::steady_clock::now();
    its_close = dlssg_shutdown();
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
}
}  // namespace fx
using namespace fx;
int main(int argc, char** argv) {
    const std::string mode = argc > 1 ? argv[1] : "order";
    State& s = st();
    s.loaded = mode != "not_loaded", s.active = s.loaded && mode != "gave_up", s.ready = true;
    s.new_token = fake_token, s.reflex_sleep = fake_sleep, s.pcl_marker = fake_marker;
    s.dlssg_set_options = fake_options, s.dlssg_get_state = fake_state, s.shutdown = fake_shutdown;
    s.mode = s.active ? sl::DLSSGMode::eOn : sl::DLSSGMode::eOff, s.frames = 1;
    dlssg_marker(DLSSG_SIM_END);   /* while Streamline makes the frames, a marker goes to it; never otherwise */
    CHECK(count("marker") == (s.active ? 1 : 0));
    if (mode == "gave_up" || mode == "not_loaded") {
        release(sleep_go);
        dlssg_sleep();
        CHECK(size() == 0);   /* no token, no sleep, no marker */
        CHECK(timed_shutdown() < 1.0);
        if (mode == "gave_up") {   /* Streamline's loader is still the renderer's: shut down all the same, DLSS-G off already */
            CHECK(its_close && dev.closing.load());
            CHECK(at("settle") == 0 && at("idle") == 1 && at("slShutdown") == 2 && size() == 3);
            bool held = false;   /* asked from another thread: this one holds them */
            std::thread([&] { held = !present_image_mu.try_lock() && !dev.present_mu.try_lock(); }).join();
            CHECK(held);
        } else {
            CHECK(size() == 0 && !closing() && !its_close && !dev.closing.load());   /* the close is vk_main.cpp's own */
            CHECK(present_image_mu.try_lock() && dev.present_mu.try_lock());
            present_image_mu.unlock(), dev.present_mu.unlock();
        }
        std::printf("PASS: %s\n", mode == "gave_up" ? "given up on this GPU, nothing goes to Streamline's features and the exit shuts it down"
                                                    : "without Streamline's loader the exit does nothing");
    } else if (mode == "order") {
        std::thread sleeper([] { dlssg_sleep(); });   /* the presentation thread, in Reflex's sleep */
        CHECK(seen("sleep in"));
        std::thread closer([] { (void)dlssg_shutdown(); });   /* the window's thread: the window closed */
        while (!closing()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        CHECK(dev.closing.load());   /* no swapchain is made from the close on */
        const size_t began = size();
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        /* it waits for the sleep, with the present lock and the queue's free: the sleeping thread presents next */
        CHECK(at("idle") < 0 && at("off") < 0 && at("slShutdown") < 0);
        CHECK(present_image_mu.try_lock());
        present_image_mu.unlock();
        CHECK(dev.present_mu.try_lock());
        dev.present_mu.unlock();
        /* nothing is let in meanwhile: no marker, no other sleep */
        dlssg_marker(DLSSG_PRESENT_START);
        dlssg_sleep();
        CHECK(size() == began);
        release(sleep_go);
        sleeper.join();
        closer.join();
        /* the sleep ended without its simulation-start marker; then idle, DLSS-G off, slShutdown */
        for (size_t i = began; i < size(); i++) CHECK(event(i) != "marker");
        CHECK(at("sleep out") >= 0 && at("sleep out") < at("settle") && at("settle") < at("idle") && at("idle") < at("off") &&
              at("off") < at("slShutdown"));
        CHECK(count("slShutdown") == 1 && count("off") == 1 && logged("Streamline shut down for the exit"));
        /* the presents and the queue stay held until the process ends: nothing reaches Streamline after it */
        CHECK(!present_image_mu.try_lock());
        CHECK(!dev.present_mu.try_lock());
        /* a second close, and any call after it, does nothing, at once */
        const size_t after = size();
        CHECK(timed_shutdown() < 1.0 && its_close);
        dlssg_marker(DLSSG_PRESENT_END);
        dlssg_sleep();
        CHECK(size() == after && count("slShutdown") == 1);
        std::printf("PASS: the exit waits for Reflex's sleep with the presents free, lets nothing in, then idle, DLSS-G off, "
                    "slShutdown, once\n");
    } else if (mode == "stuck_present") {
        kExitWait = std::chrono::milliseconds(300);
        std::thread presenter([] {   /* a present that does not end */
            std::lock_guard l(present_image_mu);
            note("presenting");
            std::unique_lock w(ev_mu);
            ev_cv.wait(w, [] { return let_go; });
        });
        CHECK(seen("presenting"));
        const double took = timed_shutdown();
        CHECK(took >= 0.25 && took < 3.0 && its_close);   /* still Streamline's close: no retire through its hooks */
        CHECK(at("idle") < 0 && at("off") < 0 && at("slShutdown") < 0 && logged("a present did not end within"));
        CHECK(dev.present_mu.try_lock());   /* nothing left held */
        dev.present_mu.unlock();
        release(let_go);
        presenter.join();
        CHECK(present_image_mu.try_lock());
        present_image_mu.unlock();
        const size_t after = size();
        dlssg_marker(DLSSG_SIM_START);   /* the exit began all the same: nothing goes to Streamline */
        CHECK(size() == after);
        std::printf("PASS: a present stuck at the exit leaves Streamline as it is after the wait, nothing held\n");
    } else if (mode == "stuck_sleep") {
        kExitWait = std::chrono::milliseconds(300);
        std::thread sleeper([] { dlssg_sleep(); });
        CHECK(seen("sleep in"));
        const double took = timed_shutdown();
        CHECK(took >= 0.25 && took < 3.0 && its_close);
        CHECK(at("idle") < 0 && at("slShutdown") < 0 && logged("Reflex sleep did not end within"));
        CHECK(present_image_mu.try_lock());   /* never taken */
        present_image_mu.unlock();
        const size_t after = size();
        release(sleep_go);
        sleeper.join();
        for (size_t i = after; i < size(); i++) CHECK(event(i) != "marker");
        std::printf("PASS: a Reflex sleep stuck at the exit leaves Streamline as it is after the wait, the presents free\n");
    } else if (mode == "no_engine") {
        /* DLSS FG asked (Frame generation 2x by DLSS FG, a temporal Upscaler) and making frames; then the Upscaler's engine
           does not run here (its runtime missing, NGX's answer, XeSS's DLL): DLSS-G has nothing to make frames from */
        opts.framegen = 2, opts.framegen_by = 0, opts.dlss = 1;
        s.frames_max = 1, s.inputs_frame = 0;   /* (the stand-in's first token: its inputs given) */
        for (int i = 0; i < 3; i++) dlssg_before_present(true);
        CHECK(s.active && s.mode == sl::DLSSGMode::eOn);   /* the engine runs: on */
        engine_runs = false;
        dlssg_before_present(false);   /* the window's own present: not a game frame, nothing decided */
        CHECK(s.active);
        menu_open = true;   /* the Insert menu open: the row may be changing, nothing decided */
        dlssg_before_present(true);
        CHECK(s.active);
        menu_open = false, engine_runs = true;
        dlssg_before_present(true);
        CHECK(s.active && s.mode == sl::DLSSGMode::eOn);
        const int limits_before = limits_off;
        engine_runs = false;
        dlssg_before_present(true);   /* a game frame, the menu closed, no engine: Streamline gives up */
        CHECK(!s.active && s.making == 0 && logged("DLSS Frame Generation does not go through it in this run"));
        CHECK(s.mode == sl::DLSSGMode::eOff && event(size() - 1) == "off" && limits_off == limits_before + 1);
        const size_t after = size();
        engine_runs = true;   /* once given up, for the run */
        dlssg_before_present(true);
        CHECK(!s.active && size() == after);
        std::printf("PASS: with the Upscaler's engine not running, Streamline gives up on a game frame (DLSS-G off, Reflex's "
                    "limit off), not under the menu\n");
    } else if (mode == "upscaler_change") {
        /* Streamline started with DLAA (the rows as saved: 2x by DLSS FG) and making DLSS FG's frames; then the Upscaler row
           changed in the game. Streamline starts with DLSS or DLAA alone (dlssg_rules.h): another Upscaler waits for the
           restart, and DLSS goes on until then. With FSR 4 it ran at once: no engine on this GPU, Streamline gave up and its
           DLSS-G plugin still presented every frame */
        s.start.framegen = 2, s.start.framegen_by = 0, s.start.upscaler = ur::DLAA;
        opts.framegen = 2, opts.framegen_by = 0, opts.dlss = ur::DLAA;
        s.frames_max = 1, s.inputs_frame = 0;
        for (int i = 0; i < 3; i++) dlssg_before_present(true);
        CHECK(s.active && s.mode == sl::DLSSGMode::eOn && temporal_mode() == ur::DLAA);
        opts.dlss = ur::DLSS;   /* DLSS: Streamline starts with it too, at once */
        dlssg_before_present(true);
        CHECK(temporal_mode() == ur::DLSS && s.active && s.mode == sl::DLSSGMode::eOn);
        for (int to : {ur::FSR4, ur::FSR3, ur::FSR4_AA, ur::XESS, ur::XESS_AA, ur::GAME, ur::OFF}) {
            opts.dlss = to;
            dlssg_before_present(true);   /* a game frame, the menu closed */
            CHECK(temporal_mode() == ur::DLSS && temporal_wanted() && !temporal_off() && !fsr3_chosen() && !fsr3_wanted());
            CHECK(s.active && s.mode == sl::DLSSGMode::eOn && s.frames == 1);   /* DLSS-G on, at 2x */
        }
        opts.dlss = ur::DLAA;   /* back to DLAA: at once */
        CHECK(temporal_mode() == ur::DLAA);
        /* without Streamline's loader every change runs at once, as before */
        s.loaded = false, s.active = false;
        opts.dlss = ur::XESS;
        CHECK(temporal_mode() == ur::XESS);
        opts.dlss = ur::FSR3;
        CHECK(fsr3_chosen() && temporal_mode() == ur::FSR3);
        opts.dlss = ur::OFF;
        CHECK(temporal_off());
        std::printf("PASS: under Streamline an Upscaler other than DLSS or DLAA waits for the restart, DLSS and DLSS-G going on; "
                    "DLSS to DLAA, and without Streamline any change, at once\n");
    } else {
        CHECK(!"a mode: order, stuck_present, stuck_sleep, gave_up, not_loaded, no_engine or upscaler_change");
    }
}
"""

# The hold's time, from vk_replay.cpp's own lines: the present job's writes on the render thread (case JOB_PRESENT), the
# game's thread's in present_tagged without Threaded rendering, what do_present declares and takes, and the hold in
# present_image_with_trial; Streamline on, dlssg_hold counted. Prints, for a present job of the render thread (time 1000,
# wait 250), then one of the game's thread, then a present made outside do_present: the holds so far and the last one's
# time and wait
HOLD_FIXTURE = r"""
#include <cstdint>
#include <cstdio>
#include <thread>
typedef struct VkImage_T* VkImage;
static VkImage const image = reinterpret_cast<VkImage>(uintptr_t(1));
static bool async = true, streamline = true;
[[maybe_unused]] static bool async_on() { return async; }
[[maybe_unused]] static bool dlssg_active() { return streamline; }
void present_image_with_trial(VkImage src);
static int holds = 0, elsewhere = -1;
static long long held_at = -1, held_wait = -1;
static void dlssg_hold(int64_t game_at, int64_t game_wait) {
    holds++, held_at = game_at, held_wait = game_wait;
    if (elsewhere < 0) {   /* another thread presents during this one's do_present (in vk_replay.cpp after the hold, which
                              keeps present_image_mu): not with this one's time */
        elsewhere = 0;
        std::thread([] { const int before = holds; present_image_with_trial(image); elsewhere = holds - before; }).join();
    }
}
struct QueueLock { void lock() {} void unlock() {} };
int64_t present_game_at = 0;
int64_t present_game_wait = 0;
/* DECLS */
static long long recorded = 0;   // (stands in for framegen_record's reads)
void present_image_with_trial([[maybe_unused]] VkImage src) {
    QueueLock qk;
/* HOLD */
}
void do_present(VkImage src) {
/* SCOPE */
    {
/* TAKE */
        recorded += game_at + game_wait;
    }
    present_image_with_trial(src);
}
struct Job { int64_t game_at = 0, game_wait = 0; };
void render_thread_present(const Job& j, VkImage src = image) {
/* RENDER_WRITE */
    do_present(src);
}
void game_thread_present(int64_t game_at) {
    (void)game_at;
/* GAME_WRITE */
    do_present(image);
}
int main() {
    async = true;
    render_thread_present(Job{1000, 250});
    std::printf("render %d %lld %lld\n", holds, held_at, held_wait);
    std::printf("elsewhere %d\n", elsewhere);
    async = false;
    game_thread_present(2000);
    std::printf("game %d\n", holds);
    async = true;
    present_image_with_trial(image);
    std::printf("outside %d\n", holds);
    render_thread_present(Job{3000, 10}, nullptr);
    std::printf("nothing %d\n", holds);
    streamline = false;
    render_thread_present(Job{4000, 20});
    std::printf("off %d\n", holds);
    std::printf("clock %lld\n", recorded);
}
"""


def line_at(text, marker):
    """The whole line text has marker on, without its line break"""
    at = text.index(marker)
    start = text.rindex("\n", 0, at) + 1
    return text[start:text.index("\n", at)]


def hold_fixture(replay):
    """HOLD_FIXTURE with vk_replay.cpp's lines in it, cut by markers the merge of #2184 and #2456 has too"""
    after = replay.index("\n", replay.index("\nint64_t present_game_wait = 0;") + 1) + 1
    decls = replay[after:replay.index("struct PendingShot {", after)]
    do = block(replay, "void do_present(const nvn_tex* t, const int crop[4], uint64_t native_generation = 0) {")
    after = do.index("\n", do.index("trial.generation = output_trial_generation.load(std::memory_order_acquire);")) + 1
    scope = do[after:do.index("#ifdef TRACY_ENABLE", after)]
    take = do[do.rindex("\n", 0, do.index("const int64_t game_at = present_game_at;")) + 1:
              do.rindex("\n", 0, do.index("rec_value_now = value;")) + 1]
    image = block(replay[replay.index("void present_image(VkImage src"):], "void present_image_with_trial(")
    after = image.index("\n    }\n", image.index("present_defer_into->swapchain_generation = dev.swapchain_generation;")) + 7
    hold = image[after:image.index("    dlssg_before_present(src != VK_NULL_HANDLE);", after)]
    job = replay[replay.index("case JOB_PRESENT:"):replay.index("case JOB_TEXTURE_RELEASED:")]
    render = "\n".join(line_at(job, m) for m in ("present_game_at = j.game_at;", "present_game_wait = j.game_wait;"))
    sync = block(block(replay, "void present_tagged(const nvn_tex* t, const int crop[4], uint64_t generation) {"),
                 "if (!async_on()) {")
    game = "\n".join(l for l in sync.splitlines() if "present_game_" in l)
    return (HOLD_FIXTURE.replace("/* DECLS */", decls).replace("/* SCOPE */", scope).replace("/* TAKE */", take)
            .replace("/* HOLD */", hold).replace("/* RENDER_WRITE */", render).replace("/* GAME_WRITE */", game))


def need(cond, why):
    if not cond:
        raise SystemExit("FAIL: " + why)


def defined(part, name):
    """Whether part defines the function name (at a line's start, with a body)"""
    return re.search(r"^\w[^;\n]*\b%s\([^;{]*\)\s*\{" % name, part, re.M)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default="g++")
    a = p.parse_args()
    dlssg = read("vk_dlssg.cpp")
    header = read("vk_dlssg.h")
    layer = read("vk_dlssg_layer.cpp")
    rules = read("dlssg_rules.h")

    # a build without Streamline (no LONGSHOT_SL) has every function of the two headers, as stubs that say it is not there
    # and that name nothing of NVIDIA's
    sl, stubs = dlssg.split("\n#else  // (no Streamline SDK in this build", 1)
    layer_sl, layer_stubs = layer.split("\n#else  // (no Streamline in this build", 1)
    for text, with_sl, without, file in ((header, sl, stubs, "vk_dlssg.cpp"), (read("vk_dlssg_layer.h"), layer_sl, layer_stubs, "vk_dlssg_layer.cpp")):
        names = re.findall(r"^(?!/)\S[^;(]*\b(dlssg_\w+)\(", text, re.M)
        need(len(names) >= 6, "%s's header: its functions were not found" % file)
        for name in names:
            for part, what in ((with_sl, "Streamline's"), (without, "the stubs'")):
                need(defined(part, name), "%s: %s is not in %s part" % (file, name, what))
    for stub in ("PFN_vkGetInstanceProcAddr dlssg_load() { return nullptr; }", "bool dlssg_loaded() { return false; }",
                 "bool dlssg_active() { return false; }", "const char* dlssg_instance_layer() { return nullptr; }",
                 "int dlssg_restart_needed() { return 0; }", "bool dlssg_toggles() { return false; }",
                 "int dlssg_upscaler(int row) { return row; }"):
        need(stub in stubs, "vk_dlssg.cpp: a build without Streamline does not say it is not there (%s)" % stub)
    for stub in ("const char* dlssg_layer_prepare() { return nullptr; }", "bool dlssg_layer_toggle_wanted() { return false; }"):
        need(stub in layer_stubs, "vk_dlssg_layer.cpp: a build without Streamline has a layer (%s)" % stub)
    for part, file in ((stubs, "vk_dlssg.cpp"), (layer_stubs, "vk_dlssg_layer.cpp")):
        need("#include" not in part and "sl::" not in part and "logf(" not in part,
             "%s: the stubs of a build without Streamline include, name or say something" % file)
    # and nothing of Streamline's SDK is included outside LONGSHOT_SL
    for text, file in ((dlssg, "vk_dlssg.cpp"), (layer, "vk_dlssg_layer.cpp")):
        guard = re.search(r"^#if(?:def LONGSHOT_SL$| defined\(LONGSHOT_SL\))", text.split('#include "vk_dlssg', 1)[1], re.M)
        need(guard, "%s: no LONGSHOT_SL guard after its own header" % file)
        before = text.split('#include "vk_dlssg', 1)[1][:guard.start()]
        need('#include "sl' not in before and "#include <windows.h>" not in before, "%s: Streamline's headers outside LONGSHOT_SL" % file)

    # dlssg_load: nothing of Streamline is loaded unless the rows as saved ask DLSS FG for frames (dlssg_rules.h) and its
    # files are there, on a PC with an NVIDIA GPU, sl.interposer.dll with NVIDIA's signature, by its full path
    load = block(sl, "PFN_vkGetInstanceProcAddr dlssg_load() {")
    ordered(load, "run_switches(s.start), saved_rows(s.start);", "if (!n || n >= exe.size()) return nullptr;",
            "if (!dlssg_rules::starts(s.start) || !s.could) return nullptr;", "if (!nvidia_adapter()) return nullptr;",
            "verifyEmbeddedSignature(path.c_str())", "LoadLibraryW(path.c_str())", "s.init(pref",
            "s.supported(sl::kFeatureDLSS_G, sl::AdapterInfo{})", "s.loaded.store(true), s.active.store(true)",
            what="dlssg_load: Streamline is loaded before every check, or said to run before it started")
    need('"slShutdown"' in load and "!s.shutdown" in load, "dlssg_load: Streamline starts without its slShutdown")
    switches = block(sl, "void run_switches(dlssg_rules::Start& s) {")
    # (a --prepare-shaders run has no window, so no close to shut Streamline down: it is not started there)
    for line in ('s.killed = env_switch("RX_VK_DLSSG") == 0;', 's.prepare_only = env_on("LONGSHOT_PREPARE_ONLY");', "s.built = true;"):
        need(line in switches, "run_switches: no %s" % line)
    need("s.built && !s.killed && !s.prepare_only && !s.dlss_fg_off && !s.framegen_off" in rules,
         "dlssg_rules.h possible(): Streamline may start in a run that makes no DLSS FG frames")
    need(block(sl, "bool nvidia_adapter() {").count("0x10DE") == 1, "nvidia_adapter: not NVIDIA's vendor id")
    need("s.active.load(std::memory_order_relaxed) && !s.closing && s.ready" in block(sl, "bool runs(const State& s) {"),
         "runs(): a call goes to Streamline's features before they are there, after it gave up or once the exit began")
    for fn in ("void dlssg_marker(DlssgMarker m) {", "void dlssg_sleep() {", "void dlssg_inputs(", "void dlssg_before_present("):
        need("if (!runs(s)) return;" in block(sl, fn), "vk_dlssg.cpp: %s goes to Streamline whether it runs or not" % fn.split("(")[0])
    # the GPU the renderer chose, and its plugins: a no gives up, the renderer then runs as without it
    need("give_up(s," in block(sl, "void dlssg_device(VkPhysicalDevice physical) {") and "return give_up(s," in block(sl, "void dlssg_device_made() {"),
         "vk_dlssg.cpp: a GPU DLSS-G does not run on, or plugins that did not load, do not give up")
    need("if (!s.active.exchange(false)) return;" in block(sl, "void give_up(State& s, const char* why, int result) {"), "give_up: not once")

    # Streamline's loader in place of vulkan-1.dll's, asked first; Longshot's layer under it, the instance made without it
    # when the loader refuses it; the GPU and the device told to Streamline
    main_cpp = read("vk_main.cpp")
    ordered(block(main_cpp, "bool load_global() {"), "dlssg_load()", 'LoadLibraryExA("vulkan-1.dll"',
            what="load_global: Streamline's loader is not asked before vulkan-1.dll")
    instance = block(main_cpp, "bool create_instance() {")
    ordered(instance, "const char* const dlssg_layer = dlssg_instance_layer();", "without_layer = ext_list.size();",
            "if (dlssg_layer) dlssg_layer_instance_extensions(ext_list, all);",
            "for (int with_layer = dlssg_layer ? 1 : 0; with_layer >= 0 && made != VK_SUCCESS; with_layer--) {",
            "ci.enabledExtensionCount = (uint32_t)(with_layer ? ext_list.size() : without_layer);",
            what="create_instance: an instance the loader refuses with Longshot's layer is not made again without it")
    need('env_switch("RX_VK_DLSSG_LAYER") == 0' in block(sl, "const char* dlssg_instance_layer() {") and
         "if (!dlssg_active()" in block(sl, "const char* dlssg_instance_layer() {"),
         "dlssg_instance_layer: the layer is named without Streamline, or RX_VK_DLSSG_LAYER=0 does not leave it out")
    ordered(block(main_cpp, "bool create_device() {"), "dlssg_device(dev.physical);", "dlssg_layer_device_extensions(want, exts);",
            "dlssg_layer_device_features(tail);", "dlssg_device_made();",
            what="create_device: Streamline is not asked about the GPU chosen, or not told the device is made")

    # the renderer's own frame generation, NGX's DLSS-G and its own Reflex aside while Streamline makes the frames
    for name, fn in (("vk_framegen.cpp", "bool framegen_on() {"), ("vk_fsr3fi.cpp", "uint32_t frames_wanted() {"),
                     ("vk_reflex.cpp", "bool on() {")):
        need("!dlssg_active()" in block(read(name), fn), "%s: %s still runs with Streamline's DLSS Frame Generation" % (name, fn.split("(")[0]))
    framegen = read("vk_framegen.cpp")
    need("if (dlssg_active()) return 0;" in block(framegen, "int framegen_now() {"), "framegen_now: the renderer's sets beside Streamline's")
    need("dlssg_making()" in block(framegen, "FgMaker framegen_maker() {"), "framegen_maker: the menu does not say who makes the frames under Streamline")
    ngx = read("vk_dlss.cpp")
    need("bool fg_asked() { return fg_chosen() && !dlssg_active(); }" in ngx, "vk_dlss.cpp: NGX's DLSS-G is asked beside Streamline's")
    need("if (dlssg_active()) return FG_SKIP_NONE;" in block(ngx, "int dlss_fg_unavailable() {"), "dlss_fg_unavailable: NGX asked under Streamline")
    need("dlssg_active()" in block(ngx, "void dlss_fg_probe() {"), "dlss_fg_probe: NGX's DLSS-G started under Streamline")
    need("dlssg_frames_most()" in block(ngx, "int dlss_fg_most() {"), "dlss_fg_most: not Streamline's most frames while it makes them")
    reflex = read("vk_reflex.cpp")
    for fn, to in (("void reflex_present_begins() {", "dlssg_marker(DLSSG_SIM_END), dlssg_marker(DLSSG_SUBMIT_START)"),
                   ("void reflex_submit_ends() {", "dlssg_marker(DLSSG_SUBMIT_END)"),
                   ("const void* reflex_present_id(const void* next) {", "dlssg_marker(DLSSG_PRESENT_START), next"),
                   ("void reflex_presented() {", "dlssg_marker(DLSSG_PRESENT_END)"), ("void reflex_sleep() {", "dlssg_sleep()")):
        need(block(reflex, fn).split("{", 1)[1].lstrip().startswith("if (dlssg_active()) return %s;" % to),
             "vk_reflex.cpp: %s does not hand over to Streamline's Reflex first" % fn.split("(")[0])

    # the present: a game frame's held with the queue's lock let go, then on or off for this one, before vkQueuePresentKHR,
    # which present_out makes (present ahead, #1702: present_image ends in it, or present_commit later for a deferred one);
    # every present on the render thread while Streamline's loader is the renderer's
    replay = read("vk_replay.cpp")
    # its time: the present job's, as do_present took it (present_game_at is frame generation's too, #2184, and do_present
    # takes it once); without Threaded rendering (RX_VK_ASYNC=0) the present is the game's own thread's: no hold there, which
    # would put the game's thread to sleep for the hold and the game under 60 fps (and its wait for the renderer is not
    # known there); and nothing held for a present made outside do_present
    with tempfile.TemporaryDirectory(prefix="dlssg-hold-") as tmp:
        cpp, exe = Path(tmp) / "hold.cpp", Path(tmp) / "hold"
        cpp.write_text(hold_fixture(replay), encoding="utf-8")
        subprocess.run([a.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread", str(cpp), "-o", str(exe)], check=True)
        said = dict(l.split(" ", 1) for l in subprocess.run([str(exe)], check=True, capture_output=True,
                                                            text=True).stdout.splitlines())
    need(said["render"] != "0 -1 -1",
         "present_image_with_trial: a game frame's present under Streamline is never held: do_present took the present "
         "job's time (present_game_at) before the hold reads it")
    need(said["elsewhere"] == "0", "present_image_with_trial: a present made on another thread during another's do_present "
         "is held too, with that one's time (present_shown_at is not this thread's alone)")
    need(said["render"] == "1 1000 250",
         "present_image_with_trial: the hold is not given the present job's time and wait once (holds, time, wait: %s)"
         % said["render"])
    need(said["game"] == "1", "present_image_with_trial: without Threaded rendering a game frame's present is held on the "
         "game's own thread")
    need(said["outside"] == "1", "present_image_with_trial: a present made outside do_present (present_image) is held, "
         "with the time of the present before it")
    need(said["nothing"] == "1", "present_image_with_trial: a present without the game's picture is held")
    need(said["off"] == "1", "present_image_with_trial: a present is held with Streamline's frame generation off")
    # frame generation's clock (#2184) reads each present's time, the game's own thread's too: 1000 + 250, 2000,
    # 3000 + 10 and 4000 + 20
    need(said["clock"] == "10280", "do_present: frame generation's clock does not read every present job's time and "
         "wait, or without Threaded rendering present_tagged's own (read %s of 10280)" % said["clock"])
    image = block(replay[replay.index("void present_image(VkImage src"):], "void present_image_with_trial(")
    ordered(image, "if (present_defer_into) {", "dlssg_hold(",
            what="present_image: a deferred present is held too (it is not shown yet)")
    held = image.rindex("\n    if (", 0, image.index("dlssg_hold("))
    ordered(image[held:], "qk.unlock();", "dlssg_hold(present_shown_at, present_shown_wait);", "qk.lock();",
            "dlssg_before_present(src != VK_NULL_HANDLE);", "present_out(index, qk, trial, preview_path);",
            what="present_image: Streamline is not told about each present in its place, or the hold keeps the queue's lock")
    # (present_commit is the display threads' alone, vk_framegen.cpp's and present_thread_on's, and neither runs while
    # Streamline makes the frames: nothing of it there)
    need("dlssg_" not in block(replay, "bool present_commit("), "present_commit: a call to Streamline where none runs")
    need("vkQueuePresentKHR(" in block(replay, "static bool present_out(uint32_t index, std::unique_lock<decltype(dev.present_mu)>& qk, "
                                                "OutputTrialSource trial, bool preview_path) {"),
         "present_out no longer presents: Streamline's hook may not be before vkQueuePresentKHR")
    need("static const bool on = !dlssg_loaded();" in block(replay, "bool present_thread_on() {"),
         "present_thread_on: presents leave the render thread under Streamline's loader")
    need("if (src) dlssg_backbuffer(rect.x, rect.y, rect.w, rect.h, dev.swap_extent.width, dev.swap_extent.height);" in image,
         "present_image: DLSS-G is not told where the game's picture is in the swapchain image")
    # the frame chain's depth and motion, once its engine drew them, copyable by Streamline
    temporal = read("vk_temporal.cpp")
    engine = block(temporal, "bool draw_engine(")
    ordered(engine, "engine.e->evaluate(cmd, f)", "dlssg_inputs(cmd, f, cam.projection, cam.current, cam.jitter, motion);",
            "s.last_frame = now;", what="draw_engine: Streamline's inputs are not the chain's frame")
    need("const VkImageUsageFlags copied = VK_IMAGE_USAGE_TRANSFER_SRC_BIT;" in engine and "iw, ih, copied)" in engine and
         "| copied)" in engine, "draw_engine: the depth and the motion cannot be copied by Streamline (TRANSFER_SRC)")
    need("std::memcpy(k.projection, c + 0x70 / 4" in block(temporal, "void temporal_note_scene_draw("),
         "temporal_note_scene_draw: the scene camera's projection (0x70) is not kept for DLSS Frame Generation")
    # Streamline's constants: no invalid motion value of 0, which marked every pixel that did not move as invalid
    need(not re.search(r"motionVectorsInvalidValue\s*=", block(sl, "void dlssg_inputs(")),
         "dlssg_inputs: motionVectorsInvalidValue set (0 marked every still pixel invalid): Streamline's default has none")

    # the Frame generation by row: the restart Streamline's start takes, asked through the row's own restart value, and no
    # row of Streamline's own
    row = re.search(r'\.row\(\{\.key = "frame_generation_by",.*?\}\)\n', read("menu/framegen.cpp"), re.S)
    need(row and ".restart = true," in row.group(0) and
         ".restart_value = MENU_HOOK([](int) { return dlssg_restart_needed(); })" in row.group(0),
         "menu/framegen.cpp: the Frame generation by row does not ask for the restart Streamline's start takes")
    needed = block(sl, "int dlssg_restart_needed() {")
    need(needed.split("{", 1)[1].lstrip().startswith("if (!dev.device) return 0;"),
         "dlssg_restart_needed: not 0 before the device is made, the value the row starts with")
    need("dlssg_rules::restart_needed(run, could, rows_now(s))" in needed, "dlssg_restart_needed: not dlssg_rules.h's rule")
    for name in ("vk_menu.cpp", "vk_menu.h"):
        need("dlssg" not in read(name), "%s: a row or an option of Streamline's own (the Frame generation rows choose it)" % name)

    # the Upscaler row under Streamline (upscaler_change above): a change that waits for the restart is said by the row's own
    # restart value (After restart, Restart needed), and the row shows and steps from the value chosen, not from what runs
    up_row = re.search(r'\.row\(\{\.key = "dlss",.*?\}, menu::after', read("menu/upscaler.cpp"), re.S)
    need(up_row and ".restart = true," in up_row.group(0) and
         ".restart_value = MENU_HOOK([](int) { return upscaler_waits() ? 1 : 0; })" in up_row.group(0),
         "menu/upscaler.cpp: the Upscaler row does not say that a change under Streamline waits for the restart")
    menu = read("vk_menu.cpp")
    need("bool upscaler_waits() { return temporal_row() != opts.dlss; }" in menu and
         "int upscaler_row() { return upscaler_waits() ? opts.dlss : upscaler(); }" in menu and
         "if (r.value == &opts.dlss) v = ur::step_engine(upscaler_row(), dir, upscalers_available());" in menu,
         "vk_menu.cpp: the Upscaler row steps from what runs while the value chosen waits for the restart")
    need("ur::engine(upscaler_row())" in block(read("menu/upscaler.cpp"), "const char* tx_upscaler(int) {"),
         "menu/upscaler.cpp: the Upscaler row shows what runs, not the value that waits for the restart")
    need("now.upscaler = dlssg_upscaler(opts.dlss);" in block(sl, "dlssg_rules::Start rows_now(const State& s) {"),
         "rows_now: the frames asked of DLSS-G and the Frame generation by row's restart read the Upscaler as set, not as it runs")
    # (the renderer reads the Upscaler row as it runs, vk_temporal.h temporal_row: the menu, the options file, a preset and
    # the pipeline list read and write the setting)
    for path in sorted(VK.rglob("*.cpp")):
        rel = path.relative_to(VK).as_posix()
        if rel in ("vk_menu.cpp", "vk_temporal.cpp", "vk_dlssg.cpp") or rel.startswith("menu/"):
            continue
        need(not re.search(r"\bopts\.dlss\b(?!_)", path.read_text(encoding="utf-8")),
             "%s reads the Upscaler row's setting: what runs is vk_temporal.h temporal_row" % rel)
    need("int temporal_row() { return dlssg_upscaler(opts.dlss); }" in temporal, "vk_temporal.cpp: temporal_row is not Streamline's")

    # NGX starts once, with the folders of its first start, which is Streamline's: the folders vk_dlss.cpp names for DLSS's
    # runtime go to Streamline after the plugins' own (0xbad00004 with nvngx_dlss.dll only in the game folder's ngx)
    folders = block(sl, "std::vector<std::wstring> dlss_runtime_folders(")
    theirs_all = block(ngx, "std::vector<fs::path> runtime_dirs(const fs::path& game) {")
    for theirs, ours in (('env_path(L"RX_VK_DLSS_DIR")', '_wgetenv(L"RX_VK_DLSS_DIR")'), ('game / L"ngx"', r'game + L"\\ngx"'),
                         ('l / L"Longshot" / L"ngx"', r'L"\\Longshot\\ngx"')):
        need(theirs in theirs_all and ours in folders,
             "dlss_runtime_folders: not the folders vk_dlss.cpp's runtime_dirs names for DLSS's runtime (%s)" % ours)
    ordered(load, "s.paths = {s.dir};", "dlss_runtime_folders(s.game_dir)", "pref.pathsToPlugins = s.path_list.data();",
            "s.init(pref", what="dlssg_load: Streamline is not told the folders of DLSS's runtime after the plugins' own")
    need("NVSDK_NGX_Result_FAIL_FeatureNotFound && dlssg_loaded()" in block(ngx, "bool ngx_init_locked() {"),
         "vk_dlss.cpp ngx_init_locked: 0xbad00004 under Streamline's loader is said as the GPU's fault, not NGX's folders")
    need("game_module_path(" not in load, "dlssg_load: the game folder through the ANSI code page (game_module_path), not wide")

    # the exit: from mkwvk_close_asked (the window's close, Quit, Restart), last, whenever Streamline's loader is the
    # renderer's (given up or not); every call after it does nothing
    ordered(block(main_cpp, "void mkwvk_close_asked(void) {"), "pipeline_cache_close();", "prepare_stop();", "dlss_fg_close();",
            "if (dlssg_shutdown()) return;", "    dev.closing.store(true, std::memory_order_release);",
            what="mkwvk_close_asked: Streamline is not shut down last before the process ends, in place of the swapchain's retire")
    display = (ROOT / "host" / "display.c").read_text(encoding="utf-8")
    ordered(display, "case WM_APP + 9:", "hos_relaunch_ask();", "case WM_CLOSE:", "rx_close_asked();",
            "hos_window_closed_exit();", what="display.c: Restart and the window's close do not ask the back end first")

    with tempfile.TemporaryDirectory(prefix="dlssg-") as tmp:
        cpp = Path(tmp) / "fixture.cpp"
        exe = Path(tmp) / "fixture"
        cpp.write_text(FIXTURE.replace("/* FRAME_LIMIT */", block(sl, "uint32_t frame_limit_us(bool on) {")), encoding="utf-8")
        subprocess.run([a.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)

        # the NVIDIA app's DLSS override: said once, as what it changes, NGX's side (DLSS's and DLSS-G's models from NVIDIA's
        # folder, with the presets the app sets); of Streamline's plugins, only what Streamline decided, once it has: kept
        # beside longshot.exe when it found no NVIDIA version of one. Streamline's warnings still go to the log
        cpp = Path(tmp) / "sl_log.cpp"
        exe = Path(tmp) / "sl_log"
        cpp.write_text(SL_LOG_FIXTURE.replace("/* SL_LOG */", block(sl, "void sl_log(sl::LogType type, const char* msg) {")),
                       encoding="utf-8")
        subprocess.run([a.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(exe)], check=True)
        said = {mode: subprocess.run([str(exe), mode], check=True, capture_output=True, text=True).stdout.splitlines()
                for mode in ("searching", "kept")}
        need(not any("plugins" in s for s in said["searching"]),
             "sl_log: Streamline's plugins are said to change before Streamline found NVIDIA's: %s" % said["searching"])
        for mode, lines in said.items():
            told = [s for s in lines if "DLSS override is on" in s]
            need(len(told) == 1, "sl_log (%s): the NVIDIA app's DLSS override is said %d times, not once" % (mode, len(told)))
            for fact in ("NGX", "NGX\\models", "preset", "nvngx_dlss.dll", "nvngx_dlssg.dll"):
                need(fact in told[0], "sl_log (%s): the override's line does not say what NGX takes (%s): %s" % (mode, fact, told[0]))
        kept = [s for s in said["kept"] if "plugins" in s]
        need(len(kept) == 1 and "keeps" in kept[0] and "beside longshot.exe" in kept[0],
             "sl_log: Streamline keeping its own plugins is not said once: %s" % said["kept"])
        need(any(s.startswith("vk: Streamline warning: ") and "Could not find version matching" in s for s in said["kept"]),
             "sl_log: Streamline's warning is not passed on")
        need(len(said["kept"]) == 3 and len(said["searching"]) == 1,
             "sl_log: Streamline's other info lines are logged: %s" % said)
        print("PASS: the NVIDIA app's DLSS override is said once, as NGX's models and presets; Streamline's plugins only "
              "once it kept its own")

        # the exit, with vk_dlssg.cpp's own code and a stand-in Streamline (SHUTDOWN_FIXTURE)
        enums = re.search(r"enum DlssgMarker \{[^}]*\};", header).group(0)
        state = "\n".join((block(sl, "struct State {") + ";", block(sl, "State& st() {"), block(sl, "bool runs(const State& s) {"),
                           block(sl, "dlssg_rules::Start rows_now(const State& s) {"),
                           block(sl, "void give_up(State& s, const char* why, int result) {")))
        cut = "\n".join(block(sl, m) for m in ("template <class M>\nbool lock_within(M& m) {", "sl::FrameToken* token() {",
                                               "sl::Result set_dlssg(State& s, sl::DLSSGMode mode, uint32_t frames) {",
                                               "void dlssg_marker(DlssgMarker m) {", "void dlssg_sleep() {",
                                               "bool dlssg_shutdown() {", "void dlssg_before_present(bool game_frame) {"))
        # (the Upscaler row as the renderer reads it: vk_temporal.cpp's temporal_mode and its neighbours, vk_fsr3.cpp's choice
        # of FSR 3.1, and what vk_dlssg.cpp says of Streamline's loader and its frames)
        chain = between(temporal, "namespace {\nint env_dlss()", "bool temporal_full_scale()")
        fsr3 = read("vk_fsr3.cpp")
        fsr3_cut = "\n".join(block(fsr3, m) for m in ("bool fsr3_chosen() {", "bool fsr3_on_chain() {", "bool fsr3_wanted() {"))
        loaded = "\n".join((block(sl, "bool dlssg_loaded() {"), block(sl, "bool dlssg_active() {"),
                            between(sl, "int dlssg_frames_most() {", "int dlssg_restart_needed() {")))
        cpp = Path(tmp) / "shutdown.cpp"
        exe = Path(tmp) / "shutdown"
        cpp.write_text(SHUTDOWN_FIXTURE.replace("/* HEADER_ENUMS */", enums).replace("/* TEMPORAL */", chain)
                       .replace("/* FSR3 */", fsr3_cut).replace("/* STATE */", state).replace("/* LOADED */", loaded)
                       .replace("/* CUT */", cut), encoding="utf-8")
        subprocess.run([a.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(VK), str(cpp), "-o", str(exe)],
                       check=True)
        for mode in ("order", "stuck_present", "stuck_sleep", "gave_up", "not_loaded", "no_engine", "upscaler_change"):
            subprocess.run([str(exe), mode], check=True, timeout=60)


if __name__ == "__main__":
    main()
