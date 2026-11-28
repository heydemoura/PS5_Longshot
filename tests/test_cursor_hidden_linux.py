"""On Linux the cursor over the game's window hides once the pads or the keys are used and shows again when the mouse is
used, as on Windows (host/display.c gm_cursor_shown, tests/test_cursor_hidden_in_play.py): before, the SDL window
(host/posix/display_sdl.c) hid it only while the mouse was captured, so a player on a controller had it in the middle of
the picture (#892). While it is hidden a move of up to 4 units does not bring it back, nor does the first move after the
window changed size. The Insert menu shows it, and so does losing the focus.

The cursor's functions are cut from display_sdl.c and run with authored stand-ins for the clock, the pads' time and SDL;
the window loop's lines that feed them, and hos_sdl.c's SDL3 and SDL2 calls, are read as text.

python -I -B tests/test_cursor_hidden_linux.py [--cc gcc]
"""
import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (tests/srccut.py: python -I leaves this folder off the path)
from srccut import between, block   # noqa: E402

RUN_ALL_ARGS = ["--cc", "{cc}"]

ROOT = Path(__file__).resolve().parents[1]
DISPLAY = (ROOT / "host" / "posix" / "display_sdl.c").read_text(encoding="utf-8")
SDL = (ROOT / "host" / "hos_sdl.c").read_text(encoding="utf-8")
CC = os.environ.get("CC", "cc")

FIXTURE = r'''
#include <math.h>
#include <stdint.h>
#include <stdio.h>
static int64_t now_ns = 1000, input_ns;
int64_t hos_now_ns(void) { return ++now_ns; }
static int menu_open;
static volatile int hos_posix_window[4] = {1};
static int calls, shown_now = 1;
static void hos_sdl_cursor(int shown) { calls++, shown_now = shown; }
static void hos_sdl_mouse_position(float *x, float *y) { *x = 500; *y = 400; }
@CURSOR@
int64_t hos_player_input_ns(void) { return input_ns; }
static void pad(void) { input_ns = ++now_ns; }
static int shown(void) { cursor_apply(); return shown_now; }
#define CHECK(x) do { if (!(x)) { printf("FAIL line %d: %s\n", __LINE__, #x); return 1; } } while (0)
int main(void) {
    CHECK(shown() && calls == 0);                   /* the window opens with its cursor shown, and SDL is not asked */
    gm_mouse_init();                                /* the current position, before any motion event */
    pad(); CHECK(!shown() && calls == 1);           /* the first pad or key input hides it */
    CHECK(!shown() && calls == 1);                  /* once: SDL is asked again only for a change */
    gm_mouse_moved(505, 400); CHECK(shown() && calls == 2);   /* the first motion shows it */
    gm_mouse_moved(500, 400); CHECK(shown());
    pad(); CHECK(!shown() && calls == 3);
    gm_mouse_moved(500, 400); CHECK(!shown());      /* a repeat at rest */
    gm_mouse_moved(503.5f, 396); CHECK(!shown());   /* within 4 units of where it was hidden */
    gm_mouse_moved(496, 404); CHECK(!shown());      /* (from that point, not from the last move) */
    gm_mouse_moved(504.5f, 400); CHECK(shown() && calls == 4);   /* past them: the mouse used */
    gm_mouse_moved(504.75f, 400); CHECK(shown());   /* shown: any other point counts, a fraction too (SDL3), */
    pad(); CHECK(!shown());
    gm_mouse_moved(507, 400); CHECK(!shown());      /* and it is the point measured from next (2.25 from it, 7 from the first) */
    gm_mouse_moved(504.75f, 395); CHECK(shown());   /* either axis, from the last point a move counted at */
    pad(); CHECK(!shown());
    gm_rebase = 1; gm_mouse_moved(900, 700); CHECK(!shown());   /* the window changed size under a cursor at rest (F11) */
    gm_mouse_moved(903, 697); CHECK(!shown());      /* the new point is the one measured from */
    gm_mouse_moved(905, 700); CHECK(shown());
    pad(); CHECK(!shown());
    gm_mouse_used(); CHECK(shown());                /* a button pressed */
    pad(); CHECK(!shown());
    menu_open = 1; CHECK(shown());                  /* the Insert menu shows it, for its clicks */
    menu_open = 0; CHECK(!shown());                 /* and closed without the mouse used, it hides again */
    hos_posix_window[0] = 0; CHECK(shown());        /* without the focus it is shown */
    hos_posix_window[0] = 1; CHECK(!shown());
    printf("PASS\n");
    return 0;
}
'''


def case(label):
    return between(DISPLAY, f"case {label}:", "\n                case ", where="host/posix/display_sdl.c")


class CursorHiddenLinuxTests(unittest.TestCase):
    def test_hidden_by_the_pads_and_shown_by_a_real_move(self):
        cursor = between(DISPLAY, "int64_t hos_player_input_ns(void);\nstatic int gm_shown", "\n/* ---- keys:",
                         where="host/posix/display_sdl.c")
        # (the fixture declares the stand-ins first; the cut's own declaration of hos_player_input_ns stays, as in the source)
        with tempfile.TemporaryDirectory(prefix="longshot-cursor-linux-") as temp:
            source, executable = Path(temp) / "fixture.c", Path(temp) / "fixture"
            source.write_text(FIXTURE.replace("@CURSOR@", cursor), encoding="utf-8")
            subprocess.run([CC, "-std=c11", "-O0", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable), "-lm"],
                           check=True)
            run = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PASS", run.stdout)

    def test_the_window_loop_feeds_it(self):
        motion = case("HOS_SDL_EV_MOUSE_MOTION")
        self.assertIn("if (!mouse_captured) ptr_in = 1, gm_mouse_moved(e.x, e.y);", motion)   # (a captured move is the camera's)
        self.assertIn("gm_mouse_used();", case("HOS_SDL_EV_MOUSE_DOWN"))
        self.assertNotIn("gm_mouse", case("HOS_SDL_EV_MOUSE_WHEEL"))   # (the wheel works hidden and does not show it, as on Windows)
        self.assertIn("gm_rebase = 1;", case("HOS_SDL_EV_RESIZED"))
        loop = block(DISPLAY, "void hos_display_loop(void) {", where="host/posix/display_sdl.c")
        self.assertLess(loop.index("gm_mouse_init();"), loop.index("hos_sdl_wait_event(&e, 4)"))
        self.assertIn("keys_update();\n        cursor_apply();\n    }", loop)

    def test_sdl3_and_sdl2_calls(self):
        call = block(SDL, "void hos_sdl_cursor(int shown) {", where="host/hos_sdl.c")
        self.assertIn("void *fn = shown ? f.ShowCursor : f.HideCursor;", call)   # SDL3: two calls, no argument
        self.assertIn("CALL(int (*)(int), ShowCursor)(shown ? 1 : 0)", call)   # SDL2: SDL_ShowCursor(SDL_ENABLE or SDL_DISABLE)
        self.assertIn('*(FARPROC *)&f.ShowCursor = GetProcAddress(m, "SDL_ShowCursor");', SDL)
        self.assertIn('if (three) *(FARPROC *)&f.HideCursor = GetProcAddress(m, "SDL_HideCursor");', SDL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    args, rest = parser.parse_known_args()
    CC = args.cc
    unittest.main(argv=[sys.argv[0]] + rest, verbosity=2)
