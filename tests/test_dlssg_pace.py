"""NVIDIA DLSS Frame Generation through NVIDIA Streamline, the parts that need no GPU, no game and none of NVIDIA's SDK:

 - when Streamline is started and when a change of the Frame generation rows takes a restart (gpu/vk/dlssg_rules.h,
   tests/dlssg_rules_test.cpp);
 - when a game frame's present goes to Streamline: the hold after the game's own present, the line through the game's
   presents, where the toggle fits (gpu/vk/dlssg_pace.h, tests/dlssg_pace_test.cpp);
 - the reader of the law NVIDIA's driver meters DLSS-G's presents by, and the bounds of its asks (gpu/vk/dlssg_law.h,
   tests/dlssg_law_test.cpp);
 - the VRR range an HDMI screen gives in its EDID, which the toggle's hold is taken from (gpu/vk/edid_vrr.h,
   tests/edid_vrr_test.cpp).

Each is compiled and run on made-up presents. And the source: the three switches' defaults are dlssg_pace.h's constants,
as gpu/vk/switches.def's rows say them; vk_dlssg.cpp holds a present through these rules alone; the layer feeds the
reader the batches the rule reads, and the game answers every ask.

python -I -B tests/test_dlssg_pace.py [--cxx g++]
"""
import argparse
import re
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"


def block(text, marker):
    """From marker to the brace closing its first one"""
    start = text.index(marker)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def need(cond, why):
    if not cond:
        raise SystemExit("FAIL: " + why)


