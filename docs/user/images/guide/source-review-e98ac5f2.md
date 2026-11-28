# Graphics guide image: source review against e98ac5f2

Retain `settings-graphics.png` after reviewing the four recorded sources that DLSS Frame Generation through NVIDIA
Streamline (#2456) changed since `main`'s review at `68ab0bc7` ([source-review-68ab0bc7.md](source-review-68ab0bc7.md)).
The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source` and capture conditions are
unchanged. The review was first made at dbf03654, on `main` be663a08; #2456 was then rebased on `main` fd68f82c, where
e98ac5f2 is the same change.

`python -I -B scripts/check_guide_screenshots.py --review-sources` flagged these inputs:

| Source | Change and retention decision |
|---|---|
| `gpu/vk/menu/framegen.cpp` | The Frame generation by row takes a restart while Streamline is to start or stop (`dlssg_restart_needed`), and its line then says what the restart changes. The row is not pictured: the menu hides it while Frame generation is Off (`vk_menu.cpp`), the capture's state. Its label, values and help are unchanged. |
| `gpu/vk/vk_dlss.h` | Two comments say what Streamline changes in `dlss_fg_unavailable` and `dlss_fg_most`. No declaration changes. `main`'s DLSS 5 changes to it since be663a08 are kept as `main`'s review records them. |
| `gpu/vk/vk_framegen.h` | A comment: `framegen_now` is 0 while Streamline makes the frames. No declaration changes. |
| `gpu/vk/vk_internal.h` | A declaration (`fatal_end`) for the renderer's fatal end. Internal; no menu row. |

The fixture changed with them: `scripts/screenshots/menu.cpp` stands in for `dlssg_active` (false) and
`dlssg_restart_needed` (0), which the Frame generation module now calls. With them the row's line is the one the fixture
drew before, and `fixture_files` records the file's new hash.

The pictured Graphics rows, their values, defaults, help and order, the top tab strip and the keyboard footer remain what
the image and the guide show. This review is made from the sources and the image. No fresh rendering, pixel comparison,
game run, live GPU check or new capture is claimed: the capture needs Linux with SDL2 and libpng, and the fixture was
not linked on the Windows PC this was written on (`scripts/screenshots/check_menu_capture.py` is its check).
