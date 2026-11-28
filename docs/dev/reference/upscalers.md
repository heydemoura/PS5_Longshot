# Upscalers and frame generation

The game draws its 3D scene smaller than the output and upscales it with its own network. Longshot can replace that one
dispatch with a temporal upscaler (DLSS, FSR 3.1, FSR 4, XeSS), with a plain stretch (Off), or leave the network to run.
Separately, frame generation makes frames between the game's at the present. This page covers what each part is given and
the rules it keeps. Player settings are in [Settings](../../user/graphics-and-controls.md). How frame generation's sets
are paced and presented is in [architecture](architecture.md#frame-generations-presents).

## The engines

The Upscaler row's values are in [`upscaler_rows.h`](../../../gpu/vk/upscaler_rows.h). Saved values never change meaning,
so new engines are added at the end.

| Value | Upscaler | Code | Needs |
|---|---|---|---|
| 0 | The game's own network | — | Nothing. The TAAU stand-in replaces it where the network would run on software multiplies (`RX_VK_TAAU_KEYS`) |
| 1, 2 | NVIDIA DLSS, DLAA | [`vk_dlss.cpp`](../../../gpu/vk/vk_dlss.cpp) | A build with NVIDIA's NGX SDK (`LONGSHOT_NGX`), an RTX GPU, NGX's runtime |
| 3 | AMD FSR 3.1 | [`vk_fsr3.cpp`](../../../gpu/vk/vk_fsr3.cpp) | Nothing extra. AMD's shaders are compiled into every build (`scripts/shaders.py fsr3`) |
| 4, 5 | AMD FSR 4, its native AA | [`vk_fsr4.cpp`](../../../gpu/vk/vk_fsr4.cpp) | A build with `--upscaler fsr4` and AMD's FSR 4 library on the player's PC |
| 6 | Off | `temporal_off` in [`vk_temporal.cpp`](../../../gpu/vk/vk_temporal.cpp) | Nothing. The network is skipped and its input is stretched with a linear filter |
| 7, 8 | Intel XeSS, its Native AA | [`vk_xess.cpp`](../../../gpu/vk/vk_xess.cpp) | Windows, and Intel's `libxess.dll` beside the game |

No vendor library ships with Longshot. Each engine reports why it cannot run (`dlss_why`, `fsr4_why`, `xess_why`), and
the menu offers only the engines that can.

Eligible AMD FSR 4 passes with constant weight tables are unrolled subject to headroom and size limits. On FSR 4.1.2, the
shader set it was checked against, the expanded unroller folds the network's layers and the postpass; another FSR 4 version
keeps the previous unroller, for the layers only. A compact folded build wants 1 GiB of memory to spare on AMD's older
Windows shader compiler (RDNA 1 and 2), where its compile memory was measured, and 8 GiB everywhere else. A layer whose
plain twin could not be made is left rolled on every configuration. They are built into a
pipeline cache of their own, `cache/longshot_fsr4_pipelines.bin`
(`own_cache` in `vk_fsr4.cpp`), apart from the translator's, whose files a new translator drops: an update keeps it.
`RX_VK_PCACHE_LOAD=0` neither reads nor saves it.

DLSS, FSR 4, XeSS and FSR 3.1 are engines of one **temporal chain** (`vk_temporal.h`). The chain finds the scene, makes
the inputs, keeps the history and writes the result into the network's output. An engine only turns the inputs into a
picture (`TemporalEngine`: `start`, `prepare`, `evaluate`, `release`, `memory`, `hdr`). With `RX_VK_FSR3_CHAIN=0`, FSR 3.1
instead runs on its own inputs (`vk_fsr3_inputs.cpp`), which carry the camera's motion only.

## Vendor files

`longshot-builder` downloads a vendor's files only when asked (`--upscaler dlss,fsr4,xess`) and only with its license
accepted (`--accept-licenses nvidia,amd,intel`, which a launcher passes once the player said yes; `license_not_accepted`
otherwise). Each file comes from its vendor at one pinned release, is checked against its SHA-256, and is kept in the
builder's cache (`<cache>/runtimes/`) for every later build (`builder/crates/build-driver/src/runtimes.rs`). The accepted
licenses are kept in the profile's `profile.json` (`licenses`, each for its pin), so its updates need no new answer
until a pin moves. `scripts/fetch.py` downloads the same
files for a developer ([setup](../setup.md#upscaler-sdks-optional)). How players turn them on is in
[advanced setup](../../user/advanced.md#optional-upscalers).

| Engine | `--upscaler` | What the build puts in the game folder | The game looks in, in order |
|---|---|---|---|
| DLSS | `dlss`: DLSS v310.9.16; built in from NVIDIA's SDK (`LONGSHOT_NGX_SDK` overrides it, and nothing of NVIDIA's is downloaded) | `nvngx_dlss`, `nvngx_dlssg` in `ngx/` | `RX_VK_DLSS_DIR`, the game folder's `ngx`, then `%LOCALAPPDATA%\Longshot\ngx` (Windows) or `$XDG_DATA_HOME/longshot/ngx`, else `~/.local/share/longshot/ngx` (Linux). NGX takes the newest runtime it finds across them |
| FSR 4 | `fsr4`: built in from AMD's MIT headers in `third_party/fidelityfx` | Windows: FSR SDK v2.4.0 (FSR 4.1.2), `amd_fidelityfx_loader_vk.dll` and `amd_fidelityfx_upscaler_vk.dll`, with `LICENSE-AMD-FSR-SDK.md`, beside the game | `RX_VK_FSR_DIR`, the executable's folder (`bin`), then the game folder |
| XeSS | `xess` (in every Windows build) | Windows: `libxess.dll` 2.0.2.68 (78 MB), with Intel's license and notices, beside the game | `RX_VK_XESS_DIR`, the executable's folder, then the game folder |
| DLSS FG through Streamline | `dlss` on Windows, with a developer's `LONGSHOT_SL_SDK=<sdk folder>` (`scripts/fetch.py streamline --accept-nvidia-license`: NVIDIA Streamline 2.14.16); the builder downloads nothing of it | Nothing: `scripts/fetch.py streamline --game <game folder>` puts Streamline's runtime (`sl.interposer.dll`, `sl.common.dll`, `sl.dlss_g.dll`, `sl.reflex.dll`, `sl.pcl.dll`, `nvngx_dlssg.dll`, `NvLowLatencyVk.dll`) beside `longshot.exe` | Beside `longshot.exe` only, and only `sl.interposer.dll` with NVIDIA's signature ([below](#dlss-fg-through-streamline)) |

- A vendor DLL in the game folder that is not the pinned one (a newer one put there by hand) is left alone.
- A build without Streamline's SDK compiles none of it (`vk_dlssg.cpp` and `vk_dlssg_layer.cpp` are stubs): a game
  folder that has Streamline's runtime beside `longshot.exe` then runs as without it. Streamline has no Linux runtime.
  No launcher builds with it yet: a player's build has no way to ask for it.

- `longshot-builder update` keeps the folder's upscalers; `--upscaler` given to it changes them (`--upscaler none` takes
  them out).
- On Linux AMD publishes no FSR 4 runtime (a Vulkan `amd_fidelityfx_upscaler_vk.so` put beside the game stands in),
  and there is no XeSS for Linux.
- `RX_VK_FSR_DIR` and `RX_VK_XESS_DIR` are paths, which `longshot_env.txt` leaves out: set them in the environment or a
  `HOS_ENV_FILE` file. `RX_VK_DLSS_DIR` is the one path `longshot_env.txt` takes ([switches](switches.md#longshot_envtxt)).
- Never put NGX's core, `_nvngx.dll`, beside the game: it comes with NVIDIA's driver. Without `nvngx_dlssg.dll` in a
  folder above, frame generation takes the driver's own copy, which may be older (one that makes 1 frame at most, or
  refuses every set).

## What an engine gets

`temporal_upscale` takes the gameplay upscale's dispatch (by program key, or by a network of the same shape whose input
is this frame's scene). It checks the scene's camera, depth, formats and size, then builds one `TemporalFrame`:

| Input | Format and meaning |
|---|---|
| `color` | RGBA16F at the render size. Tone-mapped and square-root encoded (LDR), or linear light past 1 when the engine's `hdr()` says it takes native HDR |
| `depth` | R32F, 0..1. Reversed (`1 - d`) for an engine with `fsr_depth` (FSR 3.1) when the projection puts the near plane at 0; `depth_reversed` says so. Anything else reading this depth must check that flag |
| `motion` | RG16F in render pixels, towards the previous frame, jitter removed. Camera reprojection, with skinned draws drawn again with their previous bones (actor motion) |
| `output` | RGBA16F at the output size (at the render size for DLAA and the native AA modes, scaled afterwards) |
| `jitter`, `jitter_phases` | This frame's sub-pixel offset in render pixels, and the sequence length (`dlss_jitter_phases`: 8 to 64 by scale) |
| `projection`, `near_z`, `far_z`, `fov_y` | The scene's projection without the jitter |
| `reset` | No usable history (see below) |

Inputs are allocated at the size the engine asks for (`in_w` × `in_h`), with the frame's part at the top left.
`RX_VK_REACTIVE=1` also builds a reactive mask for DLSS and FSR 3.1 from blended draws (`vk_reactive.cpp`).

### DLSS feature size

The DLSS feature is created at the quality mode's **recommended** size from `NGX_DLSS_GET_OPTIMAL_SETTINGS`. The mode's
minimum and maximum size the input images and decide whether the existing feature can be reused. Each evaluation still
receives the frame's actual render size. Creating at the maximum makes models L and M cost more even when the render size
is smaller. DLAA creates at the output size. A render size that no mode takes is refused and logged once per size.

### DLSS models

The DLSS model row (`dlss_model=`; `upscaler_rows.h` `model_preset`) asks NGX for one preset in every mode. A change
applies at once: the feature is made again.

| Row (saved value) | Preset |
|---|---|
| Transformer (K) (0, default) | K; E on a card with less than 7 GB of video memory (`cnn_for_vram`), the row then saying why |
| Transformer 2 (M) (1) | M |
| Transformer 2 (L) (2) | L |
| NVIDIA default (3) | `0`: the runtime picks per mode, so a runtime update can change it |
| CNN (E) (4), CNN (F) (5) | E, F |

`RX_VK_DLSS_PRESET=<letter>` (either case) or `0` wins over the row for one run, and the row shows
`Preset <letter> (RX_VK_DLSS_PRESET)`. The log's `vk: NVIDIA DLSS: ... preset <letter>` line is what was asked for; with
`RX_VK_DLSS_LOG=1`, NGX's `Using App hint Preset <letter>` lines are what it took.

### FSR 4 models

The FSR 4 model row (`fsr4_model=`; `RX_VK_FSR4_MODEL=auto|lite` over it for one run) picks AMD's network: Auto lets
AMD's own `IsSupported` choose (FP8 on RDNA 4, full INT8 on RDNA 3 and Strix Halo, the Navi 2 INT8 model on RX 6000, Lite
on Strix Point and Van Gogh); Lite forces the INT8 Lite provider.

The hook is for one file: FSR SDK 2.4's `amd_fidelityfx_upscaler_vk.dll` (4.1.2.4555), named by the SHA-256
the builder (`build-driver` `runtimes.rs`, checked against the table by its test) and `scripts/fetch.py` pin for it
(`fsr4_models.cpp`, `kKnown`). `fsr4_models::load_known` hashes the file beside AMD's
loader; only when it is that file does it load it from that path (before the loader asks for the name, so the loader's
own load finds this module), pin it, check that the module's file is the one hashed, and scan it. Any other file is not
loaded here and not scanned. `fsr4_models::install` then patches only each provider's `IsSupported` entry (`vtable[2]`),
after the 4.1.2 vtable shape check, and only in this process's copy; without the known library, with a shape that is not
understood, or with a vtable that changed under the scan, nothing is written (Auto). Once the loader has listed its
upscalers, `known_module_is_loaders` checks that the module under that name is still the one that was checked, and
otherwise the hook answers as AMD does (`release`). A newer AMD file is Auto only until its layout is read and its
SHA-256 added. The log's `vk: AMD FSR 4: models:` lines say what was found. Linux has no Lite provider: the row is hidden
and the switch does nothing.

Where AMD's library lists no FSR 4 for the GPU (its own `IsSupported` says no for every model: an NVIDIA or Intel GPU, a
Radeon AMD names for none) and the Lite provider is hooked, `load` asks the list again with Lite answering and, when
FSR 4 is listed then, runs every context on it (`g_standin`): FSR 4 is offered, the model row is not shown, AMD's own
shaders run (the tuned passes are measured on AMD's pick), and the log says `INT8 Lite stands in`.
`RX_VK_FSR4_STANDIN=0` leaves FSR 4 off there, as it was.

### DLSS's log and memory

NGX starts on demand: when the menu asks whether DLSS can run (`dlss_probe`), at the first DLSS evaluation, or at
DLSS frame generation's first set. A log from the title screen may have no NGX lines yet.

`RX_VK_DLSS_LOG=1` sends NGX's own lines into `longshot_log.txt` as `ngx:` (instead of NGX's log files in the game
folder's `ngx`). Two kinds look like errors and are not:

- `failed to load NGXCore: 126`, twice: NGX looking for `_nvngx.dll` and `nvngx.dll` beside the game, which are not there
  on purpose, before it loads the driver's.
- `Feature ... failed to load from cache`: NGX finding no model among its own downloads
  (`C:\ProgramData\NVIDIA\NGX\models`) before it uses the game folder's `nvngx_dlss.dll`.

DLAA draws the 3D at full size, and DLSS and DLAA add images of their own beside the game's. On an 8 GB card, busy
areas can pass the driver's allowance and stutter; Quality or Balanced is safer there. The `vk: status:` line (every 600
frames) shows the video memory in use against the allowance, with DLSS's images, jitter and actor motion appended while
it draws, and `vk: over the driver's video-memory allowance` says when the game passed it.

### Native HDR and PsychoV35

With Native HDR and the PsychoV35 tone mapper, the network's input and output are RGBA16F instead of packed B10G11R11.
DLSS/DLAA, FSR 4 and its native AA, XeSS and its Native AA, and FSR 3.1 on the chain take that scene (`takes_psychov35`).
`RX_VK_PSYCHOV35_DLSS=0` turns this off. FSR 3.1 interpolation's admission of the RGBA16F scene uses the same policy
(`upscaler_rows::psychov35_compatibility`, read once).

- The network's colour is perceptual even when stored as RGBA16F. The prep pass still decodes it to linear light. The
  typed RGBA16F output pass writes the engine's linear result without encoding it again.
- The route waits until the engine's HDR feature or context exists (`PsychoV35 waits for the native HDR ... context`).
  `RX_VK_FSR4_HDR=0` and `RX_VK_XESS_HDR=0` keep those engines on their LDR contexts.
- FSR 3.1's legacy path (`RX_VK_FSR3_CHAIN=0`) does not take RGBA16F. The TAAU stand-in and Off accept the matching
  RGBA16F pair on their own paths, without the switch.
- The log names the route: `vk: PsychoV35 <engine>: RGBA16F linear route; scene format ..., network input 97, output 97`.

The RGBA16F output shader header is generated:
`python -I -B tests/test_psychov35_dlss.py --write --glslang glslang --cxx clang++`.

## Temporal history

The chain starts a new history (`reset`) in the same frame when any of these holds:

- it is the first frame, or more than two frames have passed since the last evaluation;
- camera reprojection fails: a non-finite input or result, or a coefficient outside float range
  (`dlss_math::reprojection`). Without a valid reprojection, the old picture has no mapping to the new camera;
- a different engine draws (the previous engine is released);
- the engine made its feature or context again (`prepare` returned 2);
- the scene's format, the PsychoV35 route, or DLAA's before/after-bloom position changed. A change of position
  starts the engine's history alone: the camera and the actors are the same either side of the bloom, so that
  frame's motion is reprojected from the frame the engine drew at the other position (`moved`), not zeros;
- the previous evaluation failed with `retry` set.

Valid consecutive camera motion keeps history. There is no camera-cut heuristic beyond these.

After 120 frames with no engine evaluation (menus, Off), `temporal_idle` releases the engine's feature or context and the
chain's images.

## Camera jitter

The jitter is applied in the game's code (the hook sites `jitter.view*`, game/oot_sites.c): the scene's projection gets a Halton (2, 3)
sub-pixel offset in its uniform buffers only, so shadows and culling see the unjittered camera.

1. After each evaluation, the chain publishes one `MkwVkJitter` (render width, height, sequence length) in a
   `TemporalJitter` ([`temporal_jitter.h`](../../../gpu/vk/temporal_jitter.h)). It has its own small mutex, never the
   upscaler's GPU/NGX lock. A sample older than 30 renderer frames reads as inactive (all zeros).
2. The host calls the renderer's `jitter_sample` callback once per present (`hos_dlss_jitter_clip` in
   [`host/rx_backend.c`](../../../host/rx_backend.c)) and caches it for every view of that frame. The chain's sample wins;
   FSR 3.1's own-inputs sample is the fallback.
