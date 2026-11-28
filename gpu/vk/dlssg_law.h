// (longshot) NVIDIA DLSS Frame Generation through NVIDIA Streamline: reading the law NVIDIA's driver meters DLSS-G's presents
// by. The DLSS-G plugin presents each game frame as a batch: the made image with VK_NV_present_metering's structure
// (VkSetPresentConfigNV, for 2 images), then the game's own right after. The driver shows the two by one of two laws: an
// even one (L1 here), and one (L2) whose images reach the screen unevenly; a screen left a little over 20 ms without a new
// image changes the law, so the game holds one present that long when L2 is read (dlssg_pace.h, the toggle).
//
// The rule, from what Longshot's layer under Streamline (vk_dlssg_layer.cpp) sees: the first_pixel_out time
// VK_EXT_present_timing gives for batch j's second present follows the screen time of one of batch j-1's images. So
//   x = that time, less the call of batch j-1's first present
// is steady under L1 and spread under L2. Which image it follows is the driver's state, not the game's: the made one
// (x near 6.6 ms under L1), or the game's own, shown 8.3 ms later (x near 14 to 15 ms). One PC showed both, 23 and 24/11
// 2026 (RTX 4090, driver 621.24): the game's after the variable refresh was turned off and on, and after a night with the
// display asleep; the made one after a restart. Each has its band (kBands), with its own spread. Per half second, over the
// x in the band most of them fall in: L2 when their 90th percentile is more than the band's spread above their median, L1
// otherwise, not read under kValues of them. A half second touching a gap is a pause, not read: two batches kGapMs or more
// apart, and each hold the game reports. After kRun half seconds L2 in a row in the same band the reader asks for the toggle,
// while it reads at all (kReadable of its last kReadableOf half seconds outside pauses).
//
// The bands and the spreads are those of 2 images a batch with the game at 60 fps. Batches of another size are not
// taken. At another frame rate the reader can still read: the game's band is wide enough to take some x (x near 12 ms
// at 40 fps), so it takes only values whose two batches are the game's frame at 60 fps apart (kFrameMs, within
// kFrameSlackMs), but the made image's band takes x at 90 fps too (x near 6 ms, 24/11 2026). The game lets such an ask
// go: it holds only at 2x with the game at 60 fps (dlssg_pace.h, Hold::toggle_fits). Where it would read wrongly
// all the same (a screen without variable refresh has no such laws), the asks are bounded: after kAsksInARow asks the
// reader keeps quiet, each time twice as long, until the law is even again, which is kEvenRun half seconds L1 with none L2
// between them (one half second L1 now and then is what a reader reading wrongly gives too); and it never asks more than
// kAsksAMinute times a minute.
//
// Times are a counter's ticks (QueryPerformanceCounter's in the game), hz a second. Nothing of Vulkan or of Streamline
// here, and no clock: tests/dlssg_law_test.cpp checks it.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <utility>
#include <vector>

namespace mkwvk::dlssg_law {

constexpr uint32_t kBatch = 2;            // (the images of a batch the rule reads)
/// A band x falls in, by the image the driver's first_pixel_out follows, and the spread over which it reads L2
struct Band {
    double low_ms, high_ms, spread_ms;
    bool paced;   // (only values whose batches are kFrameMs apart)
    char name;    // (the layer's CSV and log)
};
constexpr Band kBands[2] = {
    {5.5, 9.0, 0.20, false, 'm'},   // (the made image's: set on runs of 23/11 before 18:30)
    {11.5, 17.0, 0.50, true, 'g'},  // (the game's image's: set on 24/11 on runs of 23/11 22:01 to 22:15 and 24/11 08:41 to 09:27)
};
constexpr double kFrameMs = 1000.0 / 60, kFrameSlackMs = 2.5;   // (two batches' calls apart: the game at 60 fps)
constexpr int kValues = 10;
constexpr int kRun = 2;
constexpr int kReadable = 10, kReadableOf = 20;
constexpr int kSecondMs = 2;              // (a batch's second present comes within that of its first)
constexpr int kGapMs = 25;                // (two batches that far apart: a gap)
constexpr int kGapBeforeMs = 25, kGapAfterMs = 470;     // (the pause around a gap: before its first batch, after its second)
constexpr int kHeldBeforeMs = 30, kHeldAfterMs = 500;   // (the pause around a hold the game reports)
constexpr int kReadAfterMs = 100;         // (a half second is read that long after its end: time for its values to come back)
constexpr int kAsksInARow = 3;
constexpr int kEvenRun = 3;               // (half seconds L1 with none L2 between: the law is even, the asks count from 0 again)
constexpr int kQuietS = 30;               // (the first quiet spell; doubled up to kQuietDoubles times)
constexpr int kQuietDoubles = 5;
constexpr int kAsksAMinute = 6;

/// numpy's percentile (linear) of sorted values
inline double percentile(const std::vector<double>& v, double q) {
    const double at = q / 100.0 * (double)(v.size() - 1);
    const size_t lo = (size_t)at;
    const size_t hi = std::min(lo + 1, v.size() - 1);
    return v[lo] + (at - (double)lo) * (v[hi] - v[lo]);
}

/// What the reader did, for the layer's log and its CSV
struct Event {
    char kind = 0;         // 'r' a half second read; 'w' kRun half seconds L2 in a row
    int64_t from = 0, to = 0;   // (r: the half second; w: from the time)
    char state = 0;        // r: '1' L1, '2' L2, '-' not read, 'p' a pause
    int values = 0, in_band = 0;   // (in_band: in the band most of them fall in)
    char band = 0;                 // r: the band read ('m' the made image, 'g' the game's; 0 not read)
    double spread_ms = 0, median_ms = 0;
    int l2_run = 0;
    bool reading = false;
    bool asked = false;    // w: the toggle asked
    char why = 0;          // w, not asked: 'o' the toggle is off, 'r' not reading, 'q' keeping quiet, 'm' too many this minute
};

struct Reader {
    int64_t hz = 10000000;
    bool toggle = true;   // (false: it reads, and asks nothing)

