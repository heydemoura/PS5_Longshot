"""The sticks' range is the player's: the Insert menu's "Stick range" row (stick_range in longshot_options.ini), issue 88.

A pad's driver need not report the whole way on an ordinary full push (SDL's Switch driver maps the largest value an
axis has reported to 32767), and the game needs the whole way for some moves: Link rolls only from a full tilt. The row
says at what share of a pad's own range a stick counts as fully tilted. SDL and XInput share the scaling, which keeps a
stick's direction. The scaling and the menu's row are checked alone (gcc).

python -I -B tests/test_stick_range.py [--cc gcc]
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))   # (tests/srccut.py: python -I leaves this folder off the path)
from srccut import block   # noqa: E402

RUN_ALL_ARGS = ["--cc", "gcc"]

ROOT = Path(__file__).resolve().parents[1]


def check(cond, what):
    if not cond:
        raise SystemExit("FAIL: " + what)


def option_values(api):
    """Each HOS_OPT_ enumerator's value in vk_api.h: its own, or the one before it in its enum plus 1"""
    values = {}
    for body in re.findall(r"enum\s*\{([^}]*)\}", re.sub(r"/\*.*?\*/", "", api, flags=re.S)):
        if "HOS_OPT_" not in body:
            continue
        value = -1
        for item in body.split(","):
            m = re.match(r"\s*(\w+)\s*(?:=\s*(\d+))?\s*$", item)
            if not m:
                continue
            value = int(m.group(2)) if m.group(2) else value + 1
            if m.group(1).startswith("HOS_OPT_"):
                values[m.group(1)] = value
    return values


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cc", default="gcc")
    a = p.parse_args()
    hid = (ROOT / "host" / "nn_hid.c").read_text(encoding="utf-8")
    sdl = (ROOT / "host" / "pad_sdl.c").read_text(encoding="utf-8")
    api = (ROOT / "gpu" / "vk" / "vk_api.h").read_text(encoding="utf-8")
    replay = (ROOT / "gpu" / "vk" / "vk_replay.cpp").read_text(encoding="utf-8")
    menu = (ROOT / "gpu" / "vk" / "menu" / "controller.cpp").read_text(encoding="utf-8")
    header = (ROOT / "gpu" / "vk" / "vk_menu.h").read_text(encoding="utf-8")
    settings = (ROOT / "docs" / "user" / "graphics-and-controls.md").read_text(encoding="utf-8")

    number = re.search(r"enum \{ HOS_OPT_STICK_RANGE = (\d+) \};", api)
    check(number is not None, "vk_api.h has no HOS_OPT_STICK_RANGE")
    ours = int(number.group(1))
    # (the value each HOS_OPT_ enumerator takes, an implicit one too: HOS_OPT_HUD_HEALTH after HOS_OPT_HUD_LAYOUT = 71
    # is 72, which an explicit "= 72" search never sees)
    values = option_values(api)
    others = sorted(name for name, v in values.items() if v == ours and name != "HOS_OPT_STICK_RANGE")
    check(not others, "option %d is also %s" % (ours, ", ".join(others)))
    # (nor read or answered by its bare number: rx_option(77, ...) in the host, id == 77 in the renderer)
    bare = sorted(str(f.relative_to(ROOT)) for d in ("host", "gpu") for f in (ROOT / d).rglob("*")
                  if f.suffix in (".c", ".cpp", ".h") and
                  re.search(r"\brx_option\(\s*%d\s*,|\bid == %d\b" % (ours, ours),
                            f.read_text(encoding="utf-8", errors="replace")))
    check(not bare, "option %d is used by its number in %s" % (ours, ", ".join(bare)))
    # (once a pad, after its dead zone: the scaling twice would make 90% act as 81%; XInput's pads are read on their own
    # thread, by xinput_poll, and pads_read only takes that reading)
    pads = block(hid, "static void xinput_poll(")
    check("hos_stick_range(" not in block(hid, "static void pads_read(") and pads.count("hos_stick_range(") == 1 and
          pads.index("hos_filter_stick(thumb[2], thumb[3], zone, v + 2)") <
          pads.index("hos_stick_range(rx_option(HOS_OPT_STICK_RANGE, 100), v)"),
          "XInput must scale both sticks once with the menu's range, after the dead zone")
    poll = block(sdl, "static void pads_poll")
    check(poll.count("hos_stick_range(") == 1 and
          poll.rindex("hos_filter_stick(") < poll.index("hos_stick_range(rx_option(HOS_OPT_STICK_RANGE, 100), v)"),
          "SDL must scale both sticks once with the menu's range, after the dead zone")
    option = replay[replay.index("t.option = "):]
    option = option[:option.index("};") + 2]
    check("if (id == HOS_OPT_STICK_RANGE) return opts.stick_range;" in option, "the host cannot see the menu's row")
    check("int stick_range = 100;" in header, "the option's default is not 100 (the stick as the pad reports it)")
    row = re.search(r'\.row\(\{\.key = "stick_range",.*?\}, menu::before\("@Controls"\)', menu, re.S)
    check(row is not None, "the Controls tab has no Stick range row (gpu/vk/menu/controller.cpp)")
    check(all(f in row.group(0) for f in ('.label = "Stick range"', ".value = &opts.stick_range", ".lo = 70, .hi = 100",
                                          ".step = 5", ".text = menu_text::percent")),
          "the Stick range row is not 70% to 100% in steps of 5")
    check(".preset_exempt = true" in row.group(0),
          "the row is not preset_exempt: changing the range would set the quality preset to Custom")
    check("| | Stick range | 70% to 100%, in steps of 5 | 100% |" in settings and "**Stick range:**" in settings,
          "docs/user/graphics-and-controls.md has no Stick range row and note")

    c = r'''#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef long LONG;
#define InterlockedExchange(p, v) (*(p) = (v))
static volatile LONG pad_left_raw[2];
''' + block(hid, "void hos_stick_range(") + r'''
static void expect(int percent, int32_t lx, int32_t ly, int32_t rx, int32_t ry, int32_t wlx, int32_t wly, int32_t wrx, int32_t wry) {
    int32_t v[4] = {lx, ly, rx, ry};
    const LONG before[2] = {pad_left_raw[0], pad_left_raw[1]};
    hos_stick_range(percent, v);
    if (v[0] != wlx || v[1] != wly || v[2] != wrx || v[3] != wry) {
        fprintf(stderr, "%d%%: (%ld, %ld, %ld, %ld) became (%ld, %ld, %ld, %ld), wanted (%ld, %ld, %ld, %ld)\n", percent,
                (long)lx, (long)ly, (long)rx, (long)ry, (long)v[0], (long)v[1], (long)v[2], (long)v[3],
                (long)wlx, (long)wly, (long)wrx, (long)wry);
        exit(1);
    }
    /* (the left stick as the pad gave it, for HOS_STICK_LOG: kept when it moved, left as it was by one at rest) */
    const LONG want[2] = {lx || ly ? lx : before[0], lx || ly ? ly : before[1]};
    if (pad_left_raw[0] != want[0] || pad_left_raw[1] != want[1]) {
        fprintf(stderr, "%d%%: the left stick as the pad gave it was not kept\n", percent);
        exit(1);
    }
}

static int64_t iabs64(int64_t v) { return v < 0 ? -v : v; }

int main(void) {
    /* 100%, and anything that is no percent of the row's: the sticks as the pad reports them */
    const int same[] = {100, 101, 69, 0, -5, 1000};
    for (int k = 0; k < 6; k++) {
        expect(same[k], 0, 31000, -32767, 12, 0, 31000, -32767, 12);
        expect(same[k], 23170, -23170, 0, 0, 23170, -23170, 0, 0);
    }
    /* On an axis: the whole way from that share of it on, in proportion below it; each stick by itself */
    expect(90, 0, 29491, 0, 0, 0, 32767, 0, 0);
    expect(90, 0, 31000, 0, 0, 0, 32767, 0, 0);      /* (a Switch Pro Controller's ordinary full push, #88) */
    expect(90, 0, 32767, 0, 0, 0, 32767, 0, 0);
    expect(90, 0, -30400, 0, 0, 0, -32767, 0, 0);
    expect(90, 9000, 0, 0, -18000, 10000, 0, 0, -20000);
    expect(80, 0, 0, 16000, 0, 0, 0, 20000, 0);
    expect(70, -22937, 0, 0, 0, -32767, 0, 0, 0);
    expect(90, 0, 0, 0, 0, 0, 0, 0, 0);
    /* Two pads polled in turn, the second at rest: the log keeps the moving pad's left stick, not 0, 0 */
    expect(90, 1200, 31000, 0, 0, 1268, 32767, 0, 0);
    expect(90, 0, 0, 0, 0, 0, 0, 0, 0);
    if (pad_left_raw[0] != 1200 || pad_left_raw[1] != 31000) {
        fprintf(stderr, "a second pad at rest overwrote the first pad's left stick for the log\n");
        return 1;
    }
    /* A stick past an axis's end comes back along its own direction */
    expect(90, 2000, 31000, 0, 0, 2114, 32767, 0, 0);
    expect(90, -32767, 32767, 0, 0, -32767, 32767, 0, 0);   /* (a square gate's corner stays the corner) */
    expect(90, 23170, -23170, 0, 0, 25744, -25744, 0, 0);   /* (a round gate's diagonal grows as an axis does) */
    /* Everywhere: never less than the pad gave, never past an axis's end, the same angle, the same on every side */
    for (int percent = 70; percent < 100; percent += 5)
        for (int32_t x = -32767; x <= 32767; x += 331)
            for (int32_t y = -32767; y <= 32767; y += 337) {
                int32_t v[4] = {x, y, -y, x}, m[4] = {-x, -y, y, -x};
                hos_stick_range(percent, v);
                hos_stick_range(percent, m);
                for (int k = 0; k < 4; k++) {
                    const int32_t in = k == 0 ? x : k == 1 ? y : k == 2 ? -y : x;
                    if (v[k] > 32767 || v[k] < -32767 || iabs64(v[k]) < iabs64(in) || (int64_t)v[k] * in < 0 || m[k] != -v[k]) {
                        fprintf(stderr, "%d%%: (%ld, %ld) gave %ld on axis %d\n", percent, (long)x, (long)y, (long)v[k], k);
                        exit(1);
                    }
                }
                /* (the angle: the cross product of in and out is no more than the rounding of two integer divisions) */
                if (iabs64((int64_t)x * v[1] - (int64_t)y * v[0]) > 2 * (iabs64(x) + iabs64(y)) + 2 ||
                    v[2] != -v[1] || v[3] != v[0]) {
                    fprintf(stderr, "%d%%: (%ld, %ld) turned to (%ld, %ld)\n", percent, (long)x, (long)y, (long)v[0], (long)v[1]);
                    exit(1);
                }
            }
    puts("stick: the range scales each stick along its direction, to an axis's end and no further; 100% changes nothing");
    return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix="stick-range-") as tmp:
        path = Path(tmp) / "range.c"
        path.write_text(c, encoding="utf-8")
        exe = Path(tmp) / "range.exe"
        subprocess.run([a.cc, "-std=c11", "-O1", "-Wall", "-Werror", "-o", str(exe), str(path)], check=True)
        r = subprocess.run([str(exe)], capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        check(r.returncode == 0, "range.c: " + r.stderr.strip())
    print("ok")


if __name__ == "__main__":
    main()