3. The renderer reads the applied offset back from the camera's uniform buffer, so the host only has to pick offsets.

Use the batch callback, not three scalar option queries: a resize between them gives an incoherent sample.
`RX_VK_DLSS_JITTER=0` and `RX_VK_FSR_JITTER=0` turn the jitter off.

## Texture LOD bias

The game draws its 3D at 3/4 of the output and biases its world materials' samplers by -0.5 for that. An upscaler
drawing from another share adds to the samplers the game biases below 0 (`upscale_lod_bias` in `vk_draw.cpp`,
`upscale_lod_bias_for` in `upscaler_rows.h`):

| Upscaler | Added bias |
|---|---|
| DLSS, DLAA, FSR 3.1, FSR 4, FSR 4 Native AA | `log2(share / 0.75) + offset`, offset -1: NVIDIA's DLSS guide and AMD's FSR 3.1 / FSR 4 specification, the output's detail and one mip step finer, resolved over frames (Quality -1.17, Performance -1.58, Native AA / 100% -0.58, Auto -1) |
| XeSS | `log2(share / 0.75)` below 75%; 0 at Auto and above |
| The game's own, Off, XeSS Native AA | 0 |

The bias is part of every sampler's key: the Upscaler, Render resolution and Quality preset rows make the samplers
again. `RX_VK_UPSCALE_LOD_BIAS=0` adds none; `RX_VK_DLSS_LOD_OFFSET=<steps>` sets the DLSS / FSR offset for a run (clamped to
-3..0; `0` gives the render size's own detail).

## Resources and failures

| Rule | Where |
|---|---|
| FSR 3.1 and FSR 3.1 interpolation take their constants from a slice of the recording's upload stream (`uniform_upload`), never a ring sized by CPU frames. No room means the evaluation is rejected before it touches history | `vk_fsr3.cpp`, `vk_fsr3fi.cpp` |
| Images from an old size are retired on the GPU timeline (`vk_retirement_value()`), freed once `vk_timeline_completed()` passes it. A zero ticket (no timeline) is never freed early | `drop_image`, `free_dead` |
| Engine resources released while work may be in flight go through `temporal_later` | `vk_temporal.h` |
| A failed DLSS feature creation is not retried until a DLSS setting changes (`dlss_retry`), except one refused for want of video memory: it is tried again after 600 frames, at most three creations, then the refusal stays; any other result is final at once | `vk_dlss.cpp` `prepare`, `dlss_checks.h` `retry` |
| `prepare` returning -1, or a failed `evaluate` without `retry`, disables the engine for the run; the game's network draws from then on | `draw_engine` |
| DLSS and FSR 3.1 set `retry`: a failed evaluation drops that frame to the network and resets history. DLSS remakes its feature after 30 failures in a row | `kEngine`, `kChainEngine` |

General rules for retirement, mapped memory and failed creations are in
[architecture](architecture.md#rules-for-resources-and-presentation).

## Frame generation

The Frame generation row (`opts.framegen`: 0 Off, 1 Auto, 2–8 a fixed multiplier) and the Frame generation by row choose
who makes the frames. `framegen_record` in [`vk_framegen.cpp`](../../../gpu/vk/vk_framegen.cpp) tries, per set:

| Order | Maker | Inputs | When |
|---|---|---|---|
| 1 | NVIDIA DLSS Frame Generation (NGX) | The chain's depth, motion and camera, copied by `dlss_fg_inputs` after a successful evaluation | Fixed multiplier only, up to the runtime's most frames + 1 (`dlss_fg_most`). Needs a temporal upscaler drawing; without one the set is skipped with `FG_SKIP_INPUTS` |
| 2 | AMD FSR 3.1 interpolation | Its own scene depth and camera motion, replaced by the chain's motion when the chain makes one (`temporal_fg_inputs` makes it even with no engine drawing) | FSR 3.1 FG chosen, or DLSS FG where DLSS-G does not run (`fsr3fi::frames_wanted`). Fixed or Auto |
| 3 | Built-in interpolation | Colour only: block-matched optical flow (`RX_VK_FRAMEGEN_RADIUS`) | Whatever the others did not make |

With DLSS FG chosen and no upscaler picture (the game's menus, movies, loading screens), the set is shown as the game's
frame alone (`dlss_fg_holds`, `FgMaker::DlssPaused`), not handed to another maker.

The shared frame-generation history and output images use `RGB10A2` for Native HDR (PQ) and `RGBA8` for SDR input.
The format follows the frames through generation and presentation. A format change drains the pending sets and restarts
interpolation history. If the device cannot use the 10-bit storage images, Longshot presents the original HDR frames.

FSR 3.1 interpolation ([`vk_fsr3fi.cpp`](../../../gpu/vk/vk_fsr3fi.cpp)) is AMD's passes compiled from patched copies
of the SDK headers (`FI_PATCHES` in `scripts/shaders.py`; `third_party/` is unchanged). The patches read each made
frame's place `t` from the constant block's padding word, so any multiplier works, not only the midpoint. Optical flow
and the dilated inputs run once per game frame, the rest once per made frame. It is called every present, even with no
frames to make, so its history is always the previous frame.

### Its history and colour

- A reset (`Inputs::reset`, a skipped sequence number, a change in PQ encoding, peak luminance or HUD-less mode) makes no
  frames that present; the caller shows the game's frame.
- The captured present can hold PQ codes in UNORM storage. Both presentation paths pass `native_pq`; the interpolation
  and optical-flow constants then get transfer function 1 and 0..the HDR peak in nits. SDR uses transfer 0 and 0..1.

### The HUD

The game draws its HUD into the same picture as the scene, after the gameplay upscale.

1. `framegen_hud_before` copies the picture right after the upscale writes it, whoever drew the upscale.
2. At the present, `hud_source` (`shaders/fg_hud.comp`) finds the HUD as what the game drew since.
3. In SDR and Expanded HDR, by default (`RX_VK_FSR_FG_HUDLESS`, on) FSR 3.1 interpolates the HUD-less copy, and the HUD
   is laid over each made frame. With `=0`, the HUD is instead marked in the source's alpha (`Inputs::hud_mask`) and kept
   in place. Native HDR uses the marked, presented PQ frame when a copy is available, regardless of this switch.
4. In SDR and Expanded HDR, the HUD layer (`RX_VK_FSR_FG_HUD_LAYER`, on): the game blends its HUD as `D = B + T × S`, so
   from this frame's and the frame before's pictures before and after the HUD, `hud_layer` (`shaders/fg_hud_layer.comp`) fits per pixel how
   see-through the HUD is (T) and its colour (B), kept from frame to frame. FSR 3.1 is given the HUD-less colour as its
   present, and `shaders/fg_hud_composite.comp` lays `B + T ×` the made frame's own scene over each made frame; where T
   isn't known yet, the presented pixel. With `=0`, the presented frame is laid over as it is, and the real frame's
   scene shows through the HUD's see-through parts. Native HDR does not use the layer, regardless of this switch.
5. With no copy this frame (no gameplay upscale, or a picture that is not the presented size), the patched passes guess
   the HUD as pixels unchanged while the scene moves. `RX_VK_FSR_FG_HUD=0` runs AMD's passes unpatched, the HUD moving
   with the scene.

### DLSS FG through Streamline

In a Windows build with NVIDIA Streamline's SDK, DLSS FG's frames are made and presented by Streamline's DLSS-G plugin
([`vk_dlssg.cpp`](../../../gpu/vk/vk_dlssg.cpp)) instead of NGX's DLSS-G called from `framegen_record`. There is no row
for it: the Frame generation rows choose it.

**When it starts.** Streamline hooks the instance, the device, the swapchain and the present (manual hooking), so
`dlssg_load` runs before Vulkan is loaded (`load_global`) and decides from the rows as saved in `longshot_options.ini`
([`dlssg_rules.h`](../../../gpu/vk/dlssg_rules.h)): Frame generation Fixed, Frame generation by DLSS FG and the Upscaler
DLSS or DLAA (`kStreamlineUpscalers`; `RX_VK_DLSS`, `RX_VK_FSR4`, `RX_VK_XESS` and `RX_VK_FSR` over the row, in
`temporal_mode`'s order). With another Upscaler the frames are made as without Streamline: NGX's DLSS-G where that
Upscaler's engine runs, else the renderer's own frame generation. FSR 3.1 and XeSS, which run on an NVIDIA GPU, so do
without Streamline's pacing: a choice, to extend `kStreamlineUpscalers` if one of them should have it. Started with FSR 4
on an NVIDIA GPU, Streamline gave up, and its DLSS-G plugin still presented every frame through a swapchain of
its own: the present call took 0.9 ms instead of 0.09, and the frames reached the screen less evenly and about 3 ms later. `RX_VK_DLSSG=0`,
`RX_VK_DLSS=0`, `RX_VK_DLSS_FG=0`, `RX_VK_FRAMEGEN=0` and `--prepare-shaders` each keep it off. Then, in order:
Streamline's files beside `longshot.exe`, an NVIDIA adapter (DXGI), NVIDIA's signature on `sl.interposer.dll`, `slInit`, and `slIsFeatureSupported` for DLSS-G. Any no leaves the run exactly as a build
without Streamline. When it started, `sl.interposer.dll`'s `vkGetInstanceProcAddr` is the renderer's loader for the
whole run.

| State | `dlssg_loaded()` | `dlssg_active()` | What runs |
|---|---|---|---|
| Not started | no | no | NGX's DLSS-G and the renderer's own frame generation, as without Streamline |
| Running | yes | yes | Streamline's DLSS-G and Reflex. `framegen_now()` is 0, `framegen_on()`, `fsr3fi::frames_wanted()` and `vk_reflex.cpp`'s own Reflex are off, NGX's DLSS-G is not asked, every present is made on the render thread (`present_thread_on`) |
| Gave up | yes | no | Streamline said no for the GPU the renderer chose (`dlssg_device`), its plugins did not load (`dlssg_device_made`), or the rows ask DLSS FG while the Upscaler row's engine does not run here (DLSS or DLAA without its runtime) (`temporal_wanted()` false on a game frame with the Insert menu closed, `dlssg_before_present`: its runtime or DLL missing, NGX's answer). The renderer as without it, over Streamline's loader, which passes every call on; but every present stays on the render thread for the run (`present_thread_on` is settled once, on `dlssg_loaded()`) |

**A restart.** A change of the rows that brings Streamline in, or takes its frames away (Auto, Built-in FG or FSR 3.1
FG while it runs: no other frames can be made beside it), applies at the next start. The Frame generation by row
carries it (`dlssg_restart_needed` is its restart value), so closing the menu asks, and its line says what the restart
changes. Frame generation Off needs none. Once Streamline's loader is the renderer's (Running or Gave up), the
Upscaler row's change to another Upscaler than DLSS or DLAA waits for the restart too: DLSS goes on until then
(`dlssg_upscaler`, which `temporal_row` asks in place of the row for `temporal_mode`, `temporal_off`, `fsr3_chosen`,
the preview and the report), and the row says After restart (`upscaler_waits` is its restart value). Changed to FSR 4
at once, Streamline gave up and its DLSS-G plugin still presented every frame. A change between DLSS and DLAA, or any
change without Streamline, is at once.

**Each frame.** `dlssg_sleep` after a present (the next frame's token, Reflex's sleep, the simulation-start marker);
`dlssg_inputs` from the temporal chain once its engine drew (depth and motion tagged, the camera's constants);
`dlssg_hold` before a game frame's present, after its acquire and its copy's submit (with Threaded rendering Off the
present is the game's own thread's, and nothing is held: no hold, no toggle); `dlssg_before_present`, which turns DLSS-G
on for a game frame with inputs and off for the Insert menu, the window's own presents, 60 game frames without inputs,
or rows that no longer ask, and gives up when the Upscaler row's engine does not run. The
other latency markers come from `vk_reflex.cpp`'s hooks. NGX starts once in a process, and under Streamline that start
is `sl.common`'s: `dlssg_load` gives Streamline the folders `vk_dlss.cpp` names for DLSS's runtime, or DLSS itself
fails with 0xbad00004.

**Reflex's frame limit.** While DLSS-G makes frames, Streamline's Reflex holds the frames shown, the made ones with the
game's, to `refresh - refresh² / 3600` (`frame_limit_us`, from the display's refresh): 116 fps at 120 Hz, 138 at 144,
224 at 240, as NVIDIA's driver caps them under G-SYNC with V-Sync. It is set with V-Sync On and Auto, and none with the
menu's V-Sync Off (`menu_vsync() == 2`), as the driver sets none without V-Sync: the renderer cannot tell a G-SYNC or
FreeSync display from one that is not (`vk_reflex.cpp` `fifo_min_interval_us`), so the menu decides, not VRR. The
game's frames are that divided by the multiplier: at 2x, 58 fps at 120 Hz, 29.5 at 60 Hz; under the game's 60 fps cap
from about 124 Hz down at 2x, and at 165 Hz at 3x (52 fps). Measured at 2x on a 48-240 Hz VRR display: at 240 Hz the
limit never binds; at 60 Hz with V-Sync On or Auto it keeps the game frames 27 ms from present to screen, where main's
FIFO queue reaches 133 ms; with V-Sync Off it held the game at 29.5 fps where main renders 60, hence none there. Auto
under Streamline is mailbox, not FIFO (`fg_fifo` needs `swapchain_dlssg()`, which is NGX's DLSS-G), so the limit keys on
the menu rather than the present mode. A fixed-refresh screen was not measured.

**The exit.** `mkwvk_close_asked` calls `dlssg_shutdown` whenever Streamline's loader is the renderer's: no call let in
any more, a Reflex sleep under way waited for, the present lock and the queue's taken and kept, the device idle, DLSS-G
off, `slShutdown`. Each wait is 2 s at most; past it Streamline is left as it is. Left to the process's end, the plugin
tears down in its DLL detach after its threads are gone, an access violation at every exit. A fatal end cannot wait
for that shutdown (it comes from any thread, perhaps under those locks): the renderer's (`replay_fatal`: a lost
device, a C++ exception out of the renderer) writes its FATAL line and the log out, then, when Streamline's loader is
the renderer's, writes the performance summary's last (the renderer's `atexit` writes it at the detach) and ends the
process before the DLLs' detach, with its exit code 3. Left to the detach, the game showed an access violation in the
plugin's teardown after the FATAL line, logged as a crash, and a crash's exit code in place of 3. The host's own FATAL
and its out-of-memory stop still end through the detach.

#### The driver's two metering laws

NVIDIA's driver shows a DLSS-G batch (the made frame, then the game's) by a schedule of its own (`VK_NV_present_metering`,
set by the plugin). At 2x with the game at 60 fps it follows one of two schedules, called laws here: under L1 the frames
reach the screen at even intervals, under L2 at uneven ones. The game cannot choose, but a gap between two frames on
the screen a few milliseconds longer than the game's frame makes the driver choose again. The pacing in
[`dlssg_pace.h`](../../../gpu/vk/dlssg_pace.h) rests on that. Its three numbers are constants there, each with a switch:

| Part | Constant, switch | What it does |
|---|---|---|
| The hold | `kHoldUs`, `RX_VK_DLSSG_HOLD` | A game frame's present goes to Streamline that long after the game's own present of it (the present job's `game_at`, which `do_present` hands to the hold), not when the renderer finished: the render time's variation no longer reaches the screen. Scaled with the game's frame above 60 fps. Not held while the game does not reach its cap, or when the present is late already |
| The grid | `kGridUs`, `RX_VK_DLSSG_HOLD_GRID` | While the game's own presents stay near a line fitted through its last 60, the hold counts from the line instead of the present: the game's own jitter is taken out too |
| The toggle | `toggle_for_floor`, `kToggleUs`, `RX_VK_DLSSG_TOGGLE` | When the reader below reads L2, one present is held that long, which leaves the screen gap that makes the driver choose again. Only at 2x with the game near 60 fps (`toggle_fits`), a second apart at least. How long: [from the screen's VRR floor](#the-toggles-hold-from-the-screen) |

The reader ([`dlssg_law.h`](../../../gpu/vk/dlssg_law.h)) tells the laws apart from when the presents reach the screen:
per half second, from each batch, the time between the first present's call of one batch and `first_pixel_out` of the
next batch's second present; a spread (p90 less the median) over the band's threshold (below) is L2. It asks after two
L2 half seconds in a row, abstains unless it read at least 10 of its last 20 half seconds, reads only batches of two,
and bounds its asks: after three holds that changed nothing it keeps quiet, each time twice as long, until it reads
three L1 half seconds with no L2 between them, and never asks more than six times a minute. With VRR off the driver has
no such laws and the reader stays quiet.

#### The toggle's hold from the screen

The driver chooses again when the screen goes without a new frame about as long as the longest frame the screen's VRR
holds, 1 / its floor: from 20.46 to 20.55 ms on a 48-240 Hz screen, just under 1 / 48 Hz (20.83 ms). A hold of 22 ms there changed
the law each time (15 of 15); 24 ms left gaps up to 27.5 ms on the screen, and holds that changed nothing left 28 to
29 ms. So the hold follows the floor: 1 / floor + 1167 µs, the margin 22 ms had at 48 Hz (`toggle_for_floor`).

At each swapchain made (`dlssg_swapchain_made`, from `create_swapchain`) the floor is read from the EDID of the
window's screen: Windows' display path for the window's monitor, then the monitor's device node and its `EDID` value
(`QueryDisplayConfig`, cfgmgr32; no rights asked). On HDMI only, and only from the HDMI Forum's block in the CTA-861
extension ([`edid_vrr.h`](../../../gpu/vk/edid_vrr.h)): a screen with its VRR turned off in its own menu gives a floor
of 0 there while its range limits still say 30 Hz. The log says it once per change (`the toggle's hold 22.0 ms, from
<screen>'s VRR floor (HDMI, 48-240 Hz in its EDID)`, or why it took the default).

Pitfalls:

- Only a 48 Hz floor was measured. The rule is used for floors of 40 to 52 Hz (20.4 to 26.2 ms) and 22 ms elsewhere,
  on DisplayPort (its ranges are not read), and where the EDID is not read; `RX_VK_DLSSG_TOGGLE=<us>` sets the hold
  by hand. By the rule, 22 ms is too short for a floor under 40 Hz (1 / 30 Hz is 33 ms): whether it changes the law
  there is not known either way, and the reader's bounds cap what holds that change nothing cost (three, then quiet
  for 30 s, doubled each time).
- The floor is read when a swapchain is made, not when the window moves: a window dragged to another screen with no
  new swapchain keeps the first screen's hold until one is made (a resize, a mode change).
- The default comes from the screen and is still no `hw` rule of [device policy](architecture.md#device-policy): its
  facts are the GPU's and the PC's, learned once before the device is made, and the screen is the window's, which
  changes during a run. It is read where the refresh is (`display_refresh_mhz`), at the swapchain.
- The laws were seen at the driver's default settings; with other settings the driver may keep L1, and the toggle
  then never fires.

Which frame `first_pixel_out` follows is the driver's state, not the game's: the batch before's made frame (that time
near 6.6 ms under L1) or its game frame, shown 8.3 ms later (near 14 to 15 ms). One PC showed both with the same build
(RTX 4090, driver 621.24): the game frame after VRR was turned off and on, and again the next morning; the made frame
after a restart. The reader has a band for each, 5.5 to 9 ms with L2 over 0.20 ms, and 11.5 to 17 ms with L2
over 0.50 ms, which takes only batches a 60 fps frame apart (at 40 fps the time falls in it too). It reads each half
second in the band most of its values fall in, and asks only on two L2 half seconds of the same band. The log says
which frame it follows (`the driver's present timing follows ...`), and once after 30 s unread (`the driver's metering
law has not been read for 30 s`).

The times come from Longshot's own Vulkan layer ([`vk_dlssg_layer.cpp`](../../../gpu/vk/vk_dlssg_layer.cpp)). Streamline's
interposer passes the game's instance layers to the loader, so a layer the game names sits between the plugin and the
driver and sees the plugin's presents, which the game does not. The layer is `longshot_vk.dll` itself: its manifest is
written at the start in `longshot_vk_layer` beside the DLL and its folder given to the loader in `VK_ADD_LAYER_PATH`.

**The layer's rule: it reads and passes on.** It asks for each present's times (`VK_EXT_present_timing`, a
`VkPresentTimingsInfoEXT` put ahead of the present's own chain) and never delays, reorders or removes a present, nor
changes a structure the plugin hands the driver. Only the game's own presents are held, in `dlssg_hold`.

Pitfalls:

- The layer is named only while Streamline is active. When the loader does not list it, the manifest cannot be written
  (a read-only game folder) or the instance fails with it, the game starts without it: no reader, no toggle, the hold
  and the grid as they are. The same on a driver without `VK_EXT_present_timing`, `VK_KHR_present_id2` or calibrated
  timestamps (`dlssg_layer_device_extensions`).
- The toggle and the reader's bands are for 2x at 60 fps. At 3x and 4x (RTX 50) and at other frame rates the toggle
  holds nothing; the hold and the grid still apply. At 3x and 4x the reader takes no batches. At 2x at another frame
  rate it can still read and ask (at 90 fps the made image's band takes some values): the game lets the ask go
  (`k,...,0` in the layer's CSV), and the log says nothing. It says `one present held 22.0 ms` where a present is held
  (32 times at most).
- The NVIDIA app's DLSS override changes NGX's side: NGX takes DLSS's and DLSS-G's models from
  `C:\ProgramData\NVIDIA\NGX\models` where it has them, with the presets the app sets, over Longshot's
  `nvngx_dlss.dll` and `nvngx_dlssg.dll`. The log says so once. Streamline then looks for NVIDIA's version of each of
  its plugins, and loads them only when it finds all of them. When it does not, the log says once that it keeps the
  ones beside `longshot.exe`; `sl.log` says which it looked for.
- `sl.log` in the game folder is Streamline's own log; its warnings and errors go to `longshot_log.txt` too.
- `RX_VK_DLSSG_LAYER_CSV=<file>` writes every present the layer sees and every half second the reader read, for
  measurements (its header comment lists the lines).

### Setup failures

Built-in interpolation is optional. Each pipeline and image is published only when complete. A failed pipeline,
sampler, image or view creation releases what was made and sets `fg.failed` (atomic; Reflex reads it through
`framegen_on()`) for the session, leaving ordinary presentation. A resize drains the display jobs and waits for the device
before replacing images; size and multiplier are committed only after every image exists.

## Proving an engine ran

A selected menu row proves nothing. Check the log:

- `vk: <engine> draws OoT's upscale: WxH -> WxH`, and the status line's drawn-frame counts;
- `vk: <engine>: the game's upscale runs (<reason>)` when the chain falls back (`vk: fsr3: ...` for FSR 3.1 on its own
  inputs);
- frame generation's maker in the FPS counter and the Frame generation by row (`framegen_maker`), and DLSS-G's skip
  reason (`dlss_fg_skip`);
- for DLSS FG through Streamline, `vk: Streamline started for DLSS Frame Generation`, then `vk: Streamline DLSS Frame
  Generation: on, 2x`; `vk: Streamline: ... DLSS Frame Generation does not go through it in this run` when it gave up;
  `vk: Longshot's layer under Streamline: reads when DLSS FG's presents reach the screen` and `the driver's metering law
  is read` for the layer and its reader. No `vk: Streamline` line at all: it was not started (the rows, the files, the
  GPU or `RX_VK_DLSSG=0`).

## Switches

All switches are in [`switches.def`](../../../gpu/vk/switches.def) and [switches](switches.md). The ones you need here:

| Switch | Effect |
|---|---|
| `RX_VK_DLSS`, `RX_VK_FSR`, `RX_VK_FSR4`, `RX_VK_XESS` | Force or forbid an engine for one run, over the Upscaler row |
| `RX_VK_FSR3_CHAIN=0` | FSR 3.1 on its own inputs instead of the chain |
| `RX_VK_DLSS_PRESET` | A DLSS preset letter (or `0`) over the DLSS model row ([models](#dlss-models)) |
| `RX_VK_FSR4_MODEL=auto\|lite` | The FSR 4 network for one run, over the FSR 4 model row ([models](#fsr-4-models)) |
| `RX_VK_DLSS_LOG=1` | NGX's own lines in the game's log ([which are harmless](#dlsss-log-and-memory)) |
| `RX_VK_DLSS_DIR`, `RX_VK_FSR_DIR`, `RX_VK_XESS_DIR` | A folder looked in first for the vendor's runtime ([vendor files](#vendor-files)) |
| `RX_VK_DLSS_DEBUG` | `motion` shows the motion vectors; `alternate` shows the game's upscale every other frame |
| `RX_VK_DLSS_JITTER=0`, `RX_VK_FSR_JITTER=0` | No camera jitter for the temporal engines, or for FSR 3.1 |
| `RX_VK_UPSCALE_LOD_BIAS=0`, `RX_VK_DLSS_LOD_OFFSET=<steps>` | No upscaler texture bias, or the DLSS / FSR offset ([texture LOD bias](#texture-lod-bias)) |
| `RX_VK_FSR4_HDR=0`, `RX_VK_XESS_HDR=0` | FSR 4 and XeSS on their LDR contexts with native HDR |
| `RX_VK_REFLEX_LOG=1` | NVIDIA Reflex's latency timings in the log every 300 frames while Reflex is on |
| `RX_VK_PSYCHOV35_DLSS=0` | Turn off the PsychoV35 RGBA16F route |
| `RX_VK_FRAMEGEN=0`, `RX_VK_FRAMEGEN_MULT` | Frame generation off, or a multiplier for one run |
| `RX_VK_DLSS_FG`, `RX_VK_FSR_FG` | Force or forbid DLSS-G or FSR 3.1 interpolation |
| `RX_VK_DLSSG=0` | Never start Streamline: NGX's DLSS-G makes DLSS FG's frames ([Streamline](#dlss-fg-through-streamline)) |
| `RX_VK_DLSSG_HOLD=<us>`, `RX_VK_DLSSG_HOLD_GRID=<us>`, `RX_VK_DLSSG_TOGGLE=<us>` | A/B the pacing of Streamline's presents, `0` each part off ([the laws](#the-drivers-two-metering-laws)); the toggle's hold set, in place of the one from the screen's floor. With the hold off, turn the toggle off too: the reader then reads the uneven law most of the time and asks a few times a minute, each a 28 to 31 ms screen gap |
| `RX_VK_DLSSG_LAYER=0`, `RX_VK_DLSSG_LAYER_CSV=<file>`, `RX_VK_DLSSG_RECT=0` | Without Longshot's layer under Streamline (so no toggle); what the layer sees, in a file; DLSS-G given the whole swapchain image, bars included (as even on the RTX 4090, 0.8 ms more latency) |
| `RX_VK_FSR_FG_HUDLESS=0`, `RX_VK_FSR_FG_HUD_LAYER=0`, `RX_VK_FSR_FG_HUD=0` | A/B the HUD handling |

## Tools and checks

| File | What it is |
|---|---|
| `scripts/shaders.py fsr3` | Compiles FSR 3.1's upscaler and interpolation passes into `gpu/vk/shaders` |
| `scripts/fetch.py dlss\|fsr\|xess\|streamline` | Fetch NVIDIA's, AMD's and Intel's SDK files for a local build ([setup](../setup.md#upscaler-sdks-optional)) |
| `tests/test_dlssg.py` | Streamline's seams in the renderer, the stubs of a build without it, Reflex's frame limit, the exit with a stand-in SDK |
| `tests/test_dlssg_pace.py` | The start rules, the hold, the grid and the law reader on made-up presents; the switches' defaults |
| `tests/test_sl_sdk.py` | The Streamline fetch and its file tables |
| `tests/test_temporal_history.py` | Reprojection, reset rules and every engine's reset assignment |
| `tests/test_temporal_jitter.py` | Jitter publication, freshness and the host's per-frame cache |
| `tests/test_dlss_feature_size.py` | DLSS creation size, input range and reuse |
| `tests/test_psychov35_*.py` | The RGBA16F route per engine, the default and its opt-out |
| `tests/test_fsr_image_retirement.py`, `tests/test_fsr_uniform_ownership.py` | FSR image retirement and constant ownership |
| `tests/test_framegen_resources.py` | Frame generation setup failures and resize |
| `tests/test_fsr3fi_hud_*.py`, `tests/test_fsr3fi_hudless_colour.py` | The HUD copy, mark and HUD-less source |

These checks run without a GPU or game. They do not prove image quality or GPU behaviour; test those in the game (see
[developing](../developing.md#run-the-game-for-a-test)).
