// (tests/test_dlssg_pace.py) gpu/vk/dlssg_rules.h: when NVIDIA Streamline is started for DLSS Frame Generation, the frames
// it is asked for, and when a change of the Frame generation rows takes a restart
#include "../gpu/vk/dlssg_rules.h"

#include <cstdio>
#include <cstdlib>

using namespace mkwvk::dlssg_rules;

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); std::exit(1); } } while (0)

// A build with Streamline, the rows asking DLSS FG for frames: Fixed 2x, by DLSS FG, the Upscaler DLSS (1)
static Start asking() {
    Start s;
    s.built = true;
    s.framegen = 2, s.framegen_by = 0, s.upscaler = 1;
    return s;
}

int main() {
    // ---- the start ----
    CHECK(!starts(Start{}));   // (nothing set: a build without Streamline, the rows' defaults)
    CHECK(starts(asking()));
    {
        Start s = asking();
        s.built = false;   // (a build without Streamline's SDK: never)
        CHECK(!possible(s) && !starts(s));
    }
    // (the run's switches that leave no DLSS FG to make: RX_VK_DLSSG=0, --prepare-shaders, RX_VK_DLSS=0 or RX_VK_DLSS_FG=0,
    // RX_VK_FRAMEGEN=0)
    for (int i = 0; i < 4; i++) {
        Start s = asking();
        (i == 0 ? s.killed : i == 1 ? s.prepare_only : i == 2 ? s.dlss_fg_off : s.framegen_off) = true;
        CHECK(!possible(s) && !starts(s) && asked(s));
    }
    // (the Frame generation row: Off and Auto never, each fixed multiplier does)
    for (int fg = 0; fg <= 8; fg++) {
        Start s = asking();
        s.framegen = fg;
        CHECK(starts(s) == (fg >= 2));
    }
    // (the Frame generation by row: DLSS FG alone, unless RX_VK_DLSS_FG=1 forces it)
    for (int by = 0; by <= 2; by++) {
        Start s = asking();
        s.framegen_by = by;
        CHECK(starts(s) == (by == 0));
        s.dlss_fg_forced = true;
        CHECK(starts(s));
    }
    // (the Upscaler row: DLSS (1) and DLAA (2) alone. With FSR 3.1, FSR 4 or XeSS, NGX's DLSS-G or the renderer's own frame
    // generation makes the frames, as without Streamline: started with FSR 4, which does not run on an NVIDIA GPU, Streamline
    // gave up, and its DLSS-G plugin still presented every frame through a swapchain of its own for the whole run)
    for (int up = 0; up <= 8; up++) {
        Start s = asking();
        s.upscaler = up;
        const bool dlss = up == 1 || up == 2;
        CHECK(temporal(s) == (up != 0 && up != 6));   // (what DLSS-G is asked for while it runs, frames())
        CHECK(starts(s) == dlss);
        CHECK(restart_needed(NOT_STARTED, true, s) == dlss);   // (offered only where it would start)
        CHECK(restart_needed(RUNNING, true, s) == !dlss);      // (the Upscaler changed while it runs: off at the restart)
        // (changed in the game while Streamline's loader is the renderer's: another than DLSS or DLAA waits for the restart,
        // DLSS going on until then, vk_dlssg.h dlssg_upscaler)
        CHECK(upscaler_waits(asking(), up) == !dlss);
        // (a switch that chooses the run's Upscaler goes over the row: RX_VK_DLSS=1 or 2 starts it, RX_VK_FSR=1, RX_VK_FSR4 or
        // RX_VK_XESS does not. Started by RX_VK_DLSS, the row's value runs only where no switch goes over it: nothing waits)
        for (int sw = 0; sw <= 8; sw++) {
            s.upscaler_switch = sw;
            CHECK(starts(s) == (sw == 1 || sw == 2));
            if (sw == 1 || sw == 2) CHECK(!upscaler_waits(s, (up + 3) % 9));
        }
    }

    // ---- the made frames a set ----
    {
        Start s = asking();
        CHECK(frames(s, 1) == 1);   // (2x on an RTX 40)
        s.framegen = 4;
        CHECK(frames(s, 1) == 1);   // (4x asked where DLSS FG makes one: 2x)
        CHECK(frames(s, 3) == 3);   // (an RTX 50: 4x)
        s.framegen = 3;
        CHECK(frames(s, 3) == 2);
        s.framegen = 8;
        CHECK(frames(s, 3) == 3);
        CHECK(frames(s, 0) == 0);   // (not known yet: none)
        s.framegen = 1;
        CHECK(frames(s, 3) == 0);   // (Auto: not DLSS FG's)
        s.framegen = 0;
        CHECK(frames(s, 3) == 0);
        s = asking();
        s.framegen_by = 1;
        CHECK(frames(s, 3) == 0);   // (the rows changed to another maker while it runs: no frames)
        s = asking();
        s.upscaler = 6;
        CHECK(frames(s, 3) == 0);   // (the Upscaler turned off while it runs: nothing to make them from)
    }

    // ---- the restart ----
    // not started (NGX's DLSS FG, or the renderer's own frame generation): asked for when the rows would start Streamline,
    // and it could start
    CHECK(restart_needed(NOT_STARTED, true, asking()));
    CHECK(!restart_needed(NOT_STARTED, false, asking()));   // (its files are not there, or DLSS FG does not run on this GPU)
    {
        Start s = asking();
        s.framegen = 0;
        CHECK(!restart_needed(NOT_STARTED, true, s));
        s.framegen = 1;
        CHECK(!restart_needed(NOT_STARTED, true, s));
        s = asking();
        s.framegen_by = 2;
        CHECK(!restart_needed(NOT_STARTED, true, s));
        s = asking();
        s.killed = true;
        CHECK(!restart_needed(NOT_STARTED, true, s));   // (RX_VK_DLSSG=0: NGX makes them, in this run and the next)
    }
    // running: asked for when another is to make the frames; Frame generation Off needs none, and neither does another
    // multiplier
    CHECK(!restart_needed(RUNNING, true, asking()));
    {
        Start s = asking();
        s.framegen = 4;
        CHECK(!restart_needed(RUNNING, true, s));
        s.framegen = 0;
        CHECK(!restart_needed(RUNNING, true, s));
        s.framegen = 1;
        CHECK(restart_needed(RUNNING, true, s));   // (Auto: the renderer's own)
        s = asking();
        s.framegen_by = 1;
        CHECK(restart_needed(RUNNING, true, s));
        s = asking();
        s.upscaler = 6;
        CHECK(restart_needed(RUNNING, true, s));   // (no temporal Upscaler: the renderer's own frame generation makes them)
        s.framegen = 0;
        CHECK(!restart_needed(RUNNING, true, s));
    }
    // it gave up on this GPU: the renderer runs as without it, whatever the rows say
    for (int fg = 0; fg <= 8; fg++)
        for (int by = 0; by <= 2; by++) {
            Start s = asking();
            s.framegen = fg, s.framegen_by = by;
            CHECK(!restart_needed(GAVE_UP, true, s));
        }

    std::puts("PASS: Streamline starts where the saved rows ask DLSS FG for frames, and a change of them that needs it, or no "
              "longer does, takes a restart");
    return 0;
}
