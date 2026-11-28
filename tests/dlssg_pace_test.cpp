// (tests/test_dlssg_pace.py) gpu/vk/dlssg_pace.h: when a game frame's present goes to Streamline. The hold after the
// game's own present, the line through the game's presents it counts from (the grid), and where the toggle fits. Made-up
// presents on a made-up clock, in nanoseconds
#include "../gpu/vk/dlssg_pace.h"

#include <cstdio>
#include <cstdlib>

using namespace mkwvk::dlssg_pace;

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); std::exit(1); } } while (0)

constexpr int64_t kMs = 1000000, kUs = 1000;
constexpr int64_t kStart = 5000 * kMs;   // (the clock at the first present)
constexpr int64_t kLag = 3 * kMs;        // (the render thread reaches the present that long after the game's)

static bool close_to(int64_t a, int64_t b, int64_t within) { return std::llabs(a - b) <= within; }

// A jitter of at most `most` either way, the same on every run
static uint32_t seed = 12345;
static int64_t jitter(int64_t most) {
    seed = seed * 1664525u + 1013904223u;
    return (int64_t)((seed >> 8) % (uint32_t)(2 * most + 1)) - most;
}

// The game presenting every `frame`, a present every call
struct Game {
    Hold hold;
    int64_t frame = kFrame60;
    int64_t line = kStart - kFrame60;   // (the game's own clock: where its present would be without jitter)
    int64_t at = 0;                     // (the last present)
    int64_t present(int64_t off = 0, int64_t wait = 0, int64_t lag = kLag) {
        line += frame;
        at = line + off;
        return hold.target(at, wait, at + lag);
    }
};

static void hold_alone() {
    Game g;
    g.hold.grid_max = 0;
    // (the first present: no frame time yet, it goes now)
    CHECK(g.present() == 0);
    // (from the second: the hold after the game's own present, whatever the render thread's lag)
    for (int i = 0; i < 200; i++) {
        const int64_t lag = kLag + (i % 7) * 900 * kUs;   // (3 to 8.4 ms)
        CHECK(g.present(0, 0, lag) == g.at + kHoldUs * kUs);
        CHECK(!g.hold.on_grid);
    }
    // (late already: it goes now)
    CHECK(g.present(0, 0, kHoldUs * kUs) == 0);
    CHECK(g.present(0, 0, 15 * kMs) == 0);
    // (a present with no time, the same one again, one from before it: not held, and not taken in)
    CHECK(g.hold.target(0, 0, g.at + kLag) == 0);
    CHECK(g.hold.target(g.at, 0, g.at + kLag) == 0);
    CHECK(g.hold.target(g.at - kMs, 0, g.at + kLag) == 0);
    // (nor with the clock before the present, or the render thread a tenth of a second behind)
    CHECK(g.hold.target(g.at + kFrame60, 0, g.at + kFrame60 - 1) == 0);
    CHECK(g.hold.target(g.at + kFrame60, 0, g.at + kFrame60 + 101 * kMs) == 0);
    CHECK(g.hold.last_game == g.at && g.hold.frame == kFrame60);
    CHECK(g.present() == g.at + kHoldUs * kUs);
    // (the switch at 0: no hold)
    Game off;
    off.hold.hold = 0;
    for (int i = 0; i < 100; i++) CHECK(off.present() == 0);
}

