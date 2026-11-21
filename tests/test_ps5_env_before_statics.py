"""On the console longshot_env.txt is read before the renderer's file-scope switches.

The renderer is linked into the title there, not loaded later as a library: a switch it reads at file scope
(`const int x = getenv(...)`, RX_VK_SHOT_W among them) is read when the C++ objects are made, before main, where the
file had not been read yet. A value set in the file was ignored. host/hos_main.c reads the file from a constructor of
priority 101, ahead of every constructor without one, and again from main.

Two checks: the source has that constructor and the file's fixed name for it (no game path is known before main);
and a small program of the same shape, built here, sees the file's value at file scope with the constructor and not
without it.

python -I -B tests/test_ps5_env_before_statics.py [--cxx c++]
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_ALL_ARGS = ["--cxx", "{cxx}"]
RUN_ALL_SKIP = {"windows": "the console's start order, modelled with POSIX setenv: checked on Linux"}

MODEL = r"""
#include <cstdio>
#include <cstdlib>
#include <cstring>
static void load() {
    FILE* f = fopen(getenv("T_ENV_FILE"), "r");
    char line[128];
    while (f && fgets(line, sizeof line, f)) {
        line[strcspn(line, "\n")] = 0;
        if (char* eq = strchr(line, '=')) *eq = 0, setenv(line, eq + 1, 1);
    }
    if (f) fclose(f);
}
#ifdef EARLY
__attribute__((constructor(101))) static void load_early() { load(); }
#endif
const int kBlock = getenv("RX_T_BLOCK") ? atoi(getenv("RX_T_BLOCK")) : 32;   // (a file-scope switch, as the renderer's)
int main() {
    load();
    printf("%d\n", kBlock);
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cxx", default="c++")
    args = ap.parse_args()
    failures = []
    host = (ROOT / "host" / "hos_main.c").read_text(encoding="utf-8")
    if not re.search(r"#ifdef __PROSPERO__\n(?:/\*.*?\*/\n)?__attribute__\(\(constructor\(101\)\)\) static void "
                     r"load_longshot_env_early\(void\) \{ load_longshot_env\(\); \}\n#endif", host, re.S):
        failures.append("host/hos_main.c: no constructor reads longshot_env.txt before the C++ objects are made")
    loader = host[host.index("static void load_longshot_env(void) {"):]
    loader = loader[:loader.index("\n}\n")]
    if not re.search(r'#ifdef __PROSPERO__[^\n]*\n\s*snprintf\(path, sizeof path, "/app0/longshot_env.txt"\);\n#else', loader):
        failures.append("host/hos_main.c: the console's file is not named by its fixed path (the game's path is not known "
                        "before main)")
    with tempfile.TemporaryDirectory(prefix="ps5-env-statics-") as tmp:
        tmp = Path(tmp)
        (tmp / "model.cpp").write_text(MODEL, encoding="utf-8")
        (tmp / "env.txt").write_text("RX_T_BLOCK=16\n", encoding="utf-8")
        env = dict(os.environ, T_ENV_FILE=str(tmp / "env.txt"))
        env.pop("RX_T_BLOCK", None)
        for name, flags, want, what in (("early", ["-DEARLY"], "16", "with the constructor the file-scope switch has the file's value"),
                                        ("late", [], "32", "without it the switch keeps its default (the order before)")):
            exe = tmp / name
            subprocess.run([args.cxx, "-std=c++17", *flags, str(tmp / "model.cpp"), "-o", str(exe)], check=True)
            got = subprocess.run([str(exe)], env=env, capture_output=True, text=True, check=True).stdout.strip()
            if got != want:
                failures.append("%s: got %s, not %s" % (what, got, want))
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("PASS: longshot_env.txt is read before the file-scope switches on the console")
    return 0


if __name__ == "__main__":
    sys.exit(main())
