# Graphics guide image: source review against a852f1bc

Retain `settings-graphics.png` after reviewing the five recorded sources that #2456 changed for an Upscaler change under
NVIDIA Streamline since its review at e98ac5f2 ([source-review-e98ac5f2.md](source-review-e98ac5f2.md)). The PNG, its
dimensions and SHA-256, its caption, capture date, version, `captured_source` and capture conditions are unchanged.

`python -I -B scripts/check_guide_screenshots.py --review-sources` flagged these inputs:

| Source | Change and retention decision |
|---|---|
| `gpu/vk/menu/upscaler.cpp` | The Upscaler row takes a restart (`.restart`, its restart value `upscaler_waits`), and its text, help and steps read `upscaler_row()`. Without Streamline nothing waits: `upscaler_row()` is what the row read before, and the row is not tagged After restart. Its label, values and help are unchanged. |
| `gpu/vk/vk_menu.cpp` | `upscaler_waits()` and `upscaler_row()`, and the row's step from `upscaler_row()`. Without Streamline (the fixture's `temporal_row` returns the row's value) both give what the menu drew before. |
| `gpu/vk/menu_module.h` | The two declarations. No row changes. |
| `gpu/vk/menu_restart.h` | `kMax` from 16 to 24 restart slots. No row or text changes. |
| `gpu/vk/vk_temporal.h` | The `temporal_row` declaration. No menu row. |

The fixture changed with them (a852f1bc): `scripts/screenshots/menu.cpp` stands in for `temporal_row`, with the row's own
value, and `fixture_files` records the file's new hash.

The pictured Graphics rows, their values, defaults, help and order, the top tab strip and the keyboard footer remain what
the image and the guide show. This review is made from the sources and the image. No fresh rendering, pixel comparison,
game run or new capture is claimed: the capture needs Linux with SDL2 and libpng, not on the Windows PC this was written
on (`scripts/screenshots/check_menu_capture.py` is its check).
