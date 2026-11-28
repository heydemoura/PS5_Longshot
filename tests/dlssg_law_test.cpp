// (tests/test_dlssg_pace.py) gpu/vk/dlssg_law.h: the reader of the law NVIDIA's driver meters DLSS-G's presents by, and
// its asks for the toggle. Made-up presents and screen times on a made-up counter (10 MHz, QueryPerformanceCounter's
// usual): the game at 60 fps presenting batches of two, each batch's second present reaching the screen about 7 ms after
// the batch before's first call, steadily (the even law, L1 here) or spread over more than a millisecond (the uneven one,
// L2); or about 14 ms after it, where the driver's time follows the game's image instead of the made one, with a quarter of
// them near 12 ms under L1, and spread over 3 ms under L2. Nothing here comes from a run of the game
#include "../gpu/vk/dlssg_law.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>

using namespace mkwvk::dlssg_law;

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); std::exit(1); } } while (0)

constexpr int64_t kHz = 10000000, kMs = kHz / 1000, kS = kHz;
constexpr int64_t kFrame = kHz / 60;       // (the game at 60 fps)
constexpr int64_t kToggle = 22 * kMs;      // (the game's hold for an ask)

static uint32_t seed = 4242;
static double noise(double most) {   // (-most to most, the same on every run)
    seed = seed * 1664525u + 1013904223u;
    return ((double)((seed >> 8) & 0xffff) / 32767.5 - 1.0) * most;
}
static double l1() { return 7.0 + noise(0.1); }
static double l2() { return 7.0 + noise(0.6); }
static double g1() { return noise(1.0) > 0.5 ? 12.1 + noise(0.1) : 14.2 + noise(0.15); }   // (the game's image's band)
static double g2() { return 14.2 + noise(1.5); }

/// The game, the DLSS-G plugin and the layer around the reader
struct Sim {
    Reader r;
    int64_t t = 1000 * kS;   // (the counter at the last batch's first call)
    uint64_t n = 100;        // (the next present's number)
    int64_t before = 0;      // (the batch before's first call)
    uint64_t back_n = 0;     // (a time not come back yet: the last batch's second present, and its first_pixel_out)
    int64_t back_at = 0;
    enum { HOLD, DROP } answer = HOLD;
    bool hold_next = false;
    bool times = true;       // (the driver gives the presents' times)
    // (first_pixel_out in a domain of its own: nanoseconds from elsewhere, calibrated against the counter now and then)
    bool domain = false, calibrate = true;
    std::vector<Event> log;
    std::vector<int64_t> asks, holds;

    int64_t domain_from = 5000000000000ll;   // (the domain's time at the counter's 123456789)
    int64_t to_domain(int64_t q) const { return (q - 123456789) * 100 + domain_from; }

    /// One game frame: its batch, x_ms after the batch before's first call on the screen; the frame `step` after the last
    void frame(double x_ms, int64_t step = kFrame) {
        if (hold_next) {   // (the game holds this frame's present for the ask: the hold starts a little before it)
            hold_next = false;
            r.held(t + step - 6 * kMs);
            holds.push_back(t + step);
            step += kToggle;
        }
        t += step;
        if (domain && calibrate && n % 120 == 100 % 120) r.calibrated(to_domain(t) + (int64_t)noise(300), t);
        r.batch_first(n, t);
        r.other_present(n + 1, t + kMs / 5);
        if (back_n && times) r.time(back_n, (uint64_t)(domain ? to_domain(back_at) : back_at), !domain);
        back_n = before ? n + 1 : 0, back_at = before + (int64_t)(x_ms * (double)kMs);
        before = t, n += 2;
        for (const Event& e : r.events) log.push_back(e);
        r.events.clear();
        if (r.ask_at) {   // (the layer hands the ask to the game, which holds its next present or lets the ask go)
            CHECK(r.ask_at == t);
            r.ask_at = 0;
            asks.push_back(t);
            if (answer == HOLD) hold_next = true;
            else r.ask_dropped();
        }
    }
    void run(double seconds, double (*law)(), int64_t step = kFrame) {
        for (const int64_t end = t + (int64_t)(seconds * (double)kS); t < end;) frame(law(), step);
    }
    int count(char kind, char what) const {   // (r: its state; w: its why, or 'a' asked)
        int c = 0;
        for (const Event& e : log) c += e.kind == kind && (kind == 'r' ? e.state == what : what == 'a' ? e.asked : e.why == what);
        return c;
    }
    int count(char kind) const {
        int c = 0;
        for (const Event& e : log) c += e.kind == kind;
        return c;
    }
};

