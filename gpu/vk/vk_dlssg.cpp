// (longshot) NVIDIA DLSS Frame Generation through NVIDIA Streamline 2.14 (vk_dlssg.h), on Vulkan with manual hooking (the
// SDK's docs: ProgrammingGuideManualHooking.md, ProgrammingGuideDLSS_G.md). Streamline has to see the instance, the device
// and the swapchain made, so it is started before Vulkan is loaded, from the rows as saved (dlssg_rules.h): it then gives
// the renderer sl.interposer.dll's vkGetInstanceProcAddr in place of vulkan-1.dll's (vk_main.cpp load_global). That one
// returns vulkan-1.dll's own functions except for the few Streamline intercepts (sl_hooks.h: the instance's and the
// device's creation, the swapchain's, acquire and present). A change of the rows that needs it, or no longer does, takes a
// restart (dlssg_restart_needed).
//
// Each frame, in the order the renderer goes through it:
//  - dlssg_sleep after a present: the next frame's token, Reflex's sleep (sl.reflex) and the PCL simulation-start marker;
//  - dlssg_inputs from the temporal upscaler's frame chain (vk_temporal.cpp, after its engine drew): the chain's depth and
//    motion tagged (copied by Streamline at once, eOnlyValidNow, into the chain's command buffer) and the frame's constants
//    from the scene camera, its jitter taken off. Without that chain (the game's own menus, movies, loading screens) there
//    are no inputs and frame generation stays off;
//  - dlssg_hold before a game frame's present, after its acquire and its copy's submit: the present held to an even time
//    (dlssg_pace.h);
//  - dlssg_marker for the rest of the frame (vk_reflex.cpp's hooks), and dlssg_before_present: frame generation on for a
//    game frame, off with the Insert menu open, for the window's own presents, and after a while without inputs.
// The renderer's own frame generation (vk_framegen.cpp, vk_fsr3fi.cpp), NGX's DLSS-G (vk_dlss.cpp) and its own Reflex
// (vk_reflex.cpp) are off while Streamline is active: DLSS-G works only with Streamline's Reflex. When the window closes (an
// exit or a Restart), dlssg_shutdown shuts Streamline down before the host ends the process, and every dlssg_* call does
// nothing after that.
//
// DLSS (vk_dlss.cpp) runs beside it: NGX starts once in a process, with the folders the first start named for its runtimes,
// and with Streamline that start is sl.common's, when the device is made. So the folders vk_dlss.cpp names for DLSS's
// runtime go to Streamline too (dlssg_load's pathsToPlugins, after the plugins' own folder): without them NGX did not look in
// the game folder's ngx\, and DLSS and DLAA failed with 0xbad00004 (feature not found) unless nvngx_dlss.dll was also beside
// longshot.exe or NGX had a DLSS of its own downloads (C:\ProgramData\NVIDIA\NGX\models).
//
// Streamline's binaries are NVIDIA's, under NVIDIA's licenses (the SDK's license.txt, nvngx_dlss.license.txt and
// reflex.license.txt), never part of Longshot: scripts/fetch.py streamline --game copies them from the SDK's bin/x64 beside
// longshot.exe (sl.interposer.dll, sl.common.dll, sl.dlss_g.dll, sl.reflex.dll, sl.pcl.dll, nvngx_dlssg.dll,
// NvLowLatencyVk.dll), and sl.interposer.dll is loaded only on a PC with an NVIDIA GPU and with NVIDIA's valid signature.
// Streamline's own log goes to sl.log in the game folder, its warnings and errors to longshot_log.txt too.
#ifdef LONGSHOT_SL   // (sl_security.h's signature check needs Windows 8's WinTrust declarations: before the first windows.h)
#undef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif
#include "vk_dlssg.h"

#ifdef LONGSHOT_SL

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "dlss_math.h"
#include "dlssg_pace.h"
#include "dlssg_rules.h"
#include "edid_vrr.h"
#include "vk_dlss.h"
#include "vk_dlssg_layer.h"
#include "vk_menu.h"
#include "vk_paths.h"
#include "vk_temporal.h"

#include <windows.h>
#include <dxgi.h>
#include <cfgmgr32.h>
#include "sl.h"
#include "sl_consts.h"
#include "sl_dlss_g.h"
#include "sl_pcl.h"
#include "sl_reflex.h"
#include "sl_security.h"

#include "vk_work.h"   // (vk_recorder.h's settle_parts, for the exit; the last include, as everywhere)

