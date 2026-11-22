"""The PS5 title's trace file (host/ps5/trace_ps5.c, /app0/longshot_trace.txt) and its stall rule (host/ps5/stall_watch.h).

The crash handler is compiled and run here too: a child process installs it as the title does and stores to a bad
address; its line is in the file, with the address, and the process still dies of the signal (the handler runs once), which
is what leaves the console's own crash report.

The rule is compiled and run here: nothing before the first picture; a stall said once, after the time asked and not
before; the pictures' return said once, with how long it lasted; a second stall said again; pictures that keep coming say
nothing. The rest is read in the sources:

- a line is opened, written with O_SYNC and closed, so a crash loses none;
- the crash handler runs once (SA_RESETHAND) and neither exits nor raises: the console's own crash report is still made;
  it calls nothing but its own writers, open, write and close;
- the memory floor and an exit asked for each leave a line; the trace starts with the floor off too;
- HOS_PS5_TRACE=0 turns the file, the handler and the thread off.

python -I -B tests/test_ps5_trace.py [--cc clang]
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
#include <stdio.h>
#include "stall_watch.h"
static int bad;
#define CHECK(x, why) do { if (!(x)) { printf("FAIL: %s\n", why); bad = 1; } } while (0)
int main(void) {
    StallWatch w = {0};
    int64_t lasted = -1;
    /* before the first picture: nothing, however long */
    CHECK(stall_watch_step(&w, 0, 0, 2000, &lasted) == STALL_NONE, "a stall before any picture");
    CHECK(stall_watch_step(&w, 0, 60000, 2000, &lasted) == STALL_NONE, "a stall before any picture, a minute in");
    /* pictures coming: nothing */
    for (int i = 1; i <= 20; i++) CHECK(stall_watch_step(&w, i, 60000 + i * 250, 2000, &lasted) == STALL_NONE, "a stall while pictures come");
    /* the count stops at 20 (at 65000): not said before 2 s, said once at 2 s, not again */
    CHECK(stall_watch_step(&w, 20, 65250, 2000, &lasted) == STALL_NONE, "a stall said after 250 ms");
    CHECK(stall_watch_step(&w, 20, 66750, 2000, &lasted) == STALL_NONE, "a stall said after 1.75 s");
    CHECK(stall_watch_step(&w, 20, 67000, 2000, &lasted) == STALL_BEGAN && lasted == 2000, "no stall said at 2 s");
    CHECK(stall_watch_step(&w, 20, 67250, 2000, &lasted) == STALL_NONE, "a stall said twice");
    CHECK(stall_watch_step(&w, 20, 90000, 2000, &lasted) == STALL_NONE, "a stall said again while it lasts");
    /* the next picture ends it, with the whole time it lasted; said once */
    CHECK(stall_watch_step(&w, 21, 95000, 2000, &lasted) == STALL_ENDED && lasted == 30000, "the pictures' return is not said with the stall's length");
    CHECK(stall_watch_step(&w, 22, 95250, 2000, &lasted) == STALL_NONE, "the return said twice");
    /* a short pause (under the time) says nothing at all, at its start or its end */
    CHECK(stall_watch_step(&w, 22, 96500, 2000, &lasted) == STALL_NONE, "a short pause said");
    CHECK(stall_watch_step(&w, 23, 96750, 2000, &lasted) == STALL_NONE, "the end of a short pause said");
    /* a second stall is said again */
    CHECK(stall_watch_step(&w, 23, 99000, 2000, &lasted) == STALL_BEGAN && lasted == 2250, "a second stall is not said");
    CHECK(stall_watch_step(&w, 24, 99100, 2000, &lasted) == STALL_ENDED && lasted == 2350, "a second stall's end is not said");
    puts(bad ? "FAILED" : "ok");
    return bad;
}
"""


CRASH = r"""
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
/* (the console's machine context has mc_rip; here the handler reads a word of whatever context it is given) */
typedef struct { struct { uint64_t mc_rip; } uc_mcontext; } test_ucontext_t;
#define ucontext_t test_ucontext_t
#define TRACE_PATH "trace.txt"
static int64_t trace_t0_ms;
void hos_ps5_trace(const char *fmt, ...) { (void)fmt; }
FUNCTIONS
int main(void) {
    unlink(TRACE_PATH);
    const pid_t child = fork();
    if (child == 0) {
        struct sigaction sa;
        memset(&sa, 0, sizeof sa);
        sa.sa_sigaction = trace_signal;
        sa.sa_flags = SA_SIGINFO | SA_RESETHAND;   /* (as hos_ps5_trace_start installs it) */
        sigemptyset(&sa.sa_mask);
        sigaction(SIGSEGV, &sa, NULL);
        sigaction(SIGBUS, &sa, NULL);
        *(volatile int *)(uintptr_t)0x10 = 1;      /* (the crash) */
        _exit(0);                                  /* (never: the store faults again after the handler) */
    }
    int status = 0;
    waitpid(child, &status, 0);
    char line[512] = {0};
    const int fd = open(TRACE_PATH, O_RDONLY);
    if (fd >= 0) { (void)!read(fd, line, sizeof line - 1); close(fd); }
    if (!WIFSIGNALED(status)) { printf("FAIL: the process did not die of the signal (status %d): the console would make no crash report\n", status); return 1; }
    const int sig = WTERMSIG(status);
    char want[32];
    snprintf(want, sizeof want, "signal %d ", sig);
    if (strncmp(line, want, strlen(want))) { printf("FAIL: no line for signal %d in the trace: '%s'\n", sig, line); return 1; }
    if (!strstr(line, ": address 0x10, the program at 0x") || !strstr(line, "the console's crash report follows\n")) {
        printf("FAIL: the line does not say the address and where the program was: '%s'\n", line);
        return 1;
    }
    if (strchr(line, '\n') != line + strlen(line) - 1) { printf("FAIL: more than one line for one crash: '%s'\n", line); return 1; }
    puts("ok crash");
    return 0;
}
"""


