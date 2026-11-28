// (longshot) Longshot's own Vulkan layer under NVIDIA Streamline (vk_dlssg_layer.cpp), named in the instance only while
// Streamline makes DLSS FG's frames (vk_dlssg.h dlssg_instance_layer). Streamline's sl.interposer.dll loads vulkan-1.dll
// itself and passes the game's instance layers on to it, so a layer the game names sits between the DLSS-G plugin and the
// driver: it sees the plugin's own presents, the made image's and the game's, which the game does not. It reads when they
// reached the screen (VK_EXT_present_timing) to tell which law the driver meters them by (dlssg_law.h), and asks the game
// for the toggle (dlssg_pace.h). It passes every call on: it delays, reorders and removes nothing, and the plugin's
// structures go to the driver as they came. A build without Streamline has none of it.
#pragma once

#include <vector>

#include "vk_internal.h"

namespace mkwvk {

/// Before the instance is made: the layer's manifest written beside this DLL and its folder given to the loader
/// (VK_ADD_LAYER_PATH). The layer's name when the loader then lists it, for the instance's layers; null otherwise, with
/// the reason in the log: the game then starts without it, and without the toggle
const char* dlssg_layer_prepare();
/// What the layer needs of the instance and of the device to read the presents' times, added to `want` where the driver
/// has it (all: what it lists): VK_KHR_get_surface_capabilities2; VK_EXT_present_timing and what it rests on, with its
/// feature. Nothing unless an instance was made through the layer. A driver without them: the layer passes every call on
/// and reads nothing
void dlssg_layer_instance_extensions(std::vector<const char*>& want, const std::vector<VkExtensionProperties>& all);
void dlssg_layer_device_extensions(std::vector<const char*>& want, const std::vector<VkExtensionProperties>& all);
void dlssg_layer_device_features(void**& tail);

/// The reader read the driver's uneven law and asks the game to hold one present (dlssg_law.h): true once per ask, and
/// only within 100 ms of it
bool dlssg_layer_toggle_wanted();
/// The game's answer to an ask, its times on QueryPerformanceCounter: the present held from call to sent (the reader does
/// not read the gap it leaves), or the ask let go (held false: not counted against the reader's bounds)
void dlssg_layer_held(int64_t call, int64_t sent, bool held);

}  // namespace mkwvk