    struct Batch {
        uint64_t n0 = 0, n1 = 0;   // (its first present's number; its second's, 0 when none came)
        int64_t call0 = 0;
    };
    struct Value {
        int64_t at;   // (batch j-1's first call)
        double x;     // (ms)
        bool paced;   // (batch j kFrameMs after batch j-1, within kFrameSlackMs)
    };
    std::deque<Batch> batches;   // (the last 64)
    // first_pixel_out's time domain against the counter: the first point and the last 5, (domain time, counter)
    bool cal_first = false;
    int64_t first_ts = 0, first_q = 0;
    int64_t pts[5][2] = {};
    int n_pts = 0, pts_head = 0;
    std::deque<std::pair<int64_t, int64_t>> pauses;   // ([from, to))
    std::vector<Value> values;                        // (not in a half second read yet)
    int64_t origin = 0, slice = 0;                    // (origin 0: no batch yet; slice: the next half second to read)
    int l2_run = 0, l1_run = 0;
    char l2_band = 0;            // (the band of the L2 half seconds in a row)
    std::deque<bool> readable;   // (the last kReadableOf half seconds outside pauses: read or not)
    bool reading = false;
    // the asks
    int64_t ask_at = 0;             // (an ask not taken yet: its time; the layer takes it)
    int in_a_row = 0, quiets = 0;   // (asks since the law was last even, kEvenRun; quiet spells since)
    int64_t quiet_until = 0;
    std::deque<int64_t> asked;      // (the last minute's asks)
    std::vector<Event> events;      // (the layer takes them)

    /// Another swapchain, or none, or the same one asking in another time domain: what was read is gone, the calibration
    /// with it; the asks' bounds stay, a quiet spell too
    void reset() {
        batches.clear(), pauses.clear(), values.clear();
        cal_first = false, n_pts = pts_head = 0;
        origin = slice = 0, l2_run = l1_run = 0, l2_band = 0;
        readable.clear(), reading = false;
        ask_at = 0;
    }

    /// A present with the metering structure for kBatch images: a batch's first (n: the present's number, call: its time)
    void batch_first(uint64_t n, int64_t call) {
        if (!batches.empty() && call - batches.back().call0 >= hz * kGapMs / 1000)
            pause(batches.back().call0 - hz * kGapBeforeMs / 1000, call + hz * kGapAfterMs / 1000);
        batches.push_back({n, 0, call});
        if (batches.size() > 64) batches.pop_front();
        if (!origin) origin = call;
        read_until(call);
    }
    /// A present without the structure: the last batch's second when it is the next present, soon enough
    void other_present(uint64_t n, int64_t call) {
        if (batches.empty()) return;
        Batch& b = batches.back();
        if (!b.n1 && n == b.n0 + 1 && call - b.call0 < hz * kSecondMs / 1000) b.n1 = n;
    }
    /// Present n's first_pixel_out time come back (its domain's; the counter's own when counter_domain)
    void time(uint64_t n, uint64_t first_pixel_out, bool counter_domain) {
        if (!n || !first_pixel_out) return;
        for (size_t j = batches.size(); j-- > 1;) {
            const Batch& b = batches[j];
            if (b.n1 != n) continue;
            const Batch& before = batches[j - 1];
            double t = 0;
            if (b.call0 - before.call0 >= hz * kGapMs / 1000 || !to_counter(first_pixel_out, counter_domain, t)) return;
            if (before.call0 < origin + slice * (hz / 2)) return;   // (its half second is read already)
            const double apart_ms = (double)(b.call0 - before.call0) * 1000.0 / (double)hz;
            values.push_back({before.call0, (t - (double)before.call0) * 1000.0 / (double)hz,
                              std::fabs(apart_ms - kFrameMs) <= kFrameSlackMs});
            return;
        }
    }
    /// first_pixel_out's domain against the counter, now and then (the domain's time, the counter's)
    void calibrated(int64_t ts, int64_t q) {
        if (!cal_first) cal_first = true, first_ts = ts, first_q = q;
        pts[pts_head][0] = ts, pts[pts_head][1] = q;
        pts_head = (pts_head + 1) % 5;
        n_pts = std::min(n_pts + 1, 5);
    }
    /// The game held a present (the toggle): the gap it leaves is not read
    void held(int64_t call) { pause(call - hz * kHeldBeforeMs / 1000, call + hz * kHeldAfterMs / 1000); }
    /// The game let the last ask go (it does not hold where the toggle does not fit): not counted against the bounds
    void ask_dropped() {
        if (!asked.empty()) asked.pop_back();
        if (in_a_row > 0) in_a_row--;
    }

