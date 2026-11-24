"""Player 0's DualSense microphone on the console (host/ps5/ps5_mic.h, through host/ps5/hos_sdl_ps5.c), run on the PC with
stand-ins for sceAudioIn (a paced port that gives a counted ramp, as PS5_RetroArch's tests/microphone_ps5_test.cpp does:
Mihawk, GPL-3.0-or-later), the user, the controller and the log; and the sources read for what a run cannot show. No game, GPU
or console. From Grapnel (github.com/morpheus302/PS5_Grapnel, tests/test_ps5_microphone.py):

- HOS_PS5_MIC off: no device listed, every open refused, sceAudioIn never called;
- on: the service started once, the port made at the start (general type first, then voice chat, 48000 Hz, then 16000) and
  parked: nothing read before the game listens; one device listed, "DualSense microphone", while player 0 is there;
- what the game reads is what the port gave, in order, never blocking; the ring keeps the newest second, the oldest dropped;
- a silent state but 0 (1 no microphone, 2 another application, 4 muted, 8 no such format) gives silence, and what was queued
  before it goes; sound comes back one silent grain after the state is 0 again;
- the formats nn_audio_in.c may ask: S8, S16, S32, float, channels copied; 16000 Hz ports resampled to 48000 (three frames a
  sample), 44100 from 48000;
- the last reader closed: the capture pauses, the port stays open (no new sceAudioInOpen at the next open); the grains the port
  held while parked dropped at the next capture; an input that never blocks is paced, not spun;
- an input that fails: the port closed, made again at the next open; 300 opens and closes: one thread, no leak (the address
  sanitizer's leak check when the compiler has it);
- the close of the title: the port closed, every open refused after;
- by reading: no file or network call in the microphone's code (the voice never leaves the console), the switch read once with
  env_on, the port prepared after the controllers, the link's stub of libSceAudioIn and its five calls in ps5/tools/link.sh
  and the builder's kit and link, the title's exit and restart end the microphone before the shell's LoadExec, and the docs
  name the switch.

    python -I -B tests/test_ps5_microphone.py [--cc gcc]
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import block   # noqa: E402

RUN_ALL_ARGS = ["--cc", "gcc"]
RUN_ALL_SKIP = {"windows": "the console's microphone code runs on pthreads: checked on Linux"}

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#define _GNU_SOURCE
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "ps5_mic.h"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); exit(1); } } while (0)

/* ---- the console's stand-ins ---- */
static int init_calls, open_calls, close_calls, thread_calls, last_type, refuse_type1_48k, refuse_48k;
static int32_t last_user;
static unsigned last_rate, last_grain, last_param;
static int input_calls, silent_state, fail_input, instant_left, constant;   /* (atomics: the capture thread reads them) */
static int player_there = 1, no_user;
static int ramp;   /* (the capture thread's own: 1..20000, again and again) */
static int f_init(void) { init_calls++; return 0; }
static int f_open(int32_t user, int32_t type, int32_t index, uint32_t grain, uint32_t rate, uint32_t param) {
    open_calls++, last_user = user, last_type = type, last_grain = grain, last_rate = rate, last_param = param;
    CHECK(index == 0);
    if (rate == 48000 && (refuse_48k || (refuse_type1_48k && type == 1))) return (int)0x8026010b;
    return 7;
}
static void sleep_ns(long ns) { const struct timespec t = {ns / 1000000000L, ns % 1000000000L}; nanosleep(&t, NULL); }
static int f_input(int32_t port, void *pcm) {
    CHECK(port == 7);
    if (__atomic_load_n(&instant_left, __ATOMIC_ACQUIRE) > 0) __atomic_sub_fetch(&instant_left, 1, __ATOMIC_ACQ_REL);
    else sleep_ns(2000000);   /* (a grain: 5.3 ms on the console, 2 here) */
    __atomic_add_fetch(&input_calls, 1, __ATOMIC_ACQ_REL);
    if (__atomic_load_n(&fail_input, __ATOMIC_ACQUIRE)) return (int)0x80260105;
    int16_t *s = (int16_t *)pcm;
    const int c = __atomic_load_n(&constant, __ATOMIC_ACQUIRE);
    for (int i = 0; i < HOS_PS5_MIC_GRAIN; i++) {
        ramp = ramp % 20000 + 1;
        s[i] = (int16_t)(c ? c : ramp);
    }
    return 0;
}
static int f_silent(int32_t port) { (void)port; return __atomic_load_n(&silent_state, __ATOMIC_ACQUIRE); }
static int f_close(int32_t port) { CHECK(port == 7); close_calls++; return 0; }
static int32_t f_user(void) { return no_user ? -1 : 0x10000101; }
static int f_player(void) { return player_there; }
static int f_thread(void *(*main)(void *)) {
    thread_calls++;
    pthread_t t;
    pthread_attr_t a;
    pthread_attr_init(&a);
    pthread_attr_setdetachstate(&a, PTHREAD_CREATE_DETACHED);
    const int r = pthread_create(&t, &a, main, NULL);
    pthread_attr_destroy(&a);
    return r;
}
static int64_t f_now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}
static pthread_mutex_t log_mu = PTHREAD_MUTEX_INITIALIZER;
static char logged[1 << 20];
static size_t logged_n;
static void f_log(const char *fmt, ...) {
    char line[1024];
    va_list a;
    va_start(a, fmt);
    vsnprintf(line, sizeof line, fmt, a);
    va_end(a);
    if (getenv("TEST_VERBOSE")) fprintf(stderr, "%s\n", line);
    CHECK(strlen(line) < 600 && !strchr(line, '\n'));
    pthread_mutex_lock(&log_mu);
    logged_n += (size_t)snprintf(logged + logged_n, sizeof logged - logged_n, "%s\n", line);
    pthread_mutex_unlock(&log_mu);
}
static int said(const char *what) {
    pthread_mutex_lock(&log_mu);
    const int found = strstr(logged, what) != NULL;
    pthread_mutex_unlock(&log_mu);
    return found;
}
static int said_count(const char *what) {
    int n = 0;
    pthread_mutex_lock(&log_mu);
    for (const char *p = logged; (p = strstr(p, what)) != NULL; p++) n++;
    pthread_mutex_unlock(&log_mu);
    return n;
}
static const char *f_memory(char *buf, size_t cap) { snprintf(buf, cap, " (memory: the test's)"); return buf; }
static HosPs5MicOps ops = {f_init, f_open, f_input, f_silent, f_close, f_user, f_player, f_thread, f_now, f_log, f_memory, 0, 1};

static int inputs(void) { return __atomic_load_n(&input_calls, __ATOMIC_ACQUIRE); }
static void wait_bytes(void *m, int bytes) {
    for (int i = 0; i < 2000 && hos_ps5_mic_available(m) < bytes; i++) sleep_ns(1000000);
    CHECK(hos_ps5_mic_available(m) >= bytes);
}
static void wait_inputs(int n) {   /* (n grains more read by the thread) */
    const int to = inputs() + n;
    for (int i = 0; i < 2000 && inputs() < to; i++) sleep_ns(1000000);
    CHECK(inputs() >= to);
}
/* the ramp's samples follow each other (20000 wraps to 1); from = the one before, 0 none: the next one it should be */
static int follows(const int16_t *s, int n, int from) {
    for (int i = 0; i < n; i++) {
        if (from && s[i] != from % 20000 + 1) return 0;
        from = s[i];
    }
    return from;
}
static int all_zero(const uint8_t *b, int n) {
    for (int i = 0; i < n; i++) if (b[i]) return 0;
    return 1;
}
static uint8_t buf[1 << 21];

int main(void) {
    char ids_names[2][128];
    uint32_t ids[2];
    /* ---- off: nothing listed, nothing opened, no sceAudioIn call ---- */
    hos_ps5_mic_prepare(&ops);
    CHECK(said("ps5: mic: off (HOS_PS5_MIC=1"));
    CHECK(hos_ps5_mic_list(&ops, ids, ids_names, 2) == 0);
    CHECK(!hos_ps5_mic_open(&ops, 0, 48000, 1, HOS_PS5_MIC_S16) && strstr(hos_ps5_mic_error(), "HOS_PS5_MIC=1"));
    CHECK(!init_calls && !open_calls && !thread_calls && !inputs());

    /* ---- on: the service once, the port at the start (general refused at 48000 here: voice chat at 48000), parked ---- */
    ops.enabled = 1, refuse_type1_48k = 1;
    hos_ps5_mic_prepare(&ops);
    CHECK(init_calls == 1 && open_calls == 2 && last_type == 0 && last_rate == 48000 && last_grain == 256 && last_param == 0 &&
          last_user == 0x10000101 && thread_calls == 1);
    CHECK(said("player 0's microphone ready (user 0x10000101): port 0x7, type 0 (voice chat), 48000 Hz") &&
          said("refused first: type 1 48000 Hz 0x8026010b"));
    sleep_ns(30000000);
    CHECK(inputs() == 0);   /* (parked: no sound read before the game listens) */
    hos_ps5_mic_prepare(&ops);
    CHECK(init_calls == 1 && open_calls == 2 && thread_calls == 1);
    /* the list: player 0's controller there or not */
    CHECK(hos_ps5_mic_list(&ops, ids, ids_names, 2) == 1 && ids[0] == 1 && !strcmp(ids_names[0], "DualSense microphone"));
    player_there = 0;
    CHECK(hos_ps5_mic_list(&ops, ids, ids_names, 2) == 0);
    player_there = 1;
    CHECK(hos_ps5_mic_list(&ops, ids, ids_names, 0) == 0);
    /* opens refused: another id, a format SDL has and the console does not give here, a rate out of bounds */
    CHECK(!hos_ps5_mic_open(&ops, 5, 48000, 1, HOS_PS5_MIC_S16) && strstr(hos_ps5_mic_error(), "id 5"));
    CHECK(!hos_ps5_mic_open(&ops, 1, 48000, 1, 0x0008) && !hos_ps5_mic_open(&ops, 1, 4000, 1, HOS_PS5_MIC_S16) &&
          !hos_ps5_mic_open(&ops, 1, 48000, 9, HOS_PS5_MIC_S16));
    CHECK(inputs() == 0 && open_calls == 2);

    /* ---- the game's format (48000 Hz, mono, S16): the port's grains as they came, never blocking ---- */
    void *a = hos_ps5_mic_open(&ops, 0, 48000, 1, HOS_PS5_MIC_S16);
    CHECK(a && open_calls == 2 && said("capture 1: a reader opened (48000 Hz, 1 channel, S16; the port's 48000 Hz), 1 reading"));
    CHECK(hos_ps5_mic_read(a, buf, 0) == 0 && hos_ps5_mic_read(NULL, buf, 64) == 0 && hos_ps5_mic_available(NULL) == 0);
    wait_bytes(a, 4 * 512);
    int n = hos_ps5_mic_read(a, buf, 4 * 512);
    CHECK(n == 4 * 512 && all_zero(buf, 512));   /* (the capture's first grain: silent, its state before it not known) */
    int at = follows((int16_t *)buf + 256, 3 * 256, 0);
    CHECK(at);
    CHECK(said("capture 1: the first grain: sceAudioInInput 0, silent state 0 (sound passes); 0 grains the port held while parked dropped"));
    for (int k = 0; k < 20; k++) {   /* (what comes after, in order) */
        wait_bytes(a, 512);
        n = hos_ps5_mic_read(a, buf, 3 * 512 + 3);   /* (whole frames only) */
        CHECK(n > 0 && n % 2 == 0 && n <= 3 * 512);
        at = follows((int16_t *)buf, n / 2, at);
        CHECK(at);
    }
    /* the ring: a second's worth, the newest kept, in order */
    sleep_ns(1300000000);   /* (at 2 ms a grain: 650 grains, 332800 bytes, past 96000) */
    CHECK(hos_ps5_mic_available(a) == 96000 && ((HosPs5MicReader *)a)->dropped > 0);
    n = hos_ps5_mic_read(a, buf, sizeof buf);
    CHECK(n >= 96000 && follows((int16_t *)buf, n / 2, 0));
    CHECK(said("ps5: mic: the last second: ") && said("bytes captured,") && said("read by the game"));
    at = 0;
    hos_ps5_mic_clear(a);
    CHECK(hos_ps5_mic_available(a) < 2048);

    /* ---- silent states: silence given, what was queued dropped, sound back one grain after ---- */
    const int states[] = {4, 1, 2, 8, 5};
    for (int k = 0; k < 5; k++) {
        sleep_ns(40000000);   /* (sound queued, not read) */
        CHECK(hos_ps5_mic_available(a) > 0);
        __atomic_store_n(&silent_state, states[k], __ATOMIC_RELEASE);
        wait_inputs(3);
        n = hos_ps5_mic_read(a, buf, sizeof buf);
        CHECK(n > 0 && all_zero(buf, n));   /* (the speech queued before it never reaches the game) */
        sleep_ns(20000000);
        n = hos_ps5_mic_read(a, buf, sizeof buf);
        CHECK(n > 0 && all_zero(buf, n));
        __atomic_store_n(&silent_state, 0, __ATOMIC_RELEASE);
        wait_inputs(1);
        hos_ps5_mic_read(a, buf, sizeof buf);   /* (the grain the state changed in: silence, or its sound before it) */
        wait_inputs(3);
        n = hos_ps5_mic_read(a, buf, sizeof buf);
        CHECK(n > 512 && !all_zero(buf, n));
    }
    CHECK(said("silent state 0x4 (muted by the player): silence given, what was queued dropped") &&
          said("silent state 0x1 (no microphone)") && said("silent state 0x2 (another application has the microphone)") &&
          said("silent state 0x8 (a format the microphone does not give)") && said("silent state 0x5 (silent)") &&
          said("silent state 0 (sound passes): sound passes again after one silent grain"));

    /* ---- the other formats, beside the game's reader ---- */
    void *f = hos_ps5_mic_open(&ops, 1, 48000, 2, HOS_PS5_MIC_F32), *w = hos_ps5_mic_open(&ops, 1, 48000, 1, HOS_PS5_MIC_S32);
    void *b8 = hos_ps5_mic_open(&ops, 1, 48000, 1, HOS_PS5_MIC_S8), *r44 = hos_ps5_mic_open(&ops, 1, 44100, 1, HOS_PS5_MIC_S16);
    CHECK(f && w && b8 && r44 && said("5 reading") && said("44100 Hz, 1 channel, S16; the port's 48000 Hz, resampled"));
    const int before44 = inputs();
    wait_bytes(f, 8 * 2048);
    wait_bytes(w, 4 * 2048);
    n = hos_ps5_mic_read(f, buf, 8 * 2048);
    CHECK(n == 8 * 2048 && n % 8 == 0);
    int prev = 0, nonzero = 0;
    for (int i = 0; i < n / 8; i++) {   /* (two channels, the same; the sample over 32768) */
        float l, r;
        memcpy(&l, buf + 8 * i, 4), memcpy(&r, buf + 8 * i + 4, 4);
        CHECK(l == r && l * 32768.0f == (float)(int)(l * 32768.0f));
        const int v = (int)(l * 32768.0f);
        if (v && prev) CHECK(v == prev % 20000 + 1);
        nonzero += v != 0, prev = v;
    }
    CHECK(nonzero > 1000);
    n = hos_ps5_mic_read(w, buf, 4 * 2048);
    CHECK(n == 4 * 2048);
    prev = 0, nonzero = 0;
    for (int i = 0; i < n / 4; i++) {
        int32_t v;
        memcpy(&v, buf + 4 * i, 4);
        CHECK((v & 0xffff) == 0);
        if (v && prev) CHECK(v >> 16 == prev % 20000 + 1);
        nonzero += v != 0, prev = v >> 16;
    }
    CHECK(nonzero > 1000);
    wait_bytes(b8, 2048);
    n = hos_ps5_mic_read(b8, buf, 2048);
    prev = -1;
    for (int i = 0; i < n; i++) {   /* (the ramp's high byte: up by 0 or 1, or back down where it wraps) */
        const int v = (int8_t)buf[i];
        CHECK(v >= 0 && v <= 78);
        if (prev >= 0 && v) CHECK(v == prev || v == prev + 1 || v < prev);
        prev = v;
    }
    sleep_ns(200000000);
    const int grains44 = inputs() - before44;
    const uint64_t given44 = ((HosPs5MicReader *)r44)->given / 2;
    CHECK(given44 + 4 * 256 >= (uint64_t)grains44 * 256 * 44100 / 48000 - 3 * 256 &&
          given44 <= (uint64_t)(grains44 + 4) * 256 * 44100 / 48000 + 2);
    hos_ps5_mic_close(f), hos_ps5_mic_close(w), hos_ps5_mic_close(b8), hos_ps5_mic_close(r44);

    /* ---- the last reader closed: the capture pauses, the port stays open; the next open reads it again ---- */
    hos_ps5_mic_close(a);
    hos_ps5_mic_close(NULL);   /* (nothing) */
    CHECK(said("capture 1 paused after") && said("(no reader; the port stays open, parked)"));
    sleep_ns(20000000);
    int parked = inputs();
    sleep_ns(60000000);
    CHECK(inputs() == parked && close_calls == 0);
    /* (the grains the port held while parked come at once: dropped) */
    __atomic_store_n(&instant_left, 10, __ATOMIC_RELEASE);
    a = hos_ps5_mic_open(&ops, 1, 48000, 1, HOS_PS5_MIC_S16);
    CHECK(a && open_calls == 2 && said("capture 2: a reader opened"));
    wait_bytes(a, 4 * 512);
    CHECK(said("capture 2: the first grain: sceAudioInInput 0, silent state 0 (sound passes); 10 grains the port held while parked dropped"));
    /* (an input that never blocks: paced, not spun) */
    __atomic_store_n(&instant_left, 1 << 30, __ATOMIC_RELEASE);
    for (int i = 0; i < 2000 && !said("sceAudioInInput does not block"); i++) sleep_ns(1000000);
    CHECK(said("sceAudioInInput does not block (32 grains in a row came at once): paced here"));
    const int p0 = inputs();
    sleep_ns(200000000);
    const int paced = inputs() - p0;
    CHECK(paced >= 10 && paced <= 60);   /* (5.3 ms a grain at 48000 Hz: ~37 in 200 ms) */
    __atomic_store_n(&instant_left, 0, __ATOMIC_RELEASE);
    wait_inputs(3);
    CHECK(!hos_ps5_mic.paced);
    hos_ps5_mic_close(a);

    /* ---- an input that fails: the port closed, made again at the next open (at 16000 Hz here: resampled to 48000) ---- */
    a = hos_ps5_mic_open(&ops, 1, 48000, 1, HOS_PS5_MIC_S16);
    CHECK(a);
    __atomic_store_n(&fail_input, 1, __ATOMIC_RELEASE);
    for (int i = 0; i < 2000 && !close_calls; i++) sleep_ns(1000000);
    CHECK(close_calls == 1 && said("the microphone stopped (sceAudioInInput 0x80260105, silent state 0xffffffff): the port 0x7 closed (0)"));
    parked = inputs();
    sleep_ns(30000000);
    CHECK(inputs() == parked);   /* (no port: the thread sleeps, the reader gets nothing) */
    hos_ps5_mic_close(a);
    __atomic_store_n(&fail_input, 0, __ATOMIC_RELEASE);
    refuse_48k = 1;
    __atomic_store_n(&constant, 1000, __ATOMIC_RELEASE);
    a = hos_ps5_mic_open(&ops, 1, 48000, 1, HOS_PS5_MIC_S16);
    CHECK(a && open_calls == 5 && last_rate == 16000 && last_type == 1 && init_calls == 1 && thread_calls == 1);
    CHECK(said("type 1 (general), 16000 Hz") && said("refused first: type 1 48000 Hz 0x8026010b, type 0 48000 Hz 0x8026010b") &&
          said("48000 Hz, 1 channel, S16; the port's 16000 Hz, resampled"));
    wait_bytes(a, 8 * 1536);
    n = hos_ps5_mic_read(a, buf, sizeof buf);
    int16_t *s = (int16_t *)buf;
    int ones = 0;
    for (int i = 0; i < n / 2; i++) {   /* (a steady 1000 stays 1000; from the first grain's silence it rises to it) */
        CHECK(s[i] >= 0 && s[i] <= 1000 && (i == 0 || s[i] >= s[i - 1]));
        ones += s[i] == 1000;
    }
    CHECK(ones > n / 2 - 800);
    const int g0 = inputs();
    const uint64_t given0 = ((HosPs5MicReader *)a)->given;
    sleep_ns(100000000);
    const uint64_t frames = (((HosPs5MicReader *)a)->given - given0) / 2;
    const int grains = inputs() - g0;
    CHECK(frames >= (uint64_t)(grains - 2) * 768 && frames <= (uint64_t)(grains + 2) * 768);   /* (three frames a sample) */
    hos_ps5_mic_close(a);
    __atomic_store_n(&constant, 0, __ATOMIC_RELEASE);

    /* ---- 300 opens and closes, as the ocarina comes out and goes: one thread, the port kept, nothing left (the sanitizer) ---- */
    for (int k = 0; k < 300; k++) {
        void *m = hos_ps5_mic_open(&ops, k & 1, 48000, 1, HOS_PS5_MIC_S16);
        CHECK(m);
        if (k % 50 == 0) {
            wait_bytes(m, 512);
            CHECK(hos_ps5_mic_read(m, buf, 4096) > 0);
        }
        hos_ps5_mic_close(m);
    }
    CHECK(thread_calls == 1 && open_calls == 5 && close_calls == 1 && hos_ps5_mic.nreaders == 0);

    /* ---- the title closing: the port closed, every open refused after ---- */
    a = hos_ps5_mic_open(&ops, 1, 48000, 1, HOS_PS5_MIC_S16);
    wait_bytes(a, 512);
    hos_ps5_mic_shutdown();
    CHECK(close_calls == 2 && said("ps5: mic: closed: the port 0x7 (0)"));
    hos_ps5_mic_close(a);
    CHECK(!hos_ps5_mic_open(&ops, 1, 48000, 1, HOS_PS5_MIC_S16) && strstr(hos_ps5_mic_error(), "closed"));
    hos_ps5_mic_shutdown();
    CHECK(close_calls == 2 && said_count("ps5: mic: closed:") == 1);
    for (int i = 0; i < 500 && hos_ps5_mic.thread_on; i++) sleep_ns(1000000);
    CHECK(!hos_ps5_mic.thread_on);
    /* (never a sample in the log: levels and counts only) */
    CHECK(!said("pcm") && !said("sample "));
    printf("microphone: %d grains read by the stand-in, %d sceAudioInOpen, %d closes, %d thread\n", inputs(), open_calls,
           close_calls, thread_calls);
    return 0;
}
'''


def need(cond, what):
    if not cond:
        raise SystemExit("FAIL: " + what)


def check_sources():
    mic = (ROOT / "host/ps5/ps5_mic.h").read_text(encoding="utf-8")
    sdl = (ROOT / "host/ps5/hos_sdl_ps5.c").read_text(encoding="utf-8")
    win32 = (ROOT / "host/ps5/win32_ps5.c").read_text(encoding="utf-8")
    link = (ROOT / "ps5/tools/link.sh").read_text(encoding="utf-8")
    make_kit = (ROOT / "ps5/builder/make_kit.py").read_text(encoding="utf-8")
    pipeline = (ROOT / "ps5/builder/pipeline.py").read_text(encoding="utf-8")
    stub = (ROOT / "ps5/stubs/audio_in_link_stub.c").read_text(encoding="utf-8")
    build = (ROOT / "ps5/build.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    docs = (ROOT / "docs/PS5.md").read_text(encoding="utf-8")
    # ---- the voice never leaves the console: no file, no network, in the microphone's code ----
    code = re.sub(r"/\*.*?\*/", "", mic, flags=re.S)
    for call in ("fopen", "fwrite", "fputs", "fprintf", "write", "open", "socket", "sendto", "send", "connect", "sceNet"):
        need(not re.search(r"(?<![>.\w])%s\s*\(" % call, code), "host/ps5/ps5_mic.h calls %s" % call)
    part = sdl[sdl.index("int sceAudioInInit(void);"):sdl.index("void hos_ps5_mic_end(void)")]
    for call in ("fopen", "fwrite", "socket", "sendto"):
        need(call + "(" not in part, "hos_sdl_ps5.c's microphone part calls %s" % call)
    need("mihawk-99's PS5_RetroArch" in mic and "GPL-3.0-or-later" in mic and "PS5_Grapnel" in mic,
         "ps5_mic.h does not say what it comes from")
    # ---- the switch: read by mic_ops at its first call (after longshot_env.txt), with the switches' rule; off by default;
    # the port prepared right after the controllers ----
    ops = block(sdl, "static const HosPs5MicOps *mic_ops(void) {")
    need('env_on("HOS_PS5_MIC")' in ops and sdl.count('"HOS_PS5_MIC"') == 1, "the switch is not read once, in mic_ops")
    init = block(sdl, "int hos_sdl_init(uint32_t subsystems) {")
    need(0 <= init.find("pad_open()") < init.find("mic_start()"), "the microphone is not prepared after the controllers")
    need(sdl.count("mic_start()") == 1 and "hos_ps5_mic_prepare(mic_ops());" in block(sdl, "static void mic_start(void) {"),
         "mic_start is called from elsewhere than hos_sdl_init")
    for name in ("hos_sdl_mic_list", "hos_sdl_mic_open", "hos_sdl_mic_available", "hos_sdl_mic_read", "hos_sdl_mic_clear",
                 "hos_sdl_mic_close"):
        need(name + "(" in sdl and "hos_ps5_mic_" + name[len("hos_sdl_mic_"):] + "(" in sdl, "%s is not ps5_mic.h's" % name)
    need('#include "ps5_mic.h"' in sdl, "hos_sdl_ps5.c does not take the microphone from ps5_mic.h")
    need("mic_pad_there" in block(sdl, "void hos_sdl_pads_update(void) {"), "the list does not follow player 0's controller")
    # ---- the module: libSceAudioIn's five calls, a stub of ours named libSceAudioIn.prx for the link (never packaged) ----
    declared = set(re.findall(r"^int (sceAudioIn\w+)\(", sdl, re.M))
    stubbed = set(re.findall(r"^int (sceAudioIn\w+)\(", stub, re.M))
    need(declared == stubbed == {"sceAudioInInit", "sceAudioInOpen", "sceAudioInInput", "sceAudioInGetSilentState",
                                 "sceAudioInClose"}, "the stub's calls are not the ones hos_sdl_ps5.c makes: %s %s" % (declared, stubbed))
    need('stub libSceAudioIn "$root/ps5/stubs/audio_in_link_stub.c"' in link and '-soname "${library}.prx"' in link and
         link.count('"$work/link/stubs/libSceAudioIn.so"') == 3,
         "ps5/tools/link.sh does not build libSceAudioIn.prx's stub, link it, count its exports and name it to the native tool")
    need('("libSceAudioIn", os.path.join(ROOT, "ps5", "stubs", "audio_in_link_stub.c"))' in make_kit and
         '("libSceAgc.so", "libSceAgcDriver.so", "libSceAudioIn.so")' in make_kit, "the builder's kit has no libSceAudioIn stub")
    plink = pipeline[pipeline.index("def link(kit, info, work, objects, lld, nm):"):pipeline.index("def title(kit, info,")]
    ptitle = pipeline[pipeline.index("def title(kit, info,"):pipeline.index("def check(a):")]
    need('("libSceAgc.so", "libSceAgcDriver.so", "libSceAudioIn.so")' in plink and
         '"--stub", os.path.join(kit, "lib", "stubs", "libSceAudioIn.so")' in ptitle,
         "the builder's link does not take the libSceAudioIn stub, or does not name it to the native tool")
    need(not (ROOT / "host/ps5/audio_in_link_stub.c").exists() and "ps5/stubs" not in build and "audio_in_link_stub" not in build,
         "the stub is compiled into the title (ps5/build.py takes every .c in host/ps5)")
    # ---- the title's exit and its restart (the shell's LoadExec): the microphone's end first ----
    for marker in ("void hos_ps5_exit(int code) {", "void hos_ps5_restart(const char *why) {"):
        body = block(win32, marker)
        need(0 <= body.find("hos_ps5_mic_end();") < body.index("sceSystemServiceLoadExec("),
             "%s does not end the microphone before LoadExec" % marker)
    # ---- the docs name the switch, off by default ----
    need("HOS_PS5_MIC=1" in readme and "longshot_env.txt" in readme, "README.md does not say how the microphone is turned on")
    need("HOS_PS5_MIC=1" in docs and "PS5_RetroArch" in docs and "PS5_Grapnel" in docs,
         "docs/PS5.md does not say what the microphone is and whose the code is")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cc", default="gcc")
    a = p.parse_args()
    check_sources()
    with tempfile.TemporaryDirectory(prefix="ps5-mic-") as tmp:
        t = Path(tmp)
        (t / "mic.c").write_text(FIXTURE, encoding="utf-8")
        # (-Wno-format-truncation: the log's lines are cut at their buffers on purpose)
        base = [a.cc, "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-Wno-format-truncation",
                "-pthread", "-I", str(ROOT / "host/ps5"), str(t / "mic.c"), "-o", str(t / "mic")]
        sanitize = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        (t / "probe.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        if subprocess.run([a.cc] + sanitize + [str(t / "probe.c"), "-o", str(t / "probe")], capture_output=True).returncode == 0:
            subprocess.run(base[:1] + sanitize + base[1:], check=True)
        else:
            print("(no address sanitizer here: the leak check is not run)")
            subprocess.run(base, check=True)
        env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1", UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
        r = subprocess.run([str(t / "mic")], env=env, capture_output=True, text=True, timeout=120)
        sys.stdout.write(r.stdout)
        if r.returncode:
            sys.stderr.write(r.stderr)
            raise SystemExit("FAIL: the microphone's run (%d)" % r.returncode)
    print("PASS: player 0's DualSense microphone: off without its switch; the port made early and parked; what the game reads "
          "in order and bounded; silence and mutes; the formats; the pauses, failures, 300 opens and the close; nothing leaves "
          "the console")


if __name__ == "__main__":
    main()
