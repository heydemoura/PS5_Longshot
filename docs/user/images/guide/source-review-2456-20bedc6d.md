# Graphics guide image: #2456 rebased on main 20bedc6d

Retain `settings-graphics.png` after rebasing #2456 (DLSS Frame Generation through NVIDIA Streamline, stacked on #2184)
onto main 20bedc6d. The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source` and
capture conditions are unchanged; no new capture was made.

#2456 had reviewed its own changes to the recorded UI-source inputs in
[source-review-e98ac5f2.md](source-review-e98ac5f2.md) and [source-review-a852f1bc.md](source-review-a852f1bc.md). The
rebase adds no change of its own to these files, so the pictured rows, values, help, tab strip and footer stay as that
review found them.

The two inputs that both sides changed, recorded at the rebase:

| Source | Why both sides changed it |
|---|---|
| `gpu/vk/vk_temporal.h` | #2705 (main, reviewed there): `temporal_reshade_depth` takes the command buffer; #2456: `temporal_row`, the Upscaler row's value as the renderer runs it, and the comment of `temporal_mode`. The hunks are apart; no menu line changes. |
| `gpu/vk/vk_menu.cpp` | #2708 (main, not recorded there): `bug_report_save` puts the logs and bug reports under `diagnostics/` in an organized game folder, which no pictured row shows; #2456: `upscaler_waits` and `upscaler_row` and the Upscaler row's step from them (under Streamline an Upscaler change to another than DLSS or DLAA waits for the restart). The hunks are apart; the pictured rows draw as before. |

The other inputs that `scripts/check_guide_screenshots.py --review-sources` still reports (`gpu/vk/menu/controller.cpp`,
`gpu/vk/menu_i18n.h`, `gpu/vk/vk_api.h`, `gpu/vk/vk_menu.h`, `gpu/vk/vk_menu_i18n.cpp`, `gpu/vk/vk_paths.h`) are changed
by main alone (#1349, #2708) and reported on main 20bedc6d as well; #2456 does not change them, and this rebase
leaves their review to main.