    /// A domain's time on the counter (false: no calibration yet): the slope from the first calibration to the last, the
    /// offset the median of the last 5's
    bool to_counter(uint64_t t, bool counter_domain, double& out) const {
        if (counter_domain) return out = (double)t, true;
        if (!cal_first || n_pts < 1) return false;
        const int last = (pts_head + 4) % 5;
        const double dq = (double)(pts[last][1] - first_q);
        const double a = dq > 0 ? (double)(pts[last][0] - first_ts) / dq : 1e9 / (double)hz;
        if (a <= 0) return false;
        double b[5];
        for (int i = 0; i < n_pts; i++) b[i] = (double)pts[i][0] - a * (double)pts[i][1];
        std::sort(b, b + n_pts);
        const double off = n_pts % 2 ? b[n_pts / 2] : (b[n_pts / 2 - 1] + b[n_pts / 2]) / 2;
        out = ((double)t - off) / a;
        return true;
    }
    void pause(int64_t from, int64_t to) {
        if (!pauses.empty() && from <= pauses.back().second && to >= pauses.back().first)
            pauses.back().first = std::min(pauses.back().first, from), pauses.back().second = std::max(pauses.back().second, to);
        else pauses.emplace_back(from, to);
    }
    /// The half seconds that ended kReadAfterMs or more before `now` read, in turn
    void read_until(int64_t now) {
        const int64_t half = hz / 2;
        while (origin && origin + (slice + 1) * half + hz * kReadAfterMs / 1000 <= now)
            read_slice(origin + slice * half, origin + (slice + 1) * half, now), slice++;
    }
    void read_slice(int64_t a, int64_t b, int64_t now) {
        std::vector<double> in[2];
        Event e;
        e.kind = 'r', e.from = a, e.to = b;
        size_t keep = 0;
        for (size_t i = 0; i < values.size(); i++) {
            const Value v = values[i];
            if (v.at >= b) {
                values[keep++] = v;
                continue;
            }
            if (v.at < a) continue;
            e.values++;
            for (int k = 0; k < 2; k++)
                if (v.x >= kBands[k].low_ms && v.x <= kBands[k].high_ms && (v.paced || !kBands[k].paced)) in[k].push_back(v.x);
        }
        values.resize(keep);
        const int k = in[1].size() > in[0].size() ? 1 : 0;   // (the band most values fall in; the made image's on a tie)
        e.in_band = (int)in[k].size();
        while (!pauses.empty() && pauses.front().second <= a) pauses.pop_front();
        e.state = '-';
        for (const auto& p : pauses)
            if (p.first < b && p.second > a) e.state = 'p';
        if (e.state != 'p') {
            if (e.in_band >= kValues) {
                std::sort(in[k].begin(), in[k].end());
                e.band = kBands[k].name;
                e.median_ms = percentile(in[k], 50), e.spread_ms = percentile(in[k], 90) - e.median_ms;
                e.state = e.spread_ms > kBands[k].spread_ms ? '2' : '1';
            }
            readable.push_back(e.state != '-');
            if ((int)readable.size() > kReadableOf) readable.pop_front();
        }
        l2_run = e.state == '2' ? (e.band == l2_band ? l2_run + 1 : 1) : 0;   // (in the same band)
        l2_band = e.state == '2' ? e.band : 0;
        l1_run = e.state == '1' ? l1_run + 1 : e.state == '2' ? 0 : l1_run;   // (a pause is neither)
        if (l1_run >= kEvenRun) in_a_row = 0, quiets = 0, quiet_until = 0;
        reading = (int)std::count(readable.begin(), readable.end(), true) >= kReadable;
        e.l2_run = l2_run, e.reading = reading;
        events.push_back(e);
        if (l2_run < kRun) return;
        l2_run = 0;
        Event w;
        w.kind = 'w', w.from = now, w.reading = reading;
        while (!asked.empty() && now - asked.front() >= 60 * hz) asked.pop_front();
        if (!toggle) w.why = 'o';
        else if (!reading) w.why = 'r';
        else if (now < quiet_until) w.why = 'q';
        else if (in_a_row >= kAsksInARow) {
            // (asked that often and never L1 since: the holds do not change what is read. One ask after each quiet spell)
            quiet_until = now + ((int64_t)kQuietS * hz << std::min(quiets, kQuietDoubles));
            quiets++, in_a_row = kAsksInARow - 1;
            w.why = 'q';
        } else if ((int)asked.size() >= kAsksAMinute) w.why = 'm';
        else w.asked = true, ask_at = now, asked.push_back(now), in_a_row++;
        events.push_back(w);
    }
};

} // namespace mkwvk::dlssg_law