static void frame_rates() {
    // (30 fps: the whole hold, as at 60)
    Game slow;
    slow.frame = 2 * kFrame60, slow.line = kStart - slow.frame;
    slow.hold.grid_max = 0;
    slow.present();
    for (int i = 0; i < 100; i++) CHECK(slow.present() == slow.at + kHoldUs * kUs);
    // (120 fps: the same share of the frame, half the hold; 240: a quarter)
    for (int div : {2, 4}) {
        Game fast;
        fast.frame = kFrame60 / div, fast.line = kStart - fast.frame;
        fast.hold.grid_max = 0;
        fast.present();
        for (int i = 0; i < 100; i++) CHECK(close_to(fast.present(), fast.at + kHoldUs * kUs / div, 10));
    }
    // (above 60 fps, worked out again only when the frame time moved by a sixteenth: its own noise does not move the hold)
    Hold h;
    const int64_t at_120 = h.hold_for(kFrame60 / 2);
    CHECK(close_to(at_120, kHoldUs * kUs / 2, 10));
    CHECK(h.hold_for(kFrame60 / 2 + 400 * kUs) == at_120 && h.hold_for(kFrame60 / 2 - 400 * kUs) == at_120);
    CHECK(h.hold_for(9 * kMs) == kHoldUs * kUs * (9 * kMs) / kFrame60);   // (9 ms of 16.67: that share of the hold)
    // (at 60 fps the whole hold whatever came before, and whatever the frame time's noise: to a sixteenth above 60 fps)
    CHECK(h.hold_for(kFrame60) == kHoldUs * kUs && h.hold_for(3 * kFrame60) == kHoldUs * kUs);
    CHECK(h.hold_for(kFrame60 - 900 * kUs) == kHoldUs * kUs && h.hold_for(kFrame60 + 900 * kUs) == kHoldUs * kUs);
    CHECK(h.hold_for(kFrame60 - 1200 * kUs) < kHoldUs * kUs);
    // (a game that starts slowly and settles at 60 fps: the whole hold, not what its first frames gave)
    Game settling;
    settling.hold.grid_max = 0;
    settling.frame = 3 * kFrame60 / 4, settling.line = kStart - settling.frame;
    for (int i = 0; i < 5; i++) settling.present();
    CHECK(settling.present() < settling.at + kHoldUs * kUs);
    settling.frame = kFrame60;
    for (int i = 0; i < 100; i++) settling.present();
    CHECK(settling.present() == settling.at + kHoldUs * kUs);
}

static void below_the_cap() {
    // (the game's thread waits 2 ms for the renderer in each present: the game does not reach its cap, its presents follow
    // the renderer, and holding them would slow it further)
    Game g;
    for (int i = 0; i < 70; i++) g.present();
    CHECK(g.present() != 0 && g.hold.on_grid && !g.hold.below);
    for (int i = 0; i < 30; i++) g.present(0, 2 * kMs);
    CHECK(g.hold.below && g.present(0, 2 * kMs) == 0 && g.hold.grid.count == 0);
    CHECK(!g.hold.toggle_fits(1));
    // (a wait now and then is not that: a frame in ten)
    Game spiky;
    for (int i = 0; i < 300; i++) {
        spiky.present(0, i % 10 == 0 ? 2 * kMs : 0);
        CHECK(!spiky.hold.below);
    }
    // (back at its cap: held again, and the line once it has its presents)
    for (int i = 0; i < 40; i++) g.present();
    CHECK(!g.hold.below && g.present() == g.at + kHoldUs * kUs && !g.hold.on_grid);
    for (int i = 0; i < 70; i++) g.present();
    CHECK(g.present() != 0 && g.hold.on_grid);
}

static void grid_line() {
    // (even presents: the line is the presents, once it has Grid::W of them)
    Grid even;
    for (int i = 0; i < Grid::W; i++) CHECK(even.place(kStart + i * kFrame60, kFrame60) == 0);
    for (int i = Grid::W; i < 3 * Grid::W; i++) CHECK(close_to(even.place(kStart + i * kFrame60, kFrame60), kStart + i * kFrame60, 2));
    // (a jitter of 0.3 ms either way: the line stays within a fraction of it of the game's clock)
    Grid jittered;
    for (int i = 0; i < 600; i++) {
        const int64_t line = kStart + i * kFrame60;
        const int64_t g = jittered.place(line + jitter(300 * kUs), kFrame60);
        if (i >= Grid::W) CHECK(close_to(g, line, 250 * kUs));
    }
    // (one present 5 ms late among them: left out of the line)
    Grid late;
    for (int i = 0; i < 3 * Grid::W; i++) {
        const int64_t line = kStart + i * kFrame60;
        const int64_t g = late.place(line + (i == 70 ? 5 * kMs : 0), kFrame60);
        if (i >= Grid::W) CHECK(close_to(g, line, 2));
    }
    // (a frame the game skipped: the line keeps its numbers)
    Grid skipped;
    for (int i = 0; i < 3 * Grid::W; i++) {
        if (i == 80 || i == 81 || i == 100) continue;
        const int64_t line = kStart + i * kFrame60;
        const int64_t g = skipped.place(line, kFrame60);
        if (i >= Grid::W + 3) CHECK(close_to(g, line, 2));
    }
    // (the last presents over too many frames, every other one skipped: no line)
    Grid sparse;
    for (int i = 0; i < 3 * Grid::W; i++) CHECK(sparse.place(kStart + 2 * i * kFrame60, kFrame60) == 0);
    // (no frame time: none)
    CHECK(even.place(kStart + 4 * Grid::W * kFrame60, 0) == 0);
}

