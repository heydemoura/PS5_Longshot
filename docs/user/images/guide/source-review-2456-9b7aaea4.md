# Graphics guide image: #2456 rebased on main 9b7aaea4

Retain `settings-graphics.png` after rebasing #2456 (DLSS Frame Generation through NVIDIA Streamline, stacked on #2184)
onto main 9b7aaea4. The PNG, its dimensions and SHA-256, its caption, capture date, version, `captured_source` and
capture conditions are unchanged; no new capture was made.

Both sides had reviewed their own changes to the recorded UI-source inputs: main's #2582 (the DLSS 5 preset's Custom
set) in [source-review-dlss5-preset.md](source-review-dlss5-preset.md), #2456 in
[source-review-e98ac5f2.md](source-review-e98ac5f2.md) and [source-review-a852f1bc.md](source-review-a852f1bc.md). The
rebase adds no change of its own to these files, so the pictured rows, values, help, tab strip and footer stay as both
reviews found them. #2455's two commits that #2456 carried are left out: main has them, squashed in 9b7aaea4.

The one input that both sides changed, recorded at the rebase (`scripts/check_guide_screenshots.py --review-sources`
passes):

| Source | Why both sides changed it |
|---|---|
| `gpu/vk/vk_menu.cpp` | #2582: the options file's `dlss5_custom` line, written in `save()` and read in `menu_init()`; #2456: `upscaler_waits` and `upscaler_row` (under Streamline an Upscaler change to another than DLSS or DLAA waits for the restart) and the Upscaler row's step from them. The hunks are apart; the DLSS 5 rows stay hidden in the fixture, and without Streamline the Upscaler row draws as before. |
