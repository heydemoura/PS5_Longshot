"""On the console the Threaded rendering row's On is the recorder thread alone, and Auto is Off.

The render thread hung a base PS5 (its buffer pools have no mirrors: a replay behind the game reads memory the game has
moved on from), while the recorder thread alone ran (#40). Source checks, no game and no compiler.

python -I -B tests/test_ps5_recorder_row.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    replay = (ROOT / "gpu" / "vk" / "vk_replay.cpp").read_text(encoding="utf-8")
    menu = (ROOT / "gpu" / "vk" / "vk_menu.cpp").read_text(encoding="utf-8")
    host = (ROOT / "host" / "hos_main.c").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "PS5.md").read_text(encoding="utf-8")
    mode = replay[replay.index("int render_thread_mode() {"):]
    mode = mode[:mode.index("\n}\n")]
    choice = menu[menu.index("static const threaded::Choice& threaded_choice() {"):]
    choice = choice[:choice.index("\n}\n")]
    ps5 = choice[choice.index("#ifdef __PROSPERO__"):choice.index("#endif")]
    checks = [
        # (this tree's row has two halves, threaded_rendering.h: the render thread's reader asks the menu for its half, and the
        # console's choice never has it)
        ("return e ? atoi(e) : menu_render_thread() ? 1 : 0;" in mode, "the render thread's mode does not ask the row's render half"),
        ("rec::g_setting_thread = menu_recorder_thread" in replay, "the row's On does not reach the recorder"),
        (bool(re.search(r"if \(c\.why == threaded::Why::On\) \{\n\s*c\.render_thread = false;", ps5)),
         "the console's row can start the render thread"),
        (bool(re.search(r"if \(threaded::auto_picked\(c\) \|\| c\.why == threaded::Why::AutoFewCores\) \{\n"
                        r"\s*c = threaded::Choice\{false, false, threaded::Why::AutoUnsafe\};", ps5)),
         "Auto is not Off on the console"),
        ('{"RX_VK_DESC_POOL_DIV", "8"}' in host, "the console's descriptor pools are no longer an eighth"),
        ("RX_VK_RECORDER" not in host, "hos_main.c sets a recorder switch on the console"),
        ("## Threaded rendering" in docs and "recorder thread alone" in docs, "docs/PS5.md does not say what On is there"),
    ]
    bad = [why for ok, why in checks if not ok]
    if bad:
        raise SystemExit("FAIL: " + "; ".join(bad))
    print("PASS: the console's Threaded rendering: On is the recorder thread alone, Auto is Off")


if __name__ == "__main__":
    main()
