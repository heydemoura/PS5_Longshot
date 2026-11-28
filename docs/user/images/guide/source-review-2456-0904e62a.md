# Graphics guide image: #2456 stacked on #2184 0904e62a

Retain `settings-graphics.png` after rebasing #2456 (DLSS Frame Generation through NVIDIA Streamline) onto #2184's head
0904e62a (a fixed frame generation multiplier's sets on the game's presents). The PNG, its dimensions and SHA-256, its
caption, capture date, version, `captured_source` and capture conditions are unchanged; no new capture was made.

Both sides had reviewed their own changes to the recorded UI-source inputs: #2184 against 34f862d2 (the sentence in the
capture's notes), #2456 in [source-review-e98ac5f2.md](source-review-e98ac5f2.md) and
[source-review-a852f1bc.md](source-review-a852f1bc.md). The stack adds no change of its own to these files, so the
pictured rows, values, help, tab strip and footer stay as both reviews found them.

The one input that both sides changed, recorded at the stack (`scripts/check_guide_screenshots.py --review-sources`
passes):

| Source | Why both sides changed it |
|---|---|
| `gpu/vk/vk_framegen.h` | #2184: `framegen_record`'s `game_at` and `game_wait` arguments and their comment; #2456: a comment on `framegen_capped` (`framegen_now` is 0 while Streamline makes DLSS FG's frames). The two hunks are apart; no menu line changes. |
