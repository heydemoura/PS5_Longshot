# Graphics guide image: source review against f0a9f9bc

Retain `settings-graphics.png` after reviewing the three recorded sources that the queues' global priority switches and
`RX_VK_PRESENT_PROBE` changed since the review at main `68ab0bc7`. The PNG, its dimensions and SHA-256, its caption,
capture date, version, `captured_source`, `fixture_files` and capture conditions are unchanged.

`python -I -B scripts/check_guide_screenshots.py --review-sources` flagged these inputs:

| Source | Change and retention decision |
|---|---|
| `gpu/vk/device_policy.h` | Reads and gates the `RX_VK_QUEUE_PRIORITY` and `RX_VK_PRESENT_QUEUE_PRIORITY` levels (`parse_queue_priority`, `queue_priority_listed`, `queue_priority_refused`). Device policy for two diagnostic switches; no menu row. |
| `gpu/vk/vk_framegen.h` | Two fields on a deferred present (`game`, `set`) for `RX_VK_PRESENT_PROBE`'s file. Internal; no menu row. |
| `gpu/vk/vk_internal.h` | A declaration (`present_probe_close`) for the window's close. Internal to the renderer; no menu row. |

The pictured Graphics rows, their values, defaults, help and order, the top tab strip and the keyboard footer remain what
the image and the guide show. No fresh rendering, pixel comparison, game run, live GPU check or new capture is claimed.

The review revision in `manifest.json` (`sha256:f0a9f9bc...`) is the SHA-256 of the sorted `<path> <sha256>` lines
(UTF-8, final newline) for all 146 entries in `reviewed_source.files`.
