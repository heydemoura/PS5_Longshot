# Graphics guide image: the DLSS 5 preset's Custom set

Retain `settings-graphics.png` after the DLSS 5 preset change: the preset row's Custom gives back the player's last
values (saved as `dlss5_custom=`), and the DLSS 5 preset and DLSS 5 model rows move from the Graphics tab to the DLSS 5
settings page. Three recorded UI-source inputs change, so `scripts/check_guide_screenshots.py --review-sources` asked
for this review. The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source` and capture
conditions are unchanged; no new capture was made.

| Source | Change | In the picture? |
|---|---|---|
| `gpu/vk/menu/dlss5.cpp` | The DLSS 5 preset row's Custom set; the preset row moves to the top of the DLSS 5 settings page and the DLSS 5 model row to its bottom; the help of the preset row and of the DLSS 5 settings row | No |
| `gpu/vk/menu/dlss5.h` | The Custom set's options line, out and in (`dlss5_custom_text`, `dlss5_custom_read`) | No |
| `gpu/vk/vk_menu.cpp` | `save()` writes the `dlss5_custom=` line and `menu_init()` reads it | No |

Every DLSS 5 row shows only where DLSS 5 can run. The fixture, as a PC without a DLSS 5 file, has none
(`scripts/screenshots/menu.cpp`: `dlss_nr_offered()` is false), so the DLSS 5, DLSS 5 preset, DLSS 5 model and DLSS 5
settings rows are all hidden there, before and after this change. The `vk_menu.cpp` change is in the options file only.

The pictured rows, values, help, tab strip and footer stay as the earlier reviews found them, and
`scripts/check_guide_screenshots.py --review-sources` passes.
