# Graphics guide image: #2182 rebased on main c25ecc4a

Retain `settings-graphics.png` after rebasing #2182 (`RX_VK_PRESENT_PROBE`, the queues' global priority switches) on
`main` c25ecc4a. The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source`,
`fixture_files` and capture conditions are unchanged; no new capture was made.

Both sides changed two of the recorded UI-source inputs since #2182's last base (9a23e32e): `main` at #2456 (6e6f1160,
DLSS Frame Generation through NVIDIA Streamline, reviewed by `main` in source-review-e98ac5f2.md and
source-review-a852f1bc.md) and #2414 (3f94da5d, `present_image`'s `framegen_format`, without updating the hash), and
#2182 in [source-review-f0a9f9bc.md](source-review-f0a9f9bc.md) and the rebase reviews after it (fields for
`RX_VK_PRESENT_PROBE`'s columns, a declaration, no menu row). The rebase adds no change of its own to these files:
#2182's added and removed lines in each of them are the same before and after it.

The inputs that both sides changed, recorded at the rebase:

| Source | Why both sides changed it |
|---|---|
| `gpu/vk/vk_framegen.h` | #2182: `RX_VK_PRESENT_PROBE`'s `game` and `set`; `main`: #2456's `framegen_now` comment and #2414's `present_image` `framegen_format` |
| `gpu/vk/vk_internal.h` | #2182: `present_probe_close`; `main`: #2456's `fatal_end` |

`gpu/vk/device_policy.h`, recorded at the earlier rebases, has not changed on `main` since 9a23e32e; its hash is the
rebased file's.

`scripts/check_guide_screenshots.py --review-sources` reports sixteen `REVIEW REQUIRED` inputs on `main` c25ecc4a and
fourteen on the rebased branch, all also reported on `main`: they come from `main` and are left to its review. The
rebase adds none; `main`'s other two (`gpu/vk/device_policy.h`, `gpu/vk/vk_framegen.h`) are the inputs #2182 records.

The review revision in `manifest.json` is now `sha256:79057808b91664f095e8821b0900c11332cb19a52f3ac663a1556bc53fcfcee3`,
the SHA-256 of the sorted `<path> <sha256>` lines (UTF-8, final newline) for all 146 entries in
`reviewed_source.files`. It replaces both `main`'s `#2184 0904e62a + #2456 a852f1bc` and the `sha256:23258f4c...` that
source-review-2182-a845154d.md records.
