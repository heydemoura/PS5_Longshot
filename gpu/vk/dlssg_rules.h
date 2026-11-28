// (longshot) NVIDIA DLSS Frame Generation through NVIDIA Streamline (vk_dlssg.cpp): what is decided from the player's rows
// and the run's switches alone. Streamline's loader takes vulkan-1.dll's place before Vulkan is loaded, so whether it runs
// is settled at the start, from the rows as saved: a change of them that needs it, or no longer does, applies at the next
// start. Nothing of Vulkan or of Streamline here: tests/dlssg_rules_test.cpp checks it.
#pragma once

namespace mkwvk::dlssg_rules {

/// What decides it: the rows (as saved at the start, as they are later) and the run's switches
struct Start {
    bool built = false;            // a build with Streamline's SDK (LONGSHOT_SL: Windows)
    bool killed = false;           // RX_VK_DLSSG=0
    bool prepare_only = false;     // longshot --prepare-shaders (LONGSHOT_PREPARE_ONLY): no window, nothing to make frames for
    bool dlss_fg_off = false;      // RX_VK_DLSS=0 or RX_VK_DLSS_FG=0: no DLSS Frame Generation in this run
    bool framegen_off = false;     // RX_VK_FRAMEGEN=0: no frame generation in this run
    bool dlss_fg_forced = false;   // RX_VK_DLSS_FG=1: DLSS FG whatever the Frame generation by row says
    int upscaler_switch = -1;      // the Upscaler a switch chooses for the run over the row, -1 none (vk_temporal.cpp
                                   // temporal_mode's order: RX_VK_FSR=1, then RX_VK_DLSS, RX_VK_FSR4, RX_VK_XESS)
    int framegen = 0;              // the Frame generation row: 0 Off, 1 Auto, 2 to 8 a fixed multiplier
    int framegen_by = 0;           // the Frame generation by row: 0 DLSS FG, 1 Built-in FG, 2 FSR 3.1 FG
    int upscaler = 0;              // the Upscaler row (upscaler_rows.h): 0 the game's own, 6 Off, the others temporal
};

/// Whether this run may make DLSS FG's frames through Streamline at all, whatever the rows say
inline bool possible(const Start& s) {
    return s.built && !s.killed && !s.prepare_only && !s.dlss_fg_off && !s.framegen_off;
}

/// The Upscaler of the run: a switch's, else the row's (upscaler_rows.h values)
inline int upscaler(const Start& s) { return s.upscaler_switch >= 0 ? s.upscaler_switch : s.upscaler; }

/// Whether the Upscaler names a temporal Upscaler: DLSS-G makes its frames from one's depth and motion, and without one the
/// renderer's own frame generation makes them, as without Streamline. The row alone, not whether its engine runs on this
/// GPU: that is not known before Vulkan
inline bool temporal(const Start& s) { return upscaler(s) != 0 && upscaler(s) != 6; }

/// The Upscalers Streamline is started with (upscaler_rows.h): NVIDIA DLSS (1) and DLAA (2). With another one the frames are
/// made as without Streamline: NGX's DLSS-G where that Upscaler's engine runs, else the renderer's own frame generation.
/// Started with FSR 4, which does not run on an NVIDIA GPU, Streamline gave up, and its DLSS-G plugin still presented every
/// frame through a swapchain of its own for the whole run
constexpr int kStreamlineUpscalers[] = {1, 2};
inline bool streamline_upscaler(const Start& s) {
    for (int u : kStreamlineUpscalers)
        if (u == upscaler(s)) return true;
    return false;
}

/// Whether the Upscaler row at row, the run's switches and other rows as at the start (s), waits for the restart while
/// Streamline's loader is the renderer's: an Upscaler it is not started with. Until then DLSS goes on (vk_dlssg.h
/// dlssg_upscaler); changed to FSR 4 at once, Streamline gave up and its DLSS-G plugin still presented every frame
inline bool upscaler_waits(const Start& s, int row) {
    Start now = s;
    now.upscaler = row;
    return !streamline_upscaler(now);
}

/// Whether the rows ask DLSS FG for frames: a fixed multiplier (Auto's frames are never DLSS FG's), made by DLSS FG
inline bool asked(const Start& s) { return s.framegen >= 2 && (s.dlss_fg_forced || s.framegen_by == 0) && temporal(s); }

/// Whether Streamline is started before Vulkan is loaded
inline bool starts(const Start& s) { return possible(s) && asked(s) && streamline_upscaler(s); }

/// The made frames a set under Streamline: the row's multiplier less the game's own frame, at most what DLSS FG makes on
/// this GPU (most: 1 on an RTX 40, more on an RTX 50); 0 when the rows do not ask DLSS FG
inline int frames(const Start& s, int most) {
    if (!asked(s) || most < 1) return 0;
    return s.framegen - 1 < most ? s.framegen - 1 : most;
}

/// How the start went
enum Run {
    NOT_STARTED,   // Streamline does not run: NGX's DLSS FG, or the renderer's own frame generation
    RUNNING,       // it makes DLSS FG's frames, and the renderer's own frame generation is off for the run
    GAVE_UP,       // it started and DLSS FG does not run through it on this GPU: the renderer as without it
};

/// Whether the rows as they are now (s) take a restart to run as they say (the Frame generation by row carries it for
/// the three, gpu/vk/menu/framegen.cpp's restart_value: closing the menu asks, menu_restart.h). Running: when another is to make the frames, Auto's or a fixed multiplier's;
/// Frame generation Off needs none, nothing being made either way. Not started: when Streamline would start, and could
/// (its files are there, and as far as is known DLSS FG runs on this GPU)
inline bool restart_needed(Run run, bool could, const Start& s) {
    if (run == RUNNING) return s.framegen >= 1 && !starts(s);
    if (run == NOT_STARTED) return could && starts(s);
    return false;
}

} // namespace mkwvk::dlssg_rules