static void hold_on_the_grid() {
    // (the game's presents jitter by 0.3 ms either way: held from the line, the presents sent are as even as the game's
    // clock, not as the game's presents)
    Game g;
    seed = 777;
    int64_t before = 0, worst = 0, worst_game = 0, game_before = 0;
    for (int i = 0; i < 600; i++) {
        const int64_t t = g.present(jitter(300 * kUs));
        if (i > Grid::W + 1) {
            CHECK(t != 0 && g.hold.on_grid);
            CHECK(close_to(t, g.line + kHoldUs * kUs, 250 * kUs));
            if (before) worst = std::max<int64_t>(worst, std::llabs(t - before - kFrame60));
            worst_game = std::max<int64_t>(worst_game, std::llabs(g.at - game_before - kFrame60));
            before = t;
        }
        game_before = g.at;
    }
    CHECK(worst < 100 * kUs && worst_game > 400 * kUs);
    // (a present further from the line than the grid reaches, 1.5 ms: held from the present itself)
    CHECK(g.present(2 * kMs) == g.at + kHoldUs * kUs && !g.hold.on_grid);
    CHECK(g.present(-1 * kMs) != g.at + kHoldUs * kUs && g.hold.on_grid);
    // (the grid's switch at 0: from the present, always)
    Game off;
    off.hold.grid_max = 0;
    for (int i = 0; i < 200; i++) {
        const int64_t j = jitter(300 * kUs);
        const int64_t t = off.present(j);
        if (i) CHECK(t == off.at + kHoldUs * kUs && !off.hold.on_grid);
    }
    // (a tenth of a second without a present, a loading hitch: the line starts again from the presents after it)
    Game hitch;
    for (int i = 0; i < 100; i++) hitch.present();
    CHECK(hitch.hold.on_grid);
    hitch.line += 200 * kMs;
    for (int i = 0; i < Grid::W; i++) CHECK(hitch.present() == hitch.at + kHoldUs * kUs && !hitch.hold.on_grid);
    hitch.present();
    CHECK(hitch.hold.on_grid && hitch.hold.frame == kFrame60);
    // (120 fps: the grid reaches an eighth of the frame, 1.04 ms, where 1.5 would be a fifth of it)
    Game fast;
    fast.frame = kFrame60 / 2, fast.line = kStart - fast.frame;
    for (int i = 0; i < 100; i++) fast.present();
    CHECK(fast.hold.on_grid);
    fast.present(1200 * kUs);
    CHECK(!fast.hold.on_grid);
    Game same;
    for (int i = 0; i < 100; i++) same.present();
    same.present(1200 * kUs);
    CHECK(same.hold.on_grid);
}

static void toggle() {
    // (the toggle: at 2x, the game at its cap and at 60 fps, within a twentieth)
    CHECK(!Hold{}.toggle_fits(1));   // (no frame time yet)
    struct Case {
        double fps;
        bool fits;
    };
    for (const Case c : {Case{60, true}, Case{58, true}, Case{62.5, true}, Case{55, false}, Case{66, false}, Case{30, false},
                         Case{120, false}, Case{20, false}}) {
        Game g;
        g.frame = (int64_t)(1e9 / c.fps), g.line = kStart - g.frame;
        for (int i = 0; i < 100; i++) g.present();
        CHECK(g.hold.toggle_fits(1) == c.fits);
        CHECK(!g.hold.toggle_fits(2) && !g.hold.toggle_fits(3) && !g.hold.toggle_fits(0));   // (3x, 4x, none made)
    }
}

int main() {
    // (the defaults: the hold shorter than a frame at 60 fps and longer than the grid's reach, the toggle longer than a frame)
    static_assert(kHoldUs > 0 && kHoldUs * 1000 < kFrame60 && kGridUs > 0 && kGridUs < kHoldUs && kToggleUs * 1000 > kFrame60);
    hold_alone();
    frame_rates();
    below_the_cap();
    grid_line();
    hold_on_the_grid();
    toggle();
    std::puts("PASS: a game frame's present is held to an even time after the game's own, from the line through the game's "
              "presents where it is near it, and not where the game does not reach its cap");
    return 0;
}
