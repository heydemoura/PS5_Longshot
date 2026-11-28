"""A fatal end of the renderer under NVIDIA Streamline (gpu/vk/vk_replay.cpp replay_fatal) keeps its exit code 3, without a
GPU, the game or NVIDIA's SDK.

Left to the DLLs' detach, Streamline's DLSS-G plugin tears its frame generation down with its threads gone: seen in the
game, the FATAL line was followed by an access violation in that teardown, logged as a crash, and a crash's exit code in
place of the fatal end's (vk_dlssg.h dlssg_shutdown is the window's close, which a fatal end cannot wait for). Here
replay_fatal, cut from the real source, runs in a process that loaded a stand-in plugin whose detach ends the process
with the crash's code:
- with Streamline's loader the renderer's (dlssg_loaded), the process ends with 3 and the detach did not run; the
  performance summary's last, which the renderer's atexit writes at the detach, is written before the end instead;
- without it the end is the one before, through the detach, and replay_fatal leaves the summary to it;
- both ways the FATAL line is said and the host's queued log written out first.

python -I -B tests/test_dlssg_fatal_end.py [--cxx g++]
"""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import block  # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}"]
RUN_ALL_SKIP = {"linux": "a Windows process's end through its DLLs' detach: Streamline is Windows only"}

ROOT = Path(__file__).resolve().parents[1]
CRASH = 0xC0000005   # (what the stand-in's detach ends the process with)

PLUGIN = r"""
#include <windows.h>
// (the stand-in for the DLSS-G plugin: its detach at the process's end is the teardown that crashed)
BOOL WINAPI DllMain(HINSTANCE, DWORD why, LPVOID) {
    if (why == DLL_PROCESS_DETACH) TerminateProcess(GetCurrentProcess(), 0xC0000005u);
    return TRUE;
}
"""

FIXTURE = r"""
#include <windows.h>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
static void logf(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    std::vprintf(fmt, ap);
    va_end(ap);
    std::printf("\n");
}
struct Host {
    void (*log_flush)();
};
static void flushed() {
    std::printf("the host's log written out\n");
    std::fflush(stdout);
}
static const Host host_storage{flushed};
static const Host* host = &host_storage;
static bool loaded;
bool dlssg_loaded() { return loaded; }
void perf_report_final(int event) {
    std::printf("the performance summary's last written (%d)\n", event);
    std::fflush(stdout);
}
@@REPLAY_FATAL@@
int main(int argc, char** argv) {
    if (argc < 3 || !LoadLibraryA(argv[1])) return 9;
    loaded = argv[2][0] == '1';
    replay_fatal("a test's");
}
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default="g++")
    a = p.parse_args()
    replay = (ROOT / "gpu" / "vk" / "vk_replay.cpp").read_text(encoding="utf-8")
    fatal = block(replay, "[[noreturn]] void replay_fatal(const char* why) {")   # (the definition, not its declaration)
    with tempfile.TemporaryDirectory(prefix="dlssg-fatal-end-") as tmp:
        tmp = Path(tmp)
        (tmp / "plugin.cpp").write_text(PLUGIN, encoding="utf-8")
        (tmp / "fixture.cpp").write_text(FIXTURE.replace("@@REPLAY_FATAL@@", fatal), encoding="utf-8")
        for command in ([a.cxx, "-std=c++17", "-O1", "-shared", str(tmp / "plugin.cpp"), "-o", str(tmp / "plugin.dll")],
                        [a.cxx, "-std=c++17", "-O1", str(tmp / "fixture.cpp"), "-o", str(tmp / "fixture.exe")]):
            r = subprocess.run(command, capture_output=True, text=True, timeout=180)
            if r.returncode:
                print(r.stdout + r.stderr)
                raise SystemExit("FAIL: the fixture did not build")
        codes, summaries = {}, {}
        for streamline in (True, False):
            r = subprocess.run([str(tmp / "fixture.exe"), str(tmp / "plugin.dll"), "1" if streamline else "0"],
                               capture_output=True, text=True, timeout=60)
            codes[streamline] = r.returncode & 0xFFFFFFFF
            summaries[streamline] = r.stdout.count("the performance summary's last written (-1)")
            said = r.stdout.find("vk: FATAL: a test's")
            if said < 0 or r.stdout.find("the host's log written out") < said or "vk: FATAL: a test's" not in r.stderr:
                print(r.stdout + r.stderr)
                raise SystemExit("FAIL: a fatal end %s Streamline does not say its FATAL line and write the host's log out first"
                                 % ("under" if streamline else "without"))
        if codes[True] != 3:
            raise SystemExit("FAIL: a fatal end under Streamline's loader went through the DLLs' detach: the plugin's teardown "
                             "ended the process with %#x in place of the fatal end's exit code 3" % codes[True])
        if summaries[True] != 1:
            raise SystemExit("FAIL: a fatal end under Streamline's loader skips the DLLs' detach, and with it the renderer's "
                             "atexit: the performance summary's last was written %d times before the end, not once"
                             % summaries[True])
        if summaries[False]:
            raise SystemExit("FAIL: a fatal end without Streamline writes the performance summary's last itself: the DLLs' "
                             "detach it ends through writes it (perf_report.cpp at_exit)")
        if codes[False] != CRASH:
            raise SystemExit("FAIL: a fatal end without Streamline no longer ends through the DLLs' detach (exit code %#x)"
                             % codes[False])
    print("OK: a fatal end under Streamline's loader ends the process with 3 before the DLLs' detach; without it, as before")


if __name__ == "__main__":
    main()