def block(source, marker):
    start = source.index(marker)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=os.environ.get("CC") or shutil.which("clang") or shutil.which("gcc") or "cc")
    args = parser.parse_args()
    failed = []
    with tempfile.TemporaryDirectory(prefix="ps5-trace-") as tmp:
        folder = Path(tmp)
        (folder / "fixture.c").write_text(HARNESS, encoding="utf-8")
        exe = folder / "fixture.exe"
        subprocess.run([args.cc, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT / "host" / "ps5"), str(folder / "fixture.c"),
                        "-o", str(exe)], check=True, cwd=folder)
        got = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        print(got.stdout.strip())
        if got.returncode or got.stdout.strip() != "ok":
            failed.append("the stall rule")
    trace = (ROOT / "host" / "ps5" / "trace_ps5.c").read_text(encoding="utf-8")
    # (the crash handler itself, run off the console: its line is written, and the process still dies of the signal)
    parts = [block(trace, m) for m in ("static int64_t mono_ms(void) {", "static void trace_put(const char *text, size_t n) {",
                                       "static char *put_text(char *p, const char *s) {", "static char *put_hex(char *p, uint64_t v) {",
                                       "static char *put_dec(char *p, uint64_t v) {",
                                       "static void trace_signal(int sig, siginfo_t *si, void *context) {")]
    with tempfile.TemporaryDirectory(prefix="ps5-trace-crash-") as tmp:
        folder = Path(tmp)
        (folder / "crash.c").write_text(CRASH.replace("FUNCTIONS", "\n".join(parts)), encoding="utf-8")
        exe = folder / "crash.exe"
        subprocess.run([args.cc, "-std=gnu11", "-O0", "-w", str(folder / "crash.c"), "-o", str(exe)], check=True, cwd=folder)
        got = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, cwd=folder)
        print(got.stdout.strip())
        if got.returncode or got.stdout.strip() != "ok crash":
            failed.append("the crash handler run for real")
    win32 = (ROOT / "host" / "ps5" / "win32_ps5.c").read_text(encoding="utf-8")
    vi = (ROOT / "host" / "nn_vi.c").read_text(encoding="utf-8")
    put = block(trace, "static void trace_put(const char *text, size_t n) {")
    handler = block(trace, "static void trace_signal(int sig, siginfo_t *si, void *context) {")
    start = block(trace, "void hos_ps5_trace_start(void) {")
    floor = block(win32, "static void *memory_floor_watch(void *unused) {")
    floor_start = block(win32, "void hos_ps5_memory_floor_start(void) {")
    calls = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", handler)) - {"trace_signal", "sizeof", "if", "while", "for"}
    allowed = {"put_text", "put_hex", "put_dec", "trace_put", "mono_ms"}
    checks = [
        ("O_WRONLY | O_CREAT | O_APPEND | O_SYNC" in put and "close(fd);" in put, "a line is not written synced and closed"),
        (calls <= allowed, "the crash handler calls %s" % ", ".join(sorted(calls - allowed))),
        ("sa.sa_flags = SA_SIGINFO | SA_RESETHAND;" in start, "the crash handler is not installed to run once"),
        (not re.search(r"\b(_exit|exit|abort|raise|kill|hos_ps5_exit)\s*\(", handler), "the crash handler ends the process itself"),
        ("if (!trace_on()) return;" in start and start.index("if (!trace_on()) return;") < start.index("sigaction("),
         "HOS_PS5_TRACE=0 does not keep the handler and the thread away"),
        ("if (!trace_on()) return;" in block(trace, "void hos_ps5_trace(const char *fmt, ...) {"), "HOS_PS5_TRACE=0 still writes lines"),
        ('getenv("HOS_PS5_TRACE")' in trace, "the trace has no switch"),
        ('hos_ps5_trace("memory floor:' in floor and floor.index('hos_ps5_trace("memory floor:') < floor.index("hos_ps5_exit(3);"),
         "the memory floor closes the title with no line in the trace"),
        ('hos_ps5_trace("exit %d' in block(win32, "void hos_ps5_exit(int code) {"), "an exit asked for leaves no line in the trace"),
        (floor_start.index("hos_ps5_trace_start();") < floor_start.index("if (!floor_bytes()) {"), "with the floor off the trace does not start"),
        ("__atomic_store_n(&hos_vi_presents, presents, __ATOMIC_RELAXED);" in block(vi, "static void scan_dt(long long presents) {")
         and "scan_dt(++scan_n);" in block(vi, "void hos_vi_native_presented(const int64_t phases[4]) {"),
         "a present is not counted for the stall watch"),
        ("stall_watch_step(&w, __atomic_load_n(&hos_vi_presents, __ATOMIC_RELAXED), mono_ms(), STALL_MS, &lasted)" in trace,
         "the watch does not use the tested rule"),
    ]
    for ok, why in checks:
        if not ok:
            failed.append(why)
    for why in failed:
        print("FAIL: " + why)
    if failed:
        return 1
    print("PASS: the trace file's stall rule, its synced lines, a crash handler that leaves the console's report, and its switch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
