"""Check that PS5 CJK ImGui fallback font paths (/app0/fonts/ and /app0/w/Fonts/) are registered without disturbing PC paths."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    src = (ROOT / "gpu/vk/vk_imgui.cpp").read_text(encoding="utf-8")
    if "#ifdef __PROSPERO__" not in src:
        raise SystemExit("FAIL: gpu/vk/vk_imgui.cpp missing #ifdef __PROSPERO__ guard for PS5 CJK fonts")

    expected = {
        '"ja"': ("/app0/fonts/NotoSansCJK-Regular.ttc", "/app0/fonts/YuGothM.ttc", "/app0/w/Fonts/YuGothM.ttc", r"%WINDIR%\\Fonts\\YuGothM.ttc"),
        '"zh-Hans"': ("/app0/fonts/NotoSansCJK-Regular.ttc", "/app0/fonts/msyh.ttc", "/app0/w/Fonts/msyh.ttc", r"%WINDIR%\\Fonts\\msyh.ttc"),
        '"zh-Hant"': ("/app0/fonts/NotoSansCJK-Regular.ttc", "/app0/fonts/msjh.ttc", "/app0/w/Fonts/msjh.ttc", r"%WINDIR%\\Fonts\\msjh.ttc"),
        '"ko"': ("/app0/fonts/NotoSansCJK-Regular.ttc", "/app0/fonts/malgun.ttf", "/app0/w/Fonts/malgun.ttf", r"%WINDIR%\\Fonts\\malgun.ttf"),
    }
    for lang, needles in expected.items():
        pos = src.index("{" + lang + ",")
        end = src.index("}},", pos)
        entry = src[pos:end]
        for needle in needles:
            if needle not in entry:
                raise SystemExit(f"FAIL: entry {lang} missing {needle!r}")
        if "CJK_LINUX" not in entry:
            raise SystemExit(f"FAIL: entry {lang} lost CJK_LINUX fallback")

    print("ok: PS5 /app0/fonts and /app0/w/Fonts CJK fallbacks present alongside Windows/Linux paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
