# Native Vulkan AMD FSR 4.1.2 on PS5

On PS5 (`__PROSPERO__`), `gpu/vk/vk_fsr4.cpp` integrates a static Vulkan compute-replay backend (`gpu/vk/fsr4/fsr411.{h,cpp}`, adapted from [`Mob1K7105/beamng-fsr4-linux`](https://github.com/Mob1K7105/beamng-fsr4-linux) v0.3.0 under `GPL-2.0-or-later`) instead of dynamically loading FidelityFX DLLs/SOs.

## Design choices for PS5

- **No dynamic loading (`dlopen`)**: `Fsr411::Upscaler` links statically into the PS5 executable (`obj/fsr4_fsr411.o`) and dispatches through `gpu/vk/vk_loader.h` (`VK_NO_PROTOTYPES`).
- **No background SPIR-V unrolling**: pre-extracted SPIR-V modules (`prepass_portable.spv`, RDNA 2 / Navi2 INT8 kernels, and portable compute fallbacks) run directly without spawning heavy runtime shader compilers inside console memory limits.
- **No binary weights in git**: following Longshot's existing `Lossless.dll` model, no AMD FidelityFX binaries, SPIR-V modules, or weights are shipped in this repository. Users supply their own extracted asset sets on the console.
- **Recorder-thread safe**: `vk_fsr4.cpp` drains the command recorder via `::mkwvk::rec::boundary(::mkwvk::rec::B_DIRECT)` immediately before `Fsr411::Upscaler::Record(fr)` so `RX_VK_RECORDER=thread` works safely.
- **Unchanged translator stamp**: optional extensions `VK_KHR_compute_shader_derivatives` and `VK_VALVE_shader_mixed_float_dot_product` are probed via local booleans in `vk_main.cpp` without modifying `vk_state.h`, keeping `LONGSHOT_SHADER_STAMP` untouched.

## User-supplied asset layout

Place extracted FSR 4 asset sets (`initializer.bin`, `prepass_portable.spv`, pass SPIR-Vs) under the title folder:

```text
/app0/fsr4/lite/<tier>/...   # FSR 4.1.2 Lite INT8 (default, 12 compute passes)
/app0/fsr4/int8/<tier>/...   # FSR 4.1.1 / 4.1.2 INT8 (optional, 29 compute passes)
```

where `<tier>` matches `Fsr411::Upscaler` tiers (`t1080_m0`, `t1080_m1`, `t2160_m0`, `t2160_m1`).

Search order checked at runtime by `vk_fsr4.cpp`:
1. `$RX_VK_FSR_DIR` (if set)
2. `/app0/fsr4/<variant>`
3. `/data/longshot/PPSA99025/fsr4/<variant>`
4. `/data/eglantineps5/user/fsr4/<variant>`
5. `/app0/fsr4` and `/data/longshot/PPSA99025/fsr4` directly

## Environment switches (`longshot_env.txt`)

- `RX_VK_FSR4=0`: disables FSR 4 even when assets are present.
- `RX_VK_FSR4_VARIANT=int8` (or `full` / `navi2`): prefers `fsr4/int8` before falling back to `fsr4/lite`.
- `RX_VK_FSR4_PORTABLE=1`: skips `VK_VALVE_shader_mixed_float_dot_product` and forces `prepass_portable.spv`.
- `RX_VK_FSR_DIR=<path>`: custom asset directory override.

## Live Base PS5 telemetry (`Oberon GFX1013`)

Verified on a Base PS5 (firmware 12.40, `1920x1080 @ 119.88 Hz`, `AMD FSR 4` Quality `67%` using `lite/t1080_m0`, Kokiri Forest ~1,490 draws/frame):
- `AMD FSR 4: 600 frames drawn, 1 made` — 0 errors, 0 fallbacks
- GPU frame time: `17.4–21.2 ms` (`GPU-bound wait 0.1 ms`)
- FSR 4 memory footprint: `56 MB` images + `80 MB` upscaler workspace (`MemoryBytes()`)