static void percentiles() {
    const std::vector<double> v{1, 2, 3, 4};
    CHECK(percentile(v, 50) == 2.5 && percentile(v, 0) == 1 && percentile(v, 100) == 4);
    CHECK(std::abs(percentile(v, 90) - 3.7) < 1e-9);
    CHECK(percentile({5}, 90) == 5);
}

static void even_law() {
    // (a minute of the even law: every half second read L1, the reader reading from its tenth, and nothing asked)
    Sim s;
    s.run(60, l1);
    CHECK(s.count('r') >= 117 && s.count('r', '1') == s.count('r') && s.count('w') == 0 && s.asks.empty());
    int read = 0;
    for (const Event& e : s.log) {
        CHECK(e.values >= 29 && e.in_band == e.values && e.band == 'm' && e.spread_ms < kBands[0].spread_ms);
        CHECK(e.median_ms > 6.9 && e.median_ms < 7.1);
        CHECK(e.reading == (++read >= kReadable));
    }
}

static void uneven_law() {
    // (the uneven law from the fifteenth second: asked after two half seconds of it, once; the game's hold changes the law, and
    // the gap it leaves is not read)
    Sim s;
    s.run(15, l1);
    const int64_t from = s.t;
    while (s.asks.empty() && s.t < from + 10 * kS) s.frame(l2());
    CHECK(s.asks.size() == 1 && s.asks[0] - from >= 1 * kS && s.asks[0] - from <= 17 * kS / 10);
    CHECK(s.count('r', '2') == kRun && s.count('w', 'a') == 1);
    s.run(20, l1);
    CHECK(s.asks.size() == 1 && s.holds.size() == 1 && s.count('w') == 1 && s.count('r', '2') == kRun);
    CHECK(s.count('r', 'p') >= 1 && s.count('r', 'p') <= 3 && s.count('r', '-') == 0);
    CHECK(s.r.in_a_row == 0 && s.log.back().reading);
}

static void the_game_image() {
    // (the driver's time following the game's image: read in its band, L1 then L2, asked about as soon, once)
    Sim s;
    s.run(15, g1);
    CHECK(s.count('r') >= 29 && s.count('r', '1') == s.count('r') && s.count('w') == 0 && s.log.back().reading);
    for (const Event& e : s.log) CHECK(e.band == 'g' && e.in_band == e.values && e.median_ms > 13.9 && e.median_ms < 14.5);
    const int64_t from = s.t;
    while (s.asks.empty() && s.t < from + 10 * kS) s.frame(g2());
    CHECK(s.asks.size() == 1 && s.asks[0] - from >= 1 * kS && s.asks[0] - from <= 17 * kS / 10);
    CHECK(s.count('r', '2') == kRun && s.count('w', 'a') == 1);
    s.run(20, g1);
    CHECK(s.asks.size() == 1 && s.count('r', '2') == kRun && s.r.in_a_row == 0 && s.log.back().reading);
}

static void bands() {
    // (each half second read in the band most of its values fall in, the made image's on a tie; two half seconds L2 in a
    // row in the same band only; the game's band takes only values whose batches are a 60 fps frame apart)
    Reader r;
    r.hz = kHz;
    r.toggle = false;
    int64_t a = 1000 * kS;
    const auto slice = [&](int m, int g, bool spread, bool paced = true) {
        for (int i = 0; i < m; i++) r.values.push_back({a + i, 7.0 + (spread ? (i % 4) * 0.4 : 0), true});
        for (int i = 0; i < g; i++) r.values.push_back({a + m + i, 14.2 + (spread ? (i % 4) * 0.8 : 0), paced});
        r.read_slice(a, a + kS / 2, a + kS / 2 + 100 * kMs);
        a += kS / 2;
        return r.events.back().kind == 'w' ? r.events[r.events.size() - 2] : r.events.back();
    };
    Event e = slice(12, 3, true);
    CHECK(e.band == 'm' && e.state == '2' && e.l2_run == 1 && e.in_band == 12 && e.values == 15);
    e = slice(2, 12, true);
    CHECK(e.band == 'g' && e.state == '2' && e.l2_run == 1 && r.events.back().kind == 'r');
    e = slice(0, 12, true);
    CHECK(e.band == 'g' && e.state == '2' && e.l2_run == 2 && r.events.back().kind == 'w' && r.events.back().why == 'o');
    e = slice(10, 10, false);
    CHECK(e.band == 'm' && e.state == '1' && e.in_band == 10);
    e = slice(0, 12, false, false);
    CHECK(e.band == 0 && e.state == '-' && e.in_band == 0 && e.values == 12);
    // (from the game's presents: a batch a 40 fps frame after the one before is not paced, one a 60 fps frame after is)
    Reader p;
    p.hz = kHz;
    p.batch_first(10, 1000 * kS);
    p.other_present(11, 1000 * kS + kMs / 5);
    p.batch_first(12, 1000 * kS + 25 * kMs - kMs);
    p.other_present(13, 1000 * kS + 24 * kMs + kMs / 5);
    p.batch_first(14, 1000 * kS + 24 * kMs + kFrame);
    p.other_present(15, 1000 * kS + 24 * kMs + kFrame + kMs / 5);
    p.time(13, (uint64_t)(1000 * kS + 12 * kMs), true);
    p.time(15, (uint64_t)(1000 * kS + 24 * kMs + 14 * kMs), true);
    CHECK(p.values.size() == 2 && !p.values[0].paced && p.values[1].paced && std::abs(p.values[1].x - 14.0) < 1e-6);
}

