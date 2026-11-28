// NVIDIA DLSS Frame Generation through NVIDIA Streamline (vk_dlssg.cpp). Where it runs, the Insert menu's DLSS FG (Frame
// generation Fixed, Frame generation by DLSS FG, a temporal Upscaler) is Streamline's DLSS-G plugin with Streamline's
// Reflex, in place of NGX's DLSS-G called by the renderer (vk_dlss.cpp), and the game's frames go to it at even times
// (dlssg_pace.h). Built in only with Streamline's SDK (LONGSHOT_SL_SDK at the build, scripts/fetch.py streamline;
// Windows); without it, with RX_VK_DLSSG=0, or when Streamline does not start, these say it is not there and the renderer
// runs as without it.
#pragma once

#include "vk_internal.h"

namespace mkwvk {

struct TemporalFrame;

/// Before Vulkan is loaded (vk_main.cpp load_global): Streamline's vkGetInstanceProcAddr when the rows as saved ask DLSS
/// FG for frames (dlssg_rules.h) and Streamline started (it then hooks the instance, the device, the swapchain and the
/// present: manual hooking), null to load vulkan-1.dll as without it
PFN_vkGetInstanceProcAddr dlssg_load();
/// Streamline's loader is the renderer's (dlssg_load gave it): every Vulkan call goes through it until the process ends,
/// and the window's close shuts it down (dlssg_shutdown)
bool dlssg_loaded();
/// And it makes DLSS FG's frames: Reflex goes through it, and the renderer's own frame generation and Reflex are off for
/// the run. False from the start, or once Streamline gave up: on this GPU (dlssg_device, dlssg_device_made), or when the
/// Upscaler row's engine does not run here (dlssg_before_present). The renderer then runs as without it, over Streamline's
/// loader
bool dlssg_active();
/// Before the instance is made: Longshot's layer under Streamline (vk_dlssg_layer.h) for the instance's layers, its name;
/// null without Streamline, with RX_VK_DLSSG_LAYER=0, or when the loader does not list it
const char* dlssg_instance_layer();
/// Before the device is made, the GPU chosen: Streamline asked whether DLSS-G runs on it. No: it gives up
void dlssg_device(VkPhysicalDevice physical);
/// The device made (Streamline loads its plugins then): their functions, and the frames DLSS-G makes at most. None: it
/// gives up
void dlssg_device_made();
/// The frames DLSS-G makes for each of the game's at most, while it is active: 1 on an RTX 40, more on an RTX 50
int dlssg_frames_most();
/// Who makes the frames now, for the menu's lines and the FPS counter (vk_framegen.h FgMaker): 0 nobody, 1 DLSS-G, 2
/// DLSS-G with nothing to make them from (the game's own menus, movies and loading screens: the game's frames alone)
int dlssg_making();
/// The Upscaler row's value as the renderer runs it (vk_temporal.h temporal_row): row, or while Streamline's loader is the
/// renderer's (given up or not), when Streamline is not started with row's Upscaler (dlssg_rules.h upscaler_waits: another
/// than DLSS or DLAA), the last value it is started with that a reader saw: that change waits for the restart, the Upscaler
/// row saying After restart, and DLSS goes on until then. Read on any thread
int dlssg_upscaler(int row);
/// Whether the Frame generation rows as they are, with the Upscaler as it runs (dlssg_upscaler), take a restart
/// (dlssg_rules.h restart_needed): 1, else 0. 0 until the device is made, the value the rows start with
int dlssg_restart_needed();

/// Reflex through Streamline while it is active (vk_reflex.cpp's own Reflex is off then): the frame's latency markers and its
/// sleep
enum DlssgMarker { DLSSG_SIM_START, DLSSG_SIM_END, DLSSG_SUBMIT_START, DLSSG_SUBMIT_END, DLSSG_PRESENT_START, DLSSG_PRESENT_END };
void dlssg_marker(DlssgMarker m);
/// After a present: the next frame's token, Reflex's sleep, its simulation start
void dlssg_sleep();

/// The frame's inputs, from the temporal upscaler's frame chain (vk_temporal.cpp, recorded into cmd after its engine drew):
/// f's depth and motion (GENERAL, the render size at their top left), its jitter in render pixels; the scene camera's
/// projection and view-projection (row major, clip = M * position, both jittered) and its jitter in clip space. motion:
/// the motion vectors hold the camera's motion from the frame before (false: a new history)
void dlssg_inputs(VkCommandBuffer cmd, const TemporalFrame& f, const float projection[16], const float view_projection[16],
                  const float clip_jitter[2], bool motion);
/// Where the game's picture is in the swapchain image this present shows (x, y, w, h of full_w x full_h): DLSS-G is told
/// with the next frame's inputs, so it makes the picture alone and leaves the bars beside it (RX_VK_DLSSG_RECT)
void dlssg_backbuffer(uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint32_t full_w, uint32_t full_h);
/// A game frame's present, after its acquire and its copy's submit, before vkQueuePresentKHR, on the render thread (no
/// lock held: it waits; not called without Threaded rendering, where the game's own thread presents): held to an even
/// time after the game's own present (dlssg_pace.h: the hold, the grid, the toggle). game_at: when the game's thread
/// presented the frame, on bh_now's clock (0: not known); game_wait: how long that thread then waited for the renderer
void dlssg_hold(int64_t game_at, int64_t game_wait);
/// Whether the toggle is on (RX_VK_DLSSG_TOGGLE): the layer's reader asks for it then
bool dlssg_toggles();
/// A swapchain made for the game's window (vk_main.cpp create_swapchain): with RX_VK_DLSSG_TOGGLE unset, the toggle's hold
/// from the VRR floor the window's screen gives in its EDID on HDMI (dlssg_pace.h toggle_for_floor, edid_vrr.h), else 22 ms;
/// asked again at each swapchain, as the screen, or its EDID with its VRR turned off in its menu, can change (so not a
/// device policy rule: its facts are learned once, before the device, and none is the screen's)
void dlssg_swapchain_made();
/// Right before a present (vk_replay.cpp present_image): frame generation on for a game frame that gave its inputs, off
/// otherwise (the Insert menu open, the start-up screen, no inputs for a while, the rows no longer asking). A game frame
/// with the menu closed while the rows ask DLSS FG and the Upscaler's engine does not run (vk_temporal.h temporal_wanted):
/// Streamline gives up for the run
void dlssg_before_present(bool game_frame);

/// The window is closing, before the host ends the process (vk_main.cpp mkwvk_close_asked: the window's close, Quit and the
/// Insert menu's Restart): Streamline shut down while the threads it needs still run. Left to the process's end, the DLSS-G
/// plugin tore its frame generation down in its DLL detach, after ExitProcess had ended them: an access violation at every
/// exit. In order: no dlssg_* call let in any more; a Reflex sleep under way waited for, with the present lock free (that
/// thread presents next); the present lock and the queue's taken; the device idle; DLSS-G off; slShutdown. Each wait is 2 s
/// at most: past that Streamline is left as it is and the exit goes on. Both locks stay held until the process ends, so
/// nothing reaches Streamline's hooks after it shut down. Once. True when Streamline's loader is the renderer's: the close
/// is this one's then (dev.closing set here), in place of vk_main.cpp's retire of the swapchain; false, and nothing done,
/// without it. Not a fatal end's: vk_replay.cpp replay_fatal ends the process before the DLLs' detach instead
bool dlssg_shutdown();

} // namespace mkwvk
