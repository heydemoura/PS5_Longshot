"""On the console the Frame generation row's Auto can be had, and 2x is the row's default, not a variable set at every start.

host/hos_main.c set RX_VK_FRAMEGEN_MULT=2 among the console's defaults, and vk_menu.cpp read that variable as an override
of the options file with 2 as its least: the row went back to 2x at every start, and Auto (a frame for each of the display's
refreshes) could not be had by the menu, the file or the variable. Source checks, no game and no compiler.

python -I -B tests/test_ps5_framegen_fill.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    host = (ROOT / "host" / "hos_main.c").read_text(encoding="utf-8")
    menu = (ROOT / "gpu" / "vk" / "vk_menu.cpp").read_text(encoding="utf-8")
    # (the row's own texts are in its module since the menu was split: gpu/vk/menu/framegen.cpp)
    row = (ROOT / "gpu" / "vk" / "menu" / "framegen.cpp").read_text(encoding="utf-8")
    header = (ROOT / "gpu" / "vk" / "vk_menu.h").read_text(encoding="utf-8")
    console = re.search(r"#ifdef __PROSPERO__[^\n]*\n\s*int framegen = (\d+);\n#else\n\s*int framegen = (\d+);\n#endif", header)
    saved = re.search(r'fprintf\(f, "options_version=(\d+)\\n"\);', menu[menu.index("void save() {"):])
    checks = [
        ('{"RX_VK_FRAMEGEN_MULT", "2"}' not in host, "the console still sets RX_VK_FRAMEGEN_MULT=2 at every start"),
        (bool(console) and console.group(1) == "2" and console.group(2) == "0",
         "2x is not the Frame generation row's default on the console (and Off elsewhere)"),
        ('getenv("RX_VK_FRAMEGEN_MULT")) env_override(&opts.framegen, std::min(std::max(atoi(e), 1), 8))' in menu,
         "RX_VK_FRAMEGEN_MULT=1 is not Auto"),
        (bool(saved) and int(saved.group(1)) >= 7, "save() writes an options_version below 7"),
        (bool(re.search(r"#ifdef __PROSPERO__\n(?:\s*//[^\n]*\n)*\s*if \(version < 7\) opts\.framegen = 2;\n#endif", menu)),
         "an options file from before version 7 loses the console's 2x"),
        ('"Fill to %d fps"' in row and '"%dx (game at %d fps)"' in row,
         "the console's Frame generation values do not say what they do to the game's rate"),
    ]
    bad = [why for ok, why in checks if not ok]
    if bad:
        raise SystemExit("FAIL: " + "; ".join(bad))
    print("PASS: the console's Frame generation: Auto can be had, 2x is the row's default, older files keep 2x")


if __name__ == "__main__":
    main()