namespace mkwvk {

int display_refresh_mhz();   // (vk_main.cpp: the window's display's refresh, 0 when not known)
HWND app_window();   // (vk_main.cpp: the game's window)
extern std::mutex present_image_mu;   // (vk_replay.cpp: one present at a time, held from its acquire to its present)

namespace {

// ---- the game's matrices: row major, clip = M * position (column vectors) ----
void mul(const float* a, const float* b, float* out) {
    float r[16];
    for (int i = 0; i < 4; i++)
        for (int j = 0; j < 4; j++) {
            float s = 0;
            for (int k = 0; k < 4; k++) s += a[i * 4 + k] * b[k * 4 + j];
            r[i * 4 + j] = s;
        }
    std::memcpy(out, r, sizeof r);
}
constexpr float kIdentity[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
/// The inverse (dlss_math::reprojection of the identity: identity * m^-1); false when singular or not finite
bool inverse(const float* m, float* out) { return dlss_math::reprojection(m, kIdentity, out); }

struct State {
    std::mutex mu;
    // (written by dlssg_load alone, before the renderer has a second thread, and read without the lock after it)
    dlssg_rules::Start start;   // (the run's switches, and the rows as saved)
    bool could = false;         // (Streamline's files are beside longshot.exe, and it did not fail to start in this run)
    bool rect = true;           // (RX_VK_DLSSG_RECT)
    // (loaded: Streamline's loader is the renderer's. active: and it makes DLSS FG's frames; false again once it gave up)
    std::atomic<bool> loaded{false}, active{false};
    std::atomic<int> frames_max{0};   // (the frames DLSS-G makes at most; 0 until the device is made)
    std::atomic<int> making{0};       // (dlssg_making)
    std::atomic<int> upscaler{0};     // (dlssg_upscaler: the Upscaler row as it runs while a change of it waits for the restart)
    bool tried = false;
    HMODULE module = nullptr;
    PFun_slInit* init = nullptr;
    PFun_slIsFeatureSupported* supported = nullptr;
    PFun_slSetTagForFrame* set_tag = nullptr;
    PFun_slSetConstants* set_constants = nullptr;
    PFun_slGetFeatureFunction* feature_function = nullptr;
    PFun_slGetNewFrameToken* new_token = nullptr;
    PFun_slDLSSGSetOptions* dlssg_set_options = nullptr;
    PFun_slDLSSGGetState* dlssg_get_state = nullptr;
    PFun_slReflexSetOptions* reflex_set_options = nullptr;
    PFun_slReflexSleep* reflex_sleep = nullptr;
    PFun_slPCLSetMarker* pcl_marker = nullptr;
    PFun_slShutdown* shutdown = nullptr;
    bool ready = false;   // (the features' functions are there: dlssg_device_made)
    // (the exit, dlssg_shutdown: from closing on no call goes to Streamline; sleeping counts the Reflex sleeps under way, which
    // let the lock go, and slept is told as each ends)
    bool closing = false;
    int sleeping = 0;
    std::condition_variable slept;
    sl::FrameToken* token = nullptr;
    uint32_t inputs_frame = UINT32_MAX;   // (the token's index whose inputs were tagged)
    int reflex_mode = -1;
    sl::DLSSGMode mode = sl::DLSSGMode::eOff;
    uint32_t frames = 0;                  // (numFramesToGenerate as last set)
    float last_vp[16]{};                  // (the frame before's view-projection, its jitter taken off)
    bool have_last = false;
    uint32_t limit_us = UINT32_MAX;       // (Reflex's frame limit as last set)
    int missing = 0;                      // (game frames in a row without inputs)
    // (the FPS counter: the frames DLSS-G presented, the game's own among them, and the made ones already counted)
    int64_t presented = 0, own = 0, noted = 0;
    uint32_t status = 0;                  // (DLSS-G's status as last logged)
    int status_told = 0;
    // (the last present's picture in the swapchain image, x y w h, and the image's size: dlssg_backbuffer)
    uint32_t bb[4]{}, bb_full[2]{};
    std::wstring dir, game_dir;
    std::vector<std::wstring> paths;      // (Preferences::pathsToPlugins: the plugins' folder, then DLSS's runtime's)
    std::vector<const wchar_t*> path_list;
};
// (never destroyed: the process ends through ExitProcess with Streamline's threads gone, and with dlssg_shutdown's locks
// held; a destructor then has nothing to do)
State& st() {
    static State& s = *new State;
    return s;
}

/// Whether a call goes to Streamline's features now. With s.mu held
bool runs(const State& s) { return s.active.load(std::memory_order_relaxed) && !s.closing && s.ready; }

/// How long the exit waits for a Reflex sleep, and for a present, before it leaves Streamline as it is
constexpr std::chrono::milliseconds kExitWait{2000};

/// m locked within kExitWait (polled: a plain mutex, or a queue's QueueMutex, vk_internal.h); false when it was not
template <class M>
bool lock_within(M& m) {
    const auto until = std::chrono::steady_clock::now() + kExitWait;
    while (!m.try_lock()) {
        if (std::chrono::steady_clock::now() >= until) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return true;
}

int env_switch(const char* name) {   // (-1: unset)
    const char* e = getenv(name);
    return e && *e ? atoi(e) : -1;
}

/// The three rows that decide the start, as longshot_options.ini has them (the menu reads its file after the instance is
/// made: vk_menu.cpp menu_init, which clamps and takes RX_VK_FRAMEGEN_MULT over the first the same way). No file, or no
/// such line: the row's default
void saved_rows(dlssg_rules::Start& s) {
    if (FILE* f = fopen(options_path().c_str(), "r")) {   // (vk_paths.h)
        char line[512];
        while (fgets(line, sizeof line, f)) {
            if (!strncmp(line, "frame_generation=", 17)) s.framegen = std::clamp(atoi(line + 17), 0, 8);
            else if (!strncmp(line, "frame_generation_by=", 20)) s.framegen_by = std::clamp(atoi(line + 20), 0, 2);
            else if (!strncmp(line, "dlss=", 5)) s.upscaler = std::clamp(atoi(line + 5), 0, 8);
        }
        fclose(f);
    }
    if (const char* e = getenv("RX_VK_FRAMEGEN_MULT")) s.framegen = std::clamp(atoi(e), 2, 8);
}

/// The run's switches (dlssg_rules.h Start), each read as the code it belongs to reads it
void run_switches(dlssg_rules::Start& s) {
    s.built = true;
    s.killed = env_switch("RX_VK_DLSSG") == 0;
    s.prepare_only = env_on("LONGSHOT_PREPARE_ONLY");
    s.dlss_fg_off = env_switch("RX_VK_DLSS") == 0 || env_switch("RX_VK_DLSS_FG") == 0;
    s.framegen_off = getenv("RX_VK_FRAMEGEN") && atoi(getenv("RX_VK_FRAMEGEN")) == 0;
    s.dlss_fg_forced = env_switch("RX_VK_DLSS_FG") > 0;
    // (temporal_mode's order: RX_VK_FSR=1's FSR 3.1 over the others, then DLSS / DLAA, FSR 4 / its AA, XeSS / its AA)
    const int dlss = env_switch("RX_VK_DLSS"), fsr4 = env_switch("RX_VK_FSR4"), xess = env_switch("RX_VK_XESS");
    s.upscaler_switch = env_switch("RX_VK_FSR") > 0 ? 3 : dlss > 0 ? (dlss >= 2 ? 2 : 1) : fsr4 > 0 ? (fsr4 >= 2 ? 5 : 4)
                      : xess > 0 ? (xess >= 2 ? 8 : 7) : -1;
}

/// The rows as they are now, with the run's switches
dlssg_rules::Start rows_now(const State& s) {
    dlssg_rules::Start now = s.start;
    now.framegen = opts.framegen, now.framegen_by = opts.framegen_by, now.upscaler = dlssg_upscaler(opts.dlss);   // (as it runs)
    return now;
}

/// Whether the PC has an NVIDIA display adapter (DXGI's list: Vulkan is not loaded yet). Without one nothing of Streamline
/// is loaded
bool nvidia_adapter() {
    const HMODULE dxgi = LoadLibraryExW(L"dxgi.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!dxgi) return false;
    using Create = HRESULT WINAPI(REFIID, void**);
    const auto create = (Create*)(void*)GetProcAddress(dxgi, "CreateDXGIFactory1");
    IDXGIFactory1* factory = nullptr;
    bool found = false;
    if (create && SUCCEEDED(create(__uuidof(IDXGIFactory1), (void**)&factory)) && factory) {
        IDXGIAdapter1* a = nullptr;
        for (UINT i = 0; !found && factory->EnumAdapters1(i, &a) == S_OK; i++) {
            DXGI_ADAPTER_DESC1 d{};
            if (SUCCEEDED(a->GetDesc1(&d))) found = d.VendorId == 0x10DE;
            a->Release();
        }
        factory->Release();
    }
    return found;   // (dxgi.dll stays loaded: the driver's presentation loads it anyway)
}

/// The game folder (vk_paths.h's rule, wide: Streamline takes wide paths): the exe's folder, or the one above it when that
/// is bin\ beside image.bin. exe_dir ends with its separator
std::wstring game_folder(const std::wstring& exe_dir) {
    std::wstring d = exe_dir.substr(0, exe_dir.empty() ? 0 : exe_dir.size() - 1);
    const size_t slash = d.find_last_of(L"\\/");
    if (slash != std::wstring::npos && !_wcsicmp(d.c_str() + slash + 1, L"bin") &&
        GetFileAttributesW((d.substr(0, slash + 1) + L"image.bin").c_str()) != INVALID_FILE_ATTRIBUTES)
        d.resize(slash);
    return d;
}

/// Where DLSS's runtime (nvngx_dlss.dll) is, as vk_dlss.cpp's ngx_init names the folders: RX_VK_DLSS_DIR when set, the game
/// folder's ngx, %LOCALAPPDATA%\Longshot\ngx. sl.common starts NGX with the plugins' folder and every one of
/// Preferences::pathsToPlugins as where its runtimes are (Streamline's commonEntry.cpp), and NGX keeps the folders of its
/// first start: vk_dlss.cpp's own start comes later and its list is not used
std::vector<std::wstring> dlss_runtime_folders(const std::wstring& game) {
    std::vector<std::wstring> v;
    if (const wchar_t* e = _wgetenv(L"RX_VK_DLSS_DIR"); e && *e) v.push_back(e);
    v.push_back(game + L"\\ngx");
    if (const wchar_t* l = _wgetenv(L"LOCALAPPDATA"); l && *l) v.push_back(std::wstring(l) + L"\\Longshot\\ngx");
    return v;
}

void sl_log(sl::LogType type, const char* msg) {
    std::string m(msg ? msg : "");
    while (!m.empty() && (m.back() == '\n' || m.back() == '\r')) m.pop_back();
    if (type == sl::LogType::eInfo) {   // (Streamline's own sl.log has those, but for one)
        // (the NVIDIA app's DLSS override: NGX takes DLSS's and DLSS-G's models from NVIDIA's folder, with the presets the
        // app sets. Streamline then looks for NVIDIA's version of each of its plugins, and loads them only when it finds
        // all of them; when it does not, it says so and keeps the ones beside longshot.exe. sl.log says which)
        static std::atomic<bool> told{false}, kept{false};
        if (m.find("OTA override DRS key is set") != std::string::npos && !told.exchange(true)) {
            logf("vk: Streamline: the NVIDIA app's DLSS override is on: NGX takes DLSS's and DLSS-G's models from "
                 "C:\\ProgramData\\NVIDIA\\NGX\\models where it has them, with the presets the app sets, over Longshot's "
                 "nvngx_dlss.dll and nvngx_dlssg.dll (sl.log's NGX lines say which)");
        }
        if (m.find("Not all override plugins available") != std::string::npos && !kept.exchange(true)) {
            logf("vk: Streamline: NVIDIA's override of its plugins is not complete on this PC: Streamline keeps the "
                 "plugins beside longshot.exe");
        }
        return;
    }
    logf("vk: Streamline %s: %s", type == sl::LogType::eError ? "error" : "warning", m.c_str());
}

template <typename F>
bool feature_function(sl::Feature feature, const char* name, F*& out) {
    void* p = nullptr;
    State& s = st();
    const sl::Result r = s.feature_function ? s.feature_function(feature, name, p) : sl::Result::eErrorNotInitialized;
    if (r != sl::Result::eOk || !p) {
        logf("vk: Streamline: no %s (%d)", name, (int)r);
        return false;
    }
    out = reinterpret_cast<F*>(p);
    return true;
}

/// Streamline no longer makes DLSS FG's frames in this run: the renderer runs as without it, over Streamline's loader
/// (which passes every call on), and the exit still shuts Streamline down. With s.mu held
void give_up(State& s, const char* why, int result) {
    if (!s.active.exchange(false)) return;
    s.making.store(0, std::memory_order_relaxed);
    logf("vk: Streamline: %s (result %d; sl.log in the game folder says more): DLSS Frame Generation does not go through "
         "it in this run",
         why, result);
}

/// This frame's token (made at its first marker when no sleep came before it). With s.mu held
sl::FrameToken* token() {
    State& s = st();
    if (!s.token && s.new_token) s.new_token(s.token, nullptr);
    return s.token;
}

/// Reflex's frame limit while DLSS-G makes frames: the frames shown held below the display's refresh, as NVIDIA's driver caps
/// them with G-SYNC (refresh - refresh^2 / 3600: 116 at 120 Hz, 138 at 144, 224 at 240), so the output stays inside the
/// variable refresh range. Streamline's limit is on what is presented, the made frames with the game's, so it is the
/// output's interval, not the game's. 0 (none) when DLSS-G is off, the refresh is not known, or the menu's V-Sync is Off:
/// NVIDIA's driver sets that cap only with V-Sync, and V-Sync Off is asked for more frames and less latency (at 60 Hz and
/// 2x the limit held the game at 29.5 fps, where main renders 60). Auto keeps it: at 60 Hz it is what kept the latency
/// at 27 ms, where main's FIFO queue reached 133 ms
uint32_t frame_limit_us(bool on) {
    const double hz = display_refresh_mhz() / 1000.0;
    if (!on || hz < 30 || menu_vsync() == 2) return 0;
    const double out = hz - hz * hz / 3600.0;
    return (uint32_t)(1000000.0 / out + 0.5);
}

/// Streamline's Reflex as the NVIDIA Reflex row has it, at least on (DLSS-G needs it), with the frame limit above. With s.mu
/// held
void apply_reflex(bool on, uint32_t frames) {
    State& s = st();
    const int want = std::max(1, std::clamp(opts.reflex, 0, 2));
    const uint32_t limit = frame_limit_us(on);
    if ((want == s.reflex_mode && limit == s.limit_us) || !s.reflex_set_options) return;
    sl::ReflexOptions o{};
    o.mode = want == 2 ? sl::ReflexMode::eLowLatencyWithBoost : sl::ReflexMode::eLowLatency;
    o.frameLimitUs = limit;
    if (SL_SUCCEEDED(r, s.reflex_set_options(o))) {
        if (limit)
            logf("vk: Streamline Reflex: %s, the frames shown held to %.1f fps (%.1f of the game's with DLSS Frame Generation %ux)",
                 want == 2 ? "on + boost" : "on", 1e6 / limit, 1e6 / limit / (frames + 1), frames + 1);
        else logf("vk: Streamline Reflex: %s, no frame limit", want == 2 ? "on + boost" : "on");
    } else {   // (not kept as set: asked again at the next sleep)
        static bool told;
        if (!told) told = true, logf("vk: Streamline Reflex: its options were refused (%d): asked again at each frame", (int)r);
        return;
    }
    s.reflex_mode = want, s.limit_us = limit;
}

/// DLSS-G's options: its mode and the frames it makes a set, its resources kept while it is off (the Insert menu turns it
/// off and on). With s.mu held
sl::Result set_dlssg(State& s, sl::DLSSGMode mode, uint32_t frames) {
    sl::DLSSGOptions o{};
    o.mode = mode;
    o.numFramesToGenerate = std::max(frames, 1u);
    o.flags = sl::DLSSGFlags::eRetainResourcesWhenOff;
    return s.dlssg_set_options(sl::ViewportHandle(0), o);
}

}  // namespace

PFN_vkGetInstanceProcAddr dlssg_load() {
    State& s = st();
    std::lock_guard lk(s.mu);
    if (s.tried) return nullptr;
    s.tried = true;
    run_switches(s.start), saved_rows(s.start);
    if (const char* e = getenv("RX_VK_DLSSG_RECT")) s.rect = !env_value_off(e);
    std::wstring exe(32768, L'\0');
    const DWORD n = GetModuleFileNameW(nullptr, exe.data(), (DWORD)exe.size());
    if (!n || n >= exe.size()) return nullptr;   // (no folder: never a relative path, which would load sl.interposer.dll from elsewhere)
    exe.resize(n);
    s.dir = exe.substr(0, exe.find_last_of(L"\\/") + 1);   // (beside longshot.exe: bin\ in a game folder with the launcher)
    s.game_dir = game_folder(s.dir);
    const std::wstring path = s.dir + L"sl.interposer.dll";
    s.could = GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES;
    // (the rows do not ask DLSS FG for frames, or Streamline's files are not there: as a build without Streamline, and
    // nothing said)
    if (!dlssg_rules::starts(s.start) || !s.could) return nullptr;
    s.could = false;   // (from here on, until it started: a start that failed is not offered again in this run)
    if (!nvidia_adapter()) return nullptr;   // (DLSS FG's rows on another GPU: the renderer's own frame generation, as before)
    if (!sl::security::verifyEmbeddedSignature(path.c_str())) {
        logf("vk: Streamline: sl.interposer.dll has no valid NVIDIA signature: not loaded");
        return nullptr;
    }
    s.module = LoadLibraryW(path.c_str());
    if (!s.module) {
        logf("vk: Streamline: sl.interposer.dll did not load (%lu)", GetLastError());
        return nullptr;
    }
    const auto get = [&](const char* name) { return (void*)GetProcAddress(s.module, name); };
    s.init = (PFun_slInit*)get("slInit");
    s.supported = (PFun_slIsFeatureSupported*)get("slIsFeatureSupported");
    s.set_tag = (PFun_slSetTagForFrame*)get("slSetTagForFrame");
    s.set_constants = (PFun_slSetConstants*)get("slSetConstants");
    s.feature_function = (PFun_slGetFeatureFunction*)get("slGetFeatureFunction");
    s.new_token = (PFun_slGetNewFrameToken*)get("slGetNewFrameToken");
    s.shutdown = (PFun_slShutdown*)get("slShutdown");
    const auto gipa = (PFN_vkGetInstanceProcAddr)get("vkGetInstanceProcAddr");
    if (!s.init || !s.supported || !s.set_tag || !s.set_constants || !s.feature_function || !s.new_token || !s.shutdown || !gipa) {
        logf("vk: Streamline: sl.interposer.dll lacks Streamline's functions: not used");
        FreeLibrary(s.module), s.module = nullptr;
        return nullptr;
    }
    static const sl::Feature features[] = {sl::kFeatureDLSS_G, sl::kFeatureReflex, sl::kFeaturePCL};
    // (the plugins: the first of these folders that has any, beside longshot.exe; none downloaded over the air is asked for,
    // though the NVIDIA app's DLSS override has Streamline look for NVIDIA's version of each all the same (sl_log). The
    // others are where DLSS's runtime is: sl.common hands NGX every folder here, and NGX's first start is Streamline's
    // (dlss_runtime_folders))
    s.paths = {s.dir};
    for (const std::wstring& f : dlss_runtime_folders(s.game_dir)) s.paths.push_back(f);
    s.path_list.clear();
    for (const std::wstring& p : s.paths) s.path_list.push_back(p.c_str());
    sl::Preferences pref{};
    pref.showConsole = false;
    pref.logLevel = sl::LogLevel::eDefault;
    pref.pathsToPlugins = s.path_list.data();
    pref.numPathsToPlugins = (uint32_t)s.path_list.size();
    pref.pathToLogsAndData = s.game_dir.c_str();
    pref.logMessageCallback = sl_log;
    pref.flags = sl::PreferenceFlags::eDisableCLStateTracking | sl::PreferenceFlags::eUseManualHooking |
                 sl::PreferenceFlags::eUseFrameBasedResourceTagging;
    pref.featuresToLoad = features;
    pref.numFeaturesToLoad = (uint32_t)(sizeof features / sizeof features[0]);
    pref.engine = sl::EngineType::eCustom;
    pref.engineVersion = "longshot";
    pref.projectId = "7b2f5d0c-6a1e-4c39-9f84-3d5e2a1b0c96";   // (vk_dlss.cpp's NGX project id)
    pref.renderAPI = sl::RenderAPI::eVulkan;
    if (SL_FAILED(r, s.init(pref, sl::kSDKVersion))) {
        logf("vk: Streamline did not start (result %d): DLSS Frame Generation as without it", (int)r);
        return nullptr;   // (the module stays loaded: Streamline may have hooked something already)
    }
    // (whether DLSS-G runs on this PC at all, its OS and its driver: Streamline's answer for any adapter. The GPU the
    // renderer chooses is asked about once it is known, dlssg_device)
    if (SL_FAILED(r, s.supported(sl::kFeatureDLSS_G, sl::AdapterInfo{}))) {
        logf("vk: Streamline: DLSS Frame Generation is not supported on this PC (result %d; sl.log in the game folder says "
             "more): as without Streamline",
             (int)r);
        s.shutdown();
        return nullptr;
    }
    s.upscaler.store(s.start.upscaler), s.loaded.store(true), s.active.store(true), s.could = true;
    logf("vk: Streamline started for DLSS Frame Generation (its loader in place of vulkan-1.dll)");
    return gipa;
}

bool dlssg_loaded() { return st().loaded.load(std::memory_order_relaxed); }
bool dlssg_active() { return st().active.load(std::memory_order_relaxed); }

const char* dlssg_instance_layer() {
    if (!dlssg_active() || env_switch("RX_VK_DLSSG_LAYER") == 0) return nullptr;
    return dlssg_layer_prepare();
}

void dlssg_device(VkPhysicalDevice physical) {
    State& s = st();
    std::lock_guard lk(s.mu);
    if (!s.active.load() || s.closing) return;
    sl::AdapterInfo adapter{};
    adapter.vkPhysicalDevice = physical;
    if (SL_FAILED(r, s.supported(sl::kFeatureDLSS_G, adapter))) give_up(s, "DLSS Frame Generation is not supported on the GPU chosen", (int)r);
}

void dlssg_device_made() {
    State& s = st();
    std::lock_guard lk(s.mu);
    if (!s.active.load() || s.closing || s.ready) return;
    if (!feature_function(sl::kFeatureDLSS_G, "slDLSSGSetOptions", s.dlssg_set_options) ||
        !feature_function(sl::kFeatureDLSS_G, "slDLSSGGetState", s.dlssg_get_state) ||
        !feature_function(sl::kFeatureReflex, "slReflexSetOptions", s.reflex_set_options) ||
        !feature_function(sl::kFeatureReflex, "slReflexSleep", s.reflex_sleep) ||
        !feature_function(sl::kFeaturePCL, "slPCLSetMarker", s.pcl_marker))
        return give_up(s, "its plugins did not load on this device", 0);
    sl::DLSSGState state{};
    uint32_t most = 1;
    if (SL_SUCCEEDED(r, s.dlssg_get_state(sl::ViewportHandle(0), state, nullptr))) most = std::max(1u, state.numFramesToGenerateMax);
    s.frames_max.store((int)most), s.ready = true;
    logf("vk: Streamline: DLSS Frame Generation ready (up to %ux)", most + 1);
}

int dlssg_frames_most() { return dlssg_active() ? st().frames_max.load(std::memory_order_relaxed) : 0; }
int dlssg_making() { return dlssg_active() ? st().making.load(std::memory_order_relaxed) : 0; }

int dlssg_upscaler(int row) {
    State& s = st();
    if (!s.loaded.load(std::memory_order_relaxed)) return row;
    if (dlssg_rules::upscaler_waits(s.start, row)) return s.upscaler.load(std::memory_order_relaxed);   // (until the restart)
    s.upscaler.store(row, std::memory_order_relaxed);
    return row;
}

int dlssg_restart_needed() {
    if (!dev.device) return 0;
    const State& s = st();
    const dlssg_rules::Run run = s.active.load() ? dlssg_rules::RUNNING : s.loaded.load() ? dlssg_rules::GAVE_UP : dlssg_rules::NOT_STARTED;
    // (not started: offered only where it would start, and DLSS FG runs here as far as NGX knows)
    const bool could = s.could && dev.props.vendorID == 0x10DE && dlss_fg_unavailable() == FG_SKIP_NONE;
    return dlssg_rules::restart_needed(run, could, rows_now(s)) ? 1 : 0;
}

void dlssg_marker(DlssgMarker m) {
    State& s = st();
    std::lock_guard lk(s.mu);
    if (!runs(s)) return;
    sl::FrameToken* t = token();
    if (!t) return;
    static const sl::PCLMarker map[] = {sl::PCLMarker::eSimulationStart, sl::PCLMarker::eSimulationEnd,
                                        sl::PCLMarker::eRenderSubmitStart, sl::PCLMarker::eRenderSubmitEnd,
                                        sl::PCLMarker::ePresentStart, sl::PCLMarker::ePresentEnd};
    s.pcl_marker(map[m], *t);
}

void dlssg_sleep() {
    State& s = st();
    sl::FrameToken* t = nullptr;
    {
        std::lock_guard lk(s.mu);
        if (!runs(s)) return;
        apply_reflex(s.mode == sl::DLSSGMode::eOn, s.frames);
        if (s.new_token(s.token, nullptr) != sl::Result::eOk) return;
        t = s.token;
        s.sleeping++;   // (dlssg_shutdown waits for it: Streamline is not shut down under its sleep)
    }
    s.reflex_sleep(*t);   // (outside the lock: the sleep waits)
    std::lock_guard lk(s.mu);
    s.sleeping--;
    if (!s.closing && s.token == t) s.pcl_marker(sl::PCLMarker::eSimulationStart, *t);
    s.slept.notify_all();
}

bool dlssg_shutdown() {
    State& s = st();
    if (!s.loaded.load()) return false;   // (nothing without Streamline: the close is vk_main.cpp's)
    dev.closing.store(true, std::memory_order_release);   // (as that close: no swapchain is made from here on)
    {
        std::unique_lock lk(s.mu);
        if (s.closing) return true;   // (once)
        s.closing = true;             // (no dlssg_* call goes to Streamline from here on)
        // (a Reflex sleep under way let the lock go: Streamline is not shut down under it. The present lock is not taken
        // before it ends: the sleeping thread presents next)
        if (!s.slept.wait_for(lk, kExitWait, [&] { return s.sleeping == 0; })) {
            logf("vk: Streamline: its Reflex sleep did not end within 2 s: left running for the exit");
            return true;
        }
    }
    // (then no present: the one under way ends first, and none starts again. A present stuck for 2 s, in the driver or in
    // Streamline's hooks, leaves Streamline as it is rather than shut it down under that thread or hang the exit)
    if (!lock_within(present_image_mu)) {
        logf("vk: Streamline: a present did not end within 2 s: left running for the exit");
        return true;
    }
    // (the parts handed to the recorder submitted first, as vk_dlss.cpp dlss_fg_close does: QueueMutex::try_lock does not
    // wait for them)
    ::mkwvk::rec::settle_parts(::mkwvk::rec::B_QUEUE_LOCK, 2000000000ull);
    if (!lock_within(dev.present_mu)) {   // (the queue's lock: submits, and device_wait_idle_locked's)
        present_image_mu.unlock();
        logf("vk: Streamline: the queue stayed busy for 2 s: left running for the exit");
        return true;
    }
    // (the frame's work done on every queue, Streamline's own copies of the tagged depth and motion among it, before DLSS-G
    // goes off and Streamline lets its resources go. No device: closed before it was made)
    if (dev.device) device_wait_idle_locked();
    std::lock_guard lk(s.mu);
    if (s.dlssg_set_options && s.mode != sl::DLSSGMode::eOff) {
        if (SL_FAILED(r, set_dlssg(s, sl::DLSSGMode::eOff, s.frames)))
            logf("vk: Streamline: DLSS Frame Generation not turned off for the exit (%d)", (int)r);
        s.mode = sl::DLSSGMode::eOff;
    }
    const sl::Result r = s.shutdown();
    logf("vk: Streamline shut down for the exit (result %d)", (int)r);
    // (present_image_mu and dev.present_mu stay held: the process ends next (host/hos_relaunch.c hos_window_closed_exit), and
    // a present or a submit after this would go through Streamline's hooks, or use what it let go)
    return true;
}

void dlssg_inputs(VkCommandBuffer cmd, const TemporalFrame& f, const float projection[16], const float view_projection[16],
                  const float clip_jitter[2], bool motion) {
    State& s = st();
    std::lock_guard lk(s.mu);
    if (!runs(s)) return;
    if (!dlssg_rules::asked(rows_now(s))) return void(s.have_last = false);   // (not asked: the next inputs start a new history)
    sl::FrameToken* t = token();
    if (!t || (uint32_t)*t == s.inputs_frame) return;   // (once a frame: constants twice in a frame confuse DLSS-G)
    const uint32_t rw = f.render_w, rh = f.render_h;
    if (!f.depth.image || !f.motion.image || !rw || !rh) return;
    float p[16], vp[16];
    std::memcpy(p, projection, sizeof p), std::memcpy(vp, view_projection, sizeof vp);
    dlss_math::unjitter(p, clip_jitter), dlss_math::unjitter(vp, clip_jitter);
    float ip[16], view[16], cam[16];
    if (!inverse(p, ip)) return;
    mul(ip, vp, view);   // (view = P^-1 * VP)
    if (!inverse(view, cam)) return;
    // (this clip to the frame before's, VP_prev * VP^-1, and back; none across a new history)
    const bool motion_ok = s.have_last && motion;
    float to_prev[16], from_prev[16];
    if (!motion_ok || !dlss_math::reprojection(vp, s.last_vp, to_prev)) std::memcpy(to_prev, kIdentity, sizeof kIdentity);
    if (!inverse(to_prev, from_prev)) return;
    std::memcpy(s.last_vp, vp, sizeof vp), s.have_last = true;
    // (Streamline's matrices are row major for row vectors, position * M: the game's, transposed)
    const auto put = [](sl::float4x4& out, const float* m) {
        for (int r = 0; r < 4; r++) out.row[r] = sl::float4(m[0 * 4 + r], m[1 * 4 + r], m[2 * 4 + r], m[3 * 4 + r]);
    };
    sl::Constants c{};
    put(c.cameraViewToClip, p);
    put(c.clipToCameraView, ip);
    put(c.clipToLensClip, kIdentity);
    put(c.clipToPrevClip, to_prev);
    put(c.prevClipToClip, from_prev);
    c.jitterOffset = sl::float2(f.jitter[0], f.jitter[1]);
    c.mvecScale = sl::float2(1.0f / (float)rw, 1.0f / (float)rh);   // (render pixels to the [-1, 1] Streamline takes)
    c.cameraPinholeOffset = sl::float2(0.0f, 0.0f);
    // (the camera's frame in the world: its view's inverse; OpenGL's, looking down -z)
    c.cameraPos = sl::float3(cam[3], cam[7], cam[11]);
    c.cameraRight = sl::float3(cam[0], cam[4], cam[8]);
    c.cameraUp = sl::float3(cam[1], cam[5], cam[9]);
    c.cameraFwd = sl::float3(-cam[2], -cam[6], -cam[10]);
    c.cameraNear = f.near_z > 0 ? f.near_z : 0.1f;   // (the chain's, from the scene's projection)
    c.cameraFar = f.far_z > c.cameraNear ? f.far_z : 100000.0f;
    c.cameraFOV = f.fov_y > 0 ? f.fov_y : 1.0f;
    c.cameraAspectRatio = p[0] != 0 ? std::fabs(p[5] / p[0]) : 16.0f / 9.0f;
    // (motionVectorsInvalidValue left at Streamline's INVALID_FLOAT, no invalid value: the chain's motion has none, and 0
    // marked every pixel that did not move, all of a still scene under a still camera, as invalid)
    c.depthInverted = sl::Boolean::eFalse;
    c.cameraMotionIncluded = sl::Boolean::eTrue;
    c.motionVectors3D = sl::Boolean::eFalse;
    c.reset = motion_ok ? sl::Boolean::eFalse : sl::Boolean::eTrue;
    c.orthographicProjection = sl::Boolean::eFalse;
    c.motionVectorsDilated = sl::Boolean::eFalse;
    c.motionVectorsJittered = sl::Boolean::eFalse;
    const sl::ViewportHandle viewport(0);
    if (SL_FAILED(r, s.set_constants(c, *t, viewport))) {
        static bool told;
        if (!told) told = true, logf("vk: Streamline: its constants were refused (%d)", (int)r);
        return;
    }
    // (the chain's images, as large as its engine asked: the frame's part at their top left, the extent)
    sl::Resource d(sl::ResourceType::eTex2d, (void*)f.depth.image, nullptr, (void*)f.depth.view, VK_IMAGE_LAYOUT_GENERAL);
    d.width = f.depth.width, d.height = f.depth.height, d.nativeFormat = f.depth.format, d.mipLevels = 1, d.arrayLayers = 1;
    d.flags = 0;
    d.usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    sl::Resource mv(sl::ResourceType::eTex2d, (void*)f.motion.image, nullptr, (void*)f.motion.view, VK_IMAGE_LAYOUT_GENERAL);
    mv.width = f.motion.width, mv.height = f.motion.height, mv.nativeFormat = f.motion.format, mv.mipLevels = 1;
    mv.arrayLayers = 1, mv.flags = 0;
    mv.usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
               VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    const sl::Extent extent{0, 0, rw, rh};
    const sl::ResourceLifecycle now = sl::ResourceLifecycle::eOnlyValidNow;
    // (where the picture is in the swapchain image, the last present's: kBufferTypeBackbuffer with no resource, its extent
    // alone (Streamline's DLSS-G guide, 5.2; sl::Extent is top, left, w, h). Without it DLSS-G takes the whole image: with
    // bars beside a 16:9 picture, the inputs stretched over all of it. Told only when the picture is not the whole image)
    const sl::Extent bb{s.bb[1], s.bb[0], s.bb[2], s.bb[3]};
    const bool sub = s.rect && bb.width && bb.height && (bb.width != s.bb_full[0] || bb.height != s.bb_full[1]);
    const sl::ResourceTag tags[] = {sl::ResourceTag(&d, sl::kBufferTypeDepth, now, &extent),
                                    sl::ResourceTag(&mv, sl::kBufferTypeMotionVectors, now, &extent),
                                    sl::ResourceTag(nullptr, sl::kBufferTypeBackbuffer, sl::ResourceLifecycle{}, &bb)};
    if (SL_FAILED(r, s.set_tag(*t, viewport, tags, sub ? 3 : 2, reinterpret_cast<sl::CommandBuffer*>(cmd)))) {
        static bool told;
        if (!told) told = true, logf("vk: Streamline: its inputs were refused (%d)", (int)r);
        return;
    }
    s.inputs_frame = (uint32_t)*t;
    static bool told;
    if (!told) {
        told = true;
        logf("vk: Streamline DLSS Frame Generation: inputs %ux%u (near %.3f far %.1f fov %.1f deg)", rw, rh, c.cameraNear,
             c.cameraFar, c.cameraFOV * 57.29578f);
    }
}

void dlssg_backbuffer(uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint32_t full_w, uint32_t full_h) {
    State& s = st();
    std::lock_guard lk(s.mu);
    s.bb[0] = x, s.bb[1] = y, s.bb[2] = w, s.bb[3] = h, s.bb_full[0] = full_w, s.bb_full[1] = full_h;
}

void dlssg_before_present(bool game_frame) {
    const bool engine = !game_frame || temporal_wanted();   // (the engines' answers, read before s.mu is taken)
    State& s = st();
    std::lock_guard lk(s.mu);
    if (!runs(s)) return;
    sl::FrameToken* t = token();
    const bool inputs = t && (uint32_t)*t == s.inputs_frame;
    // (a frame now and then without inputs (a loading hitch, the game's upscale left out once) keeps it on: each switch off and
    // on again stopped the interpolation for seconds and changed the present's sync. Off at once only for the Insert menu and
    // the window's own presents; after 60 game frames without inputs too)
    if (inputs) s.missing = 0;
    else if (game_frame) s.missing++;
    const uint32_t want = (uint32_t)dlssg_rules::frames(rows_now(s), s.frames_max.load(std::memory_order_relaxed));
    const bool menu = menu_is_open();
    // (the rows ask DLSS FG for frames from a temporal Upscaler whose engine does not run here: its runtime or DLL missing,
    // NGX's answer, FSR 3.1 off the chain. DLSS-G would have no inputs for the session; given up, the renderer's own frame
    // generation makes them, as without Streamline. On a game frame with the menu closed: under it the row may be changing)
    if (want && game_frame && !menu && !engine) {
        if (s.mode != sl::DLSSGMode::eOff) (void)set_dlssg(s, sl::DLSSGMode::eOff, s.frames);
        s.mode = sl::DLSSGMode::eOff;
        apply_reflex(false, s.frames);   // (Reflex's frame limit off: the renderer's own Reflex takes over)
        return give_up(s, "the Upscaler row's engine does not run here, so DLSS-G has nothing to make frames from", 0);
    }
    const char* why_off = !want ? "the Frame generation rows do not ask for it" : menu ? "the Insert menu is open"
                          : !game_frame ? "no game frame" : s.missing > 60 ? "no inputs for 60 frames" : nullptr;
    const bool was_on = s.mode == sl::DLSSGMode::eOn;
    const bool on = !why_off && (inputs || was_on);
    const sl::DLSSGMode mode = on ? sl::DLSSGMode::eOn : sl::DLSSGMode::eOff;
    // (the menu's lines: DLSS FG while it makes frames, and under the open menu when it made them before; paused when it has
    // nothing to make them from; nobody when the rows do not ask)
    if (!want) s.making.store(0, std::memory_order_relaxed);
    else if (on) s.making.store(1, std::memory_order_relaxed);
    else if (!menu) s.making.store(2, std::memory_order_relaxed);
    if (was_on) {
        // (the FPS counter counts the renderer's own presents, one a game frame: the frames DLSS-G presented besides them are
        // added, from its own count of what it presented since the last ask)
        sl::DLSSGState state{};
        if (SL_SUCCEEDED(r, s.dlssg_get_state(sl::ViewportHandle(0), state, nullptr))) {
            s.presented += state.numFramesActuallyPresented, s.own++;
            const int64_t extra = s.presented - s.own - s.noted;
            if (extra > 0) menu_note_frame(false, (int)extra), s.noted += extra;
            else if (extra < 0) s.own += extra;   // (fewer than its own presented, a frame it dropped: no debt carried on)
            if ((uint32_t)state.status != s.status && s.status_told < 16) {   // (DLSS-G's own word on why it makes none)
                s.status = (uint32_t)state.status, s.status_told++;
                logf("vk: Streamline DLSS Frame Generation: status %#x", s.status);
            }
        }
    }
    if (mode == s.mode && (!on || want == s.frames)) return;
    if (SL_FAILED(r, set_dlssg(s, mode, want))) {
        static bool told;
        if (!told) told = true, logf("vk: Streamline: DLSS Frame Generation's options were refused (%d)", (int)r);
        return;
    }
    if (on) logf("vk: Streamline DLSS Frame Generation: on, %ux", want + 1);
    else logf("vk: Streamline DLSS Frame Generation: off (%s)", why_off ? why_off : "no inputs yet");
    s.mode = mode, s.frames = std::max(want, 1u);
}

namespace {

int64_t hold_now() {   // (vk_replay.cpp bh_now's clock, the one game_at is on)
    return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
int64_t counter_now() {
    LARGE_INTEGER q;
    QueryPerformanceCounter(&q);
    return q.QuadPart;
}
/// Until `at`: a high-resolution timer to half a millisecond before it (vk_framegen.cpp wait_until_near's), then a spin
void hold_until(int64_t at) {
    static const HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    const int64_t sleep = at - hold_now() - 500000;
    if (sleep > 0) {
        LARGE_INTEGER d;
        d.QuadPart = -std::max<int64_t>(1, sleep / 100);   // (relative, 100 ns units)
        if (timer && SetWaitableTimerEx(timer, &d, 0, nullptr, nullptr, nullptr, 0)) WaitForSingleObject(timer, INFINITE);
        else std::this_thread::sleep_for(std::chrono::nanoseconds(sleep));
    }
    while (hold_now() < at) YieldProcessor();
}

/// A pace switch (dlssg_pace.h): its microseconds, 0 off; its default unset, or when it is not a number from 0 to most.
/// given: whether it gave its own value
int64_t pace_switch(const char* name, int64_t def, int64_t most, bool* given = nullptr) {
    const char* e = getenv(name);
    if (given) *given = false;
    if (!e || !*e) return def;
    char* end = nullptr;
    const long long v = strtoll(e, &end, 10);
    if (end == e || *end || v < 0 || v > most) {
        logf("vk: %s=%s is not a number of microseconds from 0 to %lld: its default, %lld", name, e, (long long)most, (long long)def);
        return def;
    }
    if (given) *given = true;
    return v;
}

/// The pace as the run has it: the hold and the grid (the render thread's state), and the toggle's hold in nanoseconds
/// (written at a swapchain when it comes from the screen, read by the render thread)
struct Pace {
    dlssg_pace::Hold hold;
    std::atomic<int64_t> toggle{0};
    bool from_screen = false;   // (RX_VK_DLSSG_TOGGLE unset or not valid, its default: the hold from the screen's VRR floor)
    int64_t toggled = 0;        // (when the last toggle's hold ended)
};
Pace& pace() {
    static Pace& p = *[] {
        auto* n = new Pace;
        n->hold.hold = pace_switch("RX_VK_DLSSG_HOLD", dlssg_pace::kHoldUs, 16000) * 1000;
        n->hold.grid_max = pace_switch("RX_VK_DLSSG_HOLD_GRID", dlssg_pace::kGridUs, 8000) * 1000;
        bool given = false;
        n->toggle = pace_switch("RX_VK_DLSSG_TOGGLE", dlssg_pace::kToggleUs, 50000, &given) * 1000;
        n->from_screen = !given;
        logf("vk: Streamline DLSS Frame Generation: a game frame's present held %.1f ms after the game's own (within %.1f ms "
             "of the line through its presents: from the line), and once for %.1f ms%s when the driver shows the frames unevenly",
             n->hold.hold / 1e6, n->hold.grid_max / 1e6, n->toggle / 1e6, n->from_screen ? " (until the screen's VRR floor is read)" : "");
        return n;
    }();
    return p;
}

/// The game window's screen: whether it is on HDMI, its name and its EDID, from Windows' display paths and the monitor's
/// device node (documented calls, no rights asked; cfgmgr32's loaded here, the DLL links user32 alone). False with why
bool window_edid(bool& hdmi, std::string& name, std::vector<uint8_t>& edid, const char*& why) {
    why = "the game's window has no screen yet";
    MONITORINFOEXW mi{};
    mi.cbSize = sizeof mi;
    const HWND wnd = app_window();
    if (!wnd || !GetMonitorInfoW(MonitorFromWindow(wnd, MONITOR_DEFAULTTONEAREST), &mi)) return false;
    why = "Windows did not give the screen's display path";
    std::vector<DISPLAYCONFIG_PATH_INFO> paths;
    std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    LONG r = ERROR_INSUFFICIENT_BUFFER;
    for (int tries = 0; r == ERROR_INSUFFICIENT_BUFFER && tries < 4; tries++) {   // (the displays can change in between)
        UINT32 np = 0, nm = 0;
        if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &np, &nm) != ERROR_SUCCESS) return false;
        paths.resize(np), modes.resize(nm);
        r = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &np, paths.data(), &nm, modes.data(), nullptr);
        paths.resize(np), modes.resize(nm);
    }
    if (r != ERROR_SUCCESS) return false;
    for (const DISPLAYCONFIG_PATH_INFO& p : paths) {
        DISPLAYCONFIG_SOURCE_DEVICE_NAME source{};
        source.header = {DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME, sizeof source, p.sourceInfo.adapterId, p.sourceInfo.id};
        if (DisplayConfigGetDeviceInfo(&source.header) != ERROR_SUCCESS || wcscmp(source.viewGdiDeviceName, mi.szDevice)) continue;
        DISPLAYCONFIG_TARGET_DEVICE_NAME target{};
        target.header = {DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME, sizeof target, p.targetInfo.adapterId, p.targetInfo.id};
        if (DisplayConfigGetDeviceInfo(&target.header) != ERROR_SUCCESS) return false;
        hdmi = target.outputTechnology == DISPLAYCONFIG_OUTPUT_TECHNOLOGY_HDMI;
        char utf8[256] = "";
        WideCharToMultiByte(CP_UTF8, 0, target.monitorFriendlyDeviceName[0] ? target.monitorFriendlyDeviceName : mi.szDevice, -1,
                            utf8, sizeof utf8 - 1, nullptr, nullptr);
        name = utf8;
        // (the monitor's interface, its device instance, that node's hardware key, its EDID value)
        why = "the screen's EDID was not read";
        static const HMODULE cfg = LoadLibraryExW(L"cfgmgr32.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
        static const HMODULE adv = LoadLibraryExW(L"advapi32.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (!cfg || !adv) return false;
        const auto get_prop = (decltype(&CM_Get_Device_Interface_PropertyW))(void*)GetProcAddress(cfg, "CM_Get_Device_Interface_PropertyW");
        const auto locate = (decltype(&CM_Locate_DevNodeW))(void*)GetProcAddress(cfg, "CM_Locate_DevNodeW");
        const auto open_key = (decltype(&CM_Open_DevNode_Key))(void*)GetProcAddress(cfg, "CM_Open_DevNode_Key");
        const auto query = (decltype(&RegQueryValueExW))(void*)GetProcAddress(adv, "RegQueryValueExW");
        const auto close = (decltype(&RegCloseKey))(void*)GetProcAddress(adv, "RegCloseKey");
        if (!get_prop || !locate || !open_key || !query || !close) return false;
        const DEVPROPKEY instance_id = {{0x78c34fc8, 0x104a, 0x4aca, {0x9e, 0xa4, 0x52, 0x4d, 0x52, 0x99, 0x6e, 0x57}}, 256};   // (DEVPKEY_Device_InstanceId)
        wchar_t instance[512] = L"";
        DEVPROPTYPE type = 0;
        ULONG size = sizeof instance;
        DEVINST node = 0;
        HKEY key = nullptr;
        if (get_prop(target.monitorDevicePath, &instance_id, &type, (PBYTE)instance, &size, 0) != CR_SUCCESS ||
            locate(&node, instance, CM_LOCATE_DEVNODE_NORMAL) != CR_SUCCESS ||
            open_key(node, KEY_READ, 0, RegDisposition_OpenExisting, &key, CM_REGISTRY_HARDWARE) != CR_SUCCESS)
            return false;
        edid.assign(4096, 0);
        DWORD bytes = (DWORD)edid.size();
        const LSTATUS read = query(key, L"EDID", nullptr, nullptr, edid.data(), &bytes);
        close(key);
        if (read != ERROR_SUCCESS || bytes < 128) return false;
        edid.resize(bytes);
        return true;
    }
    return false;
}

/// A screen's VRR range as the log says it: "48-240 Hz", or "40 Hz up to the video mode's" with VRRmax 0
std::string vrr_range(const edid_vrr::Range& v) {
    char b[64];
    if (v.max_hz) snprintf(b, sizeof b, "%d-%d Hz", v.min_hz, v.max_hz);
    else snprintf(b, sizeof b, "%d Hz up to the video mode's", v.min_hz);
    return b;
}

}  // namespace

bool dlssg_toggles() { return pace().toggle > 0; }   // (0 only with RX_VK_DLSSG_TOGGLE=0: the screen's hold is never 0)

void dlssg_swapchain_made() {
    if (!dlssg_active()) return;   // (given up: nothing is held, and the log says nothing of a hold)
    Pace& p = pace();
    if (!p.from_screen) return;
    bool hdmi = false;
    std::string name;
    std::vector<uint8_t> edid;
    const char* why = "";
    int64_t us = 0;
    char said[160] = "";
    if (!window_edid(hdmi, name, edid, why)) {
        snprintf(said, sizeof said, "the default: %s", why);
    } else if (!hdmi) {
        snprintf(said, sizeof said, "the default: %s is not on HDMI (DisplayPort's VRR range is not read)", name.c_str());
    } else if (const edid_vrr::Range v = edid_vrr::hdmi_forum(edid.data(), edid.size()); !v.min_hz) {
        snprintf(said, sizeof said, "the default: %s on HDMI, its EDID gives no HDMI VRR range (VRR off in the screen's menu?)",
                 name.c_str());
    } else if (!(us = dlssg_pace::toggle_for_floor(v.min_hz))) {
        snprintf(said, sizeof said, "the default: %s on HDMI, VRR %s, a floor outside %d to %d Hz, the rule's", name.c_str(),
                 vrr_range(v).c_str(), dlssg_pace::kFloorLowHz, dlssg_pace::kFloorHighHz);
    } else {
        snprintf(said, sizeof said, "from %s's VRR floor (HDMI, %s in its EDID)", name.c_str(), vrr_range(v).c_str());
    }
    if (!us) us = dlssg_pace::kToggleUs;
    p.toggle = us * 1000;
    static std::string told;   // (said again only when it changes: the screen, its range, its mode)
    char line[256];
    snprintf(line, sizeof line, "%.1f ms, %s", us / 1000.0, said);
    if (told != line) {
        told = line;
        logf("vk: Streamline DLSS Frame Generation: the toggle's hold %s", line);
    }
}

void dlssg_hold(int64_t game_at, int64_t game_wait) {
    State& s = st();
    uint32_t frames = 0;
    {
        std::lock_guard lk(s.mu);
        if (!runs(s) || s.mode != sl::DLSSGMode::eOn) return;
        frames = s.frames;
    }
    Pace& p = pace();   // (the render thread's: one present at a time, present_image_mu)
    // (the toggle first: the layer read the uneven law. One hold of the kind a second at most, and only where it fits; an ask
    // let go is told to the layer, so it does not count against its bounds)
    if (p.toggle && dlssg_layer_toggle_wanted()) {
        const int64_t now = hold_now(), call = counter_now();
        if (p.hold.toggle_fits(frames) && (!p.toggled || now - p.toggled >= 1000000000)) {
            hold_until(now + p.toggle);
            p.toggled = hold_now();
            dlssg_layer_held(call, counter_now(), true);
        } else {
            dlssg_layer_held(call, call, false);
        }
    }
    if (const int64_t target = p.hold.target(game_at, game_wait, hold_now())) hold_until(target);
}

}  // namespace mkwvk

#else  // (no Streamline SDK in this build: nothing of it is there, and nothing is said)

namespace mkwvk {

PFN_vkGetInstanceProcAddr dlssg_load() { return nullptr; }
bool dlssg_loaded() { return false; }
bool dlssg_active() { return false; }
const char* dlssg_instance_layer() { return nullptr; }
void dlssg_device(VkPhysicalDevice) {}
void dlssg_device_made() {}
int dlssg_frames_most() { return 0; }
int dlssg_making() { return 0; }
int dlssg_upscaler(int row) { return row; }
int dlssg_restart_needed() { return 0; }
bool dlssg_toggles() { return false; }
void dlssg_swapchain_made() {}
void dlssg_marker(DlssgMarker) {}
void dlssg_sleep() {}
void dlssg_inputs(VkCommandBuffer, const TemporalFrame&, const float*, const float*, const float*, bool) {}
void dlssg_backbuffer(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t) {}
void dlssg_hold(int64_t, int64_t) {}
void dlssg_before_present(bool) {}
bool dlssg_shutdown() { return false; }

}  // namespace mkwvk

#endif
