# Graphics guide image: main 3b699a6c

Retain `settings-graphics.png` at `main` 3b699a6c. #2158 changed one recorded UI-source input, so
`scripts/check_guide_screenshots.py --review-sources` failed on `main`. The PNG, its dimensions and SHA-256, its caption,
capture date, version, `captured_source` and capture conditions are unchanged; no new capture was made.

| Source | Change | In the picture? |
|---|---|---|
| `gpu/vk/device_policy.h` | A device policy flag, `mesh_shared_warp`: how the mesh fallback's shaders are translated on Mesa's Intel driver and AMD's Windows driver on GCN. The policy's log line prints it | No: no menu row reads it |

The reviewed revision is now 3b699a6c (`3b699a6c4388fa781e60d83132f9a2178cced653`), and `scripts/check_guide_screenshots.py --review-sources` passes.
