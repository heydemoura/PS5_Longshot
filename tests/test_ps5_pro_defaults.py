"""A PS5 Pro's defaults: a new install's Output resolution and Auto's Render resolution.

- host/hos_wide_size.h hos_output_pro: 3200x1800 on a 16:9 screen of 3840x2160 or more, nothing changed on a smaller
  screen, a screen of another shape, or one not known;
- hos_output_default, built as the console builds it: on a Pro (hos_ps5_pro_mode) a 3840x2160 screen gets 3200x1800 and a
  base console 2560x1440; a 1920x1080 screen its own size on both; built as a PC builds it, 2560x1440 as before;
- gpu/vk/auto_settings.h: on the console Auto chooses AMD FSR 3.1; with the Pro's memory its Render resolution is Auto
  (dynamic), without it the tier's own; a PC's choice has no such thing;
- the sources: hos_ps5_pro_mode tells the Pro's memory from the base's, vk_main.cpp gives it to Auto, and apply_auto sets
  the row.

python -I -B tests/test_ps5_pro_defaults.py [--cxx clang++]
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUN_ALL_ARGS = ["--cxx", "{cxx}"]

FIXTURE = r"""
#include <cstdint>
#include <cstdio>
#include <cstring>
#ifdef __PROSPERO__
extern "C" int hos_ps5_pro_mode(void) { return PRO; }
#endif
#include "host/hos_wide_size.h"
#include "gpu/vk/auto_settings.h"
static int bad;
#define CHECK(x, why) do { if (!(x)) { printf("FAIL: %s\n", why); bad = 1; } } while (0)
static bool is(uint32_t sw, uint32_t sh, uint32_t w, uint32_t h, void (*f)(uint32_t, uint32_t, uint32_t*, uint32_t*), bool start) {
    uint32_t gw = 2560, gh = 1440;
    if (!start) gw = gh = 0;
    f(sw, sh, &gw, &gh);
    return gw == w && gh == h;
}
int main() {
    // the Pro's rule itself (it only ever changes the size it is given)
    CHECK(is(3840, 2160, 3200, 1800, hos_output_pro, true), "a 3840x2160 screen does not get 3200x1800");
    CHECK(is(5120, 2880, 3200, 1800, hos_output_pro, true), "a larger 16:9 screen does not get 3200x1800");
    CHECK(is(2560, 1440, 2560, 1440, hos_output_pro, true), "a 2560x1440 screen is changed");
    CHECK(is(1920, 1080, 2560, 1440, hos_output_pro, true), "a 1920x1080 screen is changed by the Pro's rule");
    CHECK(is(5120, 2160, 2560, 1440, hos_output_pro, true), "a 21:9 screen gets a 16:9 size of the Pro's");
    CHECK(is(0, 0, 2560, 1440, hos_output_pro, true), "a screen not known is changed");
#ifdef __PROSPERO__
    CHECK(is(3840, 2160, PRO ? 3200u : 2560u, PRO ? 1800u : 1440u, hos_output_default, false), "the console's default on a 3840x2160 screen");
    CHECK(is(1920, 1080, 1920, 1080, hos_output_default, false), "a 1920x1080 screen does not keep its own size on the console");
    CHECK(is(0, 0, 2560, 1440, hos_output_default, false), "a screen not known does not get 2560x1440 on the console");
    auto_settings::Hardware h;
    h.vendor = auto_settings::AMD, h.integrated = true, h.vram_mb = 8821, h.ps5_pro = PRO != 0;
    const auto_settings::Choice c = auto_settings::choose(h);
    CHECK(c.tier == 3 && c.upscaler == auto_settings::FSR3, "the console's Auto is not Balanced with AMD FSR 3.1");
    CHECK(c.dynamic == (PRO != 0), "the Render resolution at Auto is not the Pro's alone");
    CHECK((strstr(c.why, "PS5 Pro: Render resolution Auto") != nullptr) == (PRO != 0), "the choice does not say why");
#else
    CHECK(is(3840, 2160, 2560, 1440, hos_output_default, false), "a PC's default on a 3840x2160 screen is not 2560x1440");
    auto_settings::Hardware h;
    h.vendor = auto_settings::NVIDIA, h.rtx = true, h.dlss_built = true, h.vram_mb = 12288, h.ram_mb = 32768, h.ps5_pro = true;
    CHECK(!auto_settings::choose(h).dynamic, "a PC's choice has the Pro's Render resolution");
#endif
    puts(bad ? "FAILED" : "ok");
    return bad;
}
"""


def block(text, marker):
    start = text.index(marker)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default=os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++") or "g++")
    args = parser.parse_args()
    failed = []
    with tempfile.TemporaryDirectory(prefix="ps5-pro-") as tmp:
        folder = Path(tmp)
        (folder / "fixture.cpp").write_text(FIXTURE, encoding="utf-8")
        for name, flags in (("pc", []), ("base", ["-D__PROSPERO__", "-DPRO=0"]), ("pro", ["-D__PROSPERO__", "-DPRO=1"])):
            exe = folder / (name + ".exe")
            subprocess.run([args.cxx, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT), *flags,
                            str(folder / "fixture.cpp"), "-o", str(exe)], check=True, cwd=folder)
            got = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            print(("ok   " if got.returncode == 0 else "FAIL ") + name + ": " + got.stdout.strip().replace("\n", "; "))
            if got.returncode:
                failed.append(name)
    win32 = (ROOT / "host" / "ps5" / "win32_ps5.c").read_text(encoding="utf-8")
    menu = (ROOT / "gpu" / "vk" / "vk_menu.cpp").read_text(encoding="utf-8")
    main_cpp = (ROOT / "gpu" / "vk" / "vk_main.cpp").read_text(encoding="utf-8")
    mode = block(win32, "int hos_ps5_pro_mode(void) {")
    checks = [
        # (12,016 MiB as a base-generation process, 13,232 with the Pro's memory: the line between them)
        ("sceKernelGetDirectMemorySize() >= (12624ull << 20)" in mode, "hos_ps5_pro_mode does not tell the Pro's memory from the base's"),
        ("hw.ps5_pro = hos_ps5_pro_mode() != 0;" in block(main_cpp, "void auto_profile() {"), "Auto is not told of the Pro"),
        ("if (c.dynamic) opts.resolution = 0;" in block(menu, "void apply_auto() {"), "apply_auto does not set the Render resolution to Auto"),
    ]
    for ok, why in checks:
        if not ok:
            failed.append(why)
    for why in failed:
        print("FAIL: " + why)
    if failed:
        return 1
    print("PASS: a PS5 Pro's new install is 3200x1800 on a 3840x2160 screen with the Render resolution at Auto; a base console and a PC as before")
    return 0


if __name__ == "__main__":
    sys.exit(main())
