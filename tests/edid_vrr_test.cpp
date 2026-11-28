// (tests/test_dlssg_pace.py) gpu/vk/edid_vrr.h: the VRR range an HDMI screen gives in its EDID, and dlssg_pace.h's toggle
// hold from its floor. Made-up EDIDs: a base block and a CTA-861 extension, with the blocks a 48-240 Hz screen on HDMI
// gives (range limits, AMD's FreeSync block, the HDMI Forum's), and the same screen with its VRR off in its menu
#include "../gpu/vk/dlssg_pace.h"
#include "../gpu/vk/edid_vrr.h"

#include <cstdio>
#include <cstdlib>
#include <vector>

using namespace mkwvk;

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); std::exit(1); } } while (0)

struct Edid {
    std::vector<uint8_t> e = std::vector<uint8_t>(256, 0);
    size_t at = 132;   // (the next data block in the extension)
    Edid(int limits_min, int limits_max) {
        const uint8_t head[8] = {0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00};
        for (int i = 0; i < 8; i++) e[i] = head[i];
        e[126] = 1;   // (one extension)
        // (the base block's range limits, the 2nd descriptor)
        uint8_t* d = &e[72];
        d[3] = 0xFD, d[5] = (uint8_t)limits_min, d[6] = (uint8_t)limits_max;
        e[128] = 0x02, e[129] = 0x03;
        e[130] = (uint8_t)(at - 128);   // (the offset in the extension where the detailed timings start)
    }
    void block(int tag, std::vector<uint8_t> payload) {
        e[at++] = (uint8_t)(tag << 5 | payload.size());
        for (uint8_t b : payload) e[at++] = b;
        e[130] = (uint8_t)(at - 128);   // (the detailed timings start after the last data block)
    }
    void freesync(int lo, int hi) { block(3, {0x1A, 0x00, 0x00, 0x02, 0x00, (uint8_t)lo, (uint8_t)hi, 0x00}); }
    void hdmi_forum(int lo, int hi) {
        block(3, {0xD8, 0x5D, 0xC4, 0x01, 0x78, 0x00, 0x00, 0x00, (uint8_t)((hi >> 8) << 6 | lo), (uint8_t)(hi & 0xFF), 0x00});
    }
    edid_vrr::Range range() const { return edid_vrr::hdmi_forum(e.data(), e.size()); }
};

static void edids() {
    // (the 48-240 Hz screen on HDMI: the HDMI Forum's block, after AMD's)
    Edid on(48, 240);
    on.block(2, {0x01, 0x02, 0x03});   // (a video data block)
    on.freesync(48, 240);
    on.hdmi_forum(48, 240);
    CHECK(on.range().min_hz == 48 && on.range().max_hz == 240);
    // (its VRR off in its menu: VRRmin 0 in the HDMI Forum's block, while range limits say 30 Hz and FreeSync 48: none)
    Edid off(30, 240);
    off.freesync(48, 240);
    off.hdmi_forum(0, 0);
    CHECK(off.range().min_hz == 0 && off.range().max_hz == 0);
    // (no HDMI Forum block: none, whatever range limits and FreeSync say)
    Edid none(40, 144);
    none.freesync(40, 144);
    CHECK(none.range().min_hz == 0);
    // (VRRmax over 255 Hz: its two high bits)
    Edid fast(40, 255);
    fast.hdmi_forum(40, 360);
    CHECK(fast.range().min_hz == 40 && fast.range().max_hz == 360);
    // (a VRRmax not 0 and not above VRRmin: none)
    Edid odd(48, 240);
    odd.hdmi_forum(48, 48);
    CHECK(odd.range().min_hz == 0);
    Edid below(48, 240);
    below.hdmi_forum(48, 40);
    CHECK(below.range().min_hz == 0);
    // (VRRmax 0: the floor, the top left to the video mode)
    Edid open(40, 144);
    open.hdmi_forum(40, 0);
    CHECK(open.range().min_hz == 40 && open.range().max_hz == 0);
    // (the base block alone, and a block cut short: none, nothing read past the end)
    CHECK(edid_vrr::hdmi_forum(on.e.data(), 128).min_hz == 0);
    CHECK(edid_vrr::hdmi_forum(on.e.data(), 200).min_hz == 0);
    // (an HDMI Forum block said longer than the extension holds, near its end: not read past it)
    Edid cut(48, 240);
    const uint8_t hf[4] = {(uint8_t)(3 << 5 | 31), 0xD8, 0x5D, 0xC4};
    for (int i = 0; i < 4; i++) cut.e[128 + 120 + i] = hf[i];
    cut.e[130] = 127;
    CHECK(cut.range().min_hz == 0);
    // (an HDMI Forum block running past where the data blocks end, into the detailed timings: not read)
    Edid over(48, 240);
    over.hdmi_forum(48, 240);
    over.e[130] = (uint8_t)(over.at - 128 - 2);
    CHECK(over.range().min_hz == 0);
    // (a DisplayID extension first is passed over, the CTA-861 one after it read)
    Edid later(48, 240);
    later.hdmi_forum(48, 240);
    later.e.resize(384, 0);
    later.e[126] = 2;
    for (size_t i = 128; i < 256; i++) later.e[i + 128] = later.e[i], later.e[i] = 0;
    later.e[128] = 0x70;   // (DisplayID)
    CHECK(later.range().min_hz == 48 && later.range().max_hz == 240);
    // (and an HDMI Forum block inside the DisplayID extension is not read)
    Edid displayid(48, 240);
    displayid.hdmi_forum(48, 240);
    displayid.e[128] = 0x70;
    CHECK(displayid.range().min_hz == 0);
}

static void floors() {
    using namespace dlssg_pace;
    // (48 Hz, the floor measured, gives the measured hold)
    static_assert(toggle_for_floor(kFloorMeasuredHz) == kToggleUs);
    static_assert(kFloorLowHz <= kFloorMeasuredHz && kFloorMeasuredHz <= kFloorHighHz);
    // (the hold leaves the screen longer than the floor's longest frame, the same margin as at 48 Hz, and longer than a
    // frame at 60 fps)
    for (int hz = kFloorLowHz; hz <= kFloorHighHz; hz++) {
        const int64_t us = toggle_for_floor(hz);
        CHECK(us > 1000000 / hz && us - 1000000 / hz == kToggleUs - 1000000 / kFloorMeasuredHz);
        CHECK(us * 1000 > kFrame60);
    }
    CHECK(toggle_for_floor(kFloorLowHz) > toggle_for_floor(kFloorHighHz));
    // (outside the floors near the one measured, and with none: 0, the caller keeps kToggleUs)
    for (int hz : {0, 1, 24, 30, 36, kFloorLowHz - 1, kFloorHighHz + 1, 60, 120})
        CHECK(toggle_for_floor(hz) == 0);
}

int main() {
    edids();
    floors();
    std::puts("PASS: the HDMI Forum block's VRR range alone is read from an EDID, and the toggle's hold follows its floor "
              "near 48 Hz (22 ms at 48 Hz)");
    return 0;
}
