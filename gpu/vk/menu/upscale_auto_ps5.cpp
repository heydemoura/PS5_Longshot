// (PS5) The Insert menu's Auto resolution row, in the Graphics tab after Dynamic resolution: the Render resolution by the
// GPU's load, FSR's Quality, Balanced and Performance a step at a time (upscale_gov.h; vk_menu.cpp menu_upscale_gov_tick
// moves the Render resolution row, which shows the stop it is at). The Upscaler row's own Auto is a stop of that row
// (vk_menu.cpp set_row). The console alone: a file of its own, so the other platforms' rows are as they were.
#ifdef __PROSPERO__
#include "menu_module.h"

#include "upscaler_rows.h"
#include "vk_menu.h"

namespace mkwvk {

namespace {
MODULE_HINTS(hn_mode_auto, 0,
             "Off: the Render resolution is the one chosen below, or the game's dynamic one.",
             "Quality, Balanced or Performance by the GPU's load: a step down while the GPU holds the frame rate, back up when "
             "it has time to spare. The Render resolution row shows where it is.")
} // namespace

MENU_MODULE(upscale_auto_ps5) {
    menu::tab("graphics")
        .row({.key = "mode_auto",
              .label = "Auto resolution (by the load)",
              .help = "Lets the GPU's load choose between Quality, Balanced and Performance.",
              .kind = menu::Kind::Toggle,
              .value = &opts.mode_auto,
              .lo = 0, .hi = 1,
              .hint = hn_mode_auto,
              .on_change = MENU_HOOK([](int v) {
                  // (a steady size for the steps to start from: the game's dynamic one is the other row's)
                  if (v && !upscaler_rows::percent(opts.resolution)) opts.resolution = 3;
              }),
              .unsaved = true},   // (vk_menu.cpp save() writes mode_auto= beside upscaler_auto=)
             menu::after("handheld_profile").order(36));
}

} // namespace mkwvk
#endif