static void not_reading_yet() {
    // (the uneven law from the start: not asked before the reader has read its ten half seconds)
    Sim s;
    while (s.asks.empty() && s.t < 1020 * kS) s.frame(l2());
    CHECK(s.asks.size() == 1 && s.asks[0] - 1000 * kS >= 5 * kS && s.asks[0] - 1000 * kS < 6 * kS);
    CHECK(s.count('w', 'r') == kReadable / kRun - 1 && s.count('w', 'a') == 1);
}

static void holds_that_change_nothing() {
    // (a screen where the reader reads the uneven law whatever the game does, but for a half second now and then: three
    // asks, then one after each quiet spell, each twice as long)
    Sim s;
    s.run(15, l1);
    const int64_t from = s.t;
    s.run(1200, l2);
    CHECK(s.asks.size() >= 5 && s.asks.size() <= 9 && s.holds.size() + 1 >= s.asks.size());
    CHECK(s.count('r', '1') > 33);   // (the 15 s's 30 at most, and some of the 1200 s's)
    CHECK(s.asks[kAsksInARow - 1] - from < 10 * kS);
    for (size_t i = kAsksInARow; i < s.asks.size(); i++)
        CHECK(s.asks[i] - s.asks[i - 1] >= ((int64_t)kQuietS * kS << (i - kAsksInARow)));
    for (size_t i = kAsksAMinute; i < s.asks.size(); i++) CHECK(s.asks[i] - s.asks[i - kAsksAMinute] >= 60 * kS);
    CHECK(s.count('w', 'q') > 100 && s.r.quiet_until > s.t);
    // (another swapchain: what was read is gone, the quiet spell is not)
    const int64_t until = s.r.quiet_until;
    s.r.reset();
    s.before = 0, s.back_n = 0;
    CHECK(s.r.quiet_until == until && !s.r.reading);
    const size_t asked = s.asks.size();
    s.run(8, l2);
    CHECK(s.asks.size() == asked);
    // (the even law again, kEvenRun half seconds of it: the next uneven half seconds are asked about at once)
    s.run(5, l1);
    CHECK(s.r.quiet_until == 0 && s.r.in_a_row == 0);
    const int64_t again = s.t;
    while (s.asks.size() == asked && s.t < again + 10 * kS) s.frame(l2());
    CHECK(s.asks.size() == asked + 1 && s.asks.back() - again <= 17 * kS / 10);
}

static void a_law_that_comes_back() {
    // (each hold brings the even law for four seconds only: six asks a minute at most)
    Sim s;
    s.run(15, l1);
    int even = 0;
    size_t held = 0;
    for (const int64_t end = s.t + 300 * kS; s.t < end;) {
        if (s.holds.size() != held) held = s.holds.size(), even = 240;
        s.frame(even-- > 0 ? l1() : l2());
    }
    CHECK(s.asks.size() >= 4 * kAsksAMinute && s.asks.size() <= 6 * kAsksAMinute);
    for (size_t i = kAsksAMinute; i < s.asks.size(); i++) CHECK(s.asks[i] - s.asks[i - kAsksAMinute] >= 60 * kS);
    CHECK(s.count('w', 'm') > 0 && s.count('w', 'q') == 0);
}

