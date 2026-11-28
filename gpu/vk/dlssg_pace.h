// (longshot) NVIDIA DLSS Frame Generation through NVIDIA Streamline (vk_dlssg.cpp): when a game frame's present goes to
// Streamline. The DLSS-G plugin shows its made frame and then the game's at times that follow the present's call, so the
// frames reach the screen as evenly as the calls come. Three parts, each a switch (gpu/vk/switches.def):
//
//  - the hold (RX_VK_DLSSG_HOLD): the render thread presents a frame as soon as its work is recorded, a few milliseconds
//    after the game's thread presented it, and that lag varies from frame to frame. The present is held to a fixed delay
//    after the game's present instead, longer than the lag, so the calls are as even as the game's own clock;
//  - the grid (RX_VK_DLSSG_HOLD_GRID): the game's own presents jitter about their line by a fraction of a millisecond.
//    The hold counts from the line through the game's last presents while this present is near it, not from the present;
//  - the toggle (RX_VK_DLSSG_TOGGLE): NVIDIA's driver meters DLSS-G's presents by one of two laws, one of which shows
//    them unevenly, and a screen left a little over 20 ms without a new image changes the law. When Longshot's layer under
//    Streamline reads the uneven law (dlssg_law.h), one present is held that long: by default a little longer than the
//    longest frame the screen's VRR holds, read from its EDID (toggle_for_floor), else 22 ms.
//
// The three defaults were found at 2x with the game at 60 fps. The hold and the grid's reach shrink with the game's frame
// time below that, since a hold of most of a frame at 60 fps is more than a whole frame at 120; the toggle holds only
// at 2x with the game at 60 fps (Hold::toggle_fits), the rate the law's reader was made for (dlssg_law.h): an ask
// elsewhere is let go.
// Nothing of Vulkan or of Streamline here, and no clock: tests/dlssg_pace_test.cpp checks it.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>

namespace mkwvk::dlssg_pace {

// The switches' defaults, in microseconds (tests/test_dlssg_pace.py reads them here)
constexpr int64_t kHoldUs = 13000;     // RX_VK_DLSSG_HOLD
constexpr int64_t kGridUs = 1500;      // RX_VK_DLSSG_HOLD_GRID
constexpr int64_t kToggleUs = 22000;   // RX_VK_DLSSG_TOGGLE, where the screen's VRR floor is not read

/// The game's frame time the defaults were found at: 60 fps, in nanoseconds
constexpr int64_t kFrame60 = 16666667;

/// The toggle's hold from the screen's VRR floor (RX_VK_DLSSG_TOGGLE unset; the floor from the screen's EDID, edid_vrr.h).
/// The driver changes its law when the screen is left a little longer than the longest frame its VRR holds, 1 / floor:
/// measured 0.3 ms under 1/48 Hz on a 48-240 Hz screen, where 22 ms changed the law every time and left at least 20.98 ms
/// on the screen. The hold is that longest frame and the margin 22 ms had over it, so 48 Hz gives kToggleUs. Only 48 Hz
/// was measured: the rule is kept to floors near it, a hold of 20.4 ms (52 Hz) to 26.2 ms (40 Hz), and gives 0 elsewhere
/// (the caller keeps kToggleUs)
constexpr int kFloorMeasuredHz = 48;
constexpr int64_t kToggleOverFloorUs = kToggleUs - 1000000 / kFloorMeasuredHz;   // (1167 microseconds)
constexpr int kFloorLowHz = 40, kFloorHighHz = 52;
constexpr int64_t toggle_for_floor(int floor_hz) {
    return floor_hz >= kFloorLowHz && floor_hz <= kFloorHighHz ? 1000000 / floor_hz + kToggleOverFloorUs : 0;
}

/// The line through the game's last presents: where this present would be if the game's thread presented with no jitter.
/// Least squares on the presents' frame numbers (the time between two presents over the game's frame time, rounded, so a
/// skipped frame keeps the line), fitted again without the presents far off it (a third of a frame's sixteenth: 3 ms at
/// 60 fps), none while the last W presents span more than W * 3 / 2 frames
struct Grid {
    static constexpr int W = 60;
    int64_t at[W] = {}, num[W] = {};   // (the game's presents and their frame numbers; a ring, head the oldest when full)
    int count = 0, head = 0;
    int64_t last = 0, n = 0;
    void reset() { count = head = 0, last = n = 0; }
    /// The line's place for this present (0: no line yet), then the present taken in. frame: the game's frame time
    int64_t place(int64_t game_at, int64_t frame) {
        if (frame <= 0) return 0;
        if (last) n += std::max<int64_t>(1, (game_at - last + frame / 2) / frame);
        last = game_at;
        int64_t out = 0;
        if (count == W && n - num[head] <= W * 3 / 2) {
            const int64_t a0 = at[head], n0 = num[head];
            double xs[W], ys[W];
            for (int i = 0; i < W; i++) xs[i] = (double)(num[i] - n0), ys[i] = (double)(at[i] - a0);
            auto fit = [&](const bool* keep, double& b, double& c) {
                double sx = 0, sy = 0, k = 0;
                for (int i = 0; i < W; i++)
                    if (!keep || keep[i]) sx += xs[i], sy += ys[i], k++;
                const double mx = sx / k, my = sy / k;
                double sxx = 0, sxy = 0;
                for (int i = 0; i < W; i++)
                    if (!keep || keep[i]) sxx += (xs[i] - mx) * (xs[i] - mx), sxy += (xs[i] - mx) * (ys[i] - my);
                b = sxx > 0 ? sxy / sxx : (double)frame;
                c = my - b * mx;
            };
            double b = 0, c = 0;
            fit(nullptr, b, c);
            const double far_off = (double)std::min<int64_t>(3000000, frame * 3 / 16);
            bool keep[W];
            int kept = 0;
            for (int i = 0; i < W; i++) kept += keep[i] = std::fabs(ys[i] - (c + b * xs[i])) <= far_off;
            if (kept >= W / 2 && kept < W) fit(keep, b, c);
            out = a0 + (int64_t)std::llround(c + b * (double)(n - n0));
        }
        at[head] = game_at, num[head] = n;
        head = (head + 1) % W;
        count = std::min(count + 1, W);
        return out;
    }
};

/// The hold's state, the render thread's (one present at a time). Times in nanoseconds on one steady clock
struct Hold {
    int64_t hold = kHoldUs * 1000;       // (0: no hold)
    int64_t grid_max = kGridUs * 1000;   // (0: no grid)

