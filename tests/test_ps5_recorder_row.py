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
    checks = [
        (bool(re.search(r"#ifdef __PROSPERO__\n(?:\s*//[^\n]*\n)*\s*return e \? atoi\(e\) : 0;\n#else\n"
                        r"\s*return e \? atoi\(e\) : menu_threaded_rendering\(\) \? 1 : 0;\n#endif", mode)),
         "the console's row can start the render thread"),
        ("rec::g_setting_thread = menu_threaded_rendering" in replay, "the row's On does not reach the recorder"),
        (bool(re.search(r"#ifdef __PROSPERO__\n(?:\s*//[^\n]*\n)*\s*if \(c\.why == threaded::Why::AutoCpus \|\| "
                        r"c\.why == threaded::Why::AutoFewCpus\) \{\n\s*c = threaded::Choice\{false, threaded::Why::AutoUnsafe\};",
                        menu)),
         "Auto is not Off on the console"),
        ('{"RX_VK_DESC_POOL_DIV", "8"}' in host, "the console's descriptor pools are no longer an eighth"),
        # (the row chooses the recorder's mode: the console's defaults may not set the switches that do, RX_VK_RECORDER itself
        # and RX_VK_ASYNC. Those that tune the recorder once the row turns it on, RX_VK_RECORDER_OWN, RX_VK_RECORDER_END and
        # RX_VK_RECORDINGS, turn nothing on and may be there)
        (not re.search(r"RX_VK_(?:RECORDER|ASYNC)\b", host), "hos_main.c sets the recorder's mode (RX_VK_RECORDER or RX_VK_ASYNC) on the console"),
        ("## Threaded rendering" in docs and "recorder thread alone" in docs, "docs/PS5.md does not say what On is there"),
    ]
    bad = [why for ok, why in checks if not ok]
    if bad:
        raise SystemExit("FAIL: " + "; ".join(bad))
    print("PASS: the console's Threaded rendering: On is the recorder thread alone, Auto is Off")


if __name__ == "__main__":
    main()
