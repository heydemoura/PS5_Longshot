"""AMD Anti-Lag 2 (gpu/vk/vk_antilag.cpp): compiles its actual frame calls (plus menu_antilag_changed/menu_antilag from
gpu/vk/menu/framegen.cpp) against the Vulkan headers with a recording vkAntiLagUpdateAMD, checks that:
- unavailable devices (!ready) skip locking a.mu on antilag_frame/present/input and at present_out hand_off,
- a.mu is not held across vkAntiLagUpdateAMD (INPUT, PRESENT, OFF),
- turning Anti-Lag 2 off while INPUT or PRESENT is in flight defers MODE_OFF_AMD until in-flight calls finish,
- a paused MODE_OFF_AMD cannot be overtaken by a concurrent On transition (want=1 waits until Off finishes),
- concurrent menu_antilag_changed() updates synchronize cleanly with presentation/input threads, and
- device setup, present_out hand_off -> presenter_loop, present_commit, and frame generation wire it properly.
No GPU or driver required.

python -I -B tests/test_antilag.py --headers <Vulkan-Headers/include> [--cxx g++]
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import block  # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}", "--headers", "{vulkan_headers}"]

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--headers", type=Path, required=True)
p.add_argument("--cxx", default="g++")
a = p.parse_args()
source = (ROOT / "gpu" / "vk" / "vk_antilag.cpp").read_text(encoding="utf-8")


def present_image_block(text):
    return block(text, "void present_image_with_trial(VkImage src, uint32_t sw, uint32_t sh, const int crop[4], VkImageView view,\n                              bool source_srgb, bool allow_scale, OutputTrialSource trial, VkFormat framegen_format) {")


def cut_def(text, prefix):
    """Function definition starting with prefix (skips forward declarations ending in ';')"""
    pos = 0
    while True:
        at = text.find(prefix, pos)
        if at < 0:
            raise SystemExit("missing definition starting with %r" % prefix)
        nl = text.find("\n", at)
        line = text[at:nl if nl >= 0 else len(text)]
        if "{" in line:
            return block(text, line)
        pos = at + len(prefix)


errors = []

# the wiring: the device asks for the extension and its feature, the present gives the present stage right before
# vkQueuePresentKHR (on presenter_loop after presenter_wait_copy for hand_off; directly in present_out for
# present_commit), and the input stage comes after the present (with Reflex's sleep)
main = (ROOT / "gpu" / "vk" / "vk_main.cpp").read_text(encoding="utf-8")
for call in ("antilag_device_extensions(want, exts);", "antilag_device_features(tail);", "antilag_device_created();"):
    if call not in main:
        errors.append("vk_main.cpp no longer calls " + call)
if main.index("antilag_device_features(tail);") > main.index("vkGetPhysicalDeviceFeatures2(dev.physical, &f);"):
    errors.append("vk_main.cpp: Anti-Lag 2's feature joins the chain after the features were queried")

# atomic menu option publication: wanted() reads menu_antilag(), published from menu/framegen.cpp and vk_menu.cpp
wanted_fn = block(source, "bool wanted()")
if "menu_antilag()" not in wanted_fn or "opts.antilag" in wanted_fn:
    errors.append("vk_antilag.cpp: wanted() must read atomic menu_antilag() instead of plain opts.antilag")
menu_fg = (ROOT / "gpu" / "vk" / "menu" / "framegen.cpp").read_text(encoding="utf-8")
if "std::atomic<int> live_antilag" not in menu_fg or ".on_change = MENU_HOOK(menu_antilag_changed)" not in menu_fg:
    errors.append("gpu/vk/menu/framegen.cpp: AMD Anti-Lag 2 row does not publish opts.antilag atomically via menu_antilag_changed")
menu_cpp = (ROOT / "gpu" / "vk" / "vk_menu.cpp").read_text(encoding="utf-8")
if "menu_antilag_changed(opts.antilag);" not in block(menu_cpp, "void menu_init() {"):
    errors.append("vk_menu.cpp: menu_init() does not publish opts.antilag via menu_antilag_changed()")
if "menu_antilag_changed(opts.antilag);" not in block(menu_cpp, "void menu_tick() {"):
    errors.append("vk_menu.cpp: menu_tick() does not publish opts.antilag via menu_antilag_changed()")

replay = (ROOT / "gpu" / "vk" / "vk_replay.cpp").read_text(encoding="utf-8")
# (present ahead, #1720: present_image's present is its end, present_out, which present_commit also runs)
present = block(replay, "static bool present_out(uint32_t index, std::unique_lock<decltype(dev.present_mu)>& qk, OutputTrialSource trial, bool preview_path) {")
hand = block(present, "if (hand_off) {")
direct = present[present.index(hand) + len(hand):]
if "antilag_present(" in hand:
    errors.append("present_out: hand_off calls antilag_present() before handing off to presenter_loop")
if "antilag_id" not in hand or "antilag_frame()" not in hand or "present_hand_off(" not in hand:
    errors.append("present_out: hand_off does not snapshot antilag_id and pass it into present_hand_off()")
if "antilag_available()" not in hand or hand.index("antilag_available()") > hand.index("antilag_frame()"):
    errors.append("present_out: hand_off does not gate antilag_frame() on antilag_available() to skip lock when unavailable")
if "antilag_present();" not in direct or direct.index("antilag_present();") > direct.index("vkQueuePresentKHR("):
    errors.append("present_out: direct path does not give Anti-Lag 2's present stage before vkQueuePresentKHR")

presenter_struct = block(replay, "struct Presenter {")
if "antilag_id" not in presenter_struct:
    errors.append("Presenter: does not hold antilag_id alongside reflex_id")
hand_off_fn = cut_def(replay, "void present_hand_off(")
if "presenter.antilag_id = antilag_id;" not in hand_off_fn:
    errors.append("present_hand_off: does not store antilag_id into presenter")
loop = block(replay, "void presenter_loop() {")
if "presenter.antilag_id" not in loop or loop.index("presenter.antilag_id") > loop.index("lk.unlock();"):
    errors.append("presenter_loop: does not read presenter.antilag_id under presenter.mu")
if "antilag_present();" not in loop:
    errors.append("presenter_loop: does not call antilag_present() for the handed-off antilag_id")
else:
    if "antilag_present_frame = antilag_id;" not in loop or "antilag_present_frame = kAntiLagThisFrame;" not in loop:
        errors.append("presenter_loop: does not set and restore antilag_present_frame around antilag_present()")
    if not (loop.index("presenter_wait_copy(done)") < loop.index("antilag_present();") < loop.index("vkQueuePresentKHR(")):
        errors.append("presenter_loop: antilag_present() is not called after presenter_wait_copy(done) and before vkQueuePresentKHR")

if "reflex_sleep(); antilag_input();" not in block(replay, "void do_present("):
    errors.append("do_present: Anti-Lag 2's input stage is not given after the present")
# present ahead: the deferred present keeps the frame it shows until its commit, as Reflex's
if "present_defer_into->antilag_frame = antilag_present_frame;" not in present_image_block(replay):
    errors.append("present_image: a deferred present does not keep the frame it shows")
commit = block(replay, "bool present_commit(")
if "antilag_present_frame = p.antilag_frame;" not in commit or "antilag_present_frame = al_was;" not in commit:
    errors.append("present_commit: the deferred present's frame is not the one its present stage gives")
# frame generation: the set keeps its game frame, and only the set's game frame gives its present stage (both paths)
framegen = (ROOT / "gpu" / "vk" / "vk_framegen.cpp").read_text(encoding="utf-8")
if "set->antilag_id = antilag_frame();" not in framegen:
    errors.append("framegen_record: the set does not keep its game frame")
pick = "antilag_present_frame = (timed ? j.set->game[f] : f == j.set->made) ? j.set->antilag_id : 0;"
if block(framegen, "void display_main()").count(pick) != 2:
    errors.append("display_main: a set's presents (direct and present ahead) do not say which frame they show")

menu_atomic_code = "\n".join((
    "std::atomic<int> live_antilag{0};",
    block(menu_fg, "void menu_antilag_changed(int on)"),
    block(menu_fg, "int menu_antilag()"),
))
markers = ["struct AntiLag {", "AntiLag& al()", "bool wanted()", "void update(", "bool on("]
for opt in ("bool take_off()", "void send_off()"):
    if opt in source:
        markers.append(opt)
markers += ["void stage(", "bool antilag_available()", "uint64_t antilag_frame()", "void antilag_present()", "void antilag_input()"]
functions = "\n".join(block(source, m) + (";" if m.startswith("struct") else "") for m in markers)

fixture = r'''
#include <vulkan/vulkan_core.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <thread>
#include <vector>
struct {VkDevice device=VK_NULL_HANDLE;} dev;
constexpr uint64_t kAntiLagThisFrame=~0ull;
thread_local uint64_t antilag_present_frame=kAntiLagThisFrame;
template<class... T> void logf(const char*,T...){}
struct Call{VkAntiLagModeAMD mode;int stage;uint64_t frame;};
std::mutex calls_mu;
std::vector<Call> calls;

// Synchronized hook to block inside one specific vkAntiLagUpdateAMD call while a second thread calls Anti-Lag 2.
struct BlockHook {
    std::mutex mu;
    std::condition_variable cv;
    bool armed = false;
    int match_stage = -2;   // -1 matches mode Off (pPresentationInfo == nullptr)
    bool entered = false;
    bool release = false;
    bool released_in_time = false;
} hook;

void arm_hook(int match_stage) {
    std::lock_guard lk(hook.mu);
    hook.armed = true;
    hook.match_stage = match_stage;
    hook.entered = false;
    hook.release = false;
    hook.released_in_time = false;
}

void VKAPI_CALL record(VkDevice,const VkAntiLagDataAMD* d){
    const int st = d->pPresentationInfo ? (int)d->pPresentationInfo->stage : -1;
    const uint64_t fr = d->pPresentationInfo ? d->pPresentationInfo->frameIndex : 0;
    {
        std::lock_guard lk(calls_mu);
        calls.push_back({d->mode, st, fr});
    }
    std::unique_lock lk(hook.mu);
    if (hook.armed && st == hook.match_stage) {
        hook.armed = false;
        hook.entered = true;
        hook.cv.notify_all();
        hook.released_in_time = hook.cv.wait_for(lk, std::chrono::milliseconds(400), [] { return hook.release; });
    }
}
''' + menu_atomic_code + "\n" + functions + r'''
#define CHECK(c) do{if(!(c)){std::printf("FAIL line %d: %s\n",__LINE__,#c);return 1;}}while(0)
int main(){
    menu_antilag_changed(1);
    // not available: nothing is ever called AND a.mu is never locked (NVIDIA / Steam Deck zero-lock fast path)
    {
        CHECK(!antilag_available());
        std::mutex done_mu;
        std::condition_variable done_cv;
        bool done = false;
        std::unique_lock hold(al().mu);
        std::thread probe([&] {
            if (antilag_frame() == 0) {
                antilag_input();
                antilag_present();
                std::lock_guard g(done_mu);
                done = true;
                done_cv.notify_all();
            }
        });
        bool ok = false;
        {
            std::unique_lock g(done_mu);
            ok = done_cv.wait_for(g, std::chrono::milliseconds(400), [&] { return done; });
        }
        hold.unlock();
        probe.join();
        if (!ok) {
            std::puts("FAIL: unavailable Anti-Lag 2 attempted to lock a.mu");
            return 1;
        }
        CHECK(calls.empty() && antilag_frame() == 0);
    }
    al().update=record;
    al().ready=true;
    CHECK(antilag_available());
    // before the first input stage: no frame, and a present gives nothing
    CHECK(antilag_frame()==0);antilag_present();CHECK(calls.empty());
    // a frame without frame generation: its input stage, then its present stage, with one index, mode On
    antilag_input();uint64_t f=antilag_frame();CHECK(f!=0);antilag_present();
    CHECK(calls.size()==2&&calls[0].stage==VK_ANTI_LAG_STAGE_INPUT_AMD&&calls[1].stage==VK_ANTI_LAG_STAGE_PRESENT_AMD);
    CHECK(calls[0].frame==f&&calls[1].frame==f&&calls[0].mode==VK_ANTI_LAG_MODE_ON_AMD&&calls[1].mode==VK_ANTI_LAG_MODE_ON_AMD);
    // frame generation: the set's game frame is shown after the game's next input stage, with its own frame's index
    calls.clear();antilag_input();const uint64_t g=antilag_frame();antilag_input();
    antilag_present_frame=0;antilag_present();CHECK(calls.size()==2);   // (a made frame before it: nothing)
    antilag_present_frame=g;antilag_present();
    CHECK(calls.size()==3&&calls[1].frame==g+1&&calls[2].stage==VK_ANTI_LAG_STAGE_PRESENT_AMD&&calls[2].frame==g);
    antilag_present_frame=kAntiLagThisFrame;
    // concurrency 1: antilag_input() must NOT hold a.mu across vkAntiLagUpdateAMD(INPUT), so a concurrent
    // antilag_present() for the previous game frame completes while INPUT is still sleeping in the driver
    {
        calls.clear();
        const uint64_t prev_f = antilag_frame();
        CHECK(prev_f != 0);
        arm_hook(VK_ANTI_LAG_STAGE_INPUT_AMD);
        std::thread presenter([prev_f] {
            {
                std::unique_lock lk(hook.mu);
                hook.cv.wait_for(lk, std::chrono::milliseconds(400), [] { return hook.entered; });
            }
            antilag_present_frame = prev_f;
            antilag_present();
            antilag_present_frame = kAntiLagThisFrame;
            std::lock_guard lk(hook.mu);
            hook.release = true;
            hook.cv.notify_all();
        });
        antilag_input();
        presenter.join();
        if (!hook.released_in_time) {
            std::puts("FAIL: antilag_present() blocked while antilag_input() was inside vkAntiLagUpdateAMD(INPUT) (a.mu held across wait)");
            return 1;
        }
        CHECK(calls.size()==2&&calls[0].stage==VK_ANTI_LAG_STAGE_INPUT_AMD&&calls[1].stage==VK_ANTI_LAG_STAGE_PRESENT_AMD&&calls[1].frame==prev_f);
    }
    // concurrency 2: antilag_present() must NOT hold a.mu across vkAntiLagUpdateAMD(PRESENT)
    {
        const uint64_t cur_f = antilag_frame();
        CHECK(cur_f != 0);
        arm_hook(VK_ANTI_LAG_STAGE_PRESENT_AMD);
        uint64_t seen_frame = 0;
        std::thread reader([&seen_frame] {
            {
                std::unique_lock lk(hook.mu);
                hook.cv.wait_for(lk, std::chrono::milliseconds(400), [] { return hook.entered; });
            }
            seen_frame = antilag_frame();
            std::lock_guard lk(hook.mu);
            hook.release = true;
            hook.cv.notify_all();
        });
        antilag_present();
        reader.join();
        if (!hook.released_in_time) {
            std::puts("FAIL: antilag_frame() blocked while antilag_present() was inside vkAntiLagUpdateAMD(PRESENT) (a.mu held across driver call)");
            return 1;
        }
        CHECK(seen_frame == cur_f);
    }
    // concurrency 3: turning off must NOT hold a.mu across vkAntiLagUpdateAMD(OFF)
    {
        calls.clear();
        menu_antilag_changed(0);
        arm_hook(-1);
        uint64_t seen_frame = ~0ull;
        std::thread reader([&seen_frame] {
            {
                std::unique_lock lk(hook.mu);
                hook.cv.wait_for(lk, std::chrono::milliseconds(400), [] { return hook.entered; });
            }
            seen_frame = antilag_frame();
            std::lock_guard lk(hook.mu);
            hook.release = true;
            hook.cv.notify_all();
        });
        antilag_input();
        reader.join();
        if (!hook.released_in_time) {
            std::puts("FAIL: antilag_frame() blocked while update(false, nullptr) was inside vkAntiLagUpdateAMD(OFF) (a.mu held across driver call)");
            return 1;
        }
        CHECK(seen_frame == 0 && calls.size() == 1 && calls[0].mode == VK_ANTI_LAG_MODE_OFF_AMD && calls[0].stage == -1);
    }
    // row off steady-state: nothing else called, and no frame for a set to keep
    antilag_present();antilag_input();
    CHECK(calls.size()==1&&calls[0].mode==VK_ANTI_LAG_MODE_OFF_AMD&&calls[0].stage==-1&&antilag_frame()==0);
    // on again: a set made before gives nothing; the next frame's stages go out
    calls.clear();menu_antilag_changed(1);
    antilag_present_frame=g+1;antilag_present();CHECK(calls.empty());
    antilag_present_frame=kAntiLagThisFrame;antilag_present();CHECK(calls.empty());   // (no input stage since)
    antilag_input();f=antilag_frame();antilag_present();
    CHECK(calls.size()==2&&calls[1].stage==VK_ANTI_LAG_STAGE_PRESENT_AMD&&calls[1].frame==f&&f>g+1);
    // concurrency 4: turning off while antilag_input() is in flight AND another thread observes want=0 first
    // (antilag_frame / antilag_present) must defer MODE_OFF_AMD until the in-flight MODE_ON_AMD returns, so the
    // driver always ends in MODE_OFF_AMD (emitted exactly once) rather than staying stuck On
    {
        calls.clear();
        const uint64_t prev_f = antilag_frame();
        CHECK(prev_f != 0);
        arm_hook(VK_ANTI_LAG_STAGE_INPUT_AMD);
        size_t calls_while_in_flight = 0;
        uint64_t seen_frame = ~0ull;
        std::thread observer([prev_f, &calls_while_in_flight, &seen_frame] {
            {
                std::unique_lock lk(hook.mu);
                hook.cv.wait_for(lk, std::chrono::milliseconds(400), [] { return hook.entered; });
            }
            menu_antilag_changed(0);
            seen_frame = antilag_frame();
            antilag_present_frame = prev_f;
            antilag_present();
            antilag_present_frame = kAntiLagThisFrame;
            {
                std::lock_guard clk(calls_mu);
                calls_while_in_flight = calls.size();
            }
            std::lock_guard lk(hook.mu);
            hook.release = true;
            hook.cv.notify_all();
        });
        antilag_input();
        observer.join();
        CHECK(hook.released_in_time && seen_frame == 0);
        if (calls_while_in_flight != 1) {
            std::printf("FAIL: MODE_OFF_AMD was emitted while INPUT stage was still in flight (calls_while_in_flight=%zu)\n", calls_while_in_flight);
            return 1;
        }
        CHECK(calls.size() == 2 && calls[0].stage == VK_ANTI_LAG_STAGE_INPUT_AMD && calls.back().mode == VK_ANTI_LAG_MODE_OFF_AMD && calls.back().stage == -1);
        CHECK(antilag_frame() == 0);
        antilag_present();
        antilag_input();
        CHECK(calls.size() == 2);   // (Off sent once, steady-state Off sends nothing)
    }
    // concurrency 5: turning off while antilag_present() is in flight AND another thread observes want=0 first
    // also defers MODE_OFF_AMD until PRESENT returns, ending in a single MODE_OFF_AMD
    {
        menu_antilag_changed(1);
        antilag_input();
        const uint64_t cur_f = antilag_frame();
        CHECK(cur_f != 0);
        calls.clear();
        arm_hook(VK_ANTI_LAG_STAGE_PRESENT_AMD);
        size_t calls_while_in_flight = 0;
        uint64_t seen_frame = ~0ull;
        std::thread observer([&calls_while_in_flight, &seen_frame] {
            {
                std::unique_lock lk(hook.mu);
                hook.cv.wait_for(lk, std::chrono::milliseconds(400), [] { return hook.entered; });
            }
            menu_antilag_changed(0);
            seen_frame = antilag_frame();
            {
                std::lock_guard clk(calls_mu);
                calls_while_in_flight = calls.size();
            }
            std::lock_guard lk(hook.mu);
            hook.release = true;
            hook.cv.notify_all();
        });
        antilag_present();
        observer.join();
        CHECK(hook.released_in_time && seen_frame == 0);
        CHECK(calls_while_in_flight == 1);
        CHECK(calls.size() == 2 && calls[0].stage == VK_ANTI_LAG_STAGE_PRESENT_AMD && calls.back().mode == VK_ANTI_LAG_MODE_OFF_AMD && calls.back().stage == -1);
        antilag_present();
        antilag_input();
        CHECK(calls.size() == 2);
    }
    // concurrency 6 (L2): pausing a claimed Off inside vkAntiLagUpdateAMD(OFF) while another thread turns want=1
    // back on must NOT allow a MODE_ON_AMD call to overtake the in-flight MODE_OFF_AMD; once Off finishes, the next
    // frame starts MODE_ON_AMD cleanly
    {
        menu_antilag_changed(1);
        antilag_input();
        const uint64_t prev_f = antilag_frame();
        CHECK(prev_f != 0);
        calls.clear();
        menu_antilag_changed(0);
        arm_hook(-1);
        size_t calls_while_off_paused = 0;
        uint64_t seen_frame = ~0ull;
        std::thread reenable([prev_f, &calls_while_off_paused, &seen_frame] {
            {
                std::unique_lock lk(hook.mu);
                hook.cv.wait_for(lk, std::chrono::milliseconds(400), [] { return hook.entered; });
            }
            menu_antilag_changed(1);
            antilag_input();
            antilag_present_frame = prev_f;
            antilag_present();
            antilag_present_frame = kAntiLagThisFrame;
            seen_frame = antilag_frame();
            {
                std::lock_guard clk(calls_mu);
                calls_while_off_paused = calls.size();
            }
            std::lock_guard lk(hook.mu);
            hook.release = true;
            hook.cv.notify_all();
        });
        antilag_input();
        reenable.join();
        CHECK(hook.released_in_time);
        if (calls_while_off_paused != 1 || seen_frame != 0) {
            std::printf("FAIL: MODE_ON_AMD overtook in-flight MODE_OFF_AMD (calls_while_off_paused=%zu, seen_frame=%llu)\n",
                        calls_while_off_paused, (unsigned long long)seen_frame);
            return 1;
        }
        CHECK(calls.size() == 1 && calls[0].mode == VK_ANTI_LAG_MODE_OFF_AMD && calls[0].stage == -1);
        // once Off returned, want=1 activates cleanly on the next frame
        antilag_input();
        const uint64_t next_f = antilag_frame();
        CHECK(next_f > prev_f);
        antilag_present();
        CHECK(calls.size() == 3 && calls[1].mode == VK_ANTI_LAG_MODE_ON_AMD && calls[1].stage == VK_ANTI_LAG_STAGE_INPUT_AMD && calls[1].frame == next_f);
        CHECK(calls[2].mode == VK_ANTI_LAG_MODE_ON_AMD && calls[2].stage == VK_ANTI_LAG_STAGE_PRESENT_AMD && calls[2].frame == next_f);
    }
    // concurrency 7 (L1): concurrent menu_antilag_changed() toggles while presentation/input threads run
    {
        calls.clear();
        std::atomic<bool> stop{false};
        std::thread menu_thread([&stop] {
            for (int i = 0; i < 200; i++) menu_antilag_changed(i & 1);
            stop.store(true, std::memory_order_release);
        });
        std::thread presenter([&stop] {
            while (!stop.load(std::memory_order_acquire)) {
                const uint64_t id = antilag_frame();
                if (id) {
                    antilag_present_frame = id;
                    antilag_present();
                    antilag_present_frame = kAntiLagThisFrame;
                }
            }
        });
        while (!stop.load(std::memory_order_acquire)) antilag_input();
        menu_thread.join();
        presenter.join();
        // settle Off cleanly and verify steady-state Off sends nothing further
        menu_antilag_changed(0);
        antilag_input();
        const size_t after_off = calls.size();
        CHECK(antilag_frame() == 0);
        antilag_present();
        antilag_input();
        CHECK(calls.size() == after_off);
    }
    // presenter hand-off: snapshot antilag_frame() before next antilag_input() advances to f2, then present f1 on
    // presenter thread while INPUT(f2) is in flight; stale id across Off->On toggle emits nothing
    {
        menu_antilag_changed(1);
        calls.clear();
        antilag_input();
        const uint64_t f1 = antilag_frame();
        CHECK(f1 != 0 && calls.size() == 1);
        arm_hook(VK_ANTI_LAG_STAGE_INPUT_AMD);
        std::thread presenter([f1] {
            {
                std::unique_lock lk(hook.mu);
                hook.cv.wait_for(lk, std::chrono::milliseconds(400), [] { return hook.entered; });
            }
            antilag_present_frame = f1;
            antilag_present();
            antilag_present_frame = kAntiLagThisFrame;
            std::lock_guard lk(hook.mu);
            hook.release = true;
            hook.cv.notify_all();
        });
        antilag_input();
        presenter.join();
        CHECK(hook.released_in_time);
        const uint64_t f2 = antilag_frame();
        CHECK(f2 == f1 + 1 && calls.size() == 3);
        CHECK(calls[1].stage == VK_ANTI_LAG_STAGE_INPUT_AMD && calls[1].frame == f2);
        CHECK(calls[2].stage == VK_ANTI_LAG_STAGE_PRESENT_AMD && calls[2].frame == f1);
        // stale id handed off before Off->On toggle emits nothing
        menu_antilag_changed(0);
        antilag_input();
        menu_antilag_changed(1);
        calls.clear();
        antilag_present_frame = f2;
        antilag_present();
        antilag_present_frame = kAntiLagThisFrame;
        CHECK(calls.empty());
    }
    std::puts("PASS: Anti-Lag 2 stages, zero-lock unavailable fast path, non-blocking driver calls, Off/On ordering, atomic menu option, presenter hand-off and frame generation");
}
'''

cpp_rc = 0
with tempfile.TemporaryDirectory(prefix="antilag-") as tmp:
    cpp, exe = Path(tmp) / "fixture.cpp", Path(tmp) / "fixture.exe"
    cpp.write_text(fixture, encoding="utf-8")
    subprocess.run([a.cxx, "-std=c++20", "-O1", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                    "-pthread", "-I" + str(a.headers), str(cpp), "-o", str(exe)], check=True)
    clean_env = dict(os.environ)
    clean_env.pop("RX_VK_ANTILAG", None)
    cpp_rc = subprocess.run([str(exe)], env=clean_env).returncode

if errors:
    for err in errors:
        print("FAIL:", err)
if errors or cpp_rc:
    raise SystemExit(1)
