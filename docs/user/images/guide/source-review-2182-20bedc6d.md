# Graphics guide image: #2182 rebased on main 20bedc6d

Retain `settings-graphics.png` after rebasing #2182 (`RX_VK_PRESENT_PROBE`, the queues' global priority switches) on
`main` 20bedc6d. The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source`,
`fixture_files` and capture conditions are unchanged; no new capture was made.

Both sides had recorded their own changes to the recorded UI-source inputs: `main` updated their hashes, with no
review note, at #1999 (2c89a3eb, `gpu/vk/device_policy.h`'s
`fsr4_compact_builds` flag, which `vk_fsr4.cpp` reads and the log prints, and which no menu row reads) and #2705
(20bedc6d, `gpu/vk/vk_temporal.h` and `gpu/vk/vk_texture.h`, which #2182 does not change), #2182 in
[source-review-f0a9f9bc.md](source-review-f0a9f9bc.md) and [source-review-2182-9b7aaea4.md](source-review-2182-9b7aaea4.md)
(device policy for two diagnostic switches and two internal declarations, no menu row). The rebase adds no change of
its own to these files: #2182's added and removed lines in `gpu/vk/device_policy.h` are the same before and after it.

The input that both sides changed, recorded at the rebase:

| Source | Why both sides changed it |
|---|---|
| `gpu/vk/device_policy.h` | #2182: `parse_queue_priority`, `queue_priority_listed`, `queue_priority_refused`; `main`: #1999's `fsr4_compact_builds` |

`gpu/vk/vk_framegen.h` and `gpu/vk/vk_internal.h` changed on #2182's side only.

`scripts/check_guide_screenshots.py --review-sources` reports the same seven `REVIEW REQUIRED` inputs on `main` 20bedc6d
and on the rebased branch (`gpu/vk/menu/controller.cpp`, `gpu/vk/menu_i18n.h`, `gpu/vk/vk_api.h`, `gpu/vk/vk_menu.cpp`,
`gpu/vk/vk_menu.h`, `gpu/vk/vk_menu_i18n.cpp`, `gpu/vk/vk_paths.h`): they come from `main` and are left to its review.
The rebase adds none.

The review revision in `manifest.json` is now `sha256:758c30202a29eae5ee4c83f12356824217906c103b6fdf564ba75407be4d8b4c`,
the SHA-256 of the sorted `<path> <sha256>` lines (UTF-8, final newline) for all 146 entries in
`reviewed_source.files`. It replaces the `sha256:af51c33e...` that source-review-2182-9b7aaea4.md records.
