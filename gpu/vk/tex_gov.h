// (PS5) The textures' governor (RX_VK_TEX_GOV=1): how small a decoded ASTC texture is made, by where the console's free
// memory is headed and not by how much there is.
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The rule it stands in for goes by a line: under the textures' reserve free (vk_texture.cpp ps5_tex_reserve) a new texture is
// made at 128 texels across at most, and made again at its own size above a reserve and a quarter. On a base PS5 the
// arrival in Hyrule Castle Town takes about 500 MiB of the game's pages and 900 MiB of textures within seconds, so the
// reserve that lives through it is large (1,800 MiB), and a base PS5 in play has 900 to 1,800 MiB free: with that reserve
// the textures are small for the whole session, over memory that was never short (39,600 made small and 13 made again in
// 18 minutes, 2026-11-27). The governor: its own size while more than `low` is free and stays so; a level down each kDownMs
// while it is under `low`, or will be within kAheadS seconds at the rate it fell over the last second; straight to 128 when
// that reaches half way to the floor, and to 32 a quarter of the way; a level back up each kUpMs once `high` is free and it
// is not falling. So memory far above the floor is used for the picture, and an arrival that takes it fast is answered
// while there is still room.
// Pure, for tests/test_ps5_tex_gov.py.
#pragma once

namespace mkwvk::tex_gov {

struct Gov {
    unsigned level = 0;        // 0: its own size, 1: 512, 2: 256, 3: 128, 4: 32
    long long since_ms = 0;    // when the level last changed, or danger was last seen
};
struct Lines { unsigned long long low, high, floor; };
constexpr long long kAheadS = 2, kDownMs = 300, kUpMs = 3000;
constexpr unsigned cap_of(unsigned level) { return level == 0 ? 0u : level == 1 ? 512u : level == 2 ? 256u : level == 3 ? 128u : 32u; }
/// The governor's next state: room bytes free, falling by `fall` bytes a second (negative: coming back), at now_ms
constexpr Gov step(Gov g, unsigned long long room, long long fall, long long now_ms, Lines l) {
    const unsigned long long drop = fall > 0 ? (unsigned long long)fall * (unsigned long long)kAheadS : 0;
    const unsigned long long ahead = room > drop ? room - drop : 0;   // (where it will be in kAheadS seconds)
    const unsigned long long span = l.low > l.floor ? l.low - l.floor : 0;
    const unsigned long long danger = l.floor + span / 2, last = l.floor + span / 4;
    if (room < last) return Gov{4, now_ms};
    if (ahead < danger) return Gov{g.level < 3 ? 3u : g.level, now_ms};
    if (room < l.low || ahead < l.low) {
        if (g.level < 3 && now_ms - g.since_ms >= kDownMs) return Gov{g.level + 1, now_ms};
        return g;
    }
    if (g.level == 4 && now_ms - g.since_ms >= kUpMs) return Gov{3, now_ms};   // (out of the last quarter: 128 again)
    if (g.level > 0 && g.level < 4 && room >= l.high && fall <= (long long)(span / 64) && now_ms - g.since_ms >= kUpMs)
        return Gov{g.level - 1, now_ms};
    return g;
}
/// Whether one made under the cap `made` is made again now that the cap is `now` (0: none)
constexpr bool larger(unsigned now, unsigned made) { return made && (!now || now > made); }
/// Whether a texture of `bytes` made under `made` (0: its own size) is made again smaller now that the cap is `now`: only
/// one plainly larger than the cap allows (twice a square of it, a layer), so that one made under it is left
constexpr bool smaller(unsigned now, unsigned made, unsigned long long bytes, unsigned layers) {
    return now && (!made || made > now) && bytes > 2ull * 4 * now * now * (layers ? layers : 1);
}

}   // namespace mkwvk::tex_gov
