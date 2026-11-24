// (PS5) No ReShade runtime in the console's build (gpu/vk/reshade and its FX compiler are the PC builds', nx2/reshade_fx.py):
// what the renderer asks of it answers as a ReShade that is off, and the Insert menu shows none of its rows
// (menu/reshade.cpp). Nothing here in any other build.
#ifdef __PROSPERO__
#include "reshade/fx_runtime.h"

namespace mkwvk::reshade {
std::atomic<bool> enabled{false};
std::atomic<bool> needs_tick{false};
std::atomic<bool> depth_requested{false};
void select(int) {}
void preview_bypass(bool) {}
void preview_select(int, std::string) {}
void preview_end(int) {}
void select_user(std::string) {}
std::vector<std::string> user_presets() { return {}; }
std::string user_selection() { return {}; }
std::string status() { return {}; }
void tick() {}
bool record(VkCommandBuffer, Texture*, uint32_t, bool) { return false; }
}  // namespace mkwvk::reshade
#endif
