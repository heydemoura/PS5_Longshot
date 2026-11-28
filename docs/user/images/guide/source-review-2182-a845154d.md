# Graphics guide image: #2182 rebased on main a845154d

Retain `settings-graphics.png` after rebasing #2182 (`RX_VK_PRESENT_PROBE`, the queues' global priority switches) on
`main` a845154d. The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source`,
`fixture_files` and capture conditions are unchanged; no new capture was made.

Both sides had recorded their own changes to the recorded UI-source inputs: `main` reviewed `gpu/vk/vk_framegen.h` at
#2184 (a845154d, `framegen_record`'s `game_at` and `game_wait`, source review against 34f862d2 in the capture's notes)
and changed `gpu/vk/device_policy.h` and `gpu/vk/vk_internal.h` at #2439 (efe51453, RADV's `mma_stage_b_try` and
`mixed_dot_f16`, which no menu row reads) without updating their hashes; #2182 recorded its own in
[source-review-f0a9f9bc.md](source-review-f0a9f9bc.md), [source-review-2182-9b7aaea4.md](source-review-2182-9b7aaea4.md)
and [source-review-2182-20bedc6d.md](source-review-2182-20bedc6d.md) (device policy for two diagnostic switches and two
internal declarations, no menu row). The rebase adds no change of its own to these files: #2182's added and removed
lines in each of them are the same before and after it.

The inputs that both sides changed, recorded at the rebase:

| Source | Why both sides changed it |
|---|---|
| `gpu/vk/device_policy.h` | #2182: `parse_queue_priority`, `queue_priority_listed`, `queue_priority_refused`; `main`: #2439's `mma_stage_b_try` |
| `gpu/vk/vk_framegen.h` | #2182: `RX_VK_PRESENT_PROBE`'s `game` and `set` and `present_probe_close`; `main`: #2184's `framegen_record` arguments |
| `gpu/vk/vk_internal.h` | #2182: the queues' global priority declarations; `main`: #2439's `mixed_dot_f16` |

`scripts/check_guide_screenshots.py --review-sources` reports twelve `REVIEW REQUIRED` inputs on `main` a845154d and
ten on the rebased branch (`gpu/render/rx_nvn.h`, `gpu/vk/menu/controller.cpp`, `gpu/vk/menu/framegen.cpp`,
`gpu/vk/menu_i18n.h`, `gpu/vk/vk_api.h`, `gpu/vk/vk_menu.cpp`, `gpu/vk/vk_menu.h`, `gpu/vk/vk_menu_i18n.cpp`,
`gpu/vk/vk_paths.h`, `gpu/vk/vk_texture.h`), all also reported on `main`: they come from `main` and are left to its
review. The rebase adds none; `main`'s other two (`gpu/vk/device_policy.h`, `gpu/vk/vk_internal.h`) are the inputs
recorded above.

The review revision in `manifest.json` is now `sha256:23258f4c5af21cfe851dc779a5e39b292c85cd51aaa0a7861e7267300aef6d9e`,
the SHA-256 of the sorted `<path> <sha256>` lines (UTF-8, final newline) for all 146 entries in
`reviewed_source.files`. It replaces both `main`'s `34f862d2` and the `sha256:758c3020...` that
source-review-2182-20bedc6d.md records.
