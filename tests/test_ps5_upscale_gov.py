"""The console's upscaler by the GPU's load (gpu/vk/upscale_gov.h): the Upscaler row's Auto and the Resolution mode's Auto.

FSR 4 costs about 4 ms of GPU time a frame on the console whatever the 3D's size, so in a heavy place the engine gives way
(FSR 4, FSR 3.1, the plain stretch) by the frame rate, and comes back, slowly, when it is high again.

python -I -B tests/test_ps5_upscale_gov.py [--cxx g++]
"""
import argparse
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r"""#include "upscale_gov.h"
#include <cstdio>
using namespace mkwvk::upscale_gov;
static int fails;
#define CHECK(what, ok) do { if (!(ok)) { std::printf("FAIL: %s\n", what); fails++; } } while (0)
int main() {
    const unsigned all = ur::bit(ur::GAME) | ur::bit(ur::OFF) | ur::bit(ur::FSR3) | ur::bit(ur::FSR4) | ur::bit(ur::FSR4_AA);
    const unsigned no4 = ur::bit(ur::GAME) | ur::bit(ur::OFF) | ur::bit(ur::FSR3);
    Rung l[kMaxRungs];
    // the ladders
    int n = ladder(true, true, ur::GAME, 0, all, l);
    CHECK("both Auto: FSR 4 Q B P, FSR 3.1 Q B P UP, the stretch", n == 8 && l[0].up == ur::FSR4 && l[0].res == 3 &&
          l[1].res == 5 && l[2].res == 4 && l[3].up == ur::FSR3 && l[3].res == 3 && l[5].res == 4 && l[6].up == ur::FSR3 &&
          l[6].res == 6 && l[7].up == ur::OFF && l[7].res == 4);
    n = ladder(true, true, ur::FSR4, 3, no4, l);
    CHECK("no FSR 4 here: the ladder starts at FSR 3.1", n == 5 && l[0].up == ur::FSR3 && l[4].up == ur::OFF);
    n = ladder(true, false, ur::FSR4, 3, all, l);
    CHECK("the Upscaler alone at Auto: the mode chosen under each engine", n == 3 && l[0].up == ur::FSR4 && l[1].up == ur::FSR3 &&
          l[2].up == ur::OFF && l[0].res == 3 && l[1].res == 3 && l[2].res == 3);
    n = ladder(true, false, ur::FSR4, 0, all, l);
    CHECK("with the game's dynamic size, kept", n == 3 && l[0].res == 0 && l[2].res == 0);
    n = ladder(false, true, ur::FSR4, 9, all, l);
    CHECK("the mode alone at Auto: Quality to Performance under the engine chosen", n == 3 && l[0].up == ur::FSR4 &&
          l[2].up == ur::FSR4 && l[0].res == 3 && l[1].res == 5 && l[2].res == 4);
    n = ladder(false, true, ur::FSR4_AA, 9, all, l);
    CHECK("from FSR 4's native AA: FSR 4's modes", n == 3 && l[0].up == ur::FSR4);
    n = ladder(false, true, ur::GAME, 2, all, l);
    CHECK("the game's own has no 58 %: two rungs", n == 2 && l[0].res == 3 && l[1].res == 4);
    n = ladder(false, false, ur::FSR3, 5, all, l);
    CHECK("neither: what is chosen, one rung", n == 1 && l[0].up == ur::FSR3 && l[0].res == 5);
    n = ladder(true, true, ur::GAME, 0, all, l);
    CHECK("rung_of", rung_of(l, n, ur::FSR3, 5) == 4 && rung_of(l, n, ur::FSR4_AA, 3) == 0 && rung_of(l, n, ur::GAME, 2) == -1);

    // the governor: 8 rungs, the line at 40 fps (up from 50 while what a step bought is not known)
    const auto W = [](float fps, float draws = 900, bool room = true, int skip = -1, float need = kGain) {
        In in;
        in.fps = fps, in.draws = draws, in.room_ok = room, in.skip_to = skip, in.need = need;
        return in;
    };
    Gov g;
    g = step(g, 8, W(35));
    CHECK("one window under the line: nothing yet", g.rung == 0 && g.low == 1);
    g = step(g, 8, W(35));
    CHECK("two: a rung down, the frame rate kept to judge it", g.rung == 1 && g.hold == kHold && g.before == 35.0f);
    g = step(g, 8, W(20));
    CHECK("the window after a step is left out", g.rung == 1 && g.before == 35.0f);
    g = step(g, 8, W(28, 2600));
    CHECK("another scene (2,600 draws for 900): no verdict yet, and no step", g.rung == 1 && g.before == 35.0f && g.judging == 1);
    g = step(g, 8, W(38, 950));
    CHECK("the same scene again: it bought 8 %, kept, and remembered", g.rung == 1 && g.before == 0 && !g.blocked && g.gain[1] > 1.08f && g.gain[1] < 1.09f);
    g = step(g, 8, W(38));
    CHECK("still under the line: another", g.rung == 2);
    g = step(g, 8, W(38));
    g = step(g, 8, W(38.5f));
    CHECK("that one bought nothing: given back, and no step down for a while", g.rung == 1 && g.blocked == kBlocked);
    for (int i = 0; i < 10; i++) g = step(g, 8, W(38));
    CHECK("the CPU holds the frame rate: it stays", g.rung == 1);
    for (int i = 0; i < kBlocked; i++) g = step(g, 8, W(44));
    CHECK("44 fps, when the rung bought 8.6 %: the one above would be at 40.5, not 5 % over the line", g.rung == 1 && g.high == 0);
    for (int i = 0; i < 3; i++) g = step(g, 8, W(46.5f));
    CHECK("46.5: over, three windows: not yet", g.rung == 1 && g.high == 3);
    g = step(g, 8, W(46.5f));
    CHECK("four: a rung up", g.rung == 0 && g.since_up == 0);
    g = step(g, 8, W(36));   // (held out)
    for (int i = 0; i < 2; i++) g = step(g, 8, W(36));
    CHECK("it did not hold: down again, and the next step up waits twice as long", g.rung == 1 && g.wait == 8);
    g = step(g, 8, W(55));   // (held out)
    g = step(g, 8, W(55));   // (the verdict: bought)
    for (int i = 0; i < 7; i++) g = step(g, 8, W(70));
    CHECK("seven windows over: not yet", g.rung == 1);
    g = step(g, 8, W(70));
    CHECK("eight: up", g.rung == 0 && g.wait == 8);
    for (int i = 0; i < kHeld; i++) g = step(g, 8, W(45));
    CHECK("it held: the wait is four again", g.rung == 0 && g.wait == kUp);
    Gov q;
    q = step(q, 8, W(35));
    q = step(q, 8, W(35));
    for (int i = 0; i < kJudge + 1; i++) q = step(q, 8, W(30, 2600));   // (one held out, then kJudge of another scene)
    CHECK("never the same scene again: the step is given back, with nothing remembered", q.rung == 0 && q.before == 0 && q.blocked == kJudge && q.gain[1] == 0);
    Gov m;
    m.rung = 3;
    for (int i = 0; i < 40; i++) m = step(m, 8, W(58, 900, false));
    CHECK("memory short: no step up", m.rung == 3);
    for (int i = 0; i < 3; i++) m = step(m, 8, W(49));
    CHECK("what the rung bought not known: up only from 52.5 fps", m.rung == 3 && m.high == 0);
    Gov b;
    b.rung = 7;
    for (int i = 0; i < 10; i++) b = step(b, 8, W(20));
    CHECK("the last rung: no further", b.rung == 7);
    Gov u;
    float f = 20.0f;
    for (int i = 0; i < 60; i++) {
        const int was = u.rung;
        u = step(u, 8, W(f));
        if (u.rung > was) f *= 1.06f;
    }
    CHECK("each step buying 6 %, still under the line: down to the last rung", u.rung == 7);
    Gov st;
    st.rung = 6;
    st = step(st, 8, W(36));
    st = step(st, 8, W(36));
    st = step(st, 8, W(36));   // (held out)
    st = step(st, 8, W(38.5f, 900, true, -1, kGainStretch));
    CHECK("the plain stretch for 7 %: not worth its picture, given back", st.rung == 6 && st.blocked == kBlocked);
    Gov c;
    c.rung = 2;
    In capped = W(29.5f);
    capped.cap_fps = 30;
    for (int i = 0; i < 6; i++) c = step(c, 8, capped);
    CHECK("a cap of 30: up from 97 % of it", c.rung == 1);
    capped.fps = 27;
    for (int i = 0; i < 6; i++) c = step(c, 8, capped);
    CHECK("and 27 of 30 is not under its line (85 %)", c.rung <= 1 && c.low == 0);
    Gov o;
    o.rung = 5;
    o = step(o, 3, W(45));
    CHECK("a shorter ladder: the rung is cut to it", o.rung == 2);
    o = step(o, 1, W(20));
    CHECK("one rung: nothing to do", o.rung == 0);
    Gov z;
    z = step(z, 8, W(0));
    CHECK("no frame rate measured: nothing", z.rung == 0 && z.low == 0);

    // a smaller 3D that buys nothing: the next engine is tried before the step is given back
    n = ladder(true, true, ur::GAME, 0, all, l);
    CHECK("next_engine", next_engine(l, n, 1) == 3 && next_engine(l, n, 2) == 3 && next_engine(l, n, 0) == -1 &&
          next_engine(l, n, 3) == -1 && next_engine(l, n, 4) == 7 && next_engine(l, n, 7) == -1);
    Gov s;
    for (int i = 0; i < 3; i++) s = step(s, 8, W(30, 900, true, next_engine(l, n, s.rung)));
    s = step(s, 8, W(30.2f, 900, true, next_engine(l, n, s.rung)));
    CHECK("FSR 4 at Balanced bought nothing: FSR 3.1 at Quality, to beat the same frame rate", s.rung == 3 && s.back == 0 &&
          s.before == 30.0f && !s.blocked);
    Gov s2 = s;
    s = step(s, 8, W(36, 900, true, next_engine(l, n, s.rung)));   // (held out)
    s = step(s, 8, W(36, 900, true, next_engine(l, n, s.rung)));
    CHECK("it bought 6 fps: kept", s.rung == 3 && s.back == -1 && s.before == 0 && !s.blocked && s.gain[3] > 1.19f);
    for (int i = 0; i < 6; i++) s = step(s, 8, W(49));
    CHECK("49 fps would be 41 with FSR 4 back: not 5 % over the line, it stays", s.rung == 3);
    for (int i = 0; i < 6; i++) s = step(s, 8, W(52));
    CHECK("52 would be 43: up", s.rung == 2);
    s2 = step(s2, 8, W(30, 900, true, next_engine(l, n, s2.rung)));   // (held out)
    s2 = step(s2, 8, W(30.1f, 900, true, next_engine(l, n, s2.rung)));
    CHECK("nor did the engine: back to where it was, and no step down for a while", s2.rung == 0 && s2.back == -1 &&
          s2.blocked == kBlocked);

    // the Upscaler row with Auto as a stop
    int a = 0;
    CHECK("Right from the game's own: FSR 3.1", step_engine_auto(ur::GAME, &a, 1, all) == ur::FSR3 && !a);
    CHECK("Right from Off, the last: Auto, the value kept", step_engine_auto(ur::OFF, &a, 1, all) == ur::OFF && a == 1);
    CHECK("Right from Auto: the first engine", step_engine_auto(ur::OFF, &a, 1, all) == ur::GAME && !a);
    CHECK("Left from the game's own: Auto", step_engine_auto(ur::GAME, &a, -1, all) == ur::GAME && a == 1);
    CHECK("Left from Auto: the last engine", step_engine_auto(ur::FSR4, &a, -1, all) == ur::OFF && !a);
    CHECK("Left from FSR 4: FSR 3.1", step_engine_auto(ur::FSR4, &a, -1, all) == ur::FSR3 && !a);
    if (!fails) std::puts("upscale governor: ok");
    return fails ? 1 : 0;
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cxx", default="g++")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "t.cpp"
        src.write_text(FIXTURE)
        exe = Path(tmp) / "t"
        subprocess.run([args.cxx, "-std=c++20", "-Wall", "-Werror", "-I", str(ROOT / "gpu" / "vk"), str(src), "-o", str(exe)],
                       check=True)
        out = subprocess.run([str(exe)], capture_output=True, text=True)
        print(out.stdout, end="")
        if out.returncode:
            raise SystemExit("upscale governor: failed")


if __name__ == "__main__":
    main()
