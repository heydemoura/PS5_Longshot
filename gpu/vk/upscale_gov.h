/* (PS5) The upscaler by the GPU's load: the Upscaler row's Auto and the Resolution mode row's Auto (menu/upscaler.cpp), as
   plain functions. A ladder of (upscaler, Render resolution) rungs, best picture first, and a governor that goes a rung down
   while the frame rate is under a line (and the step buys some) and back up, slowly, when it is over another. No Vulkan, no menu:
   tests/test_ps5_upscale_gov.py checks them.

   Why steps and not the game's dynamic resolution alone: on the console FSR 4 costs about 4 ms of GPU time a frame whatever
   the 3D's size (measured 2026-11-27, a base PS5), so in a heavy place a smaller 3D is not enough and the engine itself has to
   give way: FSR 4, then FSR 3.1, then the plain stretch. */
#pragma once

#include "upscaler_rows.h"

namespace mkwvk::upscale_gov {

namespace ur = upscaler_rows;

struct Rung {
    int up, res;   // (opts.dlss and opts.resolution, as the rows save them)
};
constexpr int kMaxRungs = 12;

/// The ladder, best first. algo: the Upscaler row at Auto (FSR 4 where it runs, FSR 3.1 where it runs, then Off: the plain
/// stretch); else the engine chosen alone. mode: the Resolution mode at Auto (Quality 67 %, Balanced 58 %, Performance 50 %,
/// each as the engine's nearest stop; FSR 3.1 under an Auto Upscaler goes on to Ultra Performance, and Off has its smallest
/// alone); else the Render resolution chosen (0, the game's dynamic size, too). Returns the number of rungs, 1 at least
inline int ladder(bool algo, bool mode, int up, int res, unsigned available, Rung* out) {
    int engines[3], ne = 0;
    if (algo) {
        if (available & ur::bit(ur::FSR4)) engines[ne++] = ur::FSR4;
        if (available & ur::bit(ur::FSR3)) engines[ne++] = ur::FSR3;
        engines[ne++] = ur::OFF;
    } else {
        engines[ne++] = ur::engine(up);
    }
    int n = 0;
    const auto push = [&](int e, int r) {
        if (n && out[n - 1].up == e && out[n - 1].res == r) return;   // (two modes on one stop of this engine: one rung)
        if (n < kMaxRungs) out[n++] = {e, r};
    };
    for (int i = 0; i < ne; i++) {
        const int e = engines[i];
        if (!mode) {
            push(e, res);
            continue;
        }
        if (e == ur::OFF && algo) {
            push(e, 4);
            continue;
        }
        for (int m : {3, 5, 4}) push(e, ur::snap_render(e, m));
        if (e == ur::FSR3 && algo) push(e, 6);
    }
    return n;
}

/// Where the next engine's rungs begin after rung at, when at is a smaller 3D under the engine of the rung before it; else -1
inline int next_engine(const Rung* l, int n, int at) {
    if (at < 1 || at >= n || l[at].up != l[at - 1].up) return -1;
    for (int i = at + 1; i < n; i++)
        if (l[i].up != l[at].up) return i;
    return -1;
}

/// Where (up, res) is on a ladder, or -1
inline int rung_of(const Rung* l, int n, int up, int res) {
    for (int i = 0; i < n; i++)
        if (l[i].up == ur::engine(up) && l[i].res == res) return i;
    return -1;
}

/// The governor's state. A window is 120 of the game's presents (vk_replay.cpp), two to four seconds
struct Gov {
    int rung = 0;
    int low = 0, high = 0;     // (windows in a row under the low line, over the high one)
    int hold = 0;              // (windows left out after a step: targets made again, an engine's first frames)
    int wait = 4;              // (windows over the high line a step up takes: doubled when the last one did not hold)
    int since_up = 1000;       // (windows since the last step up)
    float before = 0;          // (the frame rate before the last step down, while that step is still to be judged; else 0)
    float before_draws = 0;    // (and the draws a frame then: the verdict waits for a window of the same scene)
    int judging = 0;           // (windows the verdict has waited for one)
    int back = -1;             // (the rung to give back to when the engine tried after a smaller 3D buys nothing either)
    int blocked = 0;           // (windows with no step down: the last one bought nothing, the CPU holds the frame rate)
    float gain[kMaxRungs] = {};   // (what the step down to each rung bought, as measured: 1.13 is 13 % faster; 0 not known)
};
constexpr int kDown = 2, kUp = 4, kUpMax = 32, kHold = 1, kHeld = 24, kBlocked = 40, kJudge = 6;
constexpr float kGain = 1.03f, kGainStretch = 1.10f, kSameScene = 0.15f, kGainGuess = 1.25f, kUpMargin = 1.05f;

/// One window's measures. fps: the frame rate; draws: the draws a frame (the scene's weight on the CPU); low_fps: the line
/// (menu_upscale_gov_tick: 40, under the cap when there is one); cap_fps: the cap, 0 none; room_ok: memory is not short (no
/// step up otherwise: a larger 3D and FSR 4 take some); skip_to: next_engine() of the rung now; need: the gain the rung now
/// had to buy (kGainStretch for the plain stretch, whose picture is the roughest: not for 3 %; else kGain)
struct In {
    float fps = 0, draws = 0, low_fps = 40, cap_fps = 0;
    bool room_ok = true;
    int skip_to = -1;
    float need = kGain;
};

/// One window, by the frame rate itself. (The GPU's share of the frame was the first rule and never moved: on a base PS5 at
/// 1080p it read 57 % with FSR 4 at Quality, where FSR 3.1 ran 6 fps faster and the plain stretch 8 to 10: the GPU's time is
/// on the frame's path long before it fills it. 2026-11-28.)
/// Down: under the low line two windows in a row. The step is judged by the first window after it with the draws within
/// 15 % of those before (the same scene: in Castle Town a turn of the camera is 700 draws or 2,600, 48 fps or 28, whatever
/// the upscaler; unjudged after kJudge windows, it is given back). Less than `need` faster: a smaller 3D under the same engine goes
/// on to the next engine's first rung, judged against the same frame rate (FSR 4's cost does not follow the 3D's size: Quality
/// to Performance bought nothing where FSR 3.1 bought 6 fps); else the rung is given back and no step down is tried for
/// kBlocked windows (the CPU holds the frame rate).
/// Up: over the low line by what the step down to this rung bought (kGainGuess when not measured) and 5 % more, so the rung
/// above would still be over the line, four windows in a row; a step up that came down again within kHeld windows doubles
/// that wait (to kUpMax), one that held puts it back
inline Gov step(Gov g, int rungs, const In& in) {
    if (g.rung >= rungs) g.rung = rungs - 1;
    if (g.rung < 0) g.rung = 0;
    if (rungs < 2 || !(in.fps > 0)) return g;
    if (g.since_up < 1000) g.since_up++;
    if (g.since_up == kHeld) g.wait = kUp;
    if (g.blocked > 0) g.blocked--;
    if (g.hold > 0) {
        g.hold--;
        return g;
    }
    if (g.before > 0) {   // (the step down's verdict)
        const float d = g.before_draws > 0 ? in.draws / g.before_draws : 1.0f;
        if (d > 1 + kSameScene || d < 1 - kSameScene) {
            if (++g.judging < kJudge) return g;
            // (never the same scene again: a step nothing shows the worth of is given back, and the next waits a little)
            g.rung = g.back >= 0 ? g.back : g.rung > 0 ? g.rung - 1 : 0;
            g.before = 0, g.back = -1, g.judging = 0, g.low = g.high = 0, g.hold = kHold, g.blocked = kJudge;
            return g;
        } else {
            const float ratio = in.fps / g.before;
            g.judging = 0;
            if (ratio < in.need && g.rung > 0 && g.back < 0 && in.skip_to > g.rung && in.skip_to < rungs) {
                g.back = g.rung - 1, g.rung = in.skip_to, g.low = g.high = 0, g.hold = kHold;   // (g.before: still the one to beat)
                return g;
            }
            g.before = 0;
            if (ratio < in.need && g.rung > 0) {
                g.rung = g.back >= 0 ? g.back : g.rung - 1;
                g.back = -1, g.low = g.high = 0, g.hold = kHold, g.blocked = kBlocked;
                return g;
            }
            g.back = -1;
            if (g.rung < kMaxRungs) g.gain[g.rung] = ratio > 1.5f ? 1.5f : ratio;
        }
    }
    const float bought = g.rung < kMaxRungs && g.gain[g.rung] > 0 ? g.gain[g.rung] : kGainGuess;
    float high = in.low_fps * bought * kUpMargin, low = in.low_fps;
    if (in.cap_fps > 0 && high > in.cap_fps * 0.97f) high = in.cap_fps * 0.97f, low = low < in.cap_fps * 0.85f ? low : in.cap_fps * 0.85f;
    if (in.fps < low) {
        g.high = 0;
        if (++g.low >= kDown && g.rung < rungs - 1 && !g.blocked) {
            g.rung++, g.low = 0, g.hold = kHold, g.before = in.fps, g.before_draws = in.draws, g.judging = 0;
            if (g.since_up < kHeld) g.wait = g.wait * 2 > kUpMax ? kUpMax : g.wait * 2;
            g.since_up = 1000;
        }
    } else if (in.fps > high && in.room_ok) {
        g.low = 0;
        if (++g.high >= g.wait && g.rung > 0) g.rung--, g.high = 0, g.hold = kHold, g.since_up = 0;
    } else {
        g.low = g.high = 0;
    }
    return g;
}

/// The Upscaler row's next value with Auto as one more stop, between the last engine and the first (upscaler_rows.h
/// step_engine wraps there). *auto_on: whether Auto is chosen, in and out. Leaving Auto: Right the first engine, Left the last
inline int step_engine_auto(int upscaler, int* auto_on, int dir, unsigned available) {
    constexpr int kEngines[] = {ur::GAME, ur::DLSS, ur::FSR3, ur::FSR4, ur::XESS, ur::OFF};   // (step_engine's order)
    constexpr int n = sizeof kEngines / sizeof kEngines[0];
    const auto at = [&](int u) {
        for (int i = 0; i < n; i++)
            if (kEngines[i] == ur::engine(u)) return i;
        return 0;
    };
    if (*auto_on) {
        *auto_on = 0;
        // (from before the first, or after the last: step_engine from the other end)
        return ur::step_engine(dir > 0 ? ur::OFF : ur::GAME, dir, available);
    }
    const int next = ur::step_engine(upscaler, dir, available);
    const bool wrapped = dir > 0 ? at(next) <= at(upscaler) : at(next) >= at(upscaler);
    if (!wrapped) return next;
    *auto_on = 1;
    return upscaler;   // (the governor picks from here: menu_upscale_gov_tick)
}

} // namespace mkwvk::upscale_gov
