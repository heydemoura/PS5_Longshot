# Graphics guide image: #2182 rebased on main 9b7aaea4

Retain `settings-graphics.png` after rebasing #2182 (`RX_VK_PRESENT_PROBE`, the queues' global priority switches) on
`main` 9b7aaea4. The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source`,
`fixture_files` and capture conditions are unchanged; no new capture was made.

Both sides had reviewed their own changes to the recorded UI-source inputs: `main` in
[source-review-3b699a6c.md](source-review-3b699a6c.md) (`gpu/vk/device_policy.h`'s `mesh_shared_warp` flag, #2158,
which the log prints and no menu row reads), #2182 in [source-review-f0a9f9bc.md](source-review-f0a9f9bc.md) (device
policy for two diagnostic switches and two internal declarations, no menu row). The rebase adds no change of its own to
these files: #2182's added lines are the same before and after it.

The input that both sides changed, recorded at the rebase (`scripts/check_guide_screenshots.py --review-sources`
passes):

| Source | Why both sides changed it |
|---|---|
| `gpu/vk/device_policy.h` | #2182: `parse_queue_priority`, `queue_priority_listed`, `queue_priority_refused`; `main`: its review at 3b699a6c recorded #2158's `mesh_shared_warp` |

`gpu/vk/vk_framegen.h` and `gpu/vk/vk_internal.h` changed on #2182's side only.

The review revision in `manifest.json` is now `sha256:af51c33ed6a751c59268a8927676237c9506372bc034be0b8af5e1129084225b`,
the SHA-256 of the sorted `<path> <sha256>` lines (UTF-8, final newline) for all 146 entries in
`reviewed_source.files`. It replaces the `sha256:f0a9f9bc...` that source-review-f0a9f9bc.md records.