    int64_t last_game = 0;   // (the game's present before)
    int64_t frame = 0;       // (the game's frame time, smoothed; 0 not known yet)
    int64_t wait = 0;        // (how long the game's thread waited in its presents, smoothed)
    bool below = false;      // (the game does not reach its cap: its thread waits for the renderer in each present)
    int64_t scaled = 0, scaled_for = 0;   // (the hold for this frame time, and the frame time it was worked out for)
    bool on_grid = false;    // (the last target was counted from the line)
    Grid grid;

    /// The hold at this frame time: the whole of it from 60 fps down, and to a sixteenth above, so that 60 fps's own noise
    /// never moves it; the same share of the frame above that, worked out again only when the frame time has moved by a
    /// sixteenth, so the smoothed frame time's noise does not reach the screen
    int64_t hold_for(int64_t frame_now) {
        if (frame_now >= kFrame60 - kFrame60 / 16) {
            scaled_for = 0;
            return hold;
        }
        if (!scaled_for || std::llabs(frame_now - scaled_for) > scaled_for / 16) {
            scaled_for = frame_now;
            scaled = hold * frame_now / kFrame60;
        }
        return scaled;
    }

    /// A game frame's present, right before it goes to Streamline: the time it is held to, 0 when it goes now.
    ///   game_at:   when the game's thread presented it;
    ///   game_wait: how long that thread then waited for the renderer to take it;
    ///   now:       this clock's time.
    /// Not held: without game_at, with the render thread a tenth of a second behind, while the game does not reach its cap
    /// (its presents then follow the renderer, not the game's clock), and when the present is late already. After a tenth
    /// of a second without a present the line starts again
    int64_t target(int64_t game_at, int64_t game_wait, int64_t now) {
        on_grid = false;
        const int64_t lag = now - game_at;
        if (game_at <= 0 || game_at <= last_game || lag < 0 || lag > 100000000) return 0;
        if (last_game && game_at - last_game < 100000000) frame = frame ? (frame * 7 + (game_at - last_game)) / 8 : game_at - last_game;
        else if (last_game) grid.reset();
        last_game = game_at;
        // (one millisecond of wait on average says the game is held back; half of one, that it is not)
        wait += (std::clamp<int64_t>(game_wait, 0, 2000000) - wait) / 16;
        if (wait > 1000000) below = true;
        else if (wait < 500000) below = false;
        if (below) {
            grid.reset();
            return 0;
        }
        if (hold <= 0 || frame <= 0) return 0;
        const int64_t h = hold_for(frame);
        int64_t from = game_at;
        if (grid_max > 0) {
            const int64_t g = grid.place(game_at, frame);
            if (g && std::llabs(g - game_at) <= std::min(grid_max, frame / 8)) from = g, on_grid = true;
        }
        return from + h > now ? from + h : 0;
    }

    /// Whether the toggle may hold a present now: at 2x (frames: the made frames a set), with the game at its cap and
    /// within a twentieth of 60 fps. Elsewhere the law's reader is not known to read right (its bands are 60 fps's,
    /// though it reads some at 90 fps), nor a hold that long to change the law
    bool toggle_fits(uint32_t frames) const {
        return frames == 1 && !below && frame > 0 && std::llabs(frame - kFrame60) <= kFrame60 / 20;
    }
};

} // namespace mkwvk::dlssg_pace