static void a_law_that_comes_back_at_once() {
    // (each hold brings the even law for a second only, under kEvenRun half seconds read: the holds count as changing
    // nothing, three asks and then the quiet spells)
    Sim s;
    s.run(15, l1);
    int even = 0;
    size_t held = 0;
    for (const int64_t end = s.t + 1200 * kS; s.t < end;) {
        if (s.holds.size() != held) held = s.holds.size(), even = 60;
        s.frame(even-- > 0 ? l1() : l2());
    }
    CHECK(s.asks.size() >= 5 && s.asks.size() <= 9 && s.count('w', 'q') > 100);
}

static void asks_let_go() {
    // (the game lets every ask go, the toggle not fitting where it is: nothing counts against the reader's bounds)
    Sim s;
    s.answer = Sim::DROP;
    s.run(15, l1);
    s.run(60, l2);
    CHECK(s.holds.empty() && s.asks.size() >= 50 && s.r.asked.empty() && s.r.in_a_row == 0 && s.r.quiet_until == 0);
    CHECK(s.count('w', 'q') == 0 && s.count('w', 'm') == 0);
}

static void toggle_off() {
    // (RX_VK_DLSSG_TOGGLE=0: the law is read, for the CSV, and nothing asked)
    Sim s;
    s.r.toggle = false;
    s.run(15, l1);
    s.run(30, l2);
    CHECK(s.asks.empty() && s.count('w', 'o') >= 25 && s.count('w', 'o') == s.count('w') && s.count('r', '2') >= 55);
}

static void not_read() {
    // (the game at 30 fps: every batch a gap from the one before. Nothing read, nothing asked)
    Sim slow;
    slow.run(60, l2, 2 * kFrame);
    CHECK(slow.count('r') > 100 && slow.count('r', 'p') == slow.count('r') && slow.count('w') == 0 && !slow.r.reading);
    // (at 120 fps the screen time falls under the bands, at 40 fps between them, and the game's band takes no value from
    // batches that far apart: not read either, whatever its spread)
    for (const double x : {3.2, 12.0}) {
        Sim s;
        for (int i = 0; i < 6000; i++) s.frame(x + noise(0.6), x < 5 ? kFrame / 2 : kFrame * 3 / 2 - kMs);
        CHECK(s.count('r') > 50 && s.count('r', '-') == s.count('r') && s.count('w') == 0 && !s.r.reading);
        for (const Event& e : s.log) CHECK(e.in_band == 0 && e.values > 0);
    }
    // (a driver that gives no times, or times in a domain never calibrated: nothing read)
    Sim blind;
    blind.times = false;
    blind.run(30, l2);
    Sim loose;
    loose.domain = true, loose.calibrate = false;
    loose.run(30, l2);
    for (const Sim* s : {&blind, &loose}) {
        CHECK(s->count('r') > 50 && s->count('r', '-') == s->count('r') && s->count('w') == 0 && !s->r.reading);
        for (const Event& e : s->log) CHECK(e.values == 0);
    }
    // (too few half seconds read among the last twenty: two uneven ones in a row are not asked about)
    Sim few;
    for (int k = 0; k < 6; k++) {
        few.times = false;
        few.run(5, l2);
        few.times = true;
        few.run(1.6, l2);
    }
    CHECK(few.asks.empty() && few.count('w', 'r') >= 3 && few.count('w', 'r') == few.count('w'));
}

static void gaps() {
    // (a hitch of the game's, a tenth of a second without a batch: the half seconds around it are a pause, and what the
    // screen showed across it is not read)
    Sim s;
    s.run(15, l1);
    const int before = s.count('r', 'p');
    CHECK(before == 0);
    s.frame(l2(), kFrame + 100 * kMs);
    for (int i = 0; i < 20; i++) s.frame(l2());   // (uneven for a third of a second after it)
    s.run(10, l1);
    CHECK(s.count('r', 'p') >= 1 && s.count('r', 'p') <= 3 && s.count('r', '2') == 0 && s.count('w') == 0 && s.asks.empty());
    // (and a hitch does not end an uneven law's run for good: asked once the pause is over)
    Sim h;
    h.run(15, l1);
    h.run(0.7, l2);
    h.frame(l2(), kFrame + 100 * kMs);
    const int64_t from = h.t;
    while (h.asks.empty() && h.t < from + 10 * kS) h.frame(l2());
    CHECK(h.asks.size() == 1 && h.asks[0] - from < 25 * kS / 10);
}

