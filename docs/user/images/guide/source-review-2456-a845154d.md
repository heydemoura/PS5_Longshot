# Graphics guide image: #2456 rebased on main a845154d

Retain `settings-graphics.png` after rebasing #2456 (DLSS Frame Generation through NVIDIA Streamline) onto main
a845154d, where #2184 is merged. The twelve commits of #2184 that #2456 carried are left out: main has them, squashed
in a845154d. The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source` and capture
conditions are unchanged; no new capture was made.

#2456 had reviewed its own changes to the recorded UI-source inputs in
[source-review-e98ac5f2.md](source-review-e98ac5f2.md) and [source-review-a852f1bc.md](source-review-a852f1bc.md). The
rebase adds no change of its own to these files: the pull request's lines are the same as before it.

The three inputs that both sides changed, recorded at the rebase:

| Source | Why both sides changed it |
|---|---|
| `gpu/vk/menu/framegen.cpp` | #2704 (main, not recorded there): the Anti-Lag 2 row gets an `on_change` hook, `menu_antilag_changed`, that publishes the row's value to the presentation threads; its text, hint and values are unchanged. #2456: the Frame generation rows' help gets Streamline's two restart lines (shown only under Streamline). The hunks are apart. |
| `gpu/vk/vk_menu.cpp` | #2704 (main, not recorded there): `menu_init` and `menu_tick` call `menu_antilag_changed(opts.antilag)`, nothing drawn. #2456: `upscaler_waits` and `upscaler_row` and the Upscaler row's step from them. The hunks are apart; the pictured rows draw as before. |
| `gpu/vk/vk_internal.h` | #2439 (main, not recorded there): the device's `mixed_dot_f16` and `software_dot2_extension_wanted`, used by AMD FSR 4's software multiplies, not by the menu. #2456: `fatal_end`, the renderer's end for a reason that is not the device's. The hunks are apart. |

The other inputs that `scripts/check_guide_screenshots.py --review-sources` still reports (`gpu/render/rx_nvn.h`,
`gpu/vk/device_policy.h`, `gpu/vk/menu/controller.cpp`, `gpu/vk/menu_i18n.h`, `gpu/vk/vk_api.h`, `gpu/vk/vk_menu.h`,
`gpu/vk/vk_menu_i18n.cpp`, `gpu/vk/vk_paths.h`, `gpu/vk/vk_texture.h`) are changed by main alone and reported on main
a845154d as well; #2456 does not change them, and this rebase leaves their review to main.