def check_source():
    pace = (VK / "dlssg_pace.h").read_text(encoding="utf-8")
    rules = (VK / "dlssg_rules.h").read_text(encoding="utf-8")
    dlssg = (VK / "vk_dlssg.cpp").read_text(encoding="utf-8")
    layer = (VK / "vk_dlssg_layer.cpp").read_text(encoding="utf-8")
    rows = {m.group(1): m.group(2) for m in re.finditer(r'^SW\((RX_VK_DLSSG\w*), \w+, \w+, "([^"]*)"',
                                                         (VK / "switches.def").read_text(encoding="utf-8"), re.M)}

    # the three defaults: one constant each, the switch's row saying the same number, the switch read with it
    pace_fn = block(dlssg, "Pace& pace() {")
    for const, switch, field in (("kHoldUs", "RX_VK_DLSSG_HOLD", "hold.hold"), ("kGridUs", "RX_VK_DLSSG_HOLD_GRID", "hold.grid_max"),
                                 ("kToggleUs", "RX_VK_DLSSG_TOGGLE", "toggle")):
        value = re.search(r"constexpr int64_t %s = (\d+);" % const, pace)
        need(value, "dlssg_pace.h: no %s, %s's default" % (const, switch))
        says = rows.get(switch, "")
        need(says.startswith(value.group(1) + " microseconds") if const != "kToggleUs" else "else %s microseconds" % value.group(1) in says,
             "gpu/vk/switches.def: %s's row does not say its default, %s microseconds (dlssg_pace.h %s)" % (switch, value.group(1), const))
        need(re.search(r'n->%s = pace_switch\("%s", dlssg_pace::%s, \d+(?:, &given)?\) \* 1000;' % (re.escape(field), switch, const), pace_fn),
             "vk_dlssg.cpp pace(): %s is not read with dlssg_pace.h's %s as its default" % (switch, const))
    need("int64_t hold = kHoldUs * 1000;" in pace and "int64_t grid_max = kGridUs * 1000;" in pace,
         "dlssg_pace.h: Hold's own defaults are not the switches'")
    # a value that is no number, or out of its range, is the default and not a hold of any length
    sw = block(dlssg, "int64_t pace_switch(const char* name, int64_t def, int64_t most, bool* given = nullptr) {")
    need("v < 0 || v > most" in sw and sw.count("return def;") == 2, "pace_switch: a value out of range is not the default")

    # the toggle's hold from the screen: unset or not valid (its default), at each swapchain made, from the HDMI Forum's block on HDMI alone, 22 ms
    # where not read; the row says the rule's numbers
    made = block(dlssg, "void dlssg_swapchain_made() {")
    need("if (!p.from_screen) return;" in made and 'n->from_screen = !given;' in pace_fn and "if (given) *given = true;" in sw,
         "vk_dlssg.cpp: the toggle's hold is taken from the screen with RX_VK_DLSSG_TOGGLE set")
    need("} else if (!hdmi) {" in made and "edid_vrr::hdmi_forum(edid.data(), edid.size())" in made and
         "dlssg_pace::toggle_for_floor(v.min_hz)" in made and "if (!us) us = dlssg_pace::kToggleUs;" in made,
         "dlssg_swapchain_made: not the HDMI Forum's floor on HDMI alone, through toggle_for_floor, 22 ms otherwise")
    main_cpp = (VK / "vk_main.cpp").read_text(encoding="utf-8")
    swap = block(main_cpp, "bool create_swapchain(uint32_t width, uint32_t height) {")
    need(swap.index("dlssg_swapchain_made();") > swap.index("dev.swapchain = sc;"), "create_swapchain: the screen's floor is not read once the swapchain is made")
    over = re.search(r"constexpr int64_t kToggleOverFloorUs = kToggleUs - 1000000 / kFloorMeasuredHz;", pace)
    low_high = re.search(r"constexpr int kFloorLowHz = (\d+), kFloorHighHz = (\d+);", pace)
    need(over and low_high and "floors of %s to %s Hz" % low_high.groups() in rows.get("RX_VK_DLSSG_TOGGLE", "") and
         "+ %d microseconds" % (22000 - 1000000 // 48) in rows.get("RX_VK_DLSSG_TOGGLE", ""),
         "gpu/vk/switches.def: RX_VK_DLSSG_TOGGLE's row does not say dlssg_pace.h's rule from the screen's floor")

    # the Upscaler row's values the start reads: the game's own and Off are the two that are not temporal, DLSS and DLAA the
    # two Streamline starts with; run_switches gives a switch's Upscaler by the same values
    up = (VK / "upscaler_rows.h").read_text(encoding="utf-8")
    need("enum { GAME = 0, DLSS = 1, DLAA = 2, FSR3 = 3, FSR4 = 4, FSR4_AA = 5, OFF = 6, XESS = 7, XESS_AA = 8," in up,
         "upscaler_rows.h: the Upscaler row's values are no longer those dlssg_rules.h and vk_dlssg.cpp's run_switches use")
    need("upscaler(s) != 0 && upscaler(s) != 6" in rules, "dlssg_rules.h temporal(): not every Upscaler but the game's own (0) and Off (6)")
    need("constexpr int kStreamlineUpscalers[] = {1, 2};" in rules, "dlssg_rules.h: Streamline starts with another Upscaler than DLSS and DLAA")
    need('s.upscaler_switch = env_switch("RX_VK_FSR") > 0 ? 3 : dlss > 0 ? (dlss >= 2 ? 2 : 1) : fsr4 > 0 ? (fsr4 >= 2 ? 5 : 4)' in
         block(dlssg, "void run_switches(dlssg_rules::Start& s) {"),
         "vk_dlssg.cpp run_switches: a switch's Upscaler not in vk_temporal.cpp temporal_mode's order (RX_VK_FSR=1, then RX_VK_DLSS, RX_VK_FSR4)")
    # the rows as saved are read under the keys the menu saves them by
    saved = block(dlssg, "void saved_rows(dlssg_rules::Start& s) {")
    keys = (ROOT / "tests" / "test_menu_ini_keys.py").read_text(encoding="utf-8")
    for key in ("frame_generation", "frame_generation_by", "dlss"):
        need('"%s="' % key in saved, "vk_dlssg.cpp saved_rows: %s= is not read" % key)
        need(re.search(r'"%s"' % key, keys), "tests/test_menu_ini_keys.py: no %s key, which vk_dlssg.cpp's saved_rows reads" % key)

    # the hold: only for a game frame while DLSS-G makes frames; the toggle only where it fits and a second after the last,
    # and every ask taken is answered, held or let go
    hold = block(dlssg, "void dlssg_hold(int64_t game_at, int64_t game_wait) {")
    need("if (!runs(s) || s.mode != sl::DLSSGMode::eOn) return;" in hold, "dlssg_hold: a present is held while DLSS-G makes no frames")
    need("p.hold.toggle_fits(frames) && (!p.toggled || now - p.toggled >= 1000000000)" in hold,
         "dlssg_hold: the toggle's hold is not kept to where it fits, a second apart")
    need(hold.count("dlssg_layer_held(") == 2 and "dlssg_layer_held(call, call, false);" in hold,
         "dlssg_hold: an ask taken from the layer is not answered both ways")
    need(hold.index("dlssg_layer_toggle_wanted()") < hold.index("p.hold.target(game_at, game_wait, hold_now())"),
         "dlssg_hold: the toggle's hold does not come before the frame's own")
    need("std::lock_guard" in hold and hold.index("hold_until(") > hold.index("Pace& p = pace();"),
         "dlssg_hold: it waits with Streamline's state locked")

    # the layer: the reader takes the batches of the size its rule reads, with the toggle as the switch has it, and its ask
    # is given once, while it is fresh
    need("cfg->numFramesPerBatch == dlssg_law::kBatch" in block(layer, "void law_present("), "the layer feeds the reader batches of any size")
    need("l.reader.toggle = dlssg_toggles()" in block(layer, "void law_swapchain("), "the layer's reader asks whatever RX_VK_DLSSG_TOGGLE says")
    # a swapchain whose time domain changes: the reader's calibration is the old domain's, so it starts again, is calibrated
    # at once, and takes no time that is not in the domain asked now (but one in QPC, which needs no calibration)
    back = block(layer, "void read_back(")
    need("void law_domain(" in layer and "l.reader.reset();" in block(layer, "void law_domain("),
         "the layer has nothing that starts the reader again when its swapchain's time domain changes")
    need(re.search(r"read_domains\(d, h, s\);\s*if \(s\.domain != was \|\| s\.domain_id != was_id\) law_domain\(h\), s\.last_calibration = 0;", back),
         "read_back: a change of the time domain keeps the reader's calibration, the old domain's")
    need(re.search(r"if \(in_qpc \|\| \(g\.timeDomain == s\.domain && g\.timeDomainId == s\.domain_id\)\)\s*law_time\(", back),
         "read_back: the reader is given a time of another domain than the one it is calibrated for")
    wanted = block(layer, "bool dlssg_layer_toggle_wanted() {")
    need("g_want.exchange(0)" in wanted and "l.reader.ask_dropped();" in wanted, "dlssg_layer_toggle_wanted: an ask is given twice, or an old one counted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="g++")
    args = parser.parse_args()
    check_source()
    with tempfile.TemporaryDirectory(prefix="dlssg-pace-") as tmp:
        for name in ("dlssg_rules", "dlssg_pace", "dlssg_law", "edid_vrr"):
            exe = Path(tmp) / name
            subprocess.run([args.cxx, "-std=c++20", "-O1", "-Wall", "-Wextra", "-Werror", str(ROOT / "tests" / (name + "_test.cpp")),
                            "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True, timeout=120)


if __name__ == "__main__":
    main()
