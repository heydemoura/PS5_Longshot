// The DLSS 5 module's (gpu/vk/menu/dlss5.cpp) that the rest of the menu uses (vk_menu.cpp menu_init and save): the renderer
// told what its rows hold, the Default preset's values and the player's Custom set (tests/test_dlss5_menu.py).
#pragma once

#include <string>

namespace mkwvk {
/// The DLSS 5 rows' values to the renderer (vk_dlss.h dlss_nr_publish), with RX_VK_DLSS_NR_DEBUG's view for this run and the
/// DLSS 5 model row as the run started (it applies after a restart)
void dlss5_publish();
/// RX_VK_DLSS_NR_MODEL: auto (or 0), nvidia (1) or fp16 (2) over the DLSS 5 model row for this run; -1 when unset or not one
/// of them
int dlss5_model_env();
/// The Default preset: the rows as tuned in the game (Kokiri Forest, an RTX 5090 at 5120x2160 with DLAA), DLSS 5 itself Off.
/// A row moved by hand from one of these makes the DLSS 5 preset row say Custom
struct Dlss5Preset {
    int resolution = 65, detail = 164, colour = 126, style = 2, strengths = 2, intensity = 162, local_structure = 122,
        local_tone = 114, skin = 74, auto_mask = 1, highlight = 17;
    bool operator==(const Dlss5Preset&) const = default;
};
inline constexpr Dlss5Preset kDlss5Default{};
/// The player's Custom set as its options line's text (vk_menu.cpp save: dlss5_custom=): the rows as last set by hand, kept
/// while the preset row is on Default and given back by Custom. Each row by its name ("resolution:65,detail:164,..."), so a
/// row added later takes its Default value. Rows that differ from the Default preset are the set (taken first); empty when
/// there is no set
std::string dlss5_custom_text();
/// dlss5_custom='s text read back (vk_menu.cpp menu_init): each value held to its row's range, a name not known or a value not
/// a number left out (its Default value); a set equal to the Default preset is no set
void dlss5_custom_read(const char* text);
} // namespace mkwvk
