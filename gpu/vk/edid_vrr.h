// (longshot) The VRR range a screen on HDMI announces in its EDID, for the toggle's hold under Streamline's DLSS Frame Gen
// (dlssg_pace.h toggle_for_floor, vk_dlssg.cpp). On HDMI the range is the HDMI Forum's vendor-specific data block
// (HF-VSDB, OUI C4-5D-D8) in a CTA-861 extension: VRRmin the low 6 bits of the block's 9th byte counting the OUI's first,
// VRRmax the two high bits of that byte then the 10th. VRRmax 0 is valid and leaves the top to the video mode, as Linux's
// amdgpu reads it ("drm/amd/display: Support HDMI VRRmax=0"); HDMI 2.1's own text, not public, was not read. That block
// alone, with no fallback: a screen with its VRR off in its own menu (a 48-240 Hz one seen) gives VRRmin 0 there while its
// base block's range limits still say 30-240 Hz and AMD's FreeSync block 48-240. DisplayPort's ranges (DisplayID's
// Adaptive-Sync block, the range limits) are not read.
// Nothing of Windows here: tests/edid_vrr_test.cpp checks it on made-up EDIDs.
#pragma once

#include <cstddef>
#include <cstdint>

namespace mkwvk::edid_vrr {

struct Range {
    int min_hz = 0, max_hz = 0;   // (min 0: none. max 0 with a min: no top of its own, the video mode's)
};

/// The HF-VSDB's VRR range of an EDID (its 128-byte blocks, the base one first): none without a CTA-861 extension holding
/// the block, with VRRmin 0 (the screen's VRR off), or with a VRRmax not 0 and not above VRRmin
inline Range hdmi_forum(const uint8_t* e, size_t n) {
    for (size_t b = 128; b + 128 <= n; b += 128) {
        const uint8_t* x = e + b;
        if (x[0] != 0x02) continue;   // (CTA-861)
        const size_t end = x[2] < 128 ? x[2] : 127;   // (the data blocks end where the detailed timings start)
        for (size_t i = 4; i < end;) {
            const int tag = x[i] >> 5, len = x[i] & 31;
            const uint8_t* p = x + i + 1;
            if (i + 1 + (size_t)len > end) break;   // (a block running into the detailed timings)
            if (tag == 3 && len >= 10 && (p[0] | p[1] << 8 | p[2] << 16) == 0xC45DD8) {
                const int lo = p[8] & 0x3F, hi = (p[8] >> 6) << 8 | p[9];
                if (lo > 0 && (hi == 0 || hi > lo)) return {lo, hi};
                return {};
            }
            i += 1 + (size_t)len;
        }
    }
    return {};
}

}  // namespace mkwvk::edid_vrr