static void time_domain() {
    // (first_pixel_out in its own domain, calibrated against the counter every second, each calibration off by a few
    // hundred nanoseconds: read as the counter's own)
    Sim s;
    s.domain = true;
    s.run(15, l1);
    CHECK(s.count('r', '1') == s.count('r') && s.count('r') >= 28 && s.log.back().reading);
    double q = 0;
    CHECK(s.r.to_counter((uint64_t)s.to_domain(s.t), false, q) && std::abs(q - (double)s.t) < 10);
    const int64_t from = s.t;
    while (s.asks.empty() && s.t < from + 10 * kS) s.frame(l2());
    CHECK(s.asks.size() == 1 && s.asks[0] - from <= 17 * kS / 10);
    // (one calibration alone: the counter's own rate taken)
    Reader one;
    one.hz = kHz;
    CHECK(!one.to_counter(12345, false, q));
    one.calibrated(s.to_domain(5000 * kS), 5000 * kS);
    CHECK(one.to_counter((uint64_t)s.to_domain(5001 * kS), false, q) && std::abs(q - (double)(5001 * kS)) < 2);
    CHECK(one.to_counter(777, true, q) && q == 777);
}

static void another_domain() {
    // (the swapchain's time domain changes while it lives: nanoseconds from another origin. The layer starts the reader again
    // and calibrates it at once (vk_dlssg_layer.cpp read_back), and the law is read as before. A reader left with the old
    // domain's first calibration draws its slope from one domain to the other, and reads the law in no half second after)
    for (const bool starts_again : {true, false}) {
        Sim s;
        s.r.hz = kHz;
        s.domain = true;
        s.run(15, l1);
        CHECK(s.log.back().reading);
        s.domain_from = 77000000000ll;
        s.back_n = 0;   // (a time asked in the old domain is not given to the reader)
        if (starts_again) s.r.reset();
        s.r.calibrated(s.to_domain(s.t), s.t);
        const size_t before = s.log.size();
        s.run(15, l1);
        int even = 0;
        for (size_t i = before; i < s.log.size(); i++) even += s.log[i].kind == 'r' && s.log[i].state == '1';
        if (starts_again) CHECK(even >= 28 && s.log.back().reading && s.asks.empty());
        else CHECK(even <= 1 && !s.log.back().reading);   // (one: the half second that ended as the domain changed)
    }
}

static void batches() {
    // (a batch's second present is the one right after its first, within 2 ms; another present is not taken for it)
    Reader r;
    r.hz = kHz;
    r.other_present(5, 1000 * kS);   // (no batch yet)
    r.batch_first(10, 1000 * kS);
    r.other_present(12, 1000 * kS + kMs / 5);   // (not the next present)
    CHECK(r.batches.back().n1 == 0);
    r.other_present(11, 1000 * kS + 3 * kMs);   // (too late)
    CHECK(r.batches.back().n1 == 0);
    r.other_present(11, 1000 * kS + kMs);
    CHECK(r.batches.back().n1 == 11);
    r.other_present(11, 1000 * kS + kMs);
    r.batch_first(20, 1000 * kS + kFrame);
    r.other_present(21, 1000 * kS + kFrame + kMs / 5);
    // (a time for a present that is no batch's second, or with no time: nothing)
    r.time(20, (uint64_t)(1000 * kS + 7 * kMs), true);
    r.time(21, 0, true);
    r.time(0, (uint64_t)(1000 * kS + 7 * kMs), true);
    CHECK(r.values.empty());
    r.time(21, (uint64_t)(1000 * kS + 7 * kMs), true);
    CHECK(r.values.size() == 1 && r.values[0].at == 1000 * kS && std::abs(r.values[0].x - 7.0) < 1e-6);
    // (the first batch has none before it)
    r.time(11, (uint64_t)(1000 * kS + 7 * kMs), true);
    CHECK(r.values.size() == 1);
    for (int i = 0; i < 200; i++) r.batch_first(100 + 2 * (uint64_t)i, 1001 * kS + i * kFrame);
    CHECK(r.batches.size() == 64);
}

int main() {
    static_assert(kBatch == 2 && kRun >= 2 && kReadable <= kReadableOf && kAsksInARow <= kAsksAMinute);
    percentiles();
    batches();
    another_domain();
    even_law();
    uneven_law();
    the_game_image();
    bands();
    not_reading_yet();
    holds_that_change_nothing();
    a_law_that_comes_back();
    a_law_that_comes_back_at_once();
    asks_let_go();
    toggle_off();
    not_read();
    gaps();
    time_domain();
    std::puts("PASS: the driver's uneven law is read from the presents' screen times, whichever image they follow, and asked "
              "about once it is read for a second, within its bounds, and nothing is asked where the reader does not read");
    return 0;
}
